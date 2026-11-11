# Уведомления в Telegram

Ведущему площадки и организатору турнира приходят сообщения в Telegram:

- «🎯 Турнир #8801: вы ведущий площадки 2. Ваши бои — в разделе «Мои площадки».»
- «✅ Турнир #8801, площадка 2: бой 4 сохранён. Следующий бой: 5.»
  (или «Площадка закрыта.», если бой был последним на площадке)

Привязка — в разделе «Профиль» (ссылка с именем пользователя в шапке).

## Что нужно один раз сделать

1. Создать бота у [@BotFather](https://t.me/BotFather): `/newbot`, запомнить токен
   и имя вида `svoyak_bot`.
2. Задать на сервере переменные окружения:

   | Переменная | Зачем | Пример |
   |---|---|---|
   | `SVOYAK_TELEGRAM_TOKEN` | токен бота из BotFather | `123456:AA...` |
   | `SVOYAK_TELEGRAM_BOT` | имя бота для ссылки `t.me/<имя>` | `svoyak_bot` |
   | `SVOYAK_TELEGRAM_SECRET` | секрет вебхука, придумать длинную строку | `hook-9f3b...` |
   | `SVOYAK_SITE_URL` | необязательно: ссылка в конце уведомления | `https://svoyak.chgk.fun` |

3. Перезапустить приложение (переменные читаются при старте).
4. Подписать бота на вебхук — один раз после каждого переезда на новый домен:

   ```bash
   curl "https://api.telegram.org/bot$SVOYAK_TELEGRAM_TOKEN/setWebhook" \
        -d "url=https://svoyak.chgk.fun/telegram/webhook/$SVOYAK_TELEGRAM_SECRET" \
        -d "secret_token=$SVOYAK_TELEGRAM_SECRET"
   ```

   Проверить: `curl "https://api.telegram.org/bot$SVOYAK_TELEGRAM_TOKEN/getWebhookInfo"`.

Без `SVOYAK_TELEGRAM_TOKEN` и `SVOYAK_TELEGRAM_BOT` страница профиля честно пишет,
что бот не настроен, а кнопка привязки не появляется — остальное приложение
работает как обычно.

## Как это устроено

1. «Привязать Telegram» → одноразовая ссылка `https://t.me/<бот>?start=link_<токен>`,
   живёт 15 минут.
2. Пользователь открывает ссылку и жмёт Start.
3. Telegram присылает вебхуку `/telegram/webhook/<секрет>` команду `/start link_<токен>`.
4. Сервер записывает в аккаунт `telegram_id` и `chat_id` и отвечает человеку в чат.

`chat_id` нужен потому, что писать бот может только тому, кто сам нажал Start.
Привязка в групповом чате не работает — только в личном.

Отвязка — в профиле, с подтверждением пароля: Telegram становится ключом
к аккаунту, молча его не отдают.

## Проверка на localhost

Бот для проверки не обязателен: всё, кроме реальной доставки в Telegram,
можно прогнать локально.

### 1. Автоматически, две секунды

```bash
python tools/telegram_selftest.py
```

Скрипт берёт копию `svoyak-test.db`, поднимает на свободном порту заглушку
`api.telegram.org` и проходит весь путь: выдача ссылки, `/start`, привязка,
чужой секрет вебхука, повторное использование ссылки, групповой чат,
назначение на площадку, сохранение боя, отвязка. Печатает список проверок
и возвращает ненулевой код, если что-то сломалось. База-образец задаётся
через `--db`.

### 2. Руками в браузере, тоже без бота

Два окна: заглушка вместо Telegram и сайт на копии тестовой базы.

```powershell
# окно 1 — вместо api.telegram.org
python tools/fake_telegram.py --port 8081

# окно 2 — сайт
Copy-Item svoyak-test.db local-test.db        # копия, чтобы не трогать рабочую базу
$env:SVOYAK_DB='local-test.db'
$env:SVOYAK_SERVER_NAME='localhost'           # родительский домен, не адрес сайта
$env:SVOYAK_TELEGRAM_TOKEN='local-token'
$env:SVOYAK_TELEGRAM_BOT='svoyak_bot'
$env:SVOYAK_TELEGRAM_SECRET='local-secret'
$env:SVOYAK_TELEGRAM_API='http://127.0.0.1:8081'
python svoyak_site_app.py
```

Дальше в браузере `http://svoyak.localhost/profile` → «Привязать Telegram».
Страница покажет ссылку вида `https://t.me/svoyak_bot?start=link_XXXX` —
настоящий Telegram её, конечно, не откроет, поэтому Start изображаем curl'ом
(настоящий адрес Telegram всё равно прислал бы ровно такой запрос):

```powershell
'{"message":{"chat":{"id":5551,"type":"private"},"from":{"id":5551,"username":"tester"},"text":"/start link_XXXX"}}' |
    Set-Content start.json -Encoding UTF8
curl.exe --resolve svoyak.localhost:80:127.0.0.1 -X POST `
  "http://svoyak.localhost/telegram/webhook/local-secret" `
  -H "Content-Type: application/json" --data-binary "@start.json"
```

JSON удобнее класть в файл: PowerShell портит кавычки при передаче строки
аргументом, а `--data-binary @файл` работает всегда.

В окне заглушки появится ответ бота («Готово: аккаунт … привязан»), а в профиле
после обновления — «Привязан @tester». Дальше можно назначить этого человека
ведущим площадки в пульте турнира и сохранить бой: уведомления так же
напечатаются в окне заглушки.

`--resolve` нужен, потому что маршруты живут на поддомене `svoyak`, а Windows
сам `svoyak.localhost` в `127.0.0.1` не переводит (браузеры переводят).

Если сайт запущен не на 80-м порту, порт входит в `SERVER_NAME`:
`SVOYAK_SERVER_NAME=localhost:8099` и `--resolve svoyak.localhost:8099:127.0.0.1`.

### 3. С настоящим ботом, но без домена и HTTPS

Мост забирает обновления long polling'ом и сам стучится в локальный вебхук:

```powershell
$env:SVOYAK_TELEGRAM_TOKEN='123456:AA...'
$env:SVOYAK_TELEGRAM_SECRET='local-secret'
python tools/telegram_bridge.py            # сайт уже запущен с теми же значениями
```

Дальше всё по-настоящему: открываете ссылку из профиля, жмёте Start, бот
отвечает в Telegram. Мост сам снимает вебхук (`deleteWebhook`) — одно другое
исключает.

Туннель (`cloudflared`, `ngrok`) для этого сайта неудобен: Telegram пришлёт
`Host` туннеля, а маршруты ждут `svoyak.<SERVER_NAME>`, так что нужен прокси
с подменой `Host` — мост проще.

## Если что-то не приходит

В профиле есть журнал «Последние уведомления» со статусом и текстом ошибки
(`HTTP 403: bot was blocked by the user`, `URLError: timed out` и т.п.).
Частые причины:

- пользователь заблокировал бота;
- бот не настроен или неверный токен;
- сервер не достаёт до `api.telegram.org`.

## Ограничения

- Доставка синхронная: сообщение уходит в момент действия, повторов нет.
- Секрет вебхука — единственная защита этого адреса; менять его вместе
  с `setWebhook`.
- Пароль по Telegram не восстанавливается: вход по-прежнему по логину и паролю.
