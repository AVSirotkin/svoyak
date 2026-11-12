"""История протокола: снимок перед перезаписью и возврат расплюсовки."""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

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
        for step in range(25):
            self.save_protocol({'%d:256' % step: 'plus'}, step)
        self.assertEqual(len(self.history()), 20)

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


if __name__ == '__main__':
    unittest.main()
