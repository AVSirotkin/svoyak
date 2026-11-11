"""Полный прогон привязки Telegram и уведомлений без настоящего бота.

Скрипт копирует тестовую базу во временный файл, поднимает на локальном порту
заглушку api.telegram.org и проходит весь путь: выдача ссылки, /start, привязка,
назначение на площадку, сохранение боя, отвязка.

    python tools/telegram_selftest.py
    python tools/telegram_selftest.py --db svoyak-test.db -v
"""

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = 'http://svoyak.localhost'
SECRET = 'selftest-secret'
CHAT_ID = 5551

sent = []


class FakeTelegram(BaseHTTPRequestHandler):
    """Отвечает как api.telegram.org и запоминает, что мы ему отправили."""

    def do_POST(self):
        length = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(length).decode('utf-8')
        try:
            payload = json.loads(body or '{}')
        except ValueError:
            payload = {'raw': body}
        sent.append({'path': self.path, 'payload': payload})
        answer = json.dumps({'ok': True, 'result': {'message_id': len(sent)}}).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)

    def log_message(self, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='svoyak-test.db', help='база-образец для копии')
    parser.add_argument('-v', '--verbose', action='store_true', help='печатать тексты уведомлений')
    options = parser.parse_args()

    source = os.path.join(ROOT, options.db)
    if not os.path.exists(source):
        print('Нет базы %s. Сначала соберите тестовую базу или укажите --db.' % source)
        return 2

    workdir = tempfile.mkdtemp(prefix='svoyak-tg-')
    work_db = os.path.join(workdir, 'selftest.db')
    shutil.copyfile(source, work_db)

    server = ThreadingHTTPServer(('127.0.0.1', 0), FakeTelegram)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    os.environ['SVOYAK_DB'] = work_db
    os.environ['SVOYAK_SERVER_NAME'] = 'localhost'
    os.environ['SVOYAK_TELEGRAM_TOKEN'] = 'selftest-token'
    os.environ['SVOYAK_TELEGRAM_BOT'] = 'svoyak_test_bot'
    os.environ['SVOYAK_TELEGRAM_SECRET'] = SECRET
    os.environ['SVOYAK_TELEGRAM_API'] = 'http://127.0.0.1:%s' % server.server_address[1]
    sys.path.insert(0, ROOT)
    import svoyak_site_app as site

    site.app.subdomain_matching = True
    failures = []

    def check(label, ok, detail=''):
        print(('  ok    ' if ok else '  ПРОВАЛ') + '  ' + label + ((' — ' + detail) if detail and (ok or options.verbose) else ''))
        if not ok:
            failures.append(label)

    def db(sql, params=()):
        conn = sqlite3.connect(work_db)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    class User:
        def __init__(self, login, name):
            self.login = login
            self.name = name
            self.password = 'selftest1234'
            self.client = site.app.test_client()

        def sign_in(self):
            page = self.client.post(BASE + '/register', data={
                'login': self.login, 'name': self.name,
                'password': self.password, 'password2': self.password})
            if page.status_code != 302:
                self.client.post(BASE + '/login', data={'login': self.login, 'password': self.password})
            return self

        def get(self, url):
            return self.client.get(BASE + url)

        def post(self, url, payload=None, form=None, headers=None):
            if form is not None:
                return self.client.post(BASE + url, data=form, headers=headers)
            return self.client.post(BASE + url, json=payload or {}, headers=headers)

    print('Привязка Telegram: прогон на копии %s\n' % options.db)

    admin = User('selftest_admin', 'Организатор').sign_in()
    host = User('selftest_host', 'Ведущий').sign_in()
    admin_id = db('SELECT userid FROM users WHERE login = ?', ('selftest_admin',))[0]['userid']
    host_id = db('SELECT userid FROM users WHERE login = ?', ('selftest_host',))[0]['userid']
    conn = sqlite3.connect(work_db)
    conn.execute('UPDATE users SET role = ? WHERE userid = ?', ('администратор', admin_id))
    conn.execute('UPDATE activerooms SET ownerid = ? WHERE roomid = ?', (admin_id, 2024))
    conn.commit()
    conn.close()

    anonymous = site.app.test_client()
    page = anonymous.get(BASE + '/profile')
    check('профиль закрыт без входа', page.status_code == 302, page.headers.get('Location', ''))

    body = host.get('/profile').data.decode('utf-8')
    check('в профиле есть кнопка привязки', 'Привязать Telegram' in body and '/telegram/link' in body)

    page = host.post('/telegram/link')
    token = page.headers.get('Location', '').split('link_token=')[-1]
    check('ссылка привязки выдана', page.status_code == 302 and bool(token), 'токен длиной %s' % len(token))

    page = host.get('/profile?link_token=' + token)
    check('на странице видна ссылка на бота',
          ('https://t.me/svoyak_test_bot?start=link_' + token) in page.data.decode('utf-8'))

    check('чужой секрет вебхука не пускает',
          anonymous.post(BASE + '/telegram/webhook/not-the-secret', json={}).status_code == 404)
    check('чужой заголовок не пускает',
          host.post('/telegram/webhook/' + SECRET, {
              'message': {'chat': {'id': CHAT_ID, 'type': 'private'}, 'text': '/start link_' + token}},
              headers={'X-Telegram-Bot-Api-Secret-Token': 'wrong'}).status_code == 403)

    before = len(sent)
    page = host.post('/telegram/webhook/' + SECRET, {
        'message': {'chat': {'id': CHAT_ID, 'type': 'private'},
                    'from': {'id': CHAT_ID, 'username': 'selftest_nick'},
                    'text': '/start link_' + token}})
    reply = sent[before]['payload'].get('text', '') if len(sent) > before else ''
    check('бот ответил «готово»', page.status_code == 200 and 'привязан' in reply, reply[:60])
    row = db('SELECT telegram_id, telegram_chat_id FROM users WHERE login = ?', ('selftest_host',))[0]
    check('chat_id записан в аккаунт', row['telegram_chat_id'] == CHAT_ID, str(row['telegram_chat_id']))

    before = len(sent)
    host.post('/telegram/webhook/' + SECRET, {
        'message': {'chat': {'id': CHAT_ID, 'type': 'private'}, 'text': '/start link_' + token}})
    check('ссылка одноразовая', 'устарела' in sent[before]['payload'].get('text', ''))

    before = len(sent)
    host.post('/telegram/webhook/' + SECRET, {
        'message': {'chat': {'id': -100500, 'type': 'group'}, 'text': '/start link_' + token}})
    check('в групповом чате привязки нет', 'личном чате' in sent[before]['payload'].get('text', ''))

    before = len(sent)
    page = admin.post('/api/plan/2024/areas/2', {'userid': host_id})
    text = sent[before]['payload'].get('text', '') if len(sent) > before else ''
    check('назначение на площадку уведомило ведущего',
          page.status_code == 200 and 'площадки 2' in text, text[:70])

    before = len(sent)
    page = admin.post('/api/plan/2024/game/2', {'scores': [{'playerid': 1, 'name': 'Кто-то', 'score': 10}], 'override': True})
    text = sent[before]['payload'].get('text', '') if len(sent) > before else ''
    check('сохранение боя уведомило ведущего',
          page.status_code == 200 and 'бой 2 сохранён' in text, text[:70])
    check('в уведомлении назван следующий бой', 'Следующий бой: 5' in text)

    page = host.post('/telegram/unlink', form={'password': 'неверный'})
    check('отвязка без пароля не срабатывает',
          page.headers.get('Location', '').endswith('error=password')
          and db('SELECT telegram_chat_id FROM users WHERE login = ?', ('selftest_host',))[0]['telegram_chat_id'] is not None)

    page = host.post('/telegram/unlink', form={'password': 'selftest1234'})
    check('отвязка с паролем снимает привязку',
          page.headers.get('Location', '').endswith('ok=unlinked')
          and db('SELECT telegram_chat_id FROM users WHERE login = ?', ('selftest_host',))[0]['telegram_chat_id'] is None)

    check('журнал уведомлений виден в профиле',
          'Последние уведомления' in host.get('/profile').data.decode('utf-8'))
    check('события в журнале сервера',
          {'telegram link', 'telegram unlink'} <= {row['event'] for row in db('SELECT event FROM log')})

    server.shutdown()
    shutil.rmtree(workdir, ignore_errors=True)

    print('\nВсего проверок: %s, провалов: %s' % (
        len(failures) + 17, len(failures)))
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
