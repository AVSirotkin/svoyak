# -*- coding: utf-8 -*-
"""Повтор сыгранного боя в свободной посадке.

Вечером игроков сажают перед боем, и удачный бой иногда хочется сыграть ещё
раз — другими людьми. Пульт подсказывает, какие бои посаженные не слышали, и
умеет поставить такой бой в текущий, сдвинув запланированные на бой вперёд.
"""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
import unittest.mock
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

WORK_DIR = tempfile.mkdtemp(prefix='svoyak-replay-')
WORK_DB = os.path.join(WORK_DIR, 'replay.db')
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


def ek_topics(suffix=''):
    """Двенадцать тем: три раунда по четыре, как бой ЭК."""
    topics = []
    names = ('Первый', 'Второй', 'Третий')
    for round_index, stage in enumerate(names):
        for position in range(4):
            number = round_index * 4 + position + 1
            topics.append({
                "topic": "Тема %s %s" % (number, suffix),
                "stage": "Бой 1/8 финала. %s раунд" % stage,
                "questions": [{"value": 10,
                               "text": "Вопрос %s %s" % (number, suffix),
                               "answer": "ответ"}],
            })
    return topics


class ReplayBase(unittest.TestCase):
    """Комната со свободной посадкой и пакет на двенадцать тем."""

    def setUp(self):
        package_library.reset_caches()
        site.DB_PATH = WORK_DB
        self.suffix = uuid.uuid4().hex[:6]
        self.holder = self.registered('Правообладатель')
        self.other = self.registered('Посторонний')
        self.room = self.new_room(self.holder.userid)
        self.topics = ek_topics(self.suffix)
        self.libraryid = self.upload()
        self.packageid = self.take_to_room()
        self.package_name = "Пакет ЭК " + self.suffix

    def registered(self, name, role='ведущий'):
        client = site.app.test_client()
        login = 'rp_' + uuid.uuid4().hex[:8]
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
        roomid = 9800 + int(uuid.uuid4().hex[:3], 16)
        conn = db()
        conn.execute(
            "INSERT OR REPLACE INTO activerooms (roomid, date, venueid, finished, ownerid) "
            "VALUES (?, '2026-11-20', 0, 0, ?)", (roomid, ownerid))
        conn.commit()
        conn.close()
        return roomid

    def upload(self):
        payload = {
            "data": json.dumps({"topics": self.topics, "sourceFileName": "ek.json"},
                               ensure_ascii=False),
            "name": "Пакет ЭК " + self.suffix,
        }
        response = self.holder.post(BASE + '/api/packages/library', json=payload)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return json.loads(response.get_data(as_text=True))['libraryid']

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

    def assign(self, gameindex, themes, layoutid=None, part=None):
        response = self.holder.post(
            BASE + '/api/plan/%d/questions' % self.room,
            json={'gameindex': gameindex, 'packageid': self.packageid,
                  'themes': themes, 'layoutid': layoutid, 'part': part,
                  'force': True, 'ack_heard': True})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return response

    def play(self, gameindex, names, themes):
        """Сыгранный бой: раскладка в плане и результаты."""
        conn = db()
        try:
            payload = {"themes": [self.topics[index] for index in themes],
                       "package": self.package_name}
            conn.execute(
                "INSERT OR REPLACE INTO game_questions (roomid, gameindex, packageid, payload, "
                "updated_at) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
                (self.room, gameindex, self.packageid,
                 json.dumps(payload, ensure_ascii=False)))
            for position, name in enumerate(names, 1):
                conn.execute(
                    "INSERT INTO results (roomid, gameindex, playerid, name, score, position, "
                    "points, gamenumber) VALUES (?, ?, NULL, ?, ?, ?, ?, ?)",
                    (self.room, gameindex, name, 10 * position, float(position),
                     2.0, gameindex))
            conn.commit()
        finally:
            conn.close()

    def replays(self, names):
        query = '?players=' + ','.join(names) if names else ''
        response = self.holder.get(
            BASE + '/api/plan/%d/replays%s' % (self.room, query))
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return json.loads(response.get_data(as_text=True))

    def insert(self, source, client=None):
        return (client or self.holder).post(
            BASE + '/api/plan/%d/replay' % self.room, json={'source': source})

    def stored(self, gameindex):
        conn = db()
        try:
            row = conn.execute(
                "SELECT packageid, payload FROM game_questions WHERE roomid = ? AND gameindex = ?",
                (self.room, gameindex)).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return {"packageid": row["packageid"], "payload": json.loads(row["payload"])}

    def stored_themes(self, gameindex):
        item = self.stored(gameindex)
        return [theme["topic"] for theme in item["payload"]["themes"]] if item else None


