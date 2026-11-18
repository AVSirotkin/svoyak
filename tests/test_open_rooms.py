"""Открытые комнаты: создание без регистрации, работа гостя и привязка владельца."""

import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import unittest
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

WORK_DIR = tempfile.mkdtemp(prefix='svoyak-open-')
WORK_DB = os.path.join(WORK_DIR, 'open.db')
shutil.copyfile(os.path.join(ROOT, 'svoyak-test.db'), WORK_DB)

os.environ['SVOYAK_DB'] = WORK_DB
os.environ['SVOYAK_SERVER_NAME'] = 'localhost'
os.environ.pop('SVOYAK_TELEGRAM_TOKEN', None)

import svoyak_site_app as site  # noqa: E402

site.app.subdomain_matching = True
# Модуль приложения один на процесс: если тесты запускают несколько файлов
# сразу, база берётся из первого импорта. Переключаем её явно.
site.DB_PATH = WORK_DB
BASE = 'http://svoyak.localhost'


def db():
    conn = sqlite3.connect(WORK_DB)
    conn.row_factory = sqlite3.Row
    return conn


class GuestTests(unittest.TestCase):
    """Что доступно тому, кто не входил."""

    def setUp(self):
        # Приложение одно на процесс: направляем его в свою базу.
        site.DB_PATH = WORK_DB
        self.guest = site.app.test_client()

    def test_init_page_is_open(self):
        response = self.guest.get(BASE + '/init/0')
        self.assertEqual(response.status_code, 200)
        self.assertIn('без владельца', response.get_data(as_text=True))

    def test_guest_creates_ownerless_room(self):
        conn = db()
        rulesid = conn.execute("SELECT min(rulesid) FROM basic_rules").fetchone()[0]
        conn.close()
        response = self.guest.post(
            BASE + '/api/addroom',
            json={'date': '2026-11-11', 'venueid': 0, 'rulesid': rulesid, 'tables': 1},
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.get_data(as_text=True))
        self.assertEqual(data['Status'], 'Ok')
        roomid = data['RoomId']
        self.assertTrue(roomid)

        conn = db()
        owner = conn.execute(
            "SELECT ownerid FROM activerooms WHERE roomid = ?", (roomid,)
        ).fetchone()['ownerid']
        conn.close()
        self.assertIsNone(owner, 'комната гостя должна быть без владельца')

    def test_guest_opens_ownerless_room(self):
        conn = db()
        conn.execute("UPDATE activerooms SET ownerid = NULL WHERE roomid = 2024")
        conn.commit()
        conn.close()
        self.assertEqual(self.guest.get(BASE + '/room/2024').status_code, 200)
        self.assertEqual(self.guest.get(BASE + '/plan/2024').status_code, 200)

    def test_guest_cannot_open_foreign_room(self):
        conn = db()
        conn.execute("UPDATE activerooms SET ownerid = 1 WHERE roomid = 2024")
        conn.commit()
        conn.close()
        response = self.guest.get(BASE + '/room/2024')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response.headers['Location'])

    def test_guest_json_api_needs_login_for_foreign_room(self):
        conn = db()
        conn.execute("UPDATE activerooms SET ownerid = 1 WHERE roomid = 2024")
        conn.commit()
        conn.close()
        response = self.guest.post(BASE + '/api/plan/2024/order', json={'allow': True})
        self.assertEqual(response.status_code, 401)
        self.assertTrue(json.loads(response.get_data(as_text=True))['needs_login'])

    def test_guest_can_save_game_in_open_room(self):
        conn = db()
        conn.execute("UPDATE activerooms SET ownerid = NULL WHERE roomid = 2024")
        conn.commit()
        players = conn.execute(
            "SELECT playerid, name FROM players LIMIT 4"
        ).fetchall()
        conn.close()
        scores = [
            {'playerid': row['playerid'], 'name': row['name'], 'score': 10 * (index + 1)}
            for index, row in enumerate(players)
        ]
        response = self.guest.post(
            BASE + '/api/plan/2024/game/1', json={'scores': scores, 'override': True}
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        conn = db()
        saved = conn.execute(
            "SELECT count(*) FROM results WHERE roomid = 2024 AND gameindex = 1"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(saved, len(scores))


class ClaimTests(unittest.TestCase):
    """Комната без владельца привязывается к аккаунту."""

    def setUp(self):
        # Приложение одно на процесс: направляем его в свою базу.
        site.DB_PATH = WORK_DB
        # Логин свой у каждого теста: база общая на весь модуль.
        self.login = 'open_' + uuid.uuid4().hex[:8]
        self.host = site.app.test_client()
        response = self.host.post(
            BASE + '/register',
            data={
                'login': self.login,
                'name': 'Хозяин',
                'password': 'secret123',
                'password2': 'secret123',
            },
        )
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        conn = db()
        conn.execute("UPDATE activerooms SET ownerid = NULL WHERE roomid = 2024")
        conn.commit()
        self.userid = conn.execute(
            "SELECT userid FROM users WHERE login = ?", (self.login,)
        ).fetchone()['userid']
        conn.close()

    def test_guest_claim_requires_login(self):
        guest = site.app.test_client()
        response = guest.post(BASE + '/api/room/2024/claim')
        self.assertEqual(response.status_code, 401)

    def test_claim_sets_owner_and_closes_room(self):
        response = self.host.post(BASE + '/api/room/2024/claim')
        self.assertEqual(response.status_code, 200)
        conn = db()
        owner = conn.execute(
            "SELECT ownerid FROM activerooms WHERE roomid = 2024"
        ).fetchone()['ownerid']
        conn.close()
        self.assertEqual(owner, self.userid)
        # После привязки гостя в комнату уже не пускают.
        guest = site.app.test_client()
        self.assertEqual(guest.get(BASE + '/room/2024').status_code, 302)

    def test_claim_twice_is_conflict(self):
        self.assertEqual(self.host.post(BASE + '/api/room/2024/claim').status_code, 200)
        second = site.app.test_client()
        second.post(
            BASE + '/register',
            data={
                'login': 'second_' + uuid.uuid4().hex[:8],
                'name': 'Второй',
                'password': 'secret123',
                'password2': 'secret123',
            },
        )
        response = second.post(BASE + '/api/room/2024/claim')
        self.assertEqual(response.status_code, 409)

    def test_claim_missing_room(self):
        response = self.host.post(BASE + '/api/room/999999/claim')
        self.assertEqual(response.status_code, 404)


class FreeSeatingPlanTests(unittest.TestCase):
    """Пульт турнира у свободной посадки: правила видны, /init не предлагается."""

    SPONTAN = json.dumps({'basic_game_number': 3})
    BATUMI = json.dumps({'basic_game_number': 3, 'final_players': 4,
                         'final_participants': 'best_active'})

    def setUp(self):
        site.DB_PATH = WORK_DB
        conn = db()
        conn.execute("UPDATE activerooms SET ownerid = NULL WHERE roomid = 2030")
        self.set_rules(conn, 'Spontan', self.SPONTAN)
        conn.close()
        self.guest = site.app.test_client()

    def tearDown(self):
        conn = db()
        self.set_rules(conn, 'Spontan', self.SPONTAN)
        conn.close()

    @staticmethod
    def set_rules(conn, name, parameters):
        conn.execute("DELETE FROM rules WHERE roomid = 2030")
        conn.execute(
            "INSERT INTO rules(roomid, name, parameters, brackets) VALUES(?,?,?,NULL)",
            (2030, name, parameters),
        )
        conn.commit()

    def page(self):
        response = self.guest.get(BASE + '/plan/2030')
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        # Переносы строк в разметке не должны мешать проверке текста.
        return re.sub(r'\s+', ' ', response.get_data(as_text=True))

    def test_rules_without_final(self):
        html = self.page()
        self.assertIn('Свободная посадка', html)
        self.assertIn('первые 3 игры', html)
        self.assertIn('финального боя нет', html)
        self.assertNotIn('/init/2030', html, 'настройка занятой комнаты не работает')

    def test_rules_with_final(self):
        conn = db()
        self.set_rules(conn, 'Batumi3', self.BATUMI)
        conn.close()
        html = self.page()
        self.assertIn('4 участника', html)
        self.assertIn('лучшие по текущему зачёту', html)
        self.assertNotIn('финального боя нет', html)

    def test_plural_filter(self):
        self.assertEqual(site.plural_filter(1, 'игру', 'игры', 'игр'), 'игру')
        self.assertEqual(site.plural_filter(3, 'игру', 'игры', 'игр'), 'игры')
        self.assertEqual(site.plural_filter(5, 'игру', 'игры', 'игр'), 'игр')
        self.assertEqual(site.plural_filter(11, 'игру', 'игры', 'игр'), 'игр')
        self.assertEqual(site.plural_filter(21, 'игру', 'игры', 'игр'), 'игру')


class RoomConsoleTests(unittest.TestCase):
    """Настройки и журнал комнаты живут в пульте турнира, /admin ведёт туда же."""

    def setUp(self):
        site.DB_PATH = WORK_DB
        self.login = 'own_' + uuid.uuid4().hex[:8]
        self.owner = site.app.test_client()
        response = self.owner.post(
            BASE + '/register',
            data={'login': self.login, 'name': 'Организатор',
                  'password': 'secret123', 'password2': 'secret123'},
        )
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        conn = db()
        self.userid = conn.execute(
            "SELECT userid FROM users WHERE login = ?", (self.login,)
        ).fetchone()['userid']
        conn.execute("UPDATE activerooms SET ownerid = ? WHERE roomid = 2030", (self.userid,))
        conn.execute("UPDATE activerooms SET date = '2026-11-13', venueid = 0 WHERE roomid = 2030")
        conn.commit()
        conn.close()
        self.guest = site.app.test_client()

    def tearDown(self):
        conn = db()
        conn.execute("UPDATE activerooms SET date = '2026-11-13', venueid = 0 WHERE roomid = 2030")
        conn.execute("DELETE FROM log WHERE roomid = 2030 AND event = 'room settings'")
        conn.commit()
        conn.close()

    def page(self, client):
        response = client.get(BASE + '/plan/2030')
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return re.sub(r'\s+', ' ', response.get_data(as_text=True))

    def test_admin_redirects_to_journal(self):
        response = self.owner.get(BASE + '/admin/2030')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/plan/2030#log', response.headers['Location'])

    def test_owner_sees_settings_and_journal(self):
        html = self.page(self.owner)
        self.assertIn('Настройки комнаты', html)
        self.assertIn('Журнал комнаты', html)
        self.assertIn('Завершить комнату', html)

    def test_guest_does_not_get_into_closed_room(self):
        response = self.guest.get(BASE + '/plan/2030')
        self.assertEqual(response.status_code, 302)

    def test_settings_save_date(self):
        response = self.owner.post(
            BASE + '/api/plan/2030/settings', json={'date': '2026-11-20', 'venueid': 0}
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        conn = db()
        room = conn.execute(
            "SELECT date, venueid FROM activerooms WHERE roomid = 2030"
        ).fetchone()
        history = conn.execute(
            "SELECT date FROM roomhistory WHERE roomid = 2030"
        ).fetchone()
        events = conn.execute(
            "SELECT data FROM log WHERE roomid = 2030 AND event = 'room settings' "
            "ORDER BY rowid DESC"
        ).fetchall()
        conn.close()
        self.assertEqual(room['date'], '2026-11-20')
        self.assertEqual(room['venueid'], 0)
        self.assertEqual(history['date'], '2026-11-20')
        self.assertIn('2026-11-20', events[0]['data'])
        self.assertIn('дата 2026-11-20', site.room_log_view([
            {'id': 1, 'date': 'сегодня', 'event': 'room settings', 'data': events[0]['data']},
        ])[0]['text'])

    def test_settings_reject_bad_date(self):
        response = self.owner.post(
            BASE + '/api/plan/2030/settings', json={'date': '20.11.2026'}
        )
        self.assertEqual(response.status_code, 400)

    def test_settings_other_room_is_forbidden(self):
        other = site.app.test_client()
        other.post(
            BASE + '/register',
            data={'login': 'other_' + uuid.uuid4().hex[:8], 'name': 'Чужой',
                  'password': 'secret123', 'password2': 'secret123'},
        )
        response = other.post(
            BASE + '/api/plan/2030/settings', json={'date': '2026-11-20', 'venueid': 0}
        )
        self.assertEqual(response.status_code, 403)

    def test_log_text(self):
        self.assertEqual(site.log_text('save game result', {'gameindex': 3}), 'сохранён бой 3')
        self.assertEqual(site.log_text('add active player', {'name': 'Иванов'}),
                         'добавлен игрок Иванов')
        self.assertEqual(site.log_text('area host', {'area': 2, 'by': 'Петров'}),
                         'ведущий площадки 2 — Петров')
        self.assertEqual(site.log_text('draw', {'places': ['а', 'б']}), 'жеребьёвка, мест: 2')
        self.assertEqual(site.log_text('plan order rule', {'allow_any_order': True}),
                         'порядок боёв можно менять')
        self.assertEqual(site.log_text('какое-то событие', {}), 'какое-то событие')


if __name__ == '__main__':
    unittest.main()
