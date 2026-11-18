"""История протокола: снимок перед перезаписью и возврат расплюсовки."""

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

WORK_DIR = tempfile.mkdtemp(prefix='svoyak-protocol-')
WORK_DB = os.path.join(WORK_DIR, 'protocol.db')
shutil.copyfile(os.path.join(ROOT, 'svoyak-test.db'), WORK_DB)

os.environ['SVOYAK_DB'] = WORK_DB
os.environ['SVOYAK_SERVER_NAME'] = 'localhost'
os.environ.pop('SVOYAK_TELEGRAM_TOKEN', None)

import svoyak_site_app as site  # noqa: E402

site.app.subdomain_matching = True
# Модуль приложения один на процесс: если тесты запускают несколько файлов
# сразу, база берётся из первого импорта. Переключаем её явно.
site.DB_PATH = WORK_DB
site.ensure_tournament_tables()
site.ensure_auth_tables()
BASE = 'http://svoyak.localhost'

MARKS = {
    '0:26': 'plus', '1:83': 'plus', '2:415': 'plus', '3:256': 'plus',
    '5:256': 'minus', '7:26': 'minus',
}


def db():
    conn = sqlite3.connect(WORK_DB)
    conn.row_factory = sqlite3.Row
    return conn


class ProtocolHistoryTests(unittest.TestCase):
    def setUp(self):
        # Приложение одно на процесс: направляем его в свою базу.
        site.DB_PATH = WORK_DB
        conn = db()
        conn.execute("UPDATE activerooms SET ownerid = NULL WHERE roomid = 2024")
        conn.execute("DELETE FROM game_protocols WHERE roomid = 2024")
        conn.execute("DELETE FROM game_protocol_history WHERE roomid = 2024")
        conn.commit()
        conn.close()
        self.guest = site.app.test_client()

    def save_protocol(self, marks, pointer=0):
        return self.guest.post(
            BASE + '/api/plan/2024/game/3/protocol',
            json={'themes': [], 'marks': marks, 'pointer': pointer, 'placeholder': True},
        )

    def history(self):
        response = self.guest.get(BASE + '/api/plan/2024/game/3/protocol/history')
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return json.loads(response.get_data(as_text=True))['history']

    def test_overwrite_keeps_previous_marks(self):
        self.assertEqual(self.save_protocol(MARKS, 7).status_code, 200)
        self.assertEqual(self.save_protocol({}, 0).status_code, 200)
        history = self.history()
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['marks'], len(MARKS))
        self.assertEqual(history[0]['plus'], 4)

    def test_identical_payload_is_not_snapshotted(self):
        self.save_protocol(MARKS, 7)
        self.save_protocol(MARKS, 7)
        self.assertEqual(self.history(), [])

    def test_restore_returns_marks(self):
        self.save_protocol(MARKS, 7)
        self.save_protocol({}, 0)
        snapshot_id = self.history()[0]['id']
        response = self.guest.post(
            BASE + '/api/plan/2024/game/3/protocol/restore', json={'id': snapshot_id}
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

        current = self.guest.get(BASE + '/api/plan/2024/game/3/protocol')
        protocol = json.loads(current.get_data(as_text=True))['protocol']
        self.assertEqual(protocol['marks'], MARKS)
        # Затёртая версия тоже осталась в истории — возврат обратим.
        self.assertEqual(len(self.history()), 2)

    def test_restore_unknown_snapshot(self):
        response = self.guest.post(
            BASE + '/api/plan/2024/game/3/protocol/restore', json={'id': 999999}
        )
        self.assertEqual(response.status_code, 404)

    def test_history_is_limited(self):
        # Снимок делается, когда отметок становится меньше: так выглядит сброс.
        marks = {'%d:256' % step: 'plus' for step in range(25)}
        self.save_protocol(marks, 0)
        for step in range(25):
            marks.pop('%d:256' % step)
            self.save_protocol(dict(marks), step)
        self.assertEqual(len(self.history()), 20)

    def test_growing_marks_are_not_snapshotted(self):
        # Обычная правка по ходу боя историю не засоряет.
        self.save_protocol({'0:256': 'plus'}, 0)
        self.save_protocol({'0:256': 'plus', '1:256': 'minus'}, 1)
        self.assertEqual(self.history(), [])

    def test_history_of_unknown_game(self):
        response = self.guest.get(BASE + '/api/plan/2024/game/999/protocol/history')
        self.assertEqual(response.status_code, 404)

    def test_foreign_room_needs_login(self):
        conn = db()
        conn.execute("UPDATE activerooms SET ownerid = 1 WHERE roomid = 2024")
        conn.commit()
        conn.close()
        response = self.guest.get(BASE + '/api/plan/2024/game/3/protocol/history')
        self.assertEqual(response.status_code, 401)


class ProtocolRevisionTests(unittest.TestCase):
    """Ревизии протокола: спорные версии сохраняются, решает организатор."""

    def setUp(self):
        site.DB_PATH = WORK_DB
        self.login = 'rev_' + uuid.uuid4().hex[:8]
        self.host = site.app.test_client()
        response = self.host.post(
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
        conn.execute("UPDATE activerooms SET ownerid = ? WHERE roomid = 2024", (self.userid,))
        conn.execute("DELETE FROM game_protocols WHERE roomid = 2024")
        conn.execute("DELETE FROM game_protocol_history WHERE roomid = 2024")
        conn.execute("DELETE FROM game_protocol_conflicts WHERE roomid = 2024")
        conn.commit()
        conn.close()

    def save(self, marks, base_revision=None, **extra):
        body = {'themes': [], 'marks': marks, 'pointer': 0, 'placeholder': True}
        if base_revision is not None:
            body['base_revision'] = base_revision
        body.update(extra)
        return self.host.post(BASE + '/api/plan/2024/game/3/protocol', json=body)

    def protocol(self):
        response = self.host.get(BASE + '/api/plan/2024/game/3/protocol')
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return json.loads(response.get_data(as_text=True))['protocol']

    def conflicts(self):
        response = self.host.get(
            BASE + '/api/plan/2024/game/3/protocol/conflicts'
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return json.loads(response.get_data(as_text=True))

    def test_revision_grows(self):
        self.assertEqual(self.save(MARKS).status_code, 200)
        self.assertEqual(self.protocol()['revision'], 1)
        self.assertEqual(self.save({'0:256': 'plus'}).status_code, 200)
        self.assertEqual(self.protocol()['revision'], 2)

    def test_stale_version_goes_to_conflicts(self):
        self.save({'0:256': 'plus'})
        self.save({'0:256': 'plus', '1:256': 'plus'})       # ревизия 2
        stale = {'0:256': 'plus', '2:256': 'minus'}
        response = self.save(stale, base_revision=1)         # правил версию 1
        self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
        data = json.loads(response.get_data(as_text=True))
        self.assertEqual(data['status'], 'conflict')

        # Текущая версия не тронута, спорная сохранена отдельно.
        self.assertEqual(self.protocol()['marks'], {'0:256': 'plus', '1:256': 'plus'})
        listed = self.conflicts()
        self.assertEqual(len([item for item in listed['conflicts']
                              if item['status'] == 'open']), 1)
        self.assertEqual(listed['current']['marks'], 2)

        # Организатор выбирает спорную версию — она становится текущей.
        conflict_id = data['conflict_id']
        chosen = self.host.post(
            BASE + '/api/plan/2024/game/3/protocol/conflicts/%d' % conflict_id,
            json={'action': 'choose'},
        )
        self.assertEqual(chosen.status_code, 200, chosen.get_data(as_text=True))
        self.assertEqual(self.protocol()['marks'], stale)
        self.assertEqual(self.conflicts()['conflicts'][0]['status'], 'chosen')

    def test_missing_room_is_404(self):
        # Свободная посадка обходится без схемы, но не без комнаты.
        response = self.host.get(BASE + '/api/plan/999999/game/1/protocol')
        self.assertEqual(response.status_code, 404, response.get_data(as_text=True))

    def test_pointer_only_keeps_revision(self):
        self.save({'0:256': 'plus'})
        before = self.protocol()
        response = self.host.post(
            BASE + '/api/plan/2024/game/3/protocol',
            json={'pointer_only': True, 'pointer': 5},
        )
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        after = self.protocol()
        self.assertEqual(after['revision'], before['revision'])
        self.assertEqual(after['pointer'], 5)

    def test_old_client_does_not_erase_fields(self):
        # Пульт старой версии шлёт только отметки: состав и очки не теряются.
        players = [{'playerid': 256, 'name': 'Иванов', 'place': 1}]
        self.save({'0:256': 'plus'}, players=players, scores={'256': 10},
                  scoresSource='manual', halfMinus=True, finished=False)
        self.save({'0:256': 'plus', '1:256': 'minus'})
        protocol = self.protocol()
        self.assertEqual(protocol['players'], players)
        self.assertEqual(protocol['scores'], {'256': 10})
        self.assertEqual(protocol['scoresSource'], 'manual')
        self.assertTrue(protocol['halfMinus'])


class ConsoleWiringTests(unittest.TestCase):
    """Пульты получают общий модуль синхронизации и показывают режим."""

    def setUp(self):
        site.DB_PATH = WORK_DB
        conn = db()
        conn.execute("UPDATE activerooms SET ownerid = NULL WHERE roomid = 2024")
        conn.commit()
        conn.close()
        self.guest = site.app.test_client()

    def test_console_has_sync_controls(self):
        page = self.guest.get(BASE + '/room/2024/3')
        self.assertEqual(page.status_code, 200, page.get_data(as_text=True))
        html = page.get_data(as_text=True)
        self.assertIn('/static/js/sync.js', html)
        self.assertIn('id="syncModeSelect"', html)

    def test_old_console_has_sync_controls(self):
        page = self.guest.get(BASE + '/room/2024')
        self.assertEqual(page.status_code, 200, page.get_data(as_text=True))
        html = page.get_data(as_text=True)
        self.assertIn('/static/js/sync.js', html)
        self.assertIn('id="syncModeSelect"', html)

    def test_old_console_shows_login(self):
        guest = self.guest.get(BASE + '/room/2024').get_data(as_text=True)
        self.assertIn('/login', guest)
        self.assertNotIn('user-chip', guest)

        client = site.app.test_client()
        login = 'head_' + uuid.uuid4().hex[:8]
        registered = client.post(
            BASE + '/register',
            data={'login': login, 'name': 'Ведущий Пётр',
                  'password': 'secret123', 'password2': 'secret123'},
        )
        self.assertEqual(registered.status_code, 302, registered.get_data(as_text=True))
        html = client.get(BASE + '/room/2024').get_data(as_text=True)
        self.assertIn('user-chip', html)
        self.assertIn('Ведущий Пётр', html)
        self.assertIn('/logout', html)

    def test_old_console_links_to_tournament_console(self):
        # Пульт турнира доступен из пульта и у свободной посадки, где сетки нет.
        for roomid in (2024, 2030):
            conn = db()
            conn.execute("UPDATE activerooms SET ownerid = NULL WHERE roomid = ?", (roomid,))
            conn.commit()
            conn.close()
            html = self.guest.get(BASE + '/room/%d' % roomid).get_data(as_text=True)
            self.assertIn('href="/plan/%d"' % roomid, html)

    def test_plan_page_shows_last_sync(self):
        # Активность показывается по текущему бою площадки.
        conn = site.get_db_connection()
        try:
            plan = site.build_room_plan(conn, 2024)
            current = plan.current_game(3)
        finally:
            conn.close()
        self.assertIsNotNone(current)
        self.guest.post(
            BASE + '/api/plan/2024/game/%d/protocol' % current.gameindex,
            json={'themes': [], 'marks': {'0:256': 'plus'}, 'pointer': 2,
                  'placeholder': True, 'mode': 'limited'},
        )
        page = self.guest.get(BASE + '/plan/2024')
        self.assertEqual(page.status_code, 200, page.get_data(as_text=True))
        html = page.get_data(as_text=True)
        self.assertIn('data-synced=', html)
        self.assertIn('ограниченный', html)


class FreeSeatingTests(unittest.TestCase):
    """Свободная посадка: схемы нет, а протокол боя всё равно пишется.

    Такую комнату ведёт старый пульт: он сам нумерует бои, площадок и
    турнирной сетки у неё нет.
    """

    def setUp(self):
        site.DB_PATH = WORK_DB
        conn = db()
        conn.execute("UPDATE activerooms SET ownerid = NULL WHERE roomid = 2030")
        conn.execute("DELETE FROM game_protocols WHERE roomid = 2030")
        conn.execute("DELETE FROM game_protocol_history WHERE roomid = 2030")
        conn.commit()
        conn.close()
        self.guest = site.app.test_client()

    def test_room_has_no_plan(self):
        conn = site.get_db_connection()
        try:
            self.assertIsNone(site.build_room_plan(conn, 2030))
        finally:
            conn.close()

    def test_protocol_round_trip(self):
        saved = self.guest.post(
            BASE + '/api/plan/2030/game/1/protocol',
            json={'themes': [], 'marks': {'0:1': 'plus', '1:2': 'minus'}, 'pointer': 2,
                  'mode': 'sync'},
        )
        self.assertEqual(saved.status_code, 200, saved.get_data(as_text=True))
        self.assertEqual(json.loads(saved.get_data(as_text=True))['revision'], 1)

        loaded = self.guest.get(BASE + '/api/plan/2030/game/1/protocol')
        self.assertEqual(loaded.status_code, 200, loaded.get_data(as_text=True))
        protocol = json.loads(loaded.get_data(as_text=True))['protocol']
        self.assertIsNotNone(protocol)
        self.assertEqual(protocol['marks'], {'0:1': 'plus', '1:2': 'minus'})
        self.assertEqual(protocol['mode'], 'sync')

    def test_revision_grows_without_plan(self):
        first = self.guest.post(
            BASE + '/api/plan/2030/game/2/protocol',
            json={'themes': [], 'marks': {'0:1': 'plus'}},
        )
        base = json.loads(first.get_data(as_text=True))['revision']
        second = self.guest.post(
            BASE + '/api/plan/2030/game/2/protocol',
            json={'marks': {'0:1': 'minus'}, 'base_revision': base},
        )
        self.assertEqual(second.status_code, 200, second.get_data(as_text=True))
        self.assertEqual(json.loads(second.get_data(as_text=True))['revision'], base + 1)

    def test_old_console_works_without_plan(self):
        page = self.guest.get(BASE + '/room/2030')
        self.assertEqual(page.status_code, 200, page.get_data(as_text=True))
        self.assertIn('id="syncModeSelect"', page.get_data(as_text=True))

    def test_schema_without_games_is_free_seating(self):
        # Схема без единого боя — сетку ещё не составили: бои нумерует пульт.
        conn = db()
        conn.execute(
            "UPDATE rules SET brackets = ? WHERE roomid = 2030",
            (json.dumps({'name': 'пусто',
                         'matches': [{'id': '1', 'type': 'seed', 'participantSlots': 4}]},
                        ensure_ascii=False),),
        )
        conn.commit()
        conn.close()
        try:
            response = self.guest.get(BASE + '/api/plan/2030/game/1/protocol')
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        finally:
            conn = db()
            conn.execute("UPDATE rules SET brackets = NULL WHERE roomid = 2030")
            conn.commit()
            conn.close()

    def test_game_outside_plan_is_still_404(self):
        # Комната со схемой: боя, которого в ней нет, по-прежнему не существует.
        conn = db()
        conn.execute("UPDATE activerooms SET ownerid = NULL WHERE roomid = 2024")
        conn.commit()
        conn.close()
        response = self.guest.get(BASE + '/api/plan/2024/game/999/protocol')
        self.assertEqual(response.status_code, 404)


if __name__ == '__main__':
    unittest.main()
