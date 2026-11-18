"""Библиотека пакетов: один пакет в разных комнатах, история и слышавшие."""

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

WORK_DIR = tempfile.mkdtemp(prefix='svoyak-library-')
WORK_DB = os.path.join(WORK_DIR, 'library.db')
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


def package_data(topics):
    return {"topics": [{"topic": topic, "questions": [
        {"value": 10, "text": "вопрос по теме %s" % topic, "answer": "ответ"}]}
        for topic in topics], "sourceFileName": "test.json"}


def add_package(conn, roomid, name, topics):
    cursor = conn.execute(
        "INSERT INTO packages (roomid, name, data) VALUES (?, ?, ?)",
        (roomid, name, json.dumps(package_data(topics), ensure_ascii=False)),
    )
    packageid = cursor.lastrowid
    package_library.ensure_library(conn)
    package_library.link_package(conn, packageid)
    return packageid


def add_room(conn, roomid, ownerid=None):
    conn.execute(
        "INSERT OR REPLACE INTO activerooms (roomid, date, venueid, finished, ownerid) "
        "VALUES (?, '2026-11-14', 0, 0, ?)", (roomid, ownerid))


def log_row(conn, roomid, moment, event, **data):
    conn.execute("INSERT INTO log (date, roomid, event, data) VALUES (?, ?, ?, ?)",
                 (moment, roomid, event, json.dumps(data, ensure_ascii=False)))


class LibraryGroupingTests(unittest.TestCase):
    """Одинаковый пакет в разных комнатах — одна запись библиотеки."""

    def setUp(self):
        self.conn = db()
        self.addCleanup(self.conn.close)
        self.conn.isolation_level = None
        self.conn.execute("BEGIN IMMEDIATE")
        self.addCleanup(self._rollback)

    def _rollback(self):
        try:
            self.conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def test_same_content_is_one_package(self):
        first = add_package(self.conn, 9101, "Пакет дня", ["Кино", "Музыка"])
        second = add_package(self.conn, 9102, "Пакет дня (копия)", ["Кино", "Музыка"])
        rows = self.conn.execute(
            "SELECT libraryid FROM packages WHERE packageid IN (?, ?)", (first, second)
        ).fetchall()
        self.assertEqual(rows[0][0], rows[1][0])
        copies = package_library.library_copies(self.conn, rows[0][0])
        self.assertEqual(len(copies), 2)

    def test_different_content_is_two_packages(self):
        first = add_package(self.conn, 9103, "Один", ["Кино"])
        second = add_package(self.conn, 9104, "Другой", ["Спорт", "Наука"])
        rows = self.conn.execute(
            "SELECT libraryid FROM packages WHERE packageid IN (?, ?)", (first, second)
        ).fetchall()
        self.assertNotEqual(rows[0][0], rows[1][0])

    def test_theme_usage_shows_both_rooms(self):
        first = add_package(self.conn, 9105, "Общий", ["Кино", "Музыка"])
        second = add_package(self.conn, 9106, "Общий", ["Кино", "Музыка"])
        data = self.conn.execute("SELECT data FROM packages WHERE packageid = ?",
                                 (first,)).fetchone()[0]
        for packageid, roomid, gameindex in ((first, 9105, 1), (second, 9106, 2)):
            self.conn.execute(
                "INSERT INTO game_questions (roomid, gameindex, packageid, payload) "
                "VALUES (?, ?, ?, ?)",
                (roomid, gameindex, packageid,
                 json.dumps({"themes": [{"topic": "Кино", "questions": [
                     {"text": "вопрос по теме Кино"}]}]}, ensure_ascii=False)))
        libraryid = self.conn.execute(
            "SELECT libraryid FROM packages WHERE packageid = ?", (first,)).fetchone()[0]
        places = package_library.theme_use_by_index(self.conn, data)
        # Темы идут в порядке пакета: Кино, Музыка.
        self.assertEqual(places[0], ["комната 9105, бой 1", "комната 9106, бой 2"])
        self.assertEqual(places[1], [])


