"""Инструменты администратора сайта: удаление, перенос номеров, копии и роли."""

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

WORK_DIR = tempfile.mkdtemp(prefix='svoyak-admin-')
WORK_DB = os.path.join(WORK_DIR, 'admin.db')
shutil.copyfile(os.path.join(ROOT, 'svoyak-test.db'), WORK_DB)

os.environ['SVOYAK_DB'] = WORK_DB
os.environ['SVOYAK_SERVER_NAME'] = 'localhost'
os.environ.pop('SVOYAK_TELEGRAM_TOKEN', None)

import svoyak_site_app as site  # noqa: E402
import site_admin  # noqa: E402

site.app.subdomain_matching = True
site.DB_PATH = WORK_DB
BASE = 'http://svoyak.localhost'


def db():
    conn = sqlite3.connect(WORK_DB)
    conn.row_factory = sqlite3.Row
    return conn


def count(conn, table, roomid):
    return conn.execute(
        "SELECT COUNT(*) FROM %s WHERE roomid = ?" % table, (roomid,)
    ).fetchone()[0]


class RoomDumpTests(unittest.TestCase):
    """Копия, удаление, перенос номера и восстановление — на уровне базы."""

    def setUp(self):
        # Операции с базой не должны дотянуться до исходной базы тестов.
        self.assertNotEqual(
            os.path.abspath(site.DB_PATH),
            os.path.abspath(os.path.join(ROOT, 'svoyak-test.db')),
        )
        self.conn = db()
        self.conn.isolation_level = None
        self.conn.execute("BEGIN IMMEDIATE")

    def tearDown(self):
        self.conn.execute("ROLLBACK")
        self.conn.close()

    def test_dump_collects_room_tables(self):
        dump = site_admin.room_dump(self.conn, 2024)
        self.assertEqual(dump['roomid'], 2024)
        self.assertEqual(len(dump['tables']['activerooms']), 1)
        self.assertTrue(dump['tables']['results'], 'результаты должны попасть в копию')
        self.assertTrue(dump['tables']['rules'])

    def test_dump_missing_room(self):
        with self.assertRaises(site_admin.AdminError):
            site_admin.room_dump(self.conn, 999999)

    def test_delete_frees_number(self):
        games = count(self.conn, 'results', 2024)
        self.assertGreater(games, 0)
        dump = site_admin.room_dump(self.conn, 2024)
        removed = site_admin.delete_room(self.conn, 2024)
        self.assertGreater(removed['results'], 0)
        self.assertFalse(site_admin.room_taken(self.conn, 2024))
        for table in ('results', 'activerooms', 'rules', 'log', 'activeplayers'):
            self.assertEqual(count(self.conn, table, 2024), 0, table)
        self.assertEqual(len(dump['tables']['results']), games)

    def test_renumber_moves_everything(self):
        results = count(self.conn, 'results', 2030)
        site_admin.renumber_room(self.conn, 2030, 5000)
        self.assertFalse(site_admin.room_taken(self.conn, 2030))
        self.assertTrue(site_admin.room_taken(self.conn, 5000))
        self.assertEqual(count(self.conn, 'results', 5000), results)
        self.assertEqual(count(self.conn, 'rules', 5000), 1)

    def test_renumber_refuses_taken_number(self):
        with self.assertRaises(site_admin.AdminError):
            site_admin.renumber_room(self.conn, 2030, 2024)

    def test_restore_returns_room(self):
        dump = site_admin.room_dump(self.conn, 2030)
        results = len(dump['tables']['results'])
        site_admin.delete_room(self.conn, 2030)
        restored = site_admin.restore_room(self.conn, dump)
        self.assertEqual(restored, 2030)
        self.assertEqual(count(self.conn, 'results', 2030), results)
        self.assertEqual(count(self.conn, 'rules', 2030), 1)

    def test_restore_under_new_number(self):
        dump = site_admin.room_dump(self.conn, 2030)
        site_admin.delete_room(self.conn, 2030)
        # Номер заняли заново, пока копия лежала.
        self.conn.execute(
            "INSERT INTO activerooms (roomid, date, venueid, finished, ownerid) "
            "VALUES (2030, '2026-11-18', 0, 0, NULL)"
        )
        restored = site_admin.restore_room(self.conn, dump, 6000)
        self.assertEqual(restored, 6000)
        self.assertEqual(count(self.conn, 'rules', 6000), 1)

    def test_restore_refuses_taken_number(self):
        dump = site_admin.room_dump(self.conn, 2030)
        with self.assertRaises(site_admin.AdminError):
            site_admin.restore_room(self.conn, dump, 2024)

    def test_next_roomid_above_everything(self):
        highest = self.conn.execute(
            "SELECT MAX(roomid) FROM activerooms").fetchone()[0]
        self.assertEqual(site_admin.next_roomid(self.conn), highest + 1)


