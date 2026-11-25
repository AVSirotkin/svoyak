// Синхронизация пультов: режим работы, очередь изменений и сообщения.
//
// Режимы:
//   sync       — сохраняем через секунду после правки;
//   limited    — не чаще раза в минуту от последней попытки, плюс
//                принудительно на важных шагах (результат боя, смена режима,
//                уход со страницы);
//   autonomous — полная изоляция: ни отправок, ни пинга. Всё живёт на этом
//                устройстве и уходит только по кнопке «Синхронизировать».
//
// Событие синхронизации создаёт только значимое изменение состояния боя.
// Переключение боя, просмотр, движение указателя и повторное применение
// серверного протокола ничего не отправляют. После сбоев пауза растёт
// (1 → 2 → 4 минуты), чтобы не долбить сервер.
//
// Очередь переживает перезагрузку страницы: изменения лежат в localStorage
// и уходят, когда связь появится. Один и тот же модуль используют оба пульта.

(function (global) {
    'use strict';

    const MODES = {
        sync: {title: 'синхронный', interval: 0},
        limited: {title: 'ограниченный', interval: 60000},
        autonomous: {title: 'автономный', interval: Infinity}
    };
    const DEFAULT_MODE = 'sync';
    const SYNC_DELAY = 1200;      // пауза после последней правки в синхронном режиме
    const LIMITED_INTERVAL = 60000;   // окно ограниченного режима
    const MAX_BACKOFF = 240000;   // после сбоев не долбим сервер чаще раза в 4 минуты
    const PING_TIMEOUT = 4000;
    const SEND_TIMEOUT = 10000;

    function plural(count, one, few, many) {
        const mod10 = count % 10;
        const mod100 = count % 100;
        if (mod10 === 1 && mod100 !== 11) return one;
        if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
        return many;
    }

    function clientId() {
        let id = null;
        try {
            id = localStorage.getItem('svoyak_client_id');
        } catch (error) { /* приватный режим */ }
        if (!id) {
            id = 'c' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
            try {
                localStorage.setItem('svoyak_client_id', id);
            } catch (error) { /* не страшно: id будет новый */ }
        }
        return id;
    }

    // id окна: clientId общий на весь браузер (localStorage), а два пульта в
    // разных вкладках — это два разных писателя. По tabId видно, кто именно
    // увёл ревизию боя.
    function tabId() {
        let id = null;
        try {
            id = sessionStorage.getItem('svoyak_tab_id');
        } catch (error) { /* приватный режим */ }
        if (!id) {
            id = 't' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
            try {
                sessionStorage.setItem('svoyak_tab_id', id);
            } catch (error) { /* не страшно: id будет новый */ }
        }
        return id;
    }

    function modeKey(roomid, area) {
        return 'svoyak_sync_mode_' + roomid + '_' + area;
    }

    function getMode(roomid, area) {
        try {
            const value = localStorage.getItem(modeKey(roomid, area));
            if (value && MODES[value]) return value;
        } catch (error) { /* ignore */ }
        return DEFAULT_MODE;
    }

    function setMode(roomid, area, mode) {
        if (!MODES[mode]) return;
        try {
            localStorage.setItem(modeKey(roomid, area), mode);
        } catch (error) { /* ignore */ }
    }

    function shortStamp(value) {
        const match = String(value || '').match(/^\d{4}-\d{2}-\d{2}[ T](\d{2}:\d{2})/);
        return match ? match[1] : String(value || '');
    }

    // ---------------------------------------------------------------
    // Баннер: один на страницу, создаётся сам, чтобы шаблоны не дублировали
    // разметку. Показывается только когда есть что сказать.
    // ---------------------------------------------------------------
    const BANNER_CSS = [
        '.sync-banner{position:fixed;left:50%;bottom:16px;transform:translateX(-50%);',
        'z-index:2000;display:flex;align-items:center;gap:10px;max-width:min(720px,92vw);',
        'padding:10px 14px;border-radius:10px;background:#fff8e1;border:1px solid #f0c36d;',
        'box-shadow:0 6px 24px rgba(0,0,0,.18);font-size:14px;line-height:1.35}',
        '.sync-banner.is-offline{background:#fdecea;border-color:#e6a19a}',
        '.sync-banner[hidden]{display:none}',
        '.sync-banner-text{flex:1 1 auto}',
        '.sync-banner button{white-space:nowrap}'
    ].join('');

    function ensureBanner() {
        let banner = document.getElementById('svoyak-sync-banner');
        if (banner) return banner;
        if (!document.getElementById('svoyak-sync-style')) {
            const style = document.createElement('style');
            style.id = 'svoyak-sync-style';
            style.textContent = BANNER_CSS;
            document.head.appendChild(style);
        }
        banner = document.createElement('div');
        banner.id = 'svoyak-sync-banner';
        banner.className = 'sync-banner';
        banner.hidden = true;
        banner.innerHTML =
            '<span class="sync-banner-text"></span>' +
            '<button type="button" class="btn btn-sm btn-primary sync-banner-send">Отправить</button>' +
            '<button type="button" class="btn btn-sm btn-outline-secondary sync-banner-retry">Повторить</button>';
        document.body.appendChild(banner);
        return banner;
    }

    // ---------------------------------------------------------------
    // Очередь изменений.
    // ---------------------------------------------------------------
    function createQueue(options) {
        const roomid = options.roomid;
        const area = options.area;
        const outboxKey = 'svoyak_sync_outbox_' + roomid + '_' + area;
        const senders = options.senders || {};
        const onStatus = options.onStatus || function () {};
        const onConflict = options.onConflict || function () {};
        const onSent = options.onSent || function () {};

        let outbox = loadOutbox();
        let mode = getMode(roomid, area);
        let online = navigator.onLine;
        let flushing = false;
        let checking = false;
        let checkTimer = null;
        let timer = null;
        let lastSync = null;
        let lastAttempt = null;
        let failureDelay = 0;
        let error = '';
        let denied = false;   // сервер ответил 403: права отобрали
        let banner = null;

        function loadOutbox() {
            try {
                const saved = JSON.parse(localStorage.getItem(outboxKey) || '[]');
                return Array.isArray(saved) ? saved : [];
            } catch (error) {
                return [];
            }
        }

        function saveOutbox() {
            try {
                localStorage.setItem(outboxKey, JSON.stringify(outbox));
            } catch (error) {
                console.error('[sync] не удалось сохранить очередь', error);
            }
        }

        function pending() {
            return outbox.length;
        }

        function status() {
            return {
                mode: mode,
                modeTitle: MODES[mode].title,
                pending: outbox.length,
                online: online,
                lastSync: lastSync,
                error: error
            };
        }

        function publish() {
            onStatus(status());
            renderBanner();
        }

        function renderBanner() {
            banner = banner || ensureBanner();
            const text = banner.querySelector('.sync-banner-text');
            const send = banner.querySelector('.sync-banner-send');
            const retry = banner.querySelector('.sync-banner-retry');
            const count = outbox.length;

            banner.classList.toggle('is-offline', !online);

            // Плашка — только про настоящие проблемы. Рутинное «сохранено /
            // не отправлено» показывает строка статуса самой страницы:
            // мигающая плашка на каждой правке раздражала.
            if (!online) {
                banner.hidden = false;
                text.textContent = count
                    ? 'Нет связи с сервером. Работаем локально, ' + count + ' ' +
                      plural(count, 'изменение', 'изменения', 'изменений') + ' в очереди.'
                    : 'Нет связи с сервером. Работаем локально — данные сохраняются на этом устройстве.';
                send.hidden = true;
                retry.hidden = false;
                return;
            }
            if (error) {
                banner.hidden = false;
                text.textContent = 'Не удалось отправить: ' + error +
                    '. В очереди ' + count + ' ' +
                    plural(count, 'изменение', 'изменения', 'изменений') + '.';
                send.hidden = true;
                retry.hidden = false;
                return;
            }
            banner.hidden = true;
        }

        function fetchWithTimeout(url, init, timeoutMs) {
            const controller = new AbortController();
            const timeout = setTimeout(() => controller.abort(), timeoutMs || SEND_TIMEOUT);
            return fetch(url, Object.assign({}, init, {signal: controller.signal}))
                .finally(() => clearTimeout(timeout));
        }

        function setOnline(value) {
            if (online === value) return;
            online = value;
            publish();
            if (online) {
                schedule(0);
            } else {
                scheduleCheck();
            }
        }

        async function check() {
            if (checking) return;
            // Автономный режим не ждёт сервера: ни отправки, ни пинга.
            if (isAutonomous()) {
                scheduleCheck();
                return;
            }
            checking = true;
            try {
                const response = await fetchWithTimeout('/api/ping', {cache: 'no-store'}, PING_TIMEOUT);
                setOnline(response.ok);
            } catch (error) {
                setOnline(false);
            } finally {
                checking = false;
                scheduleCheck();
            }
        }

        function scheduleCheck() {
            clearTimeout(checkTimer);
            if (isAutonomous()) return;
            checkTimer = setTimeout(check, online ? 30000 : 8000);
        }

        // Когда отправлять: синхронный — после паузы, ограниченный — не чаще
        // раза в минуту от последней попытки, автономный — только по кнопке.
        // Автономный вообще не трогает сервер: ни отправки, ни пинга.
        function isAutonomous() {
            return mode === 'autonomous';
        }

        function nextSendIn() {
            if (mode !== 'limited' || !outbox.length) return null;
            const since = lastAttempt ? Date.now() - lastAttempt : Infinity;
            const windowLeft = Math.max(0, LIMITED_INTERVAL - since);
            const backoffLeft = failureDelay
                ? Math.max(0, failureDelay - (Date.now() - (lastAttempt || 0)))
                : 0;
            return Math.max(windowLeft, backoffLeft);
        }

        function schedule(delay) {
            clearTimeout(timer);
            if (!outbox.length) {
                publish();
                return;
            }
            if (isAutonomous()) {
                publish();
                return;
            }
            let wait;
            if (delay !== undefined) {
                wait = delay;
            } else {
                if (mode === 'sync') {
                    wait = SYNC_DELAY;
                } else {
                    // Окно считается от попытки, а не от успеха: иначе после
                    // ошибки ограничение исчезало и правки уходили подряд.
                    const since = lastAttempt ? Date.now() - lastAttempt : Infinity;
                    wait = Math.max(0, LIMITED_INTERVAL - since);
                }
                if (failureDelay) {
                    const sinceAttempt = lastAttempt ? Date.now() - lastAttempt : failureDelay;
                    wait = Math.max(wait, failureDelay - sinceAttempt);
                }
            }
            timer = setTimeout(() => flush(), wait);
            publish();
        }

        function enqueue(type, payload) {
            const last = outbox[outbox.length - 1];
            // Протокол — это снимок состояния: держим в очереди только свежий.
            if (type === 'protocol' && last && last.type === 'protocol' &&
                last.payload.gameindex === payload.gameindex) {
                last.payload = payload;
                last.updatedAt = new Date().toISOString();
            } else {
                outbox.push({
                    id: Date.now() + '_' + Math.random().toString(36).slice(2, 8),
                    type: type,
                    payload: payload,
                    createdAt: new Date().toISOString(),
                    attempts: 0
                });
            }
            saveOutbox();
            schedule();
        }

        async function send(op) {
            const sender = senders[op.type];
            if (!sender) throw new Error('Неизвестная операция ' + op.type);
            return sender(op.payload);
        }

        async function flush(force) {
            if (flushing || !outbox.length) {
                publish();
                return;
            }
            if (denied && !force) {
                // Права отобрали: не тратим попытки и не теряем изменения.
                publish();
                return;
            }
            if (force) denied = false;
            if (isAutonomous() && !force) {
                publish();
                return;
            }
            // Автономный режим не ждёт сервера вообще: пинг — это тоже запрос.
            if (!online && !force && !isAutonomous()) {
                check();
                publish();
                return;
            }
            flushing = true;
            error = '';
            lastAttempt = Date.now();
            publish();
            while (outbox.length) {
                const op = outbox[0];
                let result;
                try {
                    result = await send(op);
                } catch (failure) {
                    error = 'нет связи';
                    failureDelay = Math.min(MAX_BACKOFF, failureDelay ? failureDelay * 2 : LIMITED_INTERVAL);
                    setOnline(false);
                    break;
                }
                if (result.status === 401) {
                    window.location.href = '/login?next=' + encodeURIComponent(window.location.pathname);
                    break;
                }
                if (result.status === 403) {
                    // Ссылку управления отключили или площадку передали другому:
                    // держим изменения в очереди, пока права не вернут.
                    denied = true;
                    error = 'нет прав на эту площадку';
                    break;
                }
                if (result.ok) {
                    outbox.shift();
                    saveOutbox();
                    lastSync = Date.now();
                    failureDelay = 0;
                    if (!online) setOnline(true);
                    onSent(op, result.data);
                    continue;
                }
                if (result.status === 409 && result.data && result.data.status === 'conflict') {
                    // Версия разошлась: сервер сохранил её отдельно, решает организатор.
                    outbox.shift();
                    saveOutbox();
                    onConflict(result.data, op);
                    continue;
                }
                op.attempts = (op.attempts || 0) + 1;
                error = 'ошибка сервера ' + result.status;
                failureDelay = Math.min(MAX_BACKOFF, failureDelay ? failureDelay * 2 : LIMITED_INTERVAL);
                if (op.attempts >= 3) {
                    outbox.shift();
                    saveOutbox();
                    continue;
                }
                saveOutbox();
                break;
            }
            flushing = false;
            saveOutbox();
            publish();
            if (online && outbox.length) schedule();
        }

        function changeMode(next) {
            if (!MODES[next] || next === mode) return;
            mode = next;
            setMode(roomid, area, next);
            if (next === 'autonomous') {
                // Автономный — полная изоляция: ни отправок, ни пинга.
                clearTimeout(timer);
                clearTimeout(checkTimer);
                publish();
                return;
            }
            // Выход из автономного: сразу досылаем накопленное и снова пингуем.
            scheduleCheck();
            if (outbox.length) {
                schedule(0);
            } else {
                publish();
            }
        }

        function init() {
            publish();
            scheduleCheck();
            window.addEventListener('online', () => { online = true; publish(); schedule(0); });
            window.addEventListener('offline', () => { setOnline(false); });
            // Уход со страницы: досылаем, что успеем, чтобы не потерять правки.
            window.addEventListener('pagehide', () => {
                if (outbox.length && mode !== 'autonomous' && online) flush(true);
            });
            document.addEventListener('visibilitychange', () => {
                if (document.visibilityState === 'hidden' && outbox.length &&
                    mode !== 'autonomous' && online) {
                    flush(true);
                }
            });
            if (navigator.onLine) {
                flush();
            } else {
                check();
            }
        }

        return {
            init: init,
            enqueue: enqueue,
            flush: flush,
            check: check,
            pending: pending,
            status: status,
            mode: () => mode,
            setMode: changeMode,
            clientId: clientId,
            nextSendIn: nextSendIn,
            refreshStatus: publish
        };
    }

    global.SvoyakSync = {
        MODES: MODES,
        DEFAULT_MODE: DEFAULT_MODE,
        getMode: getMode,
        setMode: setMode,
        clientId: clientId,
        tabId: tabId,
        shortStamp: shortStamp,
        createQueue: createQueue
    };
})(window);
