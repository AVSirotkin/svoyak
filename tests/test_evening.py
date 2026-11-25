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


class SpreadTests(EveningBase):
    """«Разложить пакет целиком»: бои раскладки встают подряд одним действием."""

    def parts_of(self, layoutid):
        conn = db()
        try:
            row = conn.execute("SELECT parts FROM package_layouts WHERE layoutid = ?",
                               (layoutid,)).fetchone()
            return json.loads(row[0])
        finally:
            conn.close()

    def topics_of(self, packageid):
        conn = db()
        try:
            row = conn.execute("SELECT data FROM packages WHERE packageid = ?",
                               (packageid,)).fetchone()
            return json.loads(row[0])["topics"]
        finally:
            conn.close()

    def spread(self, start, packageid, layoutid):
        """То же, что делает кнопка: бои раскладки подряд, по одному запросу."""
        for part, indexes in enumerate(self.parts_of(layoutid)):
            response = self.holder.post(
                BASE + '/api/plan/%d/questions' % self.room,
                json={'gameindex': start + part, 'packageid': packageid,
                      'themes': indexes, 'layoutid': layoutid, 'part': part,
                      'force': True, 'ack_heard': True})
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

    def test_page_has_spread_card(self):
        text = self.plan_page()
        self.assertIn('Разложить пакет целиком', text)
        self.assertIn('id="spread-package"', text)
        self.assertIn('id="spread-layout"', text)
        self.assertIn('onclick="spreadPackage()"', text)

    def test_spread_starts_at_first_free_battle(self):
        self.assign(1, self.first_id, self.first_layout, 0, [0, 2, 4, 6, 8, 10])
        self.assign(2, self.first_id, self.first_layout, 1, [1, 3, 5, 7, 9, 11])
        text = self.plan_page()
        start = text.find('id="spread-from"')
        self.assertGreater(start, 0)
        self.assertIn('value="3"', text[start:start + 200])

    def test_spread_lays_out_whole_package(self):
        # Двенадцать тем — два боя по шесть; оба встают одним действием.
        self.spread(1, self.first_id, self.first_layout)
        parts = self.parts_of(self.first_layout)
        self.assertEqual(len(parts), 2)
        topics = self.topics_of(self.first_id)
        for part, indexes in enumerate(parts, 1):
            plan = self.plan_battle(part)
            self.assertIsNotNone(plan, part)
            self.assertEqual([theme['topic'] for theme in plan['topics']],
                             [topics[index]['topic'] for index in indexes], part)
            self.assertEqual(plan['packageid'], self.first_id)
        self.assertIsNone(self.plan_battle(3))

    def test_spread_second_package_after_first(self):
        self.spread(1, self.first_id, self.first_layout)
        self.spread(3, self.second_id, self.second_layout)
        topics = self.topics_of(self.second_id)
        plan = self.plan_battle(3)
        self.assertEqual([theme['topic'] for theme in plan['topics']],
                         [topics[index]['topic'] for index in self.parts_of(
                             self.second_layout)[0]])
        self.assertEqual(plan['packageid'], self.second_id)
        # Оба пакета легли подряд, ничего не потерялось.
        self.assertIsNone(self.plan_battle(5))


class ConsoleLayoutPlanTests(EveningBase):
    """Бой из раскладки, взятый в пульте, виден в плане и сдвигает бои."""

    def setUp(self):
        super().setUp()
        self.odd = [0, 2, 4, 6, 8, 10]
        self.even = [1, 3, 5, 7, 9, 11]
        self.assign(1, self.first_id, self.first_layout, 0, self.odd)
        self.assign(2, self.first_id, self.first_layout, 1, self.even)
        self.assign(3, self.second_id, self.second_layout, 0, self.odd)

    def play(self, gameindex, names=('Аня', 'Боря', 'Вова', 'Гоша')):
        conn = db()
        try:
            for position, name in enumerate(names, 1):
                conn.execute(
                    "INSERT INTO results (roomid, gameindex, playerid, name, score, position, "
                    "points, gamenumber) VALUES (?, ?, NULL, ?, ?, ?, 2.0, ?)",
                    (self.room, gameindex, name, 10 * position, float(position), gameindex))
            conn.commit()
        finally:
            conn.close()

    def pick(self, layoutid, part):
        return self.holder.post(
            BASE + '/api/plan/%d/battle' % self.room,
            json={'layoutid': layoutid, 'part': part})

    def topics_of_plan(self, gameindex):
        plan = self.plan_battle(gameindex)
        return [theme['topic'] for theme in plan['topics']]

    def test_picked_battle_lands_in_plan(self):
        self.play(1)
        response = self.pick(self.second_layout, 1)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = json.loads(response.get_data(as_text=True))
        self.assertTrue(data['shifted'])
        self.assertEqual(data['gameindex'], 2)
        self.assertIsNone(data['replay_of'])
        self.assertEqual(self.topics_of_plan(2),
                         ['Пакет %sВторой, тема %s' % (self.suffix, number)
                          for number in (2, 4, 6, 8, 10, 12)])

    def test_planned_battles_shift_forward(self):
        self.play(1)
        self.pick(self.second_layout, 1)
        # Что было боем 2, стало боем 3; что было боем 3, стало боем 4.
        self.assertEqual(self.topics_of_plan(3),
                         ['Пакет %sПервый, тема %s' % (self.suffix, number)
                          for number in (2, 4, 6, 8, 10, 12)])
        self.assertEqual(self.topics_of_plan(4),
                         ['Пакет %sВторой, тема %s' % (self.suffix, number)
                          for number in (1, 3, 5, 7, 9, 11)])

    def test_plan_page_shows_the_fact(self):
        self.play(1)
        self.pick(self.second_layout, 1)
        text = self.plan_page(5)
        self.assertIn('Бой взят в пульте из раскладки пакета', text)
        self.assertIn('сдвинулись', text)

    def test_console_calls_it_layout_battle(self):
        self.play(1)
        self.pick(self.second_layout, 1)
        plan = self.console_plan()
        self.assertTrue(plan['name'].startswith('Раскладка, бой 2: Пакет Второй'),
                        plan['name'])

    def test_replay_of_played_battle_is_marked(self):
        # Бой 1 сыгран; берём в бой 2 те же темы — это повтор боя 1.
        self.play(1)
        response = self.pick(self.first_layout, 0)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = json.loads(response.get_data(as_text=True))
        self.assertEqual(data['replay_of'], 1)
        self.assertTrue(data['battle']['name'].startswith('Повтор боя 1:'),
                        data['battle']['name'])
        self.assertIn('Повтор боя 1', self.plan_page(5))

    def test_same_battle_twice_does_not_shift(self):
        self.play(1)
        self.pick(self.second_layout, 1)
        before = self.topics_of_plan(3)
        response = self.pick(self.second_layout, 1)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertFalse(json.loads(response.get_data(as_text=True))['shifted'])
        # Сдвига не было: соседний бой остался тем же.
        self.assertEqual(self.topics_of_plan(3), before)

    def test_repeat_points_at_the_first_battle(self):
        # Бой 1 сыгран, и те же темы сыграны ещё раз в бою 2.
        self.play(1)
        self.pick(self.first_layout, 0)
        self.play(2)
        response = self.pick(self.first_layout, 0)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = json.loads(response.get_data(as_text=True))
        self.assertEqual(data['replay_of'], 1, 'повтор должен указывать на первый бой')

    def test_unknown_part_is_refused(self):
        self.assertEqual(self.pick(self.second_layout, 7).status_code, 404)