class ListenerTests(unittest.TestCase):
    """«Слышал» = был в зале, пока шёл бой (режим «зал») или играл в нём."""

    def setUp(self):
        self.conn = db()
        self.addCleanup(self.conn.close)
        self.conn.isolation_level = None
        self.conn.execute("BEGIN IMMEDIATE")
        self.addCleanup(self._rollback)
        add_room(self.conn, 9200)
        log_row(self.conn, 9200, "Sat Nov 14 19:00:00 2026", "add active player",
                name="Вася", playerid=1)
        log_row(self.conn, 9200, "Sat Nov 14 20:30:00 2026", "save game result",
                gameindex=1, scores=[])
        log_row(self.conn, 9200, "Sat Nov 14 21:00:00 2026", "add active player",
                name="Петя", playerid=2)
        log_row(self.conn, 9200, "Sat Nov 14 22:00:00 2026", "save game result",
                gameindex=2, scores=[])
        for gameindex, name in ((1, "Аня"), (2, "Петя"), (2, "Аня")):
            self.conn.execute(
                "INSERT INTO results (roomid, gameindex, playerid, name, score) "
                "VALUES (?, ?, 0, ?, 0)", (9200, gameindex, name))
        self.timeline = package_library.room_timeline(self.conn, 9200)
        self.times = package_library.game_times(self.conn, 9200, self.timeline[2])

    def _rollback(self):
        try:
            self.conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def test_hall_mode_counts_those_who_were_present(self):
        first = package_library.game_listeners(
            self.conn, 9200, 1, package_library.EXPOSURE_HALL, self.times, self.timeline)
        # Петя внесён после конца первого боя: ведущий так и вносит опоздавших,
        # поэтому считаем, что Петя его слышал.
        self.assertEqual([item["name"] for item in first], ["Аня", "Вася", "Петя"])
        petya = [item for item in first if item["name"] == "Петя"][0]
        vasya = [item for item in first if item["name"] == "Вася"][0]
        self.assertTrue(petya["late"])
        self.assertFalse(vasya["late"])
        second = package_library.game_listeners(
            self.conn, 9200, 2, package_library.EXPOSURE_HALL, self.times, self.timeline)
        self.assertEqual([item["name"] for item in second], ["Аня", "Вася", "Петя"])

    def test_effective_arrival_shifts_to_previous_game_end(self):
        ends = {1: (2026, 11, 14, 20, 30, 0)}
        self.assertEqual(
            package_library.effective_arrival((2026, 11, 14, 20, 35, 0), ends),
            (2026, 11, 14, 20, 30, 0))
        # Наутро отметка к вчерашнему бою уже не относится.
        self.assertEqual(
            package_library.effective_arrival((2026, 11, 15, 11, 0, 0), ends),
            (2026, 11, 15, 11, 0, 0))
        # До первого боя сдвигать некуда.
        self.assertEqual(
            package_library.effective_arrival((2026, 11, 14, 18, 0, 0), ends),
            (2026, 11, 14, 18, 0, 0))

    def test_game_mode_counts_only_players(self):
        first = package_library.game_listeners(
            self.conn, 9200, 1, package_library.EXPOSURE_GAME, self.times, self.timeline)
        self.assertEqual([item["name"] for item in first], ["Аня"])

    def test_time_parsing(self):
        self.assertEqual(package_library.parse_time("Sat Nov 14 19:00:00 2026"),
                         (2026, 11, 14, 19, 0, 0))
        self.assertEqual(package_library.parse_time("2026-11-14 19:00:00"),
                         (2026, 11, 14, 19, 0, 0))
        self.assertIsNone(package_library.parse_time("не дата"))