class AdminApiTests(unittest.TestCase):
    """Ручки панели: права, подтверждение удаления, роли, восстановление."""

    def setUp(self):
        site.DB_PATH = WORK_DB
        self.assertNotEqual(
            os.path.abspath(site.DB_PATH),
            os.path.abspath(os.path.join(ROOT, 'svoyak-test.db')),
        )
        self.admin = self.registered('администратор')
        self.host = self.registered('ведущий')
        self.guest = site.app.test_client()

    def registered(self, role):
        client = site.app.test_client()
        login = 'adm_' + uuid.uuid4().hex[:8]
        response = client.post(
            BASE + '/register',
            data={'login': login, 'name': 'Тест ' + login,
                  'password': 'secret123', 'password2': 'secret123'},
        )
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        conn = db()
        userid = conn.execute(
            "SELECT userid FROM users WHERE login = ?", (login,)
        ).fetchone()['userid']
        conn.execute("UPDATE users SET role = ? WHERE userid = ?", (role, userid))
        conn.commit()
        conn.close()
        client.userid = userid
        client.login = login
        return client

    def test_page_needs_admin(self):
        self.assertEqual(self.guest.get(BASE + '/admin').status_code, 302)
        self.assertEqual(self.host.get(BASE + '/admin').status_code, 403)
        page = self.admin.get(BASE + '/admin')
        self.assertEqual(page.status_code, 200, page.get_data(as_text=True))
        text = page.get_data(as_text=True)
        self.assertIn('Инструменты', text)
        self.assertIn('Пользователи', text)
        self.assertIn('резервные копии', text.lower())

    def test_api_needs_admin(self):
        response = self.host.post(BASE + '/api/admin/rooms/2024/delete', json={})
        self.assertEqual(response.status_code, 403)
        guest = self.guest.post(BASE + '/api/admin/rooms/2024/delete', json={})
        self.assertEqual(guest.status_code, 401)

    def test_delete_requires_typed_number(self):
        response = self.admin.post(
            BASE + '/api/admin/rooms/2024/delete', json={'confirm': '2025'})
        self.assertEqual(response.status_code, 400)
        conn = db()
        self.assertTrue(site_admin.room_taken(conn, 2024))
        conn.close()

    def test_delete_makes_backup_and_frees_number(self):
        response = self.admin.post(
            BASE + '/api/admin/rooms/2024/delete', json={'confirm': '2024'})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = json.loads(response.get_data(as_text=True))
        self.assertTrue(os.path.isfile(
            os.path.join(site_admin.backup_dir(WORK_DB), data['backup'])))
        conn = db()
        self.assertFalse(site_admin.room_taken(conn, 2024))
        conn.close()

        page = self.admin.get(BASE + '/admin').get_data(as_text=True)
        self.assertIn(data['backup'], page)

        # И тут же возвращаем комнату, чтобы не мешать остальным тестам.
        restored = self.admin.post(
            BASE + '/api/admin/rooms/restore',
            json={'name': data['backup'], 'roomid': 2024})
        self.assertEqual(restored.status_code, 200, restored.get_data(as_text=True))
        conn = db()
        self.assertGreater(count(conn, 'results', 2024), 0)
        conn.close()

    def test_renumber_and_back(self):
        response = self.admin.post(
            BASE + '/api/admin/rooms/2030/renumber', json={'roomid': 7000})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        conn = db()
        self.assertTrue(site_admin.room_taken(conn, 7000))
        events = conn.execute(
            "SELECT data FROM log WHERE roomid = 7000 AND event = 'room renumbered'"
        ).fetchall()
        conn.close()
        self.assertTrue(events)
        back = self.admin.post(
            BASE + '/api/admin/rooms/7000/renumber', json={'roomid': 2030})
        self.assertEqual(back.status_code, 200, back.get_data(as_text=True))

    def test_renumber_to_taken_number(self):
        response = self.admin.post(
            BASE + '/api/admin/rooms/2030/renumber', json={'roomid': 2024})
        self.assertEqual(response.status_code, 400)

    def test_role_change(self):
        response = self.admin.post(
            BASE + '/api/admin/users/%d/role' % self.host.userid, json={'role': 'администратор'})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        conn = db()
        role = conn.execute(
            "SELECT role FROM users WHERE userid = ?", (self.host.userid,)).fetchone()['role']
        conn.close()
        self.assertEqual(role, 'администратор')

    def test_cannot_demote_self(self):
        response = self.admin.post(
            BASE + '/api/admin/users/%d/role' % self.admin.userid, json={'role': 'ведущий'})
        self.assertEqual(response.status_code, 400)

    def test_last_admin_is_protected(self):
        conn = db()
        conn.execute("UPDATE users SET role = 'администратор' WHERE userid = ?",
                     (self.admin.userid,))
        conn.execute("UPDATE users SET role = 'ведущий' WHERE userid <> ?",
                     (self.admin.userid,))
        conn.commit()
        conn.close()
        response = self.admin.post(
            BASE + '/api/admin/users/%d/delete' % self.admin.userid, json={})
        self.assertEqual(response.status_code, 400)
        # Возвращаем роли остальным пользователям базы.
        conn = db()
        conn.execute("UPDATE users SET role = 'администратор' WHERE userid = 1")
        conn.commit()
        conn.close()

    def test_delete_user_frees_rooms(self):
        conn = db()
        conn.execute("UPDATE activerooms SET ownerid = ? WHERE roomid = 2030",
                     (self.host.userid,))
        conn.commit()
        conn.close()
        response = self.admin.post(
            BASE + '/api/admin/users/%d/delete' % self.host.userid, json={})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        conn = db()
        self.assertIsNone(conn.execute(
            "SELECT ownerid FROM activerooms WHERE roomid = 2030").fetchone()['ownerid'])
        self.assertIsNone(conn.execute(
            "SELECT userid FROM users WHERE userid = ?", (self.host.userid,)).fetchone())
        conn.close()

    def test_cannot_delete_self(self):
        response = self.admin.post(
            BASE + '/api/admin/users/%d/delete' % self.admin.userid, json={})
        self.assertEqual(response.status_code, 400)

    def test_owner_change(self):
        response = self.admin.post(
            BASE + '/api/admin/rooms/2030/owner', json={'userid': self.host.userid})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        conn = db()
        owner = conn.execute(
            "SELECT ownerid FROM activerooms WHERE roomid = 2030").fetchone()['ownerid']
        conn.close()
        self.assertEqual(owner, self.host.userid)
        back = self.admin.post(BASE + '/api/admin/rooms/2030/owner', json={'userid': 0})
        self.assertEqual(back.status_code, 200)
        conn = db()
        self.assertIsNone(conn.execute(
            "SELECT ownerid FROM activerooms WHERE roomid = 2030").fetchone()['ownerid'])
        conn.close()


    def test_unfinish_returns_room_to_game(self):
        conn = db()
        conn.execute("UPDATE activerooms SET finished = 1 WHERE roomid = 2030")
        conn.execute(
            "INSERT OR REPLACE INTO roomhistory (roomid, winerplayerid) VALUES (?, ?)",
            (2030, 1))
        conn.commit()
        conn.close()

        response = self.admin.post(BASE + '/api/admin/rooms/2030/unfinish', json={})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        conn = db()
        finished = conn.execute(
            "SELECT finished FROM activerooms WHERE roomid = 2030").fetchone()['finished']
        events = conn.execute(
            "SELECT data FROM log WHERE roomid = 2030 AND event = 'room unfinish'"
        ).fetchall()
        conn.close()
        self.assertFalse(finished)
        self.assertTrue(events, 'отмена финиша должна попасть в журнал')
        # Результаты никуда не делись, комната снова играет.
        conn = db()
        self.assertGreater(count(conn, 'results', 2030), 0)
        conn.close()

    def test_unfinish_refuses_running_room(self):
        response = self.admin.post(BASE + '/api/admin/rooms/2024/unfinish', json={})
        self.assertEqual(response.status_code, 400)

    def test_unfinish_needs_admin(self):
        response = self.host.post(BASE + '/api/admin/rooms/2030/unfinish', json={})
        self.assertEqual(response.status_code, 403)

    def test_admin_page_offers_unfinish(self):
        conn = db()
        conn.execute("UPDATE activerooms SET finished = 1 WHERE roomid = 2030")
        conn.commit()
        conn.close()
        text = self.admin.get(BASE + '/admin').get_data(as_text=True)
        self.assertIn('unfinishRoom(2030)', text)
        conn = db()
        conn.execute("UPDATE activerooms SET finished = 0 WHERE roomid = 2030")
        conn.commit()
        conn.close()


if __name__ == '__main__':
    unittest.main()