class ConsoleLayoutTests(EveningBase):
    """Пульт: взять в текущий бой бой из раскладки."""

    def list_layouts(self):
        response = self.holder.get(BASE + '/api/room/%d/layouts' % self.room)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return json.loads(response.get_data(as_text=True))['packages']

    def layout_part(self, layoutid, part):
        return self.holder.get(
            BASE + '/api/room/%d/layouts/%d/part/%d' % (self.room, layoutid, part))

    def test_room_layouts_are_listed(self):
        packages = self.list_layouts()
        self.assertEqual(len(packages), 2)
        names = {item['name'] for item in packages}
        self.assertIn('Пакет Первый ' + self.suffix, names)
        layouts = packages[0]['layouts']
        self.assertEqual(len(layouts), 1)
        self.assertEqual(layouts[0]['name'], 'Чёт/нечет')
        self.assertEqual([part['size'] for part in layouts[0]['parts']], [6, 6])
        # Названия тем идут вместе с боем: ведущему видно, что он берёт.
        self.assertEqual(layouts[0]['parts'][0]['titles'][0],
                         'Пакет %sПервый, тема 1' % self.suffix)

    def test_layout_part_returns_themes(self):
        response = self.layout_part(self.first_layout, 0)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = json.loads(response.get_data(as_text=True))
        self.assertEqual(data['part'], 0)
        self.assertEqual(data['name'], 'Чёт/нечет · бой 1')
        self.assertEqual(len(data['topics']), 6)
        self.assertEqual([theme['topic'] for theme in data['topics']],
                         ['Пакет %sПервый, тема %s' % (self.suffix, number)
                          for number in (1, 3, 5, 7, 9, 11)])

    def test_second_part_is_the_even_battle(self):
        data = json.loads(self.layout_part(self.first_layout, 1).get_data(as_text=True))
        self.assertEqual([theme['topic'] for theme in data['topics']],
                         ['Пакет %sПервый, тема %s' % (self.suffix, number)
                          for number in (2, 4, 6, 8, 10, 12)])

    def test_unknown_part_is_not_found(self):
        self.assertEqual(self.layout_part(self.first_layout, 5).status_code, 404)

    def test_foreign_layout_is_not_found(self):
        # Раскладка пакета, которого в комнате нет, пульту не нужна.
        conn = db()
        try:
            layoutid = conn.execute(
                "SELECT layoutid FROM package_layouts WHERE libraryid NOT IN "
                "(SELECT libraryid FROM packages WHERE roomid = ?) LIMIT 1",
                (self.room,)).fetchone()
        finally:
            conn.close()
        if layoutid is None:
            self.skipTest('в базе нет чужой раскладки')
        response = self.layout_part(layoutid[0], 0)
        self.assertEqual(response.status_code, 404)

    def test_console_has_layout_picker(self):
        text = self.holder.get(BASE + '/room/%d' % self.room).get_data(as_text=True)
        self.assertIn('id="layoutPickRow"', text)
        self.assertIn('id="layoutPickSelect"', text)
        self.assertIn('onchange="loadLayoutBattle()"', text)
        self.assertIn('async function initLayoutPicker()', text)
        self.assertIn('async function loadLayoutBattle()', text)

    def test_console_warns_when_battle_numbers_diverge(self):
        text = self.holder.get(BASE + '/room/%d' % self.room).get_data(as_text=True)
        self.assertIn('function showGameindexMismatch(', text)
        self.assertIn('showGameindexMismatch(serverGameindex, localNext)', text)


if __name__ == '__main__':
    unittest.main()
