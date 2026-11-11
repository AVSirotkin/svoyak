"""Мост Telegram → локальный сайт: getUpdates вместо публичного вебхука.

Годится, когда бот настоящий, а домена с HTTPS под рукой нет: скрипт забирает
обновления long polling'ом и сам отправляет их на локальный вебхук, подменяя
заголовок Host (Telegram знать про svoyak.localhost не должен).

    set SVOYAK_TELEGRAM_TOKEN=123456:AA...
    set SVOYAK_TELEGRAM_SECRET=local-secret
    python tools/telegram_bridge.py

Сайт при этом должен быть запущен с теми же токеном и секретом, например:

    SVOYAK_DB=svoyak-test.db SVOYAK_SERVER_NAME=localhost python svoyak_site_app.py

Мост сам снимает вебхук (deleteWebhook) — Telegram не любит, когда тянут
обновления и одновременно шлют их на вебхук.
"""

import argparse
import http.client
import json
import os
import sys
import urllib.error
import urllib.request


def api_call(api, token, method, params=None):
    url = '%s/bot%s/%s' % (api, token, method)
    body = json.dumps(params or {}).encode('utf-8')
    request = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode('utf-8') or '{}')
    except urllib.error.HTTPError as exc:
        return {'ok': False, 'description': 'HTTP %s: %s' % (exc.code, exc.read().decode('utf-8', 'replace')[:200])}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return {'ok': False, 'description': '%s: %s' % (type(exc).__name__, exc)}
    return payload


def post_to_site(address, port, host_header, path, secret, payload):
    """POST на локальный сайт с нужным Host: иначе Flask не узнает поддомен."""
    body = json.dumps(payload).encode('utf-8')
    conn = http.client.HTTPConnection(address, port, timeout=15)
    try:
        conn.request('POST', path, body=body, headers={
            'Content-Type': 'application/json',
            'Host': host_header,
            'X-Telegram-Bot-Api-Secret-Token': secret,
        })
        response = conn.getresponse()
        return response.status, response.read().decode('utf-8', 'replace')[:200]
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--token', default=os.environ.get('SVOYAK_TELEGRAM_TOKEN', ''))
    parser.add_argument('--secret', default=os.environ.get('SVOYAK_TELEGRAM_SECRET', ''))
    parser.add_argument('--api', default='https://api.telegram.org')
    parser.add_argument('--addr', default='127.0.0.1', help='куда стучаться (адрес сайта)')
    parser.add_argument('--port', type=int, default=80)
    parser.add_argument('--host', default='svoyak.localhost', help='Host: для Flask')
    parser.add_argument('--timeout', type=int, default=25, help='таймаут long polling')
    options = parser.parse_args()

    if not options.token or not options.secret:
        print('Нужны токен и секрет: --token/--secret или SVOYAK_TELEGRAM_TOKEN/SVOYAK_TELEGRAM_SECRET')
        return 2

    path = '/telegram/webhook/' + options.secret
    me = api_call(options.api, options.token, 'getMe')
    if not me.get('ok'):
        print('Токен не принят: %s' % me.get('description'))
        return 2
    print('Бот @%s. Обновления тяну с %s, шлю на http://%s:%s%s\n' % (
        me['result'].get('username'), options.api, options.host, options.port, path))

    dropped = api_call(options.api, options.token, 'deleteWebhook', {'drop_pending_updates': False})
    if not dropped.get('ok'):
        print('deleteWebhook не сработал: %s' % dropped.get('description'))
        return 2

    offset = None
    try:
        while True:
            result = api_call(options.api, options.token, 'getUpdates', {
                'timeout': options.timeout,
                'offset': offset,
                'allowed_updates': ['message'],
            })
            if not result.get('ok'):
                print('getUpdates: %s' % result.get('description'))
                return 1
            updates = result.get('result') or []
            for update in updates:
                offset = update.get('update_id', 0) + 1
                message = update.get('message') or {}
                text = message.get('text') or ''
                print('%s  %s' % (message.get('from', {}).get('username') or '?', text or '<без текста>'))
                status, body = post_to_site(
                    options.addr, options.port, options.host, path, options.secret, update)
                print('   сайт ответил %s %s' % (status, body if status != 200 else ''))
    except KeyboardInterrupt:
        print('\nостановлено, вебхук верните командой setWebhook')
    return 0


if __name__ == '__main__':
    sys.exit(main())
