# -*- coding: utf-8 -*-
"""Раскладки пакета: как двенадцать тем боя ЭК разложить на два боя свояка."""

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

WORK_DIR = tempfile.mkdtemp(prefix='svoyak-layouts-')
WORK_DB = os.path.join(WORK_DIR, 'layouts.db')
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


def ek_topics(suffix='', blocks=1):
    """Пакет как у ЭК: раунды по четыре темы, по три раунда на бой."""
    topics = []
    names = ('Первый', 'Второй', 'Третий')
    for block in range(blocks):
        for round_index, stage in enumerate(names):
            for position in range(4):
                number = block * 12 + round_index * 4 + position + 1
                topics.append({
                    "topic": "Тема %s %s" % (number, suffix),
                    "stage": "Кубок %s. %s раунд" % (block + 1, stage),
                    "questions": [{"value": 10,
                                   "text": "Вопрос %s %s" % (number, suffix),
                                   "answer": "ответ"}],
                })
    return topics


def package_data(topics, suffix=''):
    return {"topics": topics, "sourceFileName": "ek.json"}


class LayoutBase(unittest.TestCase):
    """Пользователь, комната и пакет на двенадцать тем с раундами."""

    def setUp(self):
        package_library.reset_caches()
        site.DB_PATH = WORK_DB
        self.suffix = uuid.uuid4().hex[:6]
        self.holder = self.registered('Правообладатель')
        self.other = self.registered('Посторонний')
        self.room = self.new_room(self.holder.userid)
        self.topics = ek_topics(self.suffix)
        self.libraryid = self.upload(self.holder, self.topics)

    def registered(self, name, role='ведущий'):
        client = site.app.test_client()
        login = 'ly_' + uuid.uuid4().hex[:8]
        response = client.post(BASE + '/register', data={
            'login': login, 'name': name + ' ' + login,
            'password': 'secret123', 'password2': 'secret123'})
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        conn = db()
        userid = conn.execute("SELECT userid FROM users WHERE login = ?",
                              (login,)).fetchone()[0]
        conn.execute("UPDATE users SET role = ? WHERE userid = ?", (role, userid))
        conn.commit()
        conn.close()
        client.userid = userid
        return client

    def new_room(self, ownerid):
        roomid = 9700 + int(uuid.uuid4().hex[:3], 16)
        conn = db()
        conn.execute(
            "INSERT OR REPLACE INTO activerooms (roomid, date, venueid, finished, ownerid) "
            "VALUES (?, '2026-11-20', 0, 0, ?)", (roomid, ownerid))
        conn.commit()
        conn.close()
        return roomid

    def upload(self, client, topics):
        payload = {
            "data": json.dumps(package_data(topics, self.suffix), ensure_ascii=False),
            "name": "Пакет ЭК " + self.suffix,
        }
        response = client.post(BASE + '/api/packages/library', json=payload)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return json.loads(response.get_data(as_text=True))['libraryid']

    def make_layout(self, client, preset='alternate', name=''):
        response = client.post(
            BASE + '/api/packages/library/%d/layouts' % self.libraryid,
            json={'preset': preset, 'name': name})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return json.loads(response.get_data(as_text=True))['layoutid']

    def layouts(self):
        conn = db()
        try:
            return package_library.package_layouts(conn, self.libraryid)
        finally:
            conn.close()

    def take_to_room(self):
        response = self.holder.post(
            BASE + '/api/packages/library/%d/take' % self.libraryid,
            json={'roomid': self.room})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        conn = db()
        try:
            return conn.execute(
                "SELECT packageid FROM packages WHERE roomid = ? AND libraryid = ?",
                (self.room, self.libraryid)).fetchone()[0]
        finally:
            conn.close()

    def assign(self, packageid, gameindex, themes, layoutid=None, part=None):
        response = self.holder.post(
            BASE + '/api/plan/%d/questions' % self.room,
            json={'gameindex': gameindex, 'packageid': packageid, 'themes': themes,
                  'layoutid': layoutid, 'part': part, 'force': True, 'ack_heard': True})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response


class PresetTests(unittest.TestCase):
    """Пресеты: три способа разложить бой ЭК из двенадцати тем."""

    def test_three_variants_of_ek_battle(self):
        topics = ek_topics('п')
        presets = {item['key']: item for item in package_library.layout_presets(topics, 6)}
        self.assertEqual(presets['flat']['parts'],
                         [[0, 1, 2, 3, 4, 5], [6, 7, 8, 9, 10, 11]])
        self.assertEqual(presets['alternate']['parts'],
                         [[0, 2, 4, 6, 8, 10], [1, 3, 5, 7, 9, 11]])
        self.assertEqual(presets['halves']['parts'],
                         [[0, 1, 4, 5, 8, 9], [2, 3, 6, 7, 10, 11]])

    def test_every_preset_covers_all_themes_once(self):
        topics = ek_topics('ц', blocks=3)
        for preset in package_library.layout_presets(topics, 6):
            used = [index for part in preset['parts'] for index in part]
            self.assertEqual(sorted(used), list(range(len(topics))), preset['key'])
            self.assertEqual(len(preset['parts']), 6, preset['key'])
            self.assertTrue(all(len(part) == 6 for part in preset['parts']), preset['key'])

    def test_package_without_rounds_has_two_presets(self):
        topics = [{"topic": "Тема %s" % index, "questions": [
            {"value": 10, "text": "вопрос %s" % index, "answer": "ответ"}]}
            for index in range(12)]
        presets = [item['key'] for item in package_library.layout_presets(topics, 6)]
        self.assertEqual(presets, ['flat', 'alternate'])

    def test_empty_package_has_no_presets(self):
        self.assertEqual(package_library.layout_presets([], 6), [])