class ReplayCandidateTests(ReplayBase):
    """Подсказка: какие сыгранные бои можно повторить этими игроками."""

    def setUp(self):
        super().setUp()
        self.play(1, ['Аня', 'Боря', 'Вова', 'Гоша'], [0, 1, 2, 3, 4, 5])
        self.play(2, ['Даша', 'Егор', 'Женя', 'Зина'], [6, 7, 8, 9, 10, 11])

    def test_seated_player_who_played_does_not_get_his_battle(self):
        data = self.replays(['Аня'])
        self.assertTrue(data['free_seating'])
        self.assertEqual([item['gameindex'] for item in data['replays']], [2])

    def test_seated_player_gets_battle_he_did_not_hear(self):
        data = self.replays(['Даша'])
        self.assertEqual([item['gameindex'] for item in data['replays']], [1])

    def test_hint_tells_the_package_and_theme_count(self):
        data = self.replays(['Даша'])
        item = data['replays'][0]
        self.assertEqual(item['package'], self.package_name)
        self.assertEqual(item['themes'], 6)
        self.assertEqual(item['players'], ['Аня', 'Боря', 'Вова', 'Гоша'])
        self.assertEqual(item['titles'][0], "Тема 1 " + self.suffix)

    def test_mixed_seating_has_nothing_to_repeat(self):
        data = self.replays(['Аня', 'Даша'])
        self.assertEqual(data['replays'], [])

    def test_no_players_no_hints(self):
        self.assertEqual(self.replays([])['replays'], [])

    def test_next_battle_is_target(self):
        self.assertEqual(self.replays(['Аня'])['gameindex'], 3)

    def test_bracket_room_has_no_replays(self):
        with unittest.mock.patch.object(site, 'build_room_plan', return_value=object()):
            data = self.replays(['Аня'])
        self.assertFalse(data['free_seating'])
        self.assertEqual(data['replays'], [])

    def test_console_asks_about_the_seated_players(self):
        """Совет считается по посаженным в бой, а не по всей комнате.

        Иначе подсказка пропадает: в комнате всегда кто-то слышал любой бой.
        """
        text = self.holder.get(BASE + '/room/%d' % self.room).get_data(as_text=True)
        self.assertIn('// Считаем по тем, кого посадили в этот бой', text)
        body = text.split('async function refreshReplays()', 1)[1].split('\n}', 1)[0]
        self.assertIn('gameData.players', body)
        self.assertNotIn('(active || [])', body)

    def test_seating_refreshes_the_hint(self):
        text = self.holder.get(BASE + '/room/%d' % self.room).get_data(as_text=True)
        body = text.split('function scheduleBattleHeard()', 1)[1].split('\n}', 1)[0]
        self.assertIn('scheduleReplaysRefresh();', body)


