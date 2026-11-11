"""Заглушка api.telegram.org: печатает уведомления вместо отправки в Telegram.

Нужна, чтобы пройти привязку и уведомления локально, вообще не имея бота.

    python tools/fake_telegram.py --port 8081

и в том же окружении, где запускается сайт:

    SVOYAK_TELEGRAM_TOKEN=local-token
    SVOYAK_TELEGRAM_BOT=svoyak_bot
    SVOYAK_TELEGRAM_SECRET=local-secret
    SVOYAK_TELEGRAM_API=http://127.0.0.1:8081

После этого всё, что сайт «отправил», видно в консоли заглушки.
"""

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

count = 0


class Handler(BaseHTTPRequestHandler):
    def _answer(self, payload):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._answer({'ok': True, 'result': {'username': 'fake_bot', 'is_bot': True}})

    def do_POST(self):
        global count
        length = int(self.headers.get('Content-Length') or 0)
        raw = self.rfile.read(length).decode('utf-8', 'replace')
        try:
            payload = json.loads(raw or '{}')
        except ValueError:
            payload = {'raw': raw}
        count += 1
        method = self.path.rstrip('/').rsplit('/', 1)[-1]
        stamp = time.strftime('%H:%M:%S')
        if method == 'sendMessage':
            text = (payload.get('text') or '').replace('\n', '\n           ')
            print('%s  -> чат %s\n           %s\n' % (stamp, payload.get('chat_id'), text), flush=True)
        else:
            print('%s  -> %s %s' % (stamp, method, raw[:200]), flush=True)
        self._answer({'ok': True, 'result': {'message_id': count}})

    def log_message(self, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8081)
    options = parser.parse_args()
    server = ThreadingHTTPServer(('127.0.0.1', options.port), Handler)
    print('Заглушка Telegram на http://127.0.0.1:%s — Ctrl+C чтобы остановить\n' % options.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nостановлено, сообщений было: %s' % count)


if __name__ == '__main__':
    main()