class LayoutTests(LayoutBase):
    """Сохранение раскладки на странице пакета."""

    def test_save_alternate_layout(self):
        layoutid = self.make_layout(self.holder, 'alternate')
        layouts = self.layouts()
        self.assertEqual(len(layouts), 1)
        self.assertEqual(layouts[0]['layoutid'], layoutid)
        self.assertEqual(layouts[0]['name'], 'Чёт/нечет')
        self.assertEqual(layouts[0]['preset'], 'alternate')
        self.assertEqual(layouts[0]['parts'],
                         [[0, 2, 4, 6, 8, 10], [1, 3, 5, 7, 9, 11]])

    def test_page_shows_layout_and_delete_removes_it(self):
        layoutid = self.make_layout(self.holder, 'halves', name='1/8, по раундам')
        page = self.holder.get(BASE + '/packages/%d' % self.libraryid)
        text = page.get_data(as_text=True)
        self.assertEqual(page.status_code, 200)
        self.assertIn('Раскладки на бои', text)
        self.assertIn('1/8, по раундам', text)
        response = self.holder.post(
            BASE + '/api/packages/library/%d/layouts/%d/delete' % (self.libraryid, layoutid),
            json={})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(self.layouts(), [])
        page = self.holder.get(BASE + '/packages/%d' % self.libraryid)
        self.assertNotIn('1/8, по раундам', page.get_data(as_text=True))

    def test_same_name_updates_layout(self):
        first = self.make_layout(self.holder, 'flat', name='Моя раскладка')
        second = self.make_layout(self.holder, 'alternate', name='Моя раскладка')
        self.assertEqual(first, second)
        layouts = self.layouts()
        self.assertEqual(len(layouts), 1)
        self.assertEqual(layouts[0]['preset'], 'alternate')

    def test_stranger_cannot_save_layout(self):
        response = self.other.post(
            BASE + '/api/packages/library/%d/layouts' % self.libraryid,
            json={'preset': 'flat'})
        self.assertEqual(response.status_code, 403, response.get_data(as_text=True))

    def test_unknown_preset_is_refused(self):
        response = self.holder.post(
            BASE + '/api/packages/library/%d/layouts' % self.libraryid,
            json={'preset': 'нет такого'})
        self.assertEqual(response.status_code, 400, response.get_data(as_text=True))

    def test_custom_parts_are_saved(self):
        response = self.holder.post(
            BASE + '/api/packages/library/%d/layouts' % self.libraryid,
            json={'name': 'Своя', 'parts': [[0, 1, 2], [3, 4, 5, 6, 7, 8, 9, 10, 11]]})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        layouts = self.layouts()
        self.assertEqual(layouts[0]['sizes'], [3, 9])

    def test_empty_parts_are_refused(self):
        response = self.holder.post(
            BASE + '/api/packages/library/%d/layouts' % self.libraryid,
            json={'name': 'Пустая', 'parts': [[], []]})
        self.assertEqual(response.status_code, 400, response.get_data(as_text=True))


class PlanAndConsoleTests(LayoutBase):
    """Раскладка в плане турнира и темы боя в пульте свободной посадки."""

    def test_plan_page_offers_layout_parts(self):
        self.make_layout(self.holder, 'halves', name='По раундам')
        self.take_to_room()
        page = self.holder.get(BASE + '/plan/%d/questions' % self.room)
        text = page.get_data(as_text=True)
        self.assertEqual(page.status_code, 200)
        self.assertIn('Раскладка', text)
        self.assertIn('По раундам', text)
        self.assertIn('бой 1', text)

    def test_console_takes_planned_themes(self):
        packageid = self.take_to_room()
        layoutid = self.make_layout(self.holder, 'halves')
        self.assign(packageid, 1, [0, 1, 4, 5, 8, 9], layoutid=layoutid, part=0)
        page = self.holder.get(BASE + '/room/%d' % self.room)
        text = page.get_data(as_text=True)
        self.assertEqual(page.status_code, 200)
        self.assertIn('planBattle', text)
        conn = db()
        payload = json.loads(conn.execute(
            "SELECT payload FROM game_questions WHERE roomid = ? AND gameindex = 1",
            (self.room,)).fetchone()["payload"])
        conn.close()
        self.assertEqual(payload["indexes"], [0, 1, 4, 5, 8, 9])
        self.assertEqual(payload["layoutid"], layoutid)
        self.assertEqual(payload["part"], 0)
        # Пульт получил ровно эти темы, в порядке раскладки.
        for number in (1, 2, 5, 6, 9, 10):
            self.assertIn("Тема %s %s" % (number, self.suffix), text)
        for number in (3, 4, 7, 8, 11, 12):
            self.assertNotIn("Тема %s %s" % (number, self.suffix), text)

    def test_console_without_assignment_has_no_plan_package(self):
        page = self.holder.get(BASE + '/room/%d' % self.room)
        text = page.get_data(as_text=True)
        self.assertEqual(page.status_code, 200)
        self.assertIn('const planBattle = null', text)

    def test_second_part_of_layout_is_assigned(self):
        packageid = self.take_to_room()
        layoutid = self.make_layout(self.holder, 'halves')
        self.assign(packageid, 1, [2, 3, 6, 7, 10, 11], layoutid=layoutid, part=1)
        conn = db()
        payload = json.loads(conn.execute(
            "SELECT payload FROM game_questions WHERE roomid = ? AND gameindex = 1",
            (self.room,)).fetchone()["payload"])
        conn.close()
        self.assertEqual(payload["part"], 1)
        self.assertEqual([theme["topic"] for theme in payload["themes"]],
                         ["Тема %s %s" % (number, self.suffix)
                          for number in (3, 4, 7, 8, 11, 12)])


if __name__ == '__main__':
    unittest.main()