class ReplayInsertTests(ReplayBase):
    """Постановка повтора в текущий бой и сдвиг плана."""

    def setUp(self):
        super().setUp()
        self.play(1, ['Аня', 'Боря', 'Вова', 'Гоша'], [0, 1, 2, 3, 4, 5])
        # Впереди запланированы бой 2 и бой 3 — их и должен сдвинуть повтор.
        self.assign(2, [6, 7, 8, 9, 10, 11])
        self.assign(3, [6, 7, 8, 9, 10, 11])

    def test_insert_puts_source_themes_into_current_battle(self):
        response = self.insert(1)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = json.loads(response.get_data(as_text=True))
        self.assertEqual(data['gameindex'], 2)
        self.assertEqual(data['source'], 1)
        self.assertEqual([theme['topic'] for theme in data['themes']],
                         self.stored_themes(1))
        self.assertEqual(self.stored_themes(2), self.stored_themes(1))

    def test_planned_battles_shift_one_forward(self):
        self.assertEqual(self.stored_themes(2),
                         ["Тема %s %s" % (number, self.suffix) for number in (7, 8, 9, 10, 11, 12)])
        self.insert(1)
        self.assertEqual(self.stored_themes(2),
                         ["Тема %s %s" % (number, self.suffix) for number in (1, 2, 3, 4, 5, 6)])
        # Бывший бой 2 стал боем 3, бывший бой 3 — боем 4.
        self.assertEqual(self.stored_themes(3),
                         ["Тема %s %s" % (number, self.suffix) for number in (7, 8, 9, 10, 11, 12)])
        self.assertEqual(self.stored_themes(4), self.stored_themes(3))
        self.assertIsNone(self.stored(5))

    def test_replay_remembers_its_source(self):
        self.insert(1)
        self.assertEqual(self.stored(2)["payload"]["replay_of"], 1)
        self.assertEqual(self.stored(2)["payload"]["package"], self.package_name)

    def test_console_receives_replay_themes(self):
        self.insert(1)
        page = self.holder.get(BASE + '/room/%d' % self.room)
        text = page.get_data(as_text=True)
        self.assertEqual(page.status_code, 200)
        start = text.find('let planBattle = ')
        line = text[start:text.find('\n', start)]
        plan = json.loads(line[len('let planBattle = '):].rstrip(';'))
        self.assertEqual([theme['topic'] for theme in plan['topics']],
                         self.stored_themes(1))
        self.assertTrue(plan['name'].startswith('Повтор боя 1'))
        self.assertEqual(plan['replay_of'], 1)

    def test_plan_page_shows_shifted_battles(self):
        self.insert(1)
        page = self.holder.get(BASE + '/plan/%d/questions' % self.room)
        text = page.get_data(as_text=True)
        self.assertEqual(page.status_code, 200)
        # Бой 4 — это бывший бой 3: он появился в плане после сдвига.
        self.assertIn('"4"', text)
        self.assertIn('Повтор боя 1', text)

    def test_not_played_source_is_refused(self):
        response = self.insert(2)
        self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
        message = json.loads(response.get_data(as_text=True))['message']
        self.assertIn('не сыгран', message)
        # План не тронут.
        self.assertEqual(self.stored_themes(2),
                         ["Тема %s %s" % (number, self.suffix) for number in (7, 8, 9, 10, 11, 12)])

    def test_started_battle_blocks_insert(self):
        conn = db()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO game_protocols (roomid, gameindex, payload, revision) "
                "VALUES (?, ?, ?, 1)",
                (self.room, 2, json.dumps({"players": [{"name": "Аня"}],
                                           "marks": {"0:1": "plus"}},
                                          ensure_ascii=False)))
            conn.commit()
        finally:
            conn.close()
        response = self.insert(1)
        self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
        message = json.loads(response.get_data(as_text=True))['message']
        self.assertIn('уже идёт', message)
        self.assertIn('отметки', message)

    def test_seated_players_do_not_block_insert(self):
        """Посаженные игроки — не прогресс: темы такого боя меняют перед игрой."""
        conn = db()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO game_protocols (roomid, gameindex, payload, revision) "
                "VALUES (?, ?, ?, 1)",
                (self.room, 2, json.dumps({"players": [{"name": "Аня"}, {"name": "Боря"}],
                                           "marks": {}, "scores": {}},
                                          ensure_ascii=False)))
            conn.commit()
        finally:
            conn.close()
        response = self.insert(1)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(self.stored(2)['payload']['replay_of'], 1)

    def test_scores_block_insert(self):
        conn = db()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO game_protocols (roomid, gameindex, payload, revision) "
                "VALUES (?, ?, ?, 1)",
                (self.room, 2, json.dumps({"players": [{"name": "Аня"}],
                                           "scores": {"Аня": 10}},
                                          ensure_ascii=False)))
            conn.commit()
        finally:
            conn.close()
        response = self.insert(1)
        self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
        self.assertIn('счёт', json.loads(response.get_data(as_text=True))['message'])

    def test_empty_draft_does_not_block_insert(self):
        conn = db()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO game_protocols (roomid, gameindex, payload, revision) "
                "VALUES (?, ?, ?, 0)",
                (self.room, 5, json.dumps({"players": [], "marks": {}, "scores": {},
                                           "finished": False}, ensure_ascii=False)))
            conn.commit()
        finally:
            conn.close()
        response = self.insert(1)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        conn = db()
        try:
            left = conn.execute(
                "SELECT COUNT(*) FROM game_protocols WHERE roomid = ? AND gameindex >= 5",
                (self.room,)).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(left, 0)

    def test_stranger_cannot_insert(self):
        response = self.insert(1, client=self.other)
        self.assertEqual(response.status_code, 403, response.get_data(as_text=True))

    def test_bracket_room_is_refused(self):
        with unittest.mock.patch.object(site, 'build_room_plan', return_value=object()):
            response = self.insert(1)
        self.assertEqual(response.status_code, 409, response.get_data(as_text=True))

    def test_bad_source_is_refused(self):
        response = self.holder.post(
            BASE + '/api/plan/%d/replay' % self.room, json={'source': 'нет'})
        self.assertEqual(response.status_code, 400, response.get_data(as_text=True))


