"""Подробный отчёт боя: берётся последнее сохранение, а не первое."""

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

WORK_DIR = tempfile.mkdtemp(prefix='svoyak-detailed-')
WORK_DB = os.path.join(WORK_DIR, 'detailed.db')
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
ROOM = 2041


def db():
    conn = sqlite3.connect(WORK_DB)
    conn.row_factory = sqlite3.Row
    return conn


def detailed_row(gameindex, players, scores):
    return json.dumps({
        'gameindex': gameindex,
        'scores': [{'name': name, 'score': str(score)}
                   for name, score in zip(players, scores)],
        'detailed': {
            'players': players,
            'scores': scores,
            'answers': [{'theme': 'Тема', 'value': 100,
                         'playerAnswers': [True, False, None, None]}],
        },
    }, ensure_ascii=False)


class DetailedReportTests(unittest.TestCase):
    def setUp(self):
        site.DB_PATH = WORK_DB
        conn = db()
        conn.execute("DELETE FROM log WHERE roomid = ?", (ROOM,))
        conn.commit()
        conn.close()
        self.client = site.app.test_client()

    def add_save(self, gameindex, players, scores):
        conn = db()
        conn.execute("INSERT INTO log (date, roomid, event, data) VALUES (?, ?, ?, ?)",
                     ('Wed Nov 19 20:00:00 2026', ROOM, 'save game result',
                      detailed_row(gameindex, players, scores)))
        conn.commit()
        conn.close()

    def report(self, gameindex):
        response = self.client.get(BASE + '/api/detailed/%d/%d' % (ROOM, gameindex))
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return json.loads(response.get_data(as_text=True))

    def test_report_uses_latest_save(self):
        # Бой сохранили дважды: сначала ошиблись, потом поправили счёт.
        self.add_save(3, ['Старый Игрок', 'Второй', '', ''], [-10, 20, 0, 0])
        self.add_save(3, ['Ковба Алексей', 'Паутов Виталий', '', ''], [70, 90, 0, 0])
        report = self.report(3)
        self.assertEqual(report['players'], ['Ковба Алексей', 'Паутов Виталий', '', ''])
        self.assertEqual(report['scores'], [70, 90, 0, 0])

    def test_report_of_other_battle_is_untouched(self):
        self.add_save(2, ['Первый', '', '', ''], [10, 0, 0, 0])
        self.add_save(4, ['Второй', '', '', ''], [20, 0, 0, 0])
        self.assertEqual(self.report(4)['players'], ['Второй', '', '', ''])
        self.assertEqual(self.report(2)['players'], ['Первый', '', '', ''])

    def test_missing_report_is_null(self):
        self.assertIsNone(self.report(9))


if __name__ == '__main__':
    unittest.main()