class SimilarTests(unittest.TestCase):
    """Похожие пакеты подтверждает администратор."""

    def setUp(self):
        self.conn = db()
        self.addCleanup(self.conn.close)
        self.conn.isolation_level = None
        self.conn.execute("BEGIN IMMEDIATE")
        self.addCleanup(self._rollback)

    def _rollback(self):
        try:
            self.conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def _pair(self, topics_one, topics_two):
        first = add_package(self.conn, 9301, "Старый", topics_one)
        second = add_package(self.conn, 9302, "Новый", topics_two)
        one = self.conn.execute("SELECT libraryid FROM packages WHERE packageid = ?",
                                (first,)).fetchone()[0]
        two = self.conn.execute("SELECT libraryid FROM packages WHERE packageid = ?",
                                (second,)).fetchone()[0]
        return one, two

    def test_partial_overlap_is_suggested(self):
        one, two = self._pair(["Кино", "Музыка", "Спорт"], ["Кино", "Музыка", "Наука"])
        similar = package_library.similar_libraries(self.conn, one)
        self.assertTrue(similar)
        self.assertEqual(similar[0]["libraryid"], two)
        self.assertFalse(similar[0]["same_content"])

    def test_full_match_is_same_content(self):
        one, two = self._pair(["Кино", "Музыка"], ["Кино", "Музыка"])
        # Одинаковое содержимое уже связалось автоматически.
        self.assertEqual(one, two)

    def test_reject_hides_suggestion(self):
        one, two = self._pair(["Кино", "Музыка", "Спорт"], ["Кино", "Музыка", "Наука"])
        self.assertTrue(package_library.similar_libraries(self.conn, one))
        package_library.reject_link(self.conn, one, two, "админ")
        self.assertFalse(package_library.similar_libraries(self.conn, one))

    def test_merge_moves_copies(self):
        one, two = self._pair(["Кино", "Музыка", "Спорт"], ["Кино", "Музыка", "Наука"])
        moved = package_library.merge_libraries(self.conn, one, two, "админ")
        self.assertEqual(moved, 1)
        copies = package_library.library_copies(self.conn, one)
        self.assertEqual(len(copies), 2)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM package_library WHERE libraryid = ?", (two,)).fetchone())


