# -*- coding: utf-8 -*-
"""Вечер из двух пакетов: пакет 1, потом пакет 2, у обоих раскладка чёт/нечет."""

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

WORK_DIR = tempfile.mkdtemp(prefix='svoyak-evening-')
WORK_DB = os.path.join(WORK_DIR, 'evening.db')
shutil.copyfile(os.path.join(ROOT, 'svoyak-test.db'), WORK_DB)

os.environ['SVOYAK_DB'] = WORK_DB
os.environ['SVOYAK_SERVER_NAME'] = 'localhost'
os.environ.pop('SVOYAK_TELEGRAM_TOKEN', None)

import svoyak_site_app as site  # noqa: E402
import package_library  # noqa: E402

site.app.subdomain_matching = True
site.DB_PATH = WORK_DB
BASE = 'http://svoyak.localhost'


def db():
    conn = sqlite3.connect(WORK_DB)
    conn.row_factory = sqlite3.Row
    return conn


def ek_topics(suffix, numbers):
    """Темы пакета: раунды по четыре, как бой ЭК. numbers — номера тем пакета."""
    topics = []
    names = ('Первый', 'Второй', 'Третий')
    for position, number in enumerate(numbers):
        topics.append({
            "topic": "Пакет %s, тема %s" % (suffix, number),
            "stage": "Бой 1/8 финала. %s раунд" % names[position // 4],
            "questions": [{"value": 10,
                           "text": "Вопрос %s-%s" % (suffix, number),
                           "answer": "ответ"}],
        })
    return topics


class EveningBase(unittest.TestCase):
    """Комната со свободной посадкой и два пакета по двенадцать тем."""

    def setUp(self):
        package_library.reset_caches()
        site.DB_PATH = WORK_DB
        self.suffix = uuid.uuid4().hex[:6]
        self.holder = self.registered()
        self.room = self.new_room(self.holder.userid)
        self.first = self.upload('Первый')
        self.second = self.upload('Второй')
        self.first_id = self.take_to_room(self.first)
        self.second_id = self.take_to_room(self.second)
        self.first_layout = self.make_layout(self.first)
        self.second_layout = self.make_layout(self.second)

    def registered(self):
        client = site.app.test_client()
        login = 'ev_' + uuid.uuid4().hex[:8]
        response = client.post(BASE + '/register', data={
            'login': login, 'name': 'Ведущий ' + login,
            'password': 'secret123', 'password2': 'secret123'})
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        conn = db()
        userid = conn.execute("SELECT userid FROM users WHERE login = ?",
                              (login,)).fetchone()[0]
        conn.commit()
        conn.close()
        client.userid = userid
        return client

    def new_room(self, ownerid):
        roomid = 9900 + int(uuid.uuid4().hex[:3], 16)
        conn = db()
        conn.execute(
            "INSERT OR REPLACE INTO activerooms (roomid, date, venueid, finished, ownerid) "
            "VALUES (?, '2026-11-20', 0, 0, ?)", (roomid, ownerid))
        conn.commit()
        conn.close()
        return roomid

    def upload(self, label):
        topics = ek_topics(self.suffix + label, range(1, 13))
        payload = {
            "data": json.dumps({"topics": topics, "sourceFileName": "ek.json"},
                               ensure_ascii=False),
            "name": "Пакет %s %s" % (label, self.suffix),
        }
        response = self.holder.post(BASE + '/api/packages/library', json=payload)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return json.loads(response.get_data(as_text=True))['libraryid']

    def take_to_room(self, libraryid):
        response = self.holder.post(
            BASE + '/api/packages/library/%d/take' % libraryid,
            json={'roomid': self.room})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        conn = db()
        try:
            return conn.execute(
                "SELECT packageid FROM packages WHERE roomid = ? AND libraryid = ?",
                (self.room, libraryid)).fetchone()[0]
        finally:
            conn.close()

    def make_layout(self, libraryid):
        response = self.holder.post(
            BASE + '/api/packages/library/%d/layouts' % libraryid,
            json={'preset': 'alternate'})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return json.loads(response.get_data(as_text=True))['layoutid']

    def assign(self, gameindex, packageid, layoutid, part, themes):
        response = self.holder.post(
            BASE + '/api/plan/%d/questions' % self.room,
            json={'gameindex': gameindex, 'packageid': packageid, 'themes': themes,
                  'layoutid': layoutid, 'part': part,
                  'force': True, 'ack_heard': True})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response

    def plan_battle(self, gameindex):
        response = self.holder.get(
            BASE + '/api/room/%d/battle/%d/plan' % (self.room, gameindex))
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return json.loads(response.get_data(as_text=True))['battle']

    def console_plan(self):
        page = self.holder.get(BASE + '/room/%d' % self.room)
        text = page.get_data(as_text=True)
        start = text.find('let planBattle = ')
        line = text[start:text.find('\n', start)]
        return json.loads(line[len('let planBattle = '):].rstrip(';'))

    def plan_page(self, extra=0):
        url = BASE + '/plan/%d/questions' % self.room
        if extra:
            url += '?extra=%d' % extra
        page = self.holder.get(url)
        self.assertEqual(page.status_code, 200, page.get_data(as_text=True))
        return page.get_data(as_text=True)


class TwoPackageTests(EveningBase):
    """Пакет 1 на первые бои, пакет 2 на следующие, оба по чёт/нечету."""

    def setUp(self):
        super().setUp()
        self.odd = [0, 2, 4, 6, 8, 10]
        self.even = [1, 3, 5, 7, 9, 11]
        # Бои 1–2 — первый пакет, бои 3–4 — второй, той же раскладкой.
        self.assign(1, self.first_id, self.first_layout, 0, self.odd)
        self.assign(2, self.first_id, self.first_layout, 1, self.even)
        self.assign(3, self.second_id, self.second_layout, 0, self.odd)
        self.assign(4, self.second_id, self.second_layout, 1, self.even)

    def test_first_battle_gets_first_package(self):
        plan = self.console_plan()
        self.assertTrue(plan['name'].startswith('План: Пакет Первый'))
        self.assertEqual([theme['topic'] for theme in plan['topics']],
                         ["Пакет %sПервый, тема %s" % (self.suffix, number)
                          for number in (1, 3, 5, 7, 9, 11)])

    def test_third_battle_gets_second_package(self):
        plan = self.plan_battle(3)
        self.assertEqual(plan['packageid'], self.second_id)
        self.assertEqual([theme['topic'] for theme in plan['topics']],
                         ["Пакет %sВторой, тема %s" % (self.suffix, number)
                          for number in (1, 3, 5, 7, 9, 11)])

    def test_each_battle_has_its_own_part(self):
        expected = [
            (1, 'Первый', (1, 3, 5, 7, 9, 11)),
            (2, 'Первый', (2, 4, 6, 8, 10, 12)),
            (3, 'Второй', (1, 3, 5, 7, 9, 11)),
            (4, 'Второй', (2, 4, 6, 8, 10, 12)),
        ]
        for gameindex, label, numbers in expected:
            plan = self.plan_battle(gameindex)
            self.assertEqual(
                [theme['topic'] for theme in plan['topics']],
                ["Пакет %s%s, тема %s" % (self.suffix, label, number)
                 for number in numbers], gameindex)

    def test_fifth_battle_has_no_plan_yet(self):
        self.assertIsNone(self.plan_battle(5))

    def test_console_says_free_seating(self):
        page = self.holder.get(BASE + '/room/%d' % self.room)
        self.assertIn('const freeSeating = true', page.get_data(as_text=True))

    def test_horizon_shows_whole_evening(self):
        # Ничего не назначено — виден только следующий бой.
        text = self.plan_page(0)
        self.assertIn('data-game="1"', text)
        self.assertNotIn('data-game="6"', text)
        # Попросили вперёд — видны все бои вечера.
        text = self.plan_page(11)
        for gameindex in (1, 4, 12):
            self.assertIn('data-game="%d"' % gameindex, text)

    def test_plan_page_knows_both_packages(self):
        text = self.plan_page(3)
        self.assertIn("Пакет Первый " + self.suffix, text)
        self.assertIn("Пакет Второй " + self.suffix, text)
        self.assertIn("Чёт/нечет", text)

    def test_console_switches_package_with_battle(self):
        """Бой 1 сыгран — пульт показывает бой 2 того же пакета."""
        conn = db()
        try:
            for position, name in enumerate(['Аня', 'Боря', 'Вова', 'Гоша'], 1):
                conn.execute(
                    "INSERT INTO results (roomid, gameindex, playerid, name, score, position, "
                    "points, gamenumber) VALUES (?, 1, NULL, ?, ?, ?, 2.0, 1)",
                    (self.room, name, 10 * position, float(position)))
            conn.commit()
        finally:
            conn.close()
        plan = self.console_plan()
        self.assertEqual([theme['topic'] for theme in plan['topics']],
                         ["Пакет %sПервый, тема %s" % (self.suffix, number)
                          for number in (2, 4, 6, 8, 10, 12)])

    def test_console_moves_to_second_package_after_two_battles(self):
        conn = db()
        try:
            for gameindex in (1, 2):
                for position, name in enumerate(['Аня', 'Боря', 'Вова', 'Гоша'], 1):
                    conn.execute(
                        "INSERT INTO results (roomid, gameindex, playerid, name, score, "
                        "position, points, gamenumber) VALUES (?, ?, NULL, ?, ?, ?, 2.0, ?)",
                        (self.room, gameindex, name, 10 * position, float(position),
                         gameindex))
            conn.commit()
        finally:
            conn.close()
        plan = self.console_plan()
        self.assertTrue(plan['name'].startswith('План: Пакет Второй'))
        self.assertEqual([theme['topic'] for theme in plan['topics']],
                         ["Пакет %sВторой, тема %s" % (self.suffix, number)
                          for number in (1, 3, 5, 7, 9, 11)])


class HorizonTests(EveningBase):
    """Раскладка вечера вперёд: бои в плане идут подряд, без пропусков."""

    def test_empty_room_shows_one_battle(self):
        page = self.holder.get(BASE + '/plan/%d/questions' % self.room)
        text = page.get_data(as_text=True)
        self.assertIn('data-game="1"', text)
        self.assertNotIn('data-game="2"', text)

    def test_extra_adds_battles(self):
        # «+5» — пять боёв сверх следующего: пустая комната показывает 1…6.
        text = self.plan_page(5)
        for gameindex in range(1, 7):
            self.assertIn('data-game="%d"' % gameindex, text)
        self.assertNotIn('data-game="7"', text)

    def test_far_planned_battle_keeps_gap_free(self):
        # Бой 1 разложен, дальше пусто: в плане всё равно видны бои 1, 2, …
        self.assign(1, self.first_id, self.first_layout, 0, [0, 2, 4, 6, 8, 10])
        text = self.plan_page(0)
        self.assertNotIn('data-game="3"', text)
        text = self.plan_page(2)
        for gameindex in (1, 2, 3, 4):
            self.assertIn('data-game="%d"' % gameindex, text)


if __name__ == '__main__':
    unittest.main()