class PlayedBattleTests(ReplayBase):
    """Сыгранный бой: темы из протокола, план его не переписывает."""

    def setUp(self):
        super().setUp()
        self.assign(1, [6, 7, 8, 9, 10, 11])
        self.assign(2, [0, 1, 2, 3, 4, 5])

    def save_protocol(self, gameindex, indexes, marks=None, players=('Аня', 'Боря')):
        conn = db()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO game_protocols (roomid, gameindex, payload, revision) "
                "VALUES (?, ?, ?, 1)",
                (self.room, gameindex, json.dumps({
                    "themes": [self.topics[index] for index in indexes],
                    "players": [{"name": name, "place": position}
                                for position, name in enumerate(players, 1)],
                    "marks": marks or {},
                    "scores": {},
                    "finished": False,
                }, ensure_ascii=False)))
            conn.commit()
        finally:
            conn.close()

    def write_plan(self, gameindex, indexes):
        """План перепривязали в обход страницы — так и вышло в комнате 483."""
        conn = db()
        try:
            conn.execute(
                "UPDATE game_questions SET payload = ? WHERE roomid = ? AND gameindex = ?",
                (json.dumps({"themes": [self.topics[index] for index in indexes],
                             "package": self.package_name}, ensure_ascii=False),
                 self.room, gameindex))
            conn.commit()
        finally:
            conn.close()

    def card(self, text, gameindex):
        start = text.find('id="game-%d"' % gameindex)
        rest = text[start:]
        end = rest.find('id="game-%d"' % (gameindex + 1))
        return rest if end < 0 else rest[:end]

    def test_heard_does_not_count_the_battle_itself(self):
        self.save_protocol(2, [0, 1, 2, 3, 4, 5])
        self.play(2, ['Аня', 'Боря'], [0, 1, 2, 3, 4, 5])
        themes = ','.join(str(index) for index in (0, 1, 2, 3, 4, 5))
        base = BASE + '/api/plan/%d/heard?packageid=%d&themes=%s&players=Аня'
        own = json.loads(self.holder.get(
            (base + '&gameindex=2') % (self.room, self.packageid, themes)
        ).get_data(as_text=True))
        self.assertEqual(own['players'], {})
        # Без номера боя Аня по-прежнему слышала этот бой: подсказка о повторе
        # и пометки работают как раньше.
        anywhere = json.loads(self.holder.get(
            base % (self.room, self.packageid, themes)
        ).get_data(as_text=True))
        self.assertIn('Аня', anywhere['players'])

    def test_played_battle_is_not_in_progress(self):
        self.save_protocol(2, [0, 1, 2, 3, 4, 5], marks={"0:1": "plus"})
        self.play(2, ['Аня', 'Боря'], [0, 1, 2, 3, 4, 5])
        text = self.holder.get(
            BASE + '/plan/%d/questions' % self.room).get_data(as_text=True)
        self.assertIn('сыгран', self.card(text, 2))
        self.assertNotIn('идёт бой', self.card(text, 2))

    def test_battle_with_marks_is_in_progress(self):
        self.save_protocol(2, [0, 1, 2, 3, 4, 5], marks={"0:1": "plus"})
        text = self.holder.get(
            BASE + '/plan/%d/questions' % self.room).get_data(as_text=True)
        self.assertIn('идёт бой', self.card(text, 2))
        # Отметки — не повод считать бой сыгранным.
        self.assertNotIn('сыгран', self.card(text, 2))

    def test_seating_alone_is_not_in_progress(self):
        # Посаженные игроки — ещё не начатый бой.
        self.save_protocol(2, [0, 1, 2, 3, 4, 5])
        text = self.holder.get(
            BASE + '/plan/%d/questions' % self.room).get_data(as_text=True)
        self.assertNotIn('идёт бой', self.card(text, 2))

    def test_played_battle_shows_protocol_themes(self):
        self.save_protocol(2, [0, 1, 2, 3, 4, 5])
        self.play(2, ['Аня', 'Боря'], [0, 1, 2, 3, 4, 5])
        self.write_plan(2, [6, 7, 8, 9, 10, 11])
        text = self.holder.get(
            BASE + '/plan/%d/questions' % self.room).get_data(as_text=True)
        card = self.card(text, 2)
        self.assertIn("Тема 1 %s" % self.suffix, card)
        self.assertNotIn("Тема 7 %s" % self.suffix, card)
        # Формы для правки нет: темы уже прочитаны.
        self.assertNotIn('назначить темы', card)
        self.assertNotIn('изменить задним числом', card)

    def test_played_battle_refuses_new_themes(self):
        self.play(2, ['Аня', 'Боря'], [0, 1, 2, 3, 4, 5])
        response = self.holder.post(
            BASE + '/api/plan/%d/questions' % self.room,
            json={'gameindex': 2, 'packageid': self.packageid,
                  'themes': [6, 7, 8, 9, 10, 11],
                  'force': True, 'ack_heard': True})
        self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
        data = json.loads(response.get_data(as_text=True))
        self.assertEqual(data['status'], 'played')
        self.assertIn('уже сыгран', data['message'])
        # План не тронут: в нём остались темы, которые играли.
        self.assertEqual(self.stored_themes(2),
                         ["Тема %s %s" % (number, self.suffix)
                          for number in (1, 2, 3, 4, 5, 6)])

    def test_played_battle_refuses_clearing(self):
        self.play(2, ['Аня', 'Боря'], [0, 1, 2, 3, 4, 5])
        response = self.holder.post(
            BASE + '/api/plan/%d/questions' % self.room,
            json={'gameindex': 2, 'themes': [], 'force': True, 'ack_heard': True})
        self.assertEqual(response.status_code, 409, response.get_data(as_text=True))
        self.assertIsNotNone(self.stored(2))

    def test_same_themes_on_played_battle_are_allowed(self):
        self.play(2, ['Аня', 'Боря'], [0, 1, 2, 3, 4, 5])
        response = self.holder.post(
            BASE + '/api/plan/%d/questions' % self.room,
            json={'gameindex': 2, 'packageid': self.packageid,
                  'themes': [0, 1, 2, 3, 4, 5],
                  'force': True, 'ack_heard': True})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

    def test_admin_may_backfill_played_battle(self):
        self.play(2, ['Аня', 'Боря'], [0, 1, 2, 3, 4, 5])
        conn = db()
        try:
            conn.execute("UPDATE users SET role = 'администратор' WHERE userid = ?",
                         (self.holder.userid,))
            conn.commit()
        finally:
            conn.close()
        response = self.holder.post(
            BASE + '/api/plan/%d/questions' % self.room,
            json={'gameindex': 2, 'packageid': self.packageid,
                  'themes': [6, 7, 8, 9, 10, 11],
                  'force': True, 'ack_heard': True, 'backfill': True})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(self.stored_themes(2),
                         ["Тема %s %s" % (number, self.suffix)
                          for number in (7, 8, 9, 10, 11, 12)])

    def test_protocol_keeps_themes_of_played_battle(self):
        self.save_protocol(2, [0, 1, 2, 3, 4, 5])
        self.play(2, ['Аня', 'Боря'], [0, 1, 2, 3, 4, 5])
        response = self.holder.post(
            BASE + '/api/plan/%d/game/2/protocol' % self.room,
            json={'themes': [self.topics[index] for index in (6, 7, 8, 9, 10, 11)],
                  'marks': {'0:1': 'plus'}, 'players': [{'name': 'Аня', 'place': 1}]})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        conn = db()
        try:
            payload = json.loads(conn.execute(
                "SELECT payload FROM game_protocols WHERE roomid = ? AND gameindex = 2",
                (self.room,)).fetchone()[0])
            events = [row[0] for row in conn.execute(
                "SELECT event FROM log WHERE roomid = ?", (self.room,))]
        finally:
            conn.close()
        self.assertEqual([theme['topic'] for theme in payload['themes']],
                         ["Тема %s %s" % (number, self.suffix)
                          for number in (1, 2, 3, 4, 5, 6)])
        # Отметка при этом сохранилась.
        self.assertEqual(payload['marks'], {'0:1': 'plus'})
        self.assertIn('protocol themes kept', events)

    def test_played_battle_themes_feed_replays(self):
        # Повтор берёт темы протокола, а не подменённого плана.
        self.save_protocol(1, [0, 1, 2, 3, 4, 5])
        self.play(1, ['Аня', 'Боря'], [0, 1, 2, 3, 4, 5])
        self.write_plan(1, [6, 7, 8, 9, 10, 11])
        data = self.replays(['Вова', 'Гоша'])
        self.assertEqual([item['gameindex'] for item in data['replays']], [1])
        self.assertIn("Тема 1 %s" % self.suffix, data['replays'][0]['titles'])


if __name__ == '__main__':
    unittest.main()
