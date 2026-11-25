"""Ссылка управления площадкой: один пульт на бой.

Ключ в адресе открывает пульт тому, кому его передали. Перевыпуск ссылки
закрывает доступ у всех, кому досталась старая.
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

WORK_DIR = tempfile.mkdtemp(prefix='svoyak-control-')
WORK_DB = os.path.join(WORK_DIR, 'control.db')
shutil.copyfile(os.path.join(ROOT, 'svoyak-test.db'), WORK_DB)

os.environ['SVOYAK_DB'] = WORK_DB
os.environ['SVOYAK_SERVER_NAME'] = 'localhost'
os.environ.pop('SVOYAK_TELEGRAM_TOKEN', None)

import svoyak_site_app as site  # noqa: E402

site.app.subdomain_matching = True
site.DB_PATH = WORK_DB
site.ensure_tournament_tables()
site.ensure_auth_tables()
BASE = 'http://svoyak.localhost'

ROOM = 2029          # в тестовой базе у комнаты есть схема: площадки 1, 2, 3
MARKS = {'0:26': 'plus'}


def db():
    conn = sqlite3.connect(WORK_DB)
    conn.row_factory = sqlite3.Row
    return conn


def protocol(marks=None):
    return {
        'themes': [],
        'marks': MANUAL_MARKS if marks is None else marks,
        'pointer': 0,
        'placeholder': True,
    }


MANUAL_MARKS = MARKS


class ControlLinkTests(unittest.TestCase):
    def setUp(self):
        site.DB_PATH = WORK_DB
        # Организатор: свежий аккаунт, комната закреплена за ним.
        self.login = 'link_' + uuid.uuid4().hex[:8]
        self.owner = site.app.test_client()
        response = self.owner.post(
            BASE + '/register',
            data={
                'login': self.login,
                'name': 'Организатор',
                'password': 'secret123',
                'password2': 'secret123',
            },
        )
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        conn = db()
        self.userid = conn.execute(
            "SELECT userid FROM users WHERE login = ?", (self.login,)
        ).fetchone()['userid']
        conn.execute(
            "UPDATE activerooms SET ownerid = ?, finished = 0 WHERE roomid = ?",
            (self.userid, ROOM),
        )
        conn.execute("DELETE FROM control_links WHERE roomid = ?", (ROOM,))
        conn.execute("DELETE FROM game_protocols WHERE roomid = ?", (ROOM,))
        conn.execute("DELETE FROM game_protocol_history WHERE roomid = ?", (ROOM,))
        conn.execute("DELETE FROM area_hosts WHERE roomid = ?", (ROOM,))
        conn.commit()
        conn.close()
        self.guest = site.app.test_client()

    # ---------------------------------------------------------- помощники
    def link(self, area):
        response = self.owner.post(
            BASE + '/api/plan/%s/control-link' % ROOM,
            json={'area': area},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = json.loads(response.get_data(as_text=True))
        self.assertTrue(data['url'])
        return data['url']

    def revoke(self, area):
        response = self.owner.delete(
            BASE + '/api/plan/%s/control-link' % ROOM, json={'area': area}
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

    def open_link(self, url, client=None):
        client = client or self.guest
        response = client.get(BASE + url)
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        return response.headers['Location']

    def save(self, gameindex, client=None):
        client = client or self.guest
        return client.post(
            BASE + '/api/plan/%s/game/%s/protocol' % (ROOM, gameindex),
            json=protocol(),
        )

    # ------------------------------------------------------------- тесты
    def test_guest_cannot_write_without_link(self):
        self.assertEqual(self.save(1).status_code, 401)

    def test_guest_cannot_open_area_without_link(self):
        response = self.guest.get(BASE + '/room/%s/1' % ROOM)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response.headers['Location'])

    def test_link_opens_area_and_allows_writing(self):
        url = self.link(1)
        location = self.open_link(url)
        self.assertEqual(location, '/room/%s/1' % ROOM)
        self.assertEqual(self.guest.get(BASE + location).status_code, 200)
        self.assertEqual(self.save(1).status_code, 200)

    def test_wrong_key_does_not_open_area(self):
        self.assertEqual(self.guest.get(BASE + '/room/%s/1?key=nope' % ROOM).status_code, 302)
        response = self.guest.post(
            BASE + '/api/plan/%s/game/1/protocol?key=nope' % ROOM, json=protocol()
        )
        self.assertEqual(response.status_code, 401)

    def test_link_covers_only_its_area(self):
        url = self.link(2)
        self.open_link(url)
        # Бой 1 — площадка 1, ссылка выдана на площадку 2.
        self.assertEqual(self.save(1).status_code, 403)
        # Свой бой по-прежнему пишется.
        self.assertEqual(self.save(2).status_code, 200)

    def test_reissue_closes_old_link(self):
        self.open_link(self.link(1))
        self.assertEqual(self.save(1).status_code, 200)
        self.link(1)   # перевыпуск: прежний ключ больше не действует
        # Гость без действующей ссылки: 401 (нужен вход) или 403 (нет прав).
        self.assertIn(self.save(1).status_code, (401, 403))

    def test_revoke_closes_area(self):
        self.open_link(self.link(1))
        self.assertEqual(self.save(1).status_code, 200)
        self.revoke(1)
        self.assertIn(self.save(1).status_code, (401, 403))

    def test_room_link_grants_whole_room(self):
        url = self.link(0)
        self.assertEqual(url, '/room/%s?key=%s' % (ROOM, url.split('key=')[1]))
        location = self.open_link(url)
        self.assertEqual(location, '/room/%s' % ROOM)
        # Ключ комнаты даёт права организатора: назначение ведущего площадки.
        response = self.guest.post(
            BASE + '/api/plan/%s/areas/2' % ROOM, json={'userid': None}
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

    def test_stranger_cannot_issue_link(self):
        stranger_login = 'stranger_' + uuid.uuid4().hex[:8]
        stranger = site.app.test_client()
        stranger.post(
            BASE + '/register',
            data={
                'login': stranger_login,
                'name': 'Посторонний',
                'password': 'secret123',
                'password2': 'secret123',
            },
        )
        response = stranger.post(
            BASE + '/api/plan/%s/control-link' % ROOM, json={'area': 1}
        )
        self.assertEqual(response.status_code, 403)

    def test_plan_page_shows_link_to_organizer(self):
        url = self.link(2)
        page = self.owner.get(BASE + '/plan/%s' % ROOM)
        self.assertEqual(page.status_code, 200)
        body = page.get_data(as_text=True)
        self.assertIn('control-link-input', body)
        self.assertIn(url, body)


if __name__ == '__main__':
    unittest.main()