class LibraryPageTests(unittest.TestCase):
    """Страницы библиотеки: кто их видит и что там есть."""

    def setUp(self):
        site.DB_PATH = WORK_DB
        self.owner = self.registered()
        self.other = self.registered()
        self.admin = self.registered(role="администратор")
        conn = db()
        add_room(conn, 9401, self.owner.userid)
        add_room(conn, 9402, self.other.userid)
        self.owner_package = add_package(conn, 9401, "Пакет владельца", ["Кино", "Музыка"])
        self.other_package = add_package(conn, 9402, "Чужой пакет", ["Спорт"])
        self.owner_library = conn.execute(
            "SELECT libraryid FROM packages WHERE packageid = ?",
            (self.owner_package,)).fetchone()[0]
        self.other_library = conn.execute(
            "SELECT libraryid FROM packages WHERE packageid = ?",
            (self.other_package,)).fetchone()[0]
        conn.commit()
        conn.close()

    def registered(self, role="ведущий"):
        client = site.app.test_client()
        login = 'lib_' + uuid.uuid4().hex[:8]
        response = client.post(BASE + '/register', data={
            'login': login, 'name': 'Библ ' + login,
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

    def test_guest_is_redirected(self):
        response = site.app.test_client().get(BASE + '/packages')
        self.assertEqual(response.status_code, 302)

    def test_owner_sees_own_package_only(self):
        page = self.owner.get(BASE + '/packages')
        self.assertEqual(page.status_code, 200, page.get_data(as_text=True))
        text = page.get_data(as_text=True)
        self.assertIn('Пакет владельца', text)
        self.assertNotIn('Чужой пакет', text)

    def test_other_user_cannot_open_foreign_package(self):
        response = self.other.get(BASE + '/packages/%d' % self.owner_library)
        self.assertEqual(response.status_code, 403)

    def test_admin_sees_everything(self):
        page = self.admin.get(BASE + '/packages')
        text = page.get_data(as_text=True)
        self.assertIn('Пакет владельца', text)
        self.assertIn('Чужой пакет', text)

    def test_detail_shows_themes_and_copy_rooms(self):
        page = self.owner.get(BASE + '/packages/%d' % self.owner_library)
        self.assertEqual(page.status_code, 200, page.get_data(as_text=True))
        text = page.get_data(as_text=True)
        self.assertIn('Кино', text)
        self.assertIn('9401', text)
        self.assertIn('весь зал', text)

    def test_take_copies_package_into_room(self):
        response = self.owner.post(
            BASE + '/api/packages/library/%d/take' % self.owner_library,
            json={'roomid': 9401})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = json.loads(response.get_data(as_text=True))
        conn = db()
        row = conn.execute("SELECT roomid, libraryid FROM packages WHERE packageid = ?",
                           (data['packageid'],)).fetchone()
        conn.close()
        self.assertEqual(row[0], 9401)
        self.assertEqual(row[1], self.owner_library)

    def test_take_into_foreign_room_is_forbidden(self):
        response = self.owner.post(
            BASE + '/api/packages/library/%d/take' % self.owner_library,
            json={'roomid': 9402})
        self.assertEqual(response.status_code, 403)

    def test_similar_block_is_admin_only(self):
        # Второй пакет владельца с общими темами — кандидат на связывание.
        conn = db()
        add_package(conn, 9401, "Пакет владельца, черновик", ["Кино", "Наука"])
        conn.commit()
        conn.close()
        owner_page = self.owner.get(BASE + '/packages/%d' % self.owner_library
                                    ).get_data(as_text=True)
        self.assertNotIn('Похожие пакеты', owner_page)
        admin_page = self.admin.get(BASE + '/packages/%d' % self.owner_library
                                    ).get_data(as_text=True)
        self.assertIn('Похожие пакеты', admin_page)
        self.assertIn('Пакет владельца, черновик', admin_page)

    def test_merge_and_reject_need_admin(self):
        response = self.owner.post(
            BASE + '/api/packages/library/%d/merge' % self.owner_library,
            json={'other': self.other_library})
        self.assertEqual(response.status_code, 403)
        response = self.admin.post(
            BASE + '/api/packages/library/%d/reject' % self.owner_library,
            json={'other': self.other_library})
        self.assertEqual(response.status_code, 200)


class ThemeIdentityTests(unittest.TestCase):
    """Тема — это её вопросы. Названия могут совпадать, вопросы — нет."""

    def setUp(self):
        self.conn = db()
        self.addCleanup(self.conn.close)
        self.conn.isolation_level = None
        self.conn.execute("BEGIN IMMEDIATE")
        self.addCleanup(self._rollback)

    def _rollback(self):
        try:
            self.conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def _package(self, themes):
        cursor = self.conn.execute(
            "INSERT INTO packages (roomid, name, data) VALUES (?, ?, ?)",
            (9600, "Пакет тем", json.dumps({"topics": themes}, ensure_ascii=False)))
        packageid = cursor.lastrowid
        package_library.ensure_library(self.conn)
        package_library.link_package(self.conn, packageid)
        data = self.conn.execute("SELECT data FROM packages WHERE packageid = ?",
                                 (packageid,)).fetchone()[0]
        libraryid = self.conn.execute(
            "SELECT libraryid FROM packages WHERE packageid = ?",
            (packageid,)).fetchone()[0]
        return packageid, libraryid, data

    def _theme(self, title, texts):
        return {"topic": title, "questions": [{"value": 10 * (i + 1), "text": text}
                                              for i, text in enumerate(texts)]}

    def test_same_title_different_questions_are_different_themes(self):
        first = self._theme("Кино", ["вопрос про Чаплина", "вопрос про Тарантино"])
        second = self._theme("Кино", ["вопрос про Куросаву", "вопрос про Феллини"])
        packageid, libraryid, data = self._package([first, second])
        # Игралась только вторая тема (с Куросавой).
        self.conn.execute(
            "INSERT INTO game_questions (roomid, gameindex, packageid, payload) VALUES (?, ?, ?, ?)",
            (9600, 1, packageid, json.dumps({"themes": [second]}, ensure_ascii=False)))
        places = package_library.theme_use_by_index(self.conn, data)
        self.assertEqual(places[0], [])
        self.assertEqual(places[1], ["комната 9600, бой 1"])

        history = package_library.theme_history(self.conn, libraryid)
        self.assertEqual(len(history), 2)
        self.assertTrue(all(item["same_title"] for item in history))
        self.assertEqual(history[0]["used"], [])
        self.assertEqual(len(history[1]["used"]), 1)

    def test_same_questions_under_another_title_are_one_theme(self):
        first = self._theme("Космос", ["вопрос про Гагарина"])
        second = self._theme("Космос-2", ["вопрос про Гагарина"])
        packageid, libraryid, data = self._package([first, second])
        # В другой комнате та же тема называется иначе — это та же тема.
        cursor = self.conn.execute(
            "INSERT INTO packages (roomid, name, data) VALUES (?, ?, ?)",
            (9601, "Пакет тем", json.dumps({"topics": [self._theme("Поехали", [
                "вопрос про Гагарина"])]}, ensure_ascii=False)))
        other = cursor.lastrowid
        package_library.link_package(self.conn, other)
        self.conn.execute(
            "INSERT INTO game_questions (roomid, gameindex, packageid, payload) VALUES (?, ?, ?, ?)",
            (9601, 1, other, json.dumps({"themes": [self._theme("Поехали", [
                "вопрос про Гагарина"])]}, ensure_ascii=False)))
        places = package_library.theme_use_by_index(self.conn, data)
        self.assertEqual(places[0], ["комната 9601, бой 1"])
        self.assertEqual(places[1], ["комната 9601, бой 1"])

    def test_signature_ignores_order_and_case(self):
        one = package_library.theme_signature(self._theme("Кино", ["Вопрос А", "Вопрос Б"]))
        two = package_library.theme_signature(self._theme("Другое", ["вопрос б", "вопрос а"]))
        three = package_library.theme_signature(self._theme("Кино", ["вопрос а", "вопрос в"]))
        self.assertEqual(one, two)
        self.assertNotEqual(one, three)


class QuestionsPageTests(unittest.TestCase):
    """Раскладка тем: сыгранная тема помечена, но назначить её можно."""

    def setUp(self):
        site.DB_PATH = WORK_DB
        self.admin = self.registered('администратор')

    def registered(self, role):
        client = site.app.test_client()
        login = 'qw_' + uuid.uuid4().hex[:8]
        response = client.post(BASE + '/register', data={
            'login': login, 'name': 'Темы ' + login,
            'password': 'secret123', 'password2': 'secret123'})
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        conn = db()
        userid = conn.execute("SELECT userid FROM users WHERE login = ?",
                              (login,)).fetchone()[0]
        conn.execute("UPDATE users SET role = ? WHERE userid = ?", (role, userid))
        conn.execute("UPDATE activerooms SET ownerid = ? WHERE roomid = 2024", (userid,))
        conn.commit()
        conn.close()
        client.userid = userid
        return client

    def test_played_theme_is_marked_in_layout(self):
        # Комната 2024 в тестовой базе — турнир по правилам со схемой боёв.
        conn = db()
        ownerid = conn.execute("SELECT ownerid FROM activerooms WHERE roomid = 2024"
                               ).fetchone()[0]
        packageid = add_package(conn, 2024, "Пакет раскладки", ["Кино", "Музыка"])
        # Тема опознаётся по вопросам: берём ровно ту тему, что лежит в пакете.
        data = json.loads(conn.execute("SELECT data FROM packages WHERE packageid = ?",
                                       (packageid,)).fetchone()[0])
        played = data["topics"][0]
        self.assertEqual(played["topic"], "Кино")
        add_room(conn, 9500, ownerid)
        conn.execute(
            "INSERT INTO game_questions (roomid, gameindex, packageid, payload) "
            "VALUES (?, ?, ?, ?)",
            (9500, 3, packageid, json.dumps({"themes": [played]}, ensure_ascii=False)))
        conn.commit()
        conn.close()

        page = self.admin.get(BASE + '/plan/2024/questions')
        self.assertEqual(page.status_code, 200, page.get_data(as_text=True))
        text = page.get_data(as_text=True)
        self.assertIn('THEME_USE', text)
        line = [row for row in text.splitlines() if 'const THEME_USE' in row][0]
        # Пометка о сыгранной теме: место, где она уже игралась. У «Музыки» пусто.
        self.assertIn('9500', line)
        self.assertTrue(line.rstrip().endswith('[]]};'), line)

    def test_free_seating_room_gets_layout_page(self):
        # Комната 2030 — свободная посадка: сетки нет, бои идут по порядку.
        conn = db()
        free = conn.execute(
            "SELECT roomid FROM activerooms ORDER BY roomid").fetchall()
        conn.close()
        roomid = None
        for row in free:
            probe = db()
            plan = None
            try:
                plan = site.build_room_plan(probe, row[0])
            except Exception:
                plan = None
            probe.close()
            if plan is None:
                roomid = row[0]
                break
        self.assertIsNotNone(roomid, "в тестовой базе нет комнаты со свободной посадкой")

        page = self.admin.get(BASE + '/plan/%s/questions' % roomid)
        self.assertEqual(page.status_code, 200, page.get_data(as_text=True))
        text = page.get_data(as_text=True)
        self.assertIn('Свободная посадка', text)
        self.assertIn('Бой 1', text)


if __name__ == '__main__':
    unittest.main()
