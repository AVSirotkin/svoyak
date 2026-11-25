"""Библиотека пакетов: одна и та же колода в разных комнатах — один пакет.

Пакет по-прежнему хранится в комнате: содержимое видят те, кому это разрешил
правообладатель. Поверх этого библиотека связывает одинаковые пакеты из разных
комнат в одну запись, чтобы видеть всю историю: в каких боях игрались темы,
когда и кто мог их слышать.

Связь — по содержимому: загрузили тот же файл в другую комнату, и это тот же
пакет. Похожие, но не совпадающие пакеты библиотека показывает
администратору как кандидатов: подтвердить, что это один пакет, может он.

Права (этап 0):

  * правообладатель — один или несколько пользователей; по умолчанию тот, кто
    загрузил. Он распоряжается пакетом: выдаёт доступы, правит, удаляет;
  * организатор — работает с пакетом и может назначить ведущих, но не может
    передавать другие права;
  * ведущий — видит и берёт пакет только в том турнире, куда его назначили;
  * открытый пакет (community) — доступен любому вошедшему для любых целей.

Ревизии: содержимое пакета не переписывается молча. Правка — это новая
ревизия, а копия в комнате помнит, из какой ревизии она сделана, поэтому
идущий бой не меняется под ведущим.
"""

import datetime
import difflib
import hashlib
import json
import math
import re
import sqlite3
import time as _time

# Как считать слышавших: весь зал или только участники боя.
EXPOSURE_HALL = "hall"
EXPOSURE_GAME = "game"
EXPOSURE_MODES = (EXPOSURE_HALL, EXPOSURE_GAME)

# Роли на пакет. Правообладателей и организаторов может быть несколько,
# ведущий всегда привязан к турниру: в другом турнире тот же пакет ему не виден.
ROLE_HOLDER = "holder"
ROLE_ORGANIZER = "organizer"
ROLE_HOST = "host"
ROLES = (ROLE_HOLDER, ROLE_ORGANIZER, ROLE_HOST)
ROLE_WEIGHT = {ROLE_HOLDER: 3, ROLE_ORGANIZER: 2, ROLE_HOST: 1}
ROLE_TITLES = {
    ROLE_HOLDER: "правообладатель",
    ROLE_ORGANIZER: "организатор",
    ROLE_HOST: "ведущий",
}

# Привязка права к турниру: 0 — без привязки (правообладатель, организатор).
SCOPE_ALL = 0

# Что видит посторонний: ничего, карточку (название, турнир, автор) или карточку
# с названиями тем. Содержимое вопросов не показывается никогда без права.
PREVIEW_NONE = "none"
PREVIEW_CARD = "card"
PREVIEW_THEMES = "themes"
PREVIEWS = (PREVIEW_NONE, PREVIEW_CARD, PREVIEW_THEMES)

# Статус пакета: закрытый (по правам) или открытый всем вошедшим.
ACCESS_PRIVATE = "private"
ACCESS_COMMUNITY = "community"
ACCESS_MODES = (ACCESS_PRIVATE, ACCESS_COMMUNITY)

# Насколько поздно ведущий может внести пришедшего: отметка в пределах этого
# времени после конца боя относится к тому бою. Дольше — считаем, что это уже
# другая партия (например, следующий день турнира).
LATE_ENTRY_HOURS = 12

# Два вопроса считаем одним и тем же, если тексты совпадают настолько (опечатки,
# перестановка слов, замена пары слов). Ответы не сравниваются.
FUZZY_RATIO = 0.8

# Тема та же, если совпало столько её вопросов (5 вопросов → нужно 4).
SAME_THEME_SHARE = 0.7

# В совсем коротких вопросах («Кто автор?») одна замена слова меняет смысл,
# поэтому мягкое сравнение начинается с этой длины в словах.
SHORT_QUESTION_WORDS = 4

MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


class LibraryError(Exception):
    """Операция не выполнена: текст пригоден для показа ведущему."""


def has_table(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone() is not None


def table_columns(conn, table):
    return [row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)]


def parse_time(value):
    """Время из журнала: там ctime («Wed Apr  3 20:27:26 2024»), либо ISO."""
    if not value:
        return None
    if isinstance(value, str) and value[:2].isalpha():
        parts = value.split()
        if len(parts) >= 5 and parts[1] in MONTHS:
            clock = parts[3].split(":")
            try:
                return (int(parts[4]), MONTHS[parts[1]], int(parts[2]),
                        int(clock[0]), int(clock[1]), int(clock[2]))
            except (ValueError, IndexError):
                return None
        return None
    text = str(value).strip().replace("T", " ")
    stamp = text.split(".")[0]
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return tuple(_time.strptime(stamp, pattern)[:6])
        except ValueError:
            continue
    return None


def format_time(moment):
    if not moment:
        return ""
    return "%02d.%02d.%04d %02d:%02d" % (
        moment[2], moment[1], moment[0], moment[3], moment[4])


# ---------------------------------------------------------------- содержимое

def package_topics(data):
    """Темы пакета: [{topic, questions: [...]}] (пакет приходит из пульта)."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return []
    if not isinstance(data, dict):
        return []
    topics = data.get("topics")
    return topics if isinstance(topics, list) else []


def normalize_text(text):
    """Текст вопроса для сравнения: без регистра, знаков и лишних пробелов."""
    if not text:
        return ""
    text = str(text).lower().replace("ё", "е")
    text = re.sub(r"[^\w]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def theme_questions(theme):
    """Тексты вопросов темы по порядку, в нормальном виде."""
    texts = []
    if not isinstance(theme, dict):
        return texts
    for question in theme.get("questions") or []:
        if isinstance(question, dict):
            text = normalize_text(question.get("text"))
            if text:
                texts.append(text)
    return texts


def theme_question_texts(theme):
    """Тексты вопросов одной темы (набор)."""
    return set(theme_questions(theme))


def theme_signature(theme):
    """Тождество темы — её вопросы, а не название.

    Тема — это набор вопросов (обычно пять). В разных пакетах темы могут
    называться одинаково, но содержать разные вопросы: это разные темы.
    """
    texts = theme_question_texts(theme)
    if not texts:
        title = theme.get("topic") if isinstance(theme, dict) else None
        if not title:
            return None
        return "title:" + " ".join(title.split()).lower()
    return hashlib.sha1("\n".join(sorted(texts)).encode("utf-8")).hexdigest()


def question_similarity(one, two):
    """Насколько похожи два вопроса: 1.0 — тот же текст, 0.0 — совсем разный.

    Ответы не участвуют: сравниваются только тексты вопросов.
    """
    if not one or not two:
        return 0.0
    if one == two:
        return 1.0
    left, right = one.split(), two.split()
    # Короткие тексты («вопрос Наука 10» и «вопрос Космос 10») отличаются одним
    # словом, но это разные вопросы: правки ищем только в длинных формулировках.
    if min(len(left), len(right)) < SHORT_QUESTION_WORDS:
        return 0.0
    # Быстрый отсев по словам: у разных вопросов мало общих слов.
    left_words, right_words = set(left), set(right)
    shared = left_words & right_words
    union = left_words | right_words
    if not shared or len(shared) < 0.5 * len(union):
        return 0.0
    longest = max(len(one), len(two))
    if abs(len(one) - len(two)) > max(24, longest // 2):
        return 0.0
    matcher = difflib.SequenceMatcher(None, one, two)
    # quick_ratio — верхняя оценка: если она ниже порога, точную не считаем.
    if matcher.quick_ratio() < FUZZY_RATIO:
        return 0.0
    return matcher.ratio()


def theme_vocabulary(theme):
    """Слова всех вопросов темы — для быстрого отсева разных тем."""
    words = set()
    for text in theme_questions(theme):
        words.update(text.split())
    return words


def theme_match(one, two):
    """Сравнение двух тем по вопросам: сколько вопросов совпало (с точностью до правок)."""
    left, right = theme_questions(one), theme_questions(two)
    if not left or not right:
        return {"exact": False, "matched": 0, "total": 0}
    exact = sorted(left) == sorted(right)
    used, matched = set(), 0
    for text in left:
        best, best_index = 0.0, None
        for index, other in enumerate(right):
            if index in used:
                continue
            score = question_similarity(text, other)
            if score > best:
                best, best_index = score, index
        if best_index is not None and best >= FUZZY_RATIO:
            used.add(best_index)
            matched += 1
    return {"exact": exact, "matched": matched, "total": max(len(left), len(right))}


def same_theme(match):
    """Одна ли это тема: полностью та же или с небольшими правками текста."""
    if match["total"] <= 0:
        return False
    if match["exact"]:
        return True
    need = max(2, math.ceil(match["total"] * SAME_THEME_SHARE))
    return match["matched"] >= need


def theme_title(theme):
    if isinstance(theme, dict):
        return (theme.get("topic") or "без названия").strip()
    if isinstance(theme, str):
        return theme.strip() or "без названия"
    return "без названия"


def theme_title_key(title):
    """Название темы для сравнения: без регистра, ё и знаков.

    Нужно для старых боёв: в их подробностях сохранились только названия тем,
    вопросов там нет, поэтому опознать тему иначе не получится.
    """
    return normalize_text(title)


def load_json(value):
    try:
        data = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# Разбор старых подробностей боёв нужен на каждый запрос истории, а лог растёт:
# запоминаем результат по состоянию лога, чтобы не разбирать его заново.
_LEGACY_CACHE = {"key": None, "games": {}}


def legacy_layouts(conn):
    """Раскладки старых боёв: названия тем из подробностей боя в логе.

    До появления пакетов в пульте ведущий сохранял бой вместе с подробностями:
    в них есть названия тем и кто что взял. Вопросов там нет, поэтому тема
    опознаётся только по названию — с соответствующей оговоркой в интерфейсе.
    """
    try:
        row = conn.execute(
            "SELECT MAX(rowid) AS last, COUNT(*) AS total FROM log").fetchone()
        tail = conn.execute(
            "SELECT date, LENGTH(data) AS size FROM log ORDER BY rowid DESC LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return {}
    key = None if row is None else (
        row["last"], row["total"],
        tail["date"] if tail is not None else None,
        tail["size"] if tail is not None else None)
    if _LEGACY_CACHE["key"] == key:
        return _LEGACY_CACHE["games"]

    games = {}
    try:
        rows = conn.execute(
            "SELECT roomid, data FROM log WHERE event = 'save game result' ORDER BY rowid"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    for row in rows:
        payload = load_json(row["data"])
        try:
            gameindex = int(payload.get("gameindex"))
        except (TypeError, ValueError):
            continue
        detailed = payload.get("detailed")
        if not isinstance(detailed, dict):
            continue
        place = (row["roomid"], gameindex)
        game = games.setdefault(place, {"titles": [], "players": []})
        for answer in detailed.get("answers") or []:
            if not isinstance(answer, dict):
                continue
            title = str(answer.get("theme") or "").strip()
            if title and title not in game["titles"]:
                game["titles"].append(title)
        for name in detailed.get("players") or []:
            name = str(name or "").strip()
            if name and name not in game["players"]:
                game["players"].append(name)
    games = {place: game for place, game in games.items() if game["titles"]}
    _LEGACY_CACHE["key"] = key
    _LEGACY_CACHE["games"] = games
    return games


# Старого боя не бывает меньше, чем тема; в пакете должны найтись почти все его
# темы, иначе это случайные совпадения названий («Учёные» есть у многих).
LEGACY_COVERAGE = 0.8
LEGACY_MIN_TITLES = 2

_LIBRARY_TITLES_CACHE = {"key": None, "index": {}}


def reset_caches():
    """Сбросить запомненные разборы (нужно тестам и ручным правкам базы)."""
    _LEGACY_CACHE["key"] = None
    _LEGACY_CACHE["games"] = {}
    _LIBRARY_TITLES_CACHE["key"] = None
    _LIBRARY_TITLES_CACHE["index"] = {}


def library_title_index(conn):
    """libraryid → названия тем пакета: {ключ: [темы]} плюс комнаты загрузки.

    Пакет — это запись библиотеки: у неё может быть несколько копий файла,
    и копии бывают не идентичны, поэтому темы складываем со всех.
    """
    try:
        row = conn.execute(
            "SELECT MAX(rowid) AS last, COUNT(*) AS total,"
            " COUNT(libraryid) AS linked, SUM(COALESCE(libraryid, 0)) AS fingerprint"
            " FROM packages").fetchone()
    except sqlite3.OperationalError:
        return {}
    key = None if row is None else tuple(row)
    if _LIBRARY_TITLES_CACHE["key"] == key:
        return _LIBRARY_TITLES_CACHE["index"]

    index = {}
    try:
        rows = conn.execute(
            "SELECT roomid, name, data, libraryid FROM packages").fetchall()
    except sqlite3.OperationalError:
        rows = []
    for row in rows:
        libraryid = row["libraryid"]
        if libraryid is None:
            continue
        entry = index.setdefault(libraryid, {"name": row["name"] or "",
                                             "titles": {}, "rooms": set()})
        entry["rooms"].add(row["roomid"])
        for theme in package_topics(row["data"]):
            title_key = theme_title_key(theme_title(theme))
            if not title_key:
                continue
            themes = entry["titles"].setdefault(title_key, [])
            signature = theme_signature(theme)
            if all(theme_signature(item) != signature for item in themes):
                themes.append(theme)
    _LIBRARY_TITLES_CACHE["key"] = key
    _LIBRARY_TITLES_CACHE["index"] = index
    return index


def ensure_battle_marks(conn):
    """Пометки ведущего: чей это на самом деле был бой."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS package_battle_marks ("
        " roomid INTEGER NOT NULL,"
        " gameindex INTEGER NOT NULL,"
        " libraryid INTEGER NOT NULL,"
        " status TEXT NOT NULL,"  # "own" — наш пакет, "foreign" — не наш
        " note TEXT,"
        " by TEXT,"
        " created_at TEXT DEFAULT CURRENT_TIMESTAMP,"
        " PRIMARY KEY (roomid, gameindex, libraryid))"
    )


def battle_marks(conn):
    """{(roomid, gameindex): {libraryid: "own"|"foreign"}} — ручные пометки."""
    try:
        rows = conn.execute(
            "SELECT roomid, gameindex, libraryid, status FROM package_battle_marks"
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    marks = {}
    for row in rows:
        try:
            gameindex = int(row["gameindex"])
        except (TypeError, ValueError):
            continue
        marks.setdefault((row["roomid"], gameindex), {})[row["libraryid"]] = row["status"]
    return marks


def set_battle_mark(conn, roomid, gameindex, libraryid, status, by=None):
    """Сказать «этот бой играли этим пакетом» или «не нашим».

    status: "own" (привязать вручную), "foreign" (убрать) или None (снять пометку).
    """
    ensure_battle_marks(conn)
    if status is None:
        conn.execute(
            "DELETE FROM package_battle_marks WHERE roomid = ? AND gameindex = ? "
            "AND libraryid = ?", (roomid, gameindex, libraryid))
        return
    if status not in ("own", "foreign"):
        raise ValueError("unknown status: %s" % status)
    conn.execute(
        "INSERT OR REPLACE INTO package_battle_marks "
        "(roomid, gameindex, libraryid, status, by) VALUES (?, ?, ?, ?, ?)",
        (roomid, gameindex, libraryid, status, by))


def attributed_legacy(conn):
    """{(roomid, gameindex): {titles, libraries}} — какие пакеты играли этот бой.

    Названия тем боя сверяются с названиями тем пакета: если в пакете нашлись
    почти все темы боя, значит играли именно его, и темы боя — это темы пакета
    со своими вопросами. Одиночные совпадения названий ничего не значат.
    Ручные пометки ведущего главнее: "own" привязывает, "foreign" убирает.
    """
    index = library_title_index(conn)
    marks = battle_marks(conn)
    result = {}
    for (roomid, gameindex), game in legacy_layouts(conn).items():
        keys = [theme_title_key(title) for title in game["titles"]]
        keys = [key for key in keys if key]
        if not keys or not index:
            continue
        need = max(LEGACY_MIN_TITLES, int(math.ceil(len(keys) * LEGACY_COVERAGE)))
        found = {}
        for libraryid, entry in index.items():
            mark = marks.get((roomid, gameindex), {}).get(libraryid)
            if mark == "foreign":
                continue
            matched = [key for key in keys if key in entry["titles"]]
            if mark != "own" and len(matched) < need:
                continue
            themes = []
            for key in matched:
                # Одноимённых тем в пакете может быть несколько: какая из них
                # игралась, по названию не понять — такую тему не берём.
                variants = entry["titles"][key]
                if len(variants) == 1:
                    themes.append(variants[0])
            if not themes:
                continue
            found[libraryid] = {"themes": themes, "matched": len(matched),
                                "total": len(keys), "manual": mark == "own"}
        if found:
            result[(roomid, gameindex)] = {
                "roomid": roomid, "gameindex": gameindex,
                "titles": game["titles"], "libraries": found,
            }
    return result


def content_hash(data):
    """Хэш содержимого: одинаковый файл даёт одинаковый хэш."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return hashlib.sha1(str(data).encode("utf-8")).hexdigest()
    canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ таблицы

def _columns(conn, table):
    return {row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)}


def add_column(conn, table, name, declaration):
    """Добавляет колонку, если её ещё нет. True — если поменяли схему."""
    if not has_table(conn, table) or name in _columns(conn, table):
        return False
    conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, declaration))
    return True


def ensure_library(conn):
    """Таблицы библиотеки и колонка packages.libraryid. Возвращает True, если что-то поменяли."""
    changed = False
    conn.execute(
        "CREATE TABLE IF NOT EXISTS package_library ("
        " libraryid INTEGER PRIMARY KEY AUTOINCREMENT,"
        " name TEXT,"
        " hash TEXT,"
        " ownerid INTEGER,"
        " created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_library_hash ON package_library(hash)")
    # Карточка канона: то, что видно даже без права на содержимое, и то, по чему
    # правообладатель решает, кому пакет открыт.
    for name, declaration in (
        ("revision", "INTEGER DEFAULT 0"),
        ("author_name", "TEXT"),
        ("tournament", "TEXT"),
        ("origin_date", "TEXT"),
        ("description", "TEXT"),
        ("preview", "TEXT DEFAULT 'card'"),
        ("access", "TEXT DEFAULT 'private'"),
        ("deleted_at", "TEXT"),
    ):
        changed = add_column(conn, "package_library", name, declaration) or changed
    conn.execute(
        "CREATE TABLE IF NOT EXISTS package_links ("
        " libraryid_a INTEGER NOT NULL,"
        " libraryid_b INTEGER NOT NULL,"
        " status TEXT NOT NULL,"
        " by TEXT,"
        " created_at TEXT DEFAULT CURRENT_TIMESTAMP,"
        " PRIMARY KEY (libraryid_a, libraryid_b))"
    )
    if has_table(conn, "packages"):
        changed = add_column(conn, "packages", "libraryid", "INTEGER") or changed
        changed = add_column(conn, "packages", "revision", "INTEGER") or changed
    ensure_battle_marks(conn)
    ensure_layouts(conn)
    changed = backfill(conn) or changed
    changed = ensure_rights(conn) or changed
    changed = ensure_revisions(conn) or changed
    changed = ensure_copy_revisions(conn) or changed
    return changed


def ensure_rights(conn):
    """Таблица прав и раздача правообладателей уже существующим пакетам."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS package_rights ("
        " libraryid INTEGER NOT NULL,"
        " userid INTEGER NOT NULL,"
        " role TEXT NOT NULL,"
        " roomid INTEGER NOT NULL DEFAULT 0,"
        " granted_by INTEGER,"
        " granted_at TEXT DEFAULT CURRENT_TIMESTAMP,"
        " PRIMARY KEY (libraryid, userid, role, roomid))"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rights_user ON package_rights(userid)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rights_room ON package_rights(roomid)")
    changed = False
    admin = None
    for row in conn.execute("SELECT libraryid, ownerid FROM package_library"):
        holder = conn.execute(
            "SELECT 1 FROM package_rights WHERE libraryid = ? AND role = ?",
            (row[0], ROLE_HOLDER),
        ).fetchone()
        if holder is not None:
            continue
        owner = row[1]
        if owner is None:
            # Владельца нет: правообладателем становится владелец комнаты, где
            # лежит копия, иначе администратор — иначе пакет останется без хозяина.
            found = conn.execute(
                "SELECT r.ownerid FROM packages p JOIN activerooms r ON r.roomid = p.roomid "
                "WHERE p.libraryid = ? AND r.ownerid IS NOT NULL "
                "ORDER BY p.packageid LIMIT 1", (row[0],),
            ).fetchone() if has_table(conn, "packages") else None
            owner = found[0] if found is not None else None
        if owner is None:
            if admin is None:
                admin = 0
                if has_table(conn, "users"):
                    row_admin = conn.execute(
                        "SELECT userid FROM users WHERE role = 'администратор' "
                        "ORDER BY userid LIMIT 1"
                    ).fetchone()
                    if row_admin is not None:
                        admin = row_admin[0]
            owner = admin or None
        if not owner:
            continue
        conn.execute(
            "INSERT INTO package_rights (libraryid, userid, role, roomid) VALUES (?, ?, ?, ?)",
            (row[0], owner, ROLE_HOLDER, SCOPE_ALL),
        )
        changed = True
    return changed


def ensure_revisions(conn):
    """Таблица ревизий; уже лежащим пакетам выдаём ревизию 1 из копии."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS package_revisions ("
        " libraryid INTEGER NOT NULL,"
        " revision INTEGER NOT NULL,"
        " data TEXT NOT NULL,"
        " hash TEXT NOT NULL,"
        " note TEXT,"
        " created_by INTEGER,"
        " created_at TEXT DEFAULT CURRENT_TIMESTAMP,"
        " PRIMARY KEY (libraryid, revision))"
    )
    changed = False
    for row in conn.execute("SELECT libraryid, revision FROM package_library"):
        latest = conn.execute(
            "SELECT MAX(revision) FROM package_revisions WHERE libraryid = ?", (row[0],)
        ).fetchone()[0]
        if latest:
            if int(row[1] or 0) != int(latest):
                conn.execute(
                    "UPDATE package_library SET revision = ? WHERE libraryid = ?",
                    (int(latest), row[0]),
                )
                changed = True
            continue
        if not has_table(conn, "packages"):
            continue
        copy = conn.execute(
            "SELECT data FROM packages WHERE libraryid = ? ORDER BY packageid DESC LIMIT 1",
            (row[0],),
        ).fetchone()
        if copy is None or not copy[0]:
            continue
        digest = content_hash(copy[0])
        conn.execute(
            "INSERT INTO package_revisions (libraryid, revision, data, hash, note) "
            "VALUES (?, 1, ?, ?, ?)",
            (row[0], copy[0], digest, "перенесено из копии в комнате"),
        )
        conn.execute(
            "UPDATE package_library SET revision = 1, hash = ? WHERE libraryid = ?",
            (digest, row[0]),
        )
        changed = True
    return changed


def ensure_copy_revisions(conn):
    """Копия в комнате помнит ревизию канона."""
    if not has_table(conn, "packages") or "revision" not in _columns(conn, "packages"):
        return False
    changed = False
    for row in conn.execute(
        "SELECT packageid, libraryid FROM packages "
        "WHERE libraryid IS NOT NULL AND revision IS NULL"
    ):
        current = conn.execute(
            "SELECT revision FROM package_library WHERE libraryid = ?", (row[1],)
        ).fetchone()
        revision = int(current[0]) if current is not None and current[0] else 1
        conn.execute(
            "UPDATE packages SET revision = ? WHERE packageid = ?", (revision, row[0])
        )
        changed = True
    return changed


def backfill(conn):
    """Раздаёт библиотечные номера уже лежащим пакетам: одинаковое — в одну запись."""
    if not has_table(conn, "packages") or "libraryid" not in table_columns(conn, "packages"):
        return False
    rows = conn.execute(
        "SELECT packageid, roomid, name, data FROM packages WHERE libraryid IS NULL"
    ).fetchall()
    if not rows:
        return False
    by_hash = {}
    for row in conn.execute("SELECT libraryid, hash FROM package_library"):
        by_hash.setdefault(row[1], row[0])
    owners = {}
    if has_table(conn, "activerooms"):
        owners = {row[0]: row[1] for row in conn.execute(
            "SELECT roomid, ownerid FROM activerooms")}
    for row in rows:
        packageid, roomid, name = row[0], row[1], row[2]
        digest = content_hash(row[3])
        libraryid = by_hash.get(digest)
        if libraryid is None:
            cursor = conn.execute(
                "INSERT INTO package_library (name, hash, ownerid) VALUES (?, ?, ?)",
                (name, digest, owners.get(roomid)),
            )
            libraryid = cursor.lastrowid
            by_hash[digest] = libraryid
        conn.execute("UPDATE packages SET libraryid = ? WHERE packageid = ?",
                     (libraryid, packageid))
    return True


def link_package(conn, packageid):
    """Привязывает пакет к библиотечной записи; создаёт её, если такой нет."""
    if not has_table(conn, "packages") or "libraryid" not in table_columns(conn, "packages"):
        return None
    row = conn.execute(
        "SELECT packageid, roomid, name, data, libraryid FROM packages WHERE packageid = ?",
        (packageid,),
    ).fetchone()
    if row is None:
        return None
    if row[4] is not None:
        return row[4]
    digest = content_hash(row[3])
    found = conn.execute(
        "SELECT libraryid FROM package_library WHERE hash = ? ORDER BY libraryid", (digest,)
    ).fetchone()
    if found is not None:
        libraryid = found[0]
    else:
        ownerid = None
        if has_table(conn, "activerooms"):
            room = conn.execute("SELECT ownerid FROM activerooms WHERE roomid = ?",
                                (row[1],)).fetchone()
            ownerid = room[0] if room is not None else None
        cursor = conn.execute(
            "INSERT INTO package_library (name, hash, ownerid) VALUES (?, ?, ?)",
            (row[2], digest, ownerid),
        )
        libraryid = cursor.lastrowid
        # Загрузивший сразу становится правообладателем: не ждём следующего
        # открытия страницы, иначе пакет на минуту останется без хозяина.
        if ownerid and has_table(conn, "package_rights"):
            grant_package_right(conn, libraryid, ownerid, ROLE_HOLDER, SCOPE_ALL, ownerid)
    conn.execute("UPDATE packages SET libraryid = ? WHERE packageid = ?", (libraryid, packageid))
    return libraryid


# ---------------------------------------------------------------- просмотр

def library_entry(conn, libraryid):
    """Карточка канона: то, что не зависит от прав."""
    if not has_table(conn, "package_library"):
        return None
    row = conn.execute(
        "SELECT libraryid, name, ownerid, hash, created_at, revision, author_name, "
        "tournament, origin_date, description, preview, access, deleted_at "
        "FROM package_library WHERE libraryid = ?", (libraryid,)
    ).fetchone()
    return dict(row) if row is not None else None


def package_roles(conn, libraryid, userid):
    """Все права человека на пакет: {роль: [турниры]} (0 — без привязки)."""
    roles = {}
    if userid is None or not has_table(conn, "package_rights"):
        return roles
    for row in conn.execute(
        "SELECT role, roomid FROM package_rights WHERE libraryid = ? AND userid = ?",
        (libraryid, int(userid)),
    ):
        roles.setdefault(row[0], []).append(int(row[1] or 0))
    return roles


def copy_rooms(conn, libraryid):
    """Турниры, в которых уже лежит копия пакета."""
    if not has_table(conn, "packages"):
        return set()
    return {
        int(row[0]) for row in conn.execute(
            "SELECT DISTINCT roomid FROM packages WHERE libraryid = ?", (libraryid,)
        ) if row[0] is not None
    }


def package_access(conn, libraryid, user, roomid=None, manages=None):
    """Права на пакет в контексте турнира (roomid=None — вне турнира).

    Ведущий видит содержимое только в своём турнире: на странице пакета без
    контекста ему показывается карточка и список турниров, где пакет доступен.

    manages(roomid) — «веду ли я эту комнату»: копия в своей комнате уже даёт
    право ведущего, даже если отдельного права на пакет нет.
    """
    entry = library_entry(conn, libraryid)
    if entry is None:
        return None
    admin = False
    userid = None
    if user is not None:
        try:
            admin = user["role"] == "администратор"
            userid = user["userid"]
        except (KeyError, IndexError, TypeError):
            admin, userid = False, None
    deleted = bool(entry.get("deleted_at"))
    community = (entry.get("access") or ACCESS_PRIVATE) == ACCESS_COMMUNITY
    preview = entry.get("preview") or PREVIEW_CARD
    if preview not in PREVIEWS:
        preview = PREVIEW_CARD
    roles = package_roles(conn, libraryid, userid)
    host_rooms = sorted(roles.get(ROLE_HOST, []))
    if ROLE_HOLDER in roles:
        role = ROLE_HOLDER
    elif ROLE_ORGANIZER in roles:
        role = ROLE_ORGANIZER
    elif roomid is not None and int(roomid) in host_rooms:
        role = ROLE_HOST
    else:
        role = None
    copied = copy_rooms(conn, libraryid) if userid is not None else set()
    # Копия в комнате, которой человек управляет, — уже сложившееся право.
    if roomid is None:
        usable = bool(copied) and manages is not None and any(manages(room) for room in copied)
    else:
        usable = int(roomid) in host_rooms or (
            int(roomid) in copied and (manages is None or manages(int(roomid))))
    allowed_role = role in (ROLE_HOLDER, ROLE_ORGANIZER)
    live = not deleted or admin
    content = bool(live and (admin or allowed_role or community or usable))
    # Карточка видна тем, кому пакет хоть как-то интересен; «ничего» скрывает
    # пакет от посторонних совсем.
    mine = bool(admin or allowed_role or host_rooms or community or usable)
    card = bool(live and (mine or preview != PREVIEW_NONE))
    can_take = bool(live and (admin or community or allowed_role
                              or (roomid is None and (host_rooms or usable))
                              or (roomid is not None and usable)))
    return {
        "libraryid": libraryid,
        "entry": entry,
        "role": role,
        "role_title": ROLE_TITLES.get(role or ""),
        "admin": admin,
        "community": community,
        "preview": preview,
        "host_rooms": host_rooms,
        "deleted": deleted,
        "card": card,
        "mine": mine,
        "content": content,
        "can_take": can_take,
        "can_edit": bool(live and (admin or allowed_role)),
        "can_delete": bool(live and (admin or role == ROLE_HOLDER)),
        "can_grant": bool(live and (admin or role == ROLE_HOLDER)),
        "can_grant_host": bool(live and (admin or role in (ROLE_HOLDER, ROLE_ORGANIZER))),
    }


def grant_package_right(conn, libraryid, userid, role, roomid=SCOPE_ALL, granted_by=None):
    """Выдаёт право. Проверку «кто вправе выдавать» делает вызывающий код."""
    if role not in ROLES:
        raise LibraryError("Неизвестная роль: %s" % role)
    roomid = int(roomid or SCOPE_ALL)
    if role == ROLE_HOST and roomid == SCOPE_ALL:
        raise LibraryError("Ведущему нужно указать турнир")
    if role != ROLE_HOST:
        roomid = SCOPE_ALL
    conn.execute(
        "INSERT INTO package_rights (libraryid, userid, role, roomid, granted_by) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(libraryid, userid, role, roomid) DO UPDATE SET "
        "granted_by = excluded.granted_by, granted_at = CURRENT_TIMESTAMP",
        (libraryid, int(userid), role, roomid, granted_by),
    )


def revoke_package_right(conn, libraryid, userid, role, roomid=SCOPE_ALL):
    """Отзывает право. Последнего правообладателя отозвать нельзя."""
    roomid = int(roomid or SCOPE_ALL)
    if role == ROLE_HOLDER:
        holders = conn.execute(
            "SELECT COUNT(*) FROM package_rights WHERE libraryid = ? AND role = ?",
            (libraryid, ROLE_HOLDER),
        ).fetchone()[0]
        mine = conn.execute(
            "SELECT 1 FROM package_rights WHERE libraryid = ? AND userid = ? AND role = ? "
            "AND roomid = ?", (libraryid, int(userid), ROLE_HOLDER, SCOPE_ALL),
        ).fetchone()
        if holders <= 1 and mine is not None:
            raise LibraryError("У пакета должен остаться правообладатель")
    conn.execute(
        "DELETE FROM package_rights WHERE libraryid = ? AND userid = ? AND role = ? AND roomid = ?",
        (libraryid, int(userid), role, roomid),
    )


def package_holders(conn, libraryid):
    """Правообладатели пакета: [{userid, name}]."""
    if not has_table(conn, "package_rights"):
        return []
    rows = conn.execute(
        "SELECT r.userid, COALESCE(u.name, u.login, '') AS name FROM package_rights r "
        "LEFT JOIN users u ON u.userid = r.userid "
        "WHERE r.libraryid = ? AND r.role = ? ORDER BY r.userid",
        (libraryid, ROLE_HOLDER),
    ).fetchall()
    return [{"userid": row[0], "name": row[1]} for row in rows]


def package_rights_view(conn, libraryid):
    """Все выданные права: [{userid, name, role, role_title, roomid, room, granted_at}]."""
    if not has_table(conn, "package_rights"):
        return []
    rows = conn.execute(
        "SELECT r.userid, COALESCE(u.name, u.login, '') AS name, r.role, r.roomid, "
        "r.granted_at, COALESCE(v.venuename, '') AS venue, ar.date "
        "FROM package_rights r "
        "LEFT JOIN users u ON u.userid = r.userid "
        "LEFT JOIN activerooms ar ON ar.roomid = r.roomid "
        "LEFT JOIN venues v ON v.venueid = ar.venueid "
        "WHERE r.libraryid = ? ORDER BY r.role DESC, r.roomid, r.userid",
        (libraryid,),
    ).fetchall()
    items = []
    for row in rows:
        room = None
        if row[3]:
            room = "турнир %s" % row[3]
            if row[5]:
                room += " · " + row[5]
            if row[6]:
                room += " · " + str(row[6])
        items.append({
            "userid": row[0],
            "name": row[1],
            "role": row[2],
            "role_title": ROLE_TITLES.get(row[2], row[2]),
            "roomid": int(row[3] or 0),
            "room": room,
            "granted_at": row[4],
        })
    return items


def can_grant_role(access, role):
    """Кто какие роли выдаёт: правообладатель — любые, организатор — ведущих."""
    if access is None:
        return False
    if access.get("admin"):
        return True
    if role == ROLE_HOST:
        return bool(access.get("can_grant_host"))
    return bool(access.get("can_grant"))


def current_package_data(conn, libraryid):
    """Содержимое действующей ревизии; для старых записей — из копии в комнате."""
    row = conn.execute(
        "SELECT data FROM package_revisions WHERE libraryid = ? "
        "ORDER BY revision DESC LIMIT 1", (libraryid,)
    ).fetchone()
    if row is not None and row[0]:
        return row[0]
    if not has_table(conn, "packages"):
        return None
    copy = conn.execute(
        "SELECT data FROM packages WHERE libraryid = ? ORDER BY packageid DESC LIMIT 1",
        (libraryid,),
    ).fetchone()
    return copy[0] if copy is not None else None


def package_theme_names(conn, libraryid):
    """Названия тем действующей ревизии (для карточки и превью)."""
    data = current_package_data(conn, libraryid)
    if data is None:
        return []
    return [theme_title(theme) for theme in package_topics(data)]


def package_revisions(conn, libraryid):
    """История ревизий: номер, когда, кто, заметка, сколько тем."""
    rows = conn.execute(
        "SELECT r.revision, r.created_at, r.note, r.hash, "
        "COALESCE(u.name, u.login, '') AS author, r.data "
        "FROM package_revisions r LEFT JOIN users u ON u.userid = r.created_by "
        "WHERE r.libraryid = ? ORDER BY r.revision DESC", (libraryid,)
    ).fetchall()
    items = []
    for row in rows:
        items.append({
            "revision": int(row[0]),
            "created_at": str(row[1] or "")[:16],
            "note": row[2] or "",
            "hash": row[3],
            "author": row[4] or "",
            "themes": len(package_topics(row[5])),
        })
    return items


def ensure_layouts(conn):
    """Раскладки пакета: как его темы разложены по боям."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS package_layouts ("
        " layoutid INTEGER PRIMARY KEY AUTOINCREMENT,"
        " libraryid INTEGER NOT NULL,"
        " name TEXT NOT NULL,"
        " preset TEXT,"
        " parts TEXT NOT NULL,"
        " created_by TEXT,"
        " created_at TEXT DEFAULT CURRENT_TIMESTAMP,"
        " updated_at TEXT DEFAULT CURRENT_TIMESTAMP,"
        " deleted_at TEXT)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_layout_library ON package_layouts(libraryid)")


def theme_rounds(topics):
    """Раунды пакета: подряд идущие темы с одинаковым stage.

    Пакет ЭК размечает раунды сам («Кубок 1/8 финала. Первый раунд»), и по этой
    разметке видно, где кончается один бой ЭК. Пакет без stage раундов не имеет.
    """
    rounds = []
    for index, topic in enumerate(topics):
        stage = theme_stage(topic)
        if stage and rounds and rounds[-1][0] == stage:
            rounds[-1][1].append(index)
        else:
            rounds.append([stage, [index]])
    if not any(stage for stage, _ in rounds):
        return []
    return [indexes for _, indexes in rounds]


def theme_stage(topic):
    """Подпись раунда из поля stage: строка или список строк."""
    stage = (topic or {}).get("stage")
    if isinstance(stage, (list, tuple)):
        return " · ".join(str(item).strip() for item in stage if str(item).strip())
    return str(stage or "").strip()


def theme_blocks(topics, per_game=6):
    """Бои ЭК: [(индексы тем, раунды)]. По stage, иначе по две игры подряд."""
    size = max(1, int(per_game or 6)) * 2
    rounds = theme_rounds(topics)
    if rounds:
        blocks, current, total = [], [], 0
        for round_indexes in rounds:
            if current and total + len(round_indexes) > size:
                blocks.append(current)
                current, total = [], 0
            current.append(list(round_indexes))
            total += len(round_indexes)
        if current:
            blocks.append(current)
        return [([index for group in block for index in group], block) for block in blocks]
    indexes = list(range(len(topics)))
    chunks = [indexes[start:start + size] for start in range(0, len(indexes), size)]
    return [(chunk, [chunk]) for chunk in chunks]


def layout_presets(topics, per_game=6):
    """Готовые раскладки: как есть, чёт/нечет, по половине раунда.

    «Как есть» режет пакет подряд по шесть тем. Две другие работают внутри
    боя ЭК (обычно 12 тем): чётные темы в один бой, нечётные в другой, либо
    первая половина каждого раунда в первый бой, вторая — во второй.
    """
    per_game = max(1, int(per_game or 6))
    if not topics:
        return []
    indexes = list(range(len(topics)))
    presets = [{
        "key": "flat",
        "name": "Как есть",
        "hint": "Темы подряд, по %s в бой." % per_game,
        "parts": [indexes[start:start + per_game]
                  for start in range(0, len(indexes), per_game)],
    }]
    rounds = theme_rounds(topics)
    alternate, halves = [], []
    for block_indexes, block_rounds in theme_blocks(topics, per_game):
        if len(block_indexes) < 2:
            continue
        alternate.append([index for position, index in enumerate(block_indexes)
                          if position % 2 == 0])
        alternate.append([index for position, index in enumerate(block_indexes)
                          if position % 2 == 1])
        if rounds:
            first, second = [], []
            for round_indexes in block_rounds:
                half = (len(round_indexes) + 1) // 2
                first.extend(round_indexes[:half])
                second.extend(round_indexes[half:])
            if first and second:
                halves.append(first)
                halves.append(second)
    if alternate:
        presets.append({
            "key": "alternate",
            "name": "Чёт/нечет",
            "hint": "В каждом бое ЭК нечётные темы — в первый бой, чётные — во второй.",
            "parts": alternate,
        })
    if halves:
        presets.append({
            "key": "halves",
            "name": "По половине раунда",
            "hint": "В каждом раунде первая половина тем — в первый бой, вторая — во второй.",
            "parts": halves,
        })
    return presets


def clean_layout_parts(parts):
    """Раскладка в виде списка списков номеров тем: без пустых частей и мусора."""
    cleaned = []
    for part in parts or []:
        indexes = []
        for index in part or []:
            try:
                indexes.append(int(index))
            except (TypeError, ValueError):
                continue
        if indexes:
            cleaned.append(indexes)
    return cleaned


def save_layout(conn, libraryid, name, parts, user=None, preset=None):
    """Создаёт раскладку или обновляет одноимённую у того же пакета."""
    ensure_layouts(conn)
    cleaned = clean_layout_parts(parts)
    if not cleaned:
        raise ValueError("в раскладке нет тем")
    author = user.get("name") if isinstance(user, dict) else user
    name = str(name or "").strip() or "Раскладка"
    payload = json.dumps(cleaned, ensure_ascii=False)
    row = conn.execute(
        "SELECT layoutid FROM package_layouts WHERE libraryid = ? AND name = ? "
        "AND deleted_at IS NULL", (libraryid, name)).fetchone()
    if row is not None:
        conn.execute(
            "UPDATE package_layouts SET parts = ?, preset = ?, "
            "updated_at = CURRENT_TIMESTAMP WHERE layoutid = ?",
            (payload, preset, row["layoutid"]))
        return row["layoutid"]
    cursor = conn.execute(
        "INSERT INTO package_layouts (libraryid, name, preset, parts, created_by) "
        "VALUES (?, ?, ?, ?, ?)", (libraryid, name, preset, payload, author))
    return cursor.lastrowid


def delete_layout(conn, layoutid):
    ensure_layouts(conn)
    cursor = conn.execute(
        "UPDATE package_layouts SET deleted_at = CURRENT_TIMESTAMP "
        "WHERE layoutid = ? AND deleted_at IS NULL", (layoutid,))
    return cursor.rowcount > 0


def package_layouts(conn, libraryid):
    """Раскладки пакета: имя, пресет, бои и номера тем в них."""
    ensure_layouts(conn)
    rows = conn.execute(
        "SELECT layoutid, name, preset, parts, created_by, created_at, updated_at "
        "FROM package_layouts WHERE libraryid = ? AND deleted_at IS NULL "
        "ORDER BY layoutid", (libraryid,)).fetchall()
    layouts = []
    for row in rows:
        # load_json годится только для словарей, а раскладка — список списков.
        try:
            raw_parts = json.loads(row["parts"] or "[]")
        except (TypeError, ValueError):
            raw_parts = []
        parts = clean_layout_parts(raw_parts if isinstance(raw_parts, list) else [])
        layouts.append({
            "layoutid": int(row["layoutid"]),
            "name": row["name"] or "",
            "preset": row["preset"] or "",
            "parts": parts,
            "battles": len(parts),
            "sizes": [len(part) for part in parts],
            "created_by": row["created_by"] or "",
            "created_at": str(row["created_at"] or "")[:16],
            "updated_at": str(row["updated_at"] or "")[:16],
        })
    return layouts


def layout_parts_view(layout, topics):
    """Раскладка с названиями тем: для страницы пакета и плана турнира."""
    parts = []
    for part in layout["parts"]:
        parts.append([{
            "index": index,
            "title": theme_title(topics[index]) if 0 <= index < len(topics) else "",
        } for index in part])
    return parts


def add_revision(conn, libraryid, data, note=None, created_by=None, name=None,
                 author_name=None, tournament=None, origin_date=None,
                 description=None):
    """Сохраняет новую ревизию и обновляет карточку пакета.

    data — уже JSON-строка или словарь. Возвращает номер новой ревизии.
    """
    if isinstance(data, (dict, list)):
        data = json.dumps(data, ensure_ascii=False)
    digest = content_hash(data)
    row = conn.execute(
        "SELECT MAX(revision) FROM package_revisions WHERE libraryid = ?", (libraryid,)
    ).fetchone()
    revision = int(row[0] or 0) + 1
    conn.execute(
        "INSERT INTO package_revisions (libraryid, revision, data, hash, note, created_by) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (libraryid, revision, data, digest, note, created_by),
    )
    fields = ["revision = ?", "hash = ?"]
    params = [revision, digest]
    for column, value in (("name", name), ("author_name", author_name),
                          ("tournament", tournament), ("origin_date", origin_date),
                          ("description", description)):
        if value is not None:
            fields.append("%s = ?" % column)
            params.append(value)
    params.append(libraryid)
    conn.execute(
        "UPDATE package_library SET %s WHERE libraryid = ?" % ", ".join(fields), params
    )
    return revision


def create_package(conn, name, data, ownerid, author_name=None, tournament=None,
                   origin_date=None, description=None, note=None):
    """Новый пакет: карточка + ревизия 1 + правообладатель. Возвращает libraryid."""
    if isinstance(data, (dict, list)):
        data = json.dumps(data, ensure_ascii=False)
    digest = content_hash(data)
    cursor = conn.execute(
        "INSERT INTO package_library (name, hash, ownerid, revision, author_name, "
        "tournament, origin_date, description, preview, access) "
        "VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?)",
        (name, digest, ownerid, author_name, tournament, origin_date, description,
         PREVIEW_CARD, ACCESS_PRIVATE),
    )
    libraryid = cursor.lastrowid
    conn.execute(
        "INSERT INTO package_revisions (libraryid, revision, data, hash, note, created_by) "
        "VALUES (?, 1, ?, ?, ?, ?)",
        (libraryid, data, digest, note or "первая загрузка", ownerid),
    )
    if ownerid:
        grant_package_right(conn, libraryid, ownerid, ROLE_HOLDER, SCOPE_ALL, ownerid)
    return libraryid


def library_rows(conn, user=None, query=None, limit=200, manages=None):
    """Список пакетов: свои с содержимым, остальные — карточками.

    Правообладатель и организатор видят пакет целиком, ведущий — только в тех
    турнирах, куда назначен, посторонний — карточку (название, турнир, автор) и,
    если правообладатель разрешил, названия тем.

    manages(roomid) — «веду ли я эту комнату»: копия в своей комнате уже даёт
    право ведущего, даже если отдельного права на пакет нет.
    """
    sql = (
        "SELECT l.libraryid, l.name, l.ownerid, l.hash, l.revision, "
        "l.preview, l.access, l.author_name, l.tournament, l.origin_date, "
        "l.deleted_at, "
        "(SELECT COUNT(*) FROM packages p WHERE p.libraryid = l.libraryid) AS copies, "
        "(SELECT COUNT(DISTINCT p.roomid) FROM packages p "
        " WHERE p.libraryid = l.libraryid) AS rooms, "
        "(SELECT COUNT(*) FROM game_questions gq JOIN packages p "
        " ON p.packageid = gq.packageid WHERE p.libraryid = l.libraryid) AS games "
        "FROM package_library l "
    )
    where, params = [], []
    admin = _role_of(user) == "администратор"
    if not admin:
        where.append("l.deleted_at IS NULL")
    if query:
        where.append("(l.name LIKE ? OR l.tournament LIKE ? OR l.author_name LIKE ?)")
        params.extend(["%" + query + "%"] * 3)
    if where:
        sql += "WHERE " + " AND ".join(where) + " "
    sql += "ORDER BY l.libraryid DESC LIMIT ?"
    params.append(limit)
    userid = _userid_of(user)
    rights = {}
    if userid is not None and has_table(conn, "package_rights"):
        for row in conn.execute(
            "SELECT libraryid, role, roomid FROM package_rights WHERE userid = ?", (userid,)
        ):
            rights.setdefault(row[0], {}).setdefault(row[1], []).append(int(row[2] or 0))
    copies_by_library = {}
    if has_table(conn, "packages"):
        for row in conn.execute(
            "SELECT DISTINCT libraryid, roomid FROM packages WHERE libraryid IS NOT NULL"
        ):
            copies_by_library.setdefault(row[0], set()).add(int(row[1] or 0))
    if manages is None:
        managed = set()
        if userid is not None and has_table(conn, "activerooms"):
            managed = {int(row[0]) for row in conn.execute(
                "SELECT roomid FROM activerooms WHERE ownerid = ?", (userid,))}
        manages = managed.__contains__
    rows = []
    for row in conn.execute(sql, params):
        libraryid = row[0]
        mine = rights.get(libraryid, {})
        host_rooms = sorted(mine.get(ROLE_HOST, []))
        copied = copies_by_library.get(libraryid, set())
        # Копия в комнате, которой человек управляет, — уже сложившееся право.
        usable = any(manages(room) for room in copied)
        community = (row[6] or ACCESS_PRIVATE) == ACCESS_COMMUNITY
        deleted = bool(row[10])
        role = ROLE_HOLDER if ROLE_HOLDER in mine else (
            ROLE_ORGANIZER if ROLE_ORGANIZER in mine else None)
        content = bool(admin or role or community or usable)
        item = {
            "libraryid": libraryid, "name": row[1], "ownerid": row[2], "hash": row[3],
            "revision": int(row[4] or 0),
            "preview": row[5] or PREVIEW_CARD,
            "community": community,
            "author": row[7], "tournament": row[8], "origin_date": row[9],
            "deleted": deleted,
            "copies": row[11], "rooms": row[12], "games": row[13],
            "topics": 0, "theme_names": [], "updated": "",
            "role": role,
            "host_rooms": host_rooms,
            "content": content,
            "usable": usable,
            "mine": bool(admin or role or community or host_rooms or usable),
        }
        first = conn.execute(
            "SELECT packageid, data, updated_at, created_at FROM packages "
            "WHERE libraryid = ? ORDER BY packageid DESC LIMIT 1", (libraryid,)
        ).fetchone()
        if first is not None:
            topics = package_topics(first[1])
            item["packageid"] = first[0]
            item["topics"] = len(topics)
            item["theme_names"] = [theme_title(theme) for theme in topics]
            item["updated"] = (first[2] or first[3] or "")[:16]
        else:
            revision = conn.execute(
                "SELECT data FROM package_revisions WHERE libraryid = ? "
                "ORDER BY revision DESC LIMIT 1", (libraryid,)
            ).fetchone()
            if revision is not None:
                topics = package_topics(revision[0])
                item["topics"] = len(topics)
                item["theme_names"] = [theme_title(theme) for theme in topics]
        # Названия тем — по разрешению: содержимое или превью «только темы».
        visible_themes = item["content"] or item["preview"] == PREVIEW_THEMES
        if not visible_themes:
            item["theme_names"] = []
        if not item["mine"] and item["preview"] == PREVIEW_NONE and not admin:
            continue
        rows.append(item)
    rows.sort(key=lambda item: (0 if item["mine"] else 1, -item["libraryid"]))
    return rows


def _userid_of(user):
    try:
        return user["userid"] if user is not None else None
    except (KeyError, IndexError, TypeError):
        return None


def _role_of(user):
    try:
        return user["role"] if user is not None else None
    except (KeyError, IndexError, TypeError):
        return None
    return rows


def library_copies(conn, libraryid):
    """Все копии пакета по комнатам."""
    rows = conn.execute(
        "SELECT p.packageid, p.roomid, p.name, p.data, p.updated_at, p.created_at, "
        "r.date, r.finished, u.name AS owner, v.venuename "
        "FROM packages p "
        "LEFT JOIN activerooms r ON r.roomid = p.roomid "
        "LEFT JOIN users u ON u.userid = r.ownerid "
        "LEFT JOIN venues v ON v.venueid = r.venueid "
        "WHERE p.libraryid = ? ORDER BY p.packageid", (libraryid,)
    ).fetchall()
    copies = []
    for row in rows:
        copies.append({
            "packageid": row[0], "roomid": row[1], "name": row[2],
            "topics": len(package_topics(row[3])),
            "updated": (row[4] or row[5] or "")[:16],
            "date": row[6], "finished": row[7], "owner": row[8], "venue": row[9],
        })
    return copies


# ------------------------------------------------------------ кто мог слышать

def room_timeline(conn, roomid):
    """Приходы, уходы и концы боёв комнаты из журнала."""
    arrivals, departures, ends = {}, {}, {}
    if not has_table(conn, "log"):
        return arrivals, departures, ends
    rows = conn.execute(
        "SELECT date, event, data FROM log WHERE roomid = ? ORDER BY rowid", (roomid,)
    ).fetchall()
    for row in rows:
        moment = parse_time(row[0])
        if moment is None:
            continue
        try:
            data = json.loads(row[2] or "{}")
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            continue
        name = data.get("name")
        if row[1] == "add active player" and name:
            arrivals.setdefault(name, []).append(moment)
        elif row[1] == "remove active player" and name:
            departures.setdefault(name, []).append(moment)
        elif row[1] == "save game result":
            try:
                gameindex = int(data.get("gameindex"))
            except (TypeError, ValueError):
                continue
            ends.setdefault(gameindex, []).append(moment)
    return arrivals, departures, ends


def game_times(conn, roomid, ends=None):
    """Когда закончился каждый бой: по журналу, иначе по протоколу."""
    if ends is None:
        _, _, ends = room_timeline(conn, roomid)
    times = {index: min(moments) for index, moments in ends.items() if moments}
    try:
        for row in conn.execute(
            "SELECT gameindex, updated_at FROM game_protocols WHERE roomid = ?", (roomid,)
        ):
            if row[0] in times:
                continue
            moment = parse_time(row[1])
            if moment:
                times[row[0]] = moment
    except sqlite3.OperationalError:
        pass
    return times


def presence(arrivals, departures):
    """Промежутки, когда игрок был в зале."""
    events = [(moment, "in") for moment in arrivals]
    events += [(moment, "out") for moment in departures]
    events.sort(key=lambda item: (item[0], 0 if item[1] == "in" else 1))
    spans, opened = [], None
    for moment, kind in events:
        if kind == "in":
            if opened is None:
                opened = moment
        else:
            if opened is not None:
                spans.append((opened, moment))
                opened = None
    if opened is not None:
        spans.append((opened, None))
    return spans


def game_players(conn, roomid, gameindex):
    """Участники боя по сохранённым результатам."""
    try:
        rows = conn.execute(
            "SELECT name FROM results WHERE roomid = ? AND gameindex = ?", (roomid, gameindex)
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    names = []
    for row in rows:
        if row[0] and row[0] not in names:
            names.append(row[0])
    return names


def moment_seconds(moment):
    """Время из журнала в секундах — чтобы считать разницу."""
    if not moment:
        return None
    return datetime.datetime(*moment).timestamp()


def previous_game_end(moment, ends):
    """Конец последнего боя, который закончился не позже этого момента."""
    earlier = [end for end in ends.values() if end is not None and end <= moment]
    return max(earlier) if earlier else None


def effective_arrival(moment, ends, limit_hours=LATE_ENTRY_HOURS):
    """Когда человек на самом деле мог прийти.

    Ведущий обычно сначала доигрывает бой и только потом вносит тех, кто пришёл
    во время боя. Поэтому отметку, поставленную вскоре после конца боя,
    относим к концу этого боя: считается, что человек слышал его.
    """
    finished = previous_game_end(moment, ends)
    if finished is None:
        return moment
    gap = moment_seconds(moment) - moment_seconds(finished)
    if gap is not None and 0 <= gap <= limit_hours * 3600:
        return finished
    return moment


def game_listeners(conn, roomid, gameindex, mode=EXPOSURE_HALL, times=None,
                   timeline=None):
    """Кто мог слышать вопросы этого боя.

    В режиме «зал» — все, кто был в комнате, пока шёл бой: пришёл до его конца и
    не ушёл до его начала. Отметка игрока, внесённого после боя (ведущий вносит
    опоздавших, уже сохранив результаты), относится к концу этого боя.
    Участники боя попадают в список всегда.
    """
    players = game_players(conn, roomid, gameindex)
    if mode == EXPOSURE_GAME:
        return [{"name": name, "played": True, "late": False} for name in players]
    if timeline is None:
        timeline = room_timeline(conn, roomid)
    arrivals, departures, ends = timeline
    if times is None:
        times = game_times(conn, roomid, ends)
    finish = times.get(gameindex)
    earlier = [moment for index, moment in times.items() if index < gameindex]
    start = max(earlier) if earlier else None

    heard = []
    late = set()
    for name, moments in arrivals.items():
        spans = presence(moments, departures.get(name, []))
        for opened, closed in spans:
            arrived = effective_arrival(opened, times)
            if finish is not None and arrived > finish:
                continue
            if start is not None and closed is not None and closed < start:
                continue
            heard.append(name)
            if arrived != opened:
                late.add(name)
            break
    for name in players:
        if name not in heard:
            heard.append(name)
    heard.sort()
    return [{"name": name, "played": name in players, "late": name in late}
            for name in heard]


PLACEHOLDER_QUESTION = re.compile(r"^вопрос \d+$")


def is_placeholder(themes, placeholder=False):
    """Заглушка вместо пакета: «Тема N / Вопрос M» — это не настоящие вопросы."""
    if placeholder:
        return True
    real = 0
    for theme in themes:
        if not isinstance(theme, dict):
            continue
        for question in theme.get("questions") or []:
            if not isinstance(question, dict):
                continue
            text = normalize_text(question.get("text"))
            if text and not PLACEHOLDER_QUESTION.match(text):
                real += 1
    return real == 0


def theme_layouts(conn):
    """Раскладки тем по боям: из плана (game_questions) и из пульта (game_protocols).

    Ведущий раскладывает темы в пульте: там они попадают в протокол боя и в план
    могут не попадать вовсе. Обе раскладки равноправны — бой один и тот же.
    """
    layouts = []
    try:
        rows = conn.execute(
            "SELECT roomid, gameindex, packageid, payload, updated_at FROM game_questions"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    for row in rows:
        payload = load_json(row["payload"])
        themes = [theme for theme in payload.get("themes") or [] if isinstance(theme, dict)]
        if not themes or is_placeholder(themes, payload.get("placeholder")):
            continue
        layouts.append({
            "roomid": row["roomid"], "gameindex": row["gameindex"],
            "packageid": row["packageid"], "themes": themes,
            "source": "plan", "updated": row["updated_at"],
        })
    try:
        rows = conn.execute(
            "SELECT roomid, gameindex, payload, updated_at FROM game_protocols"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    for row in rows:
        payload = load_json(row["payload"])
        themes = [theme for theme in payload.get("themes") or [] if isinstance(theme, dict)]
        if not themes or is_placeholder(themes, payload.get("placeholder")):
            continue
        layouts.append({
            "roomid": row["roomid"], "gameindex": row["gameindex"],
            "packageid": payload.get("packageid"), "themes": themes,
            "source": "protocol", "updated": row["updated_at"],
        })
    # Бои с результатами: их темы — то, что читали за столом. План к такому
    # бою могли перепривязать задним числом, поэтому верим протоколу.
    played = set()
    try:
        for row in conn.execute("SELECT DISTINCT roomid, gameindex FROM results"):
            played.add((row[0], row[1]))
    except sqlite3.OperationalError:
        played = set()
    # Если бой есть и в плане, и в пульте — берём тот, где тем больше: так
    # сохраняется и плановая раскладка, и то, что ведущий реально играл.
    best = {}
    for layout in layouts:
        key = (layout["roomid"], layout["gameindex"])
        current = best.get(key)
        if current is None or len(layout["themes"]) > len(current["themes"]):
            best[key] = layout
            continue
        if len(layout["themes"]) != len(current["themes"]):
            continue
        if key in played:
            if layout["source"] == "protocol":
                best[key] = layout
        elif layout["source"] == "plan":
            best[key] = layout
    # Старый бой знает только названия тем. Чтобы не ловить случайные совпадения
    # (тема «Учёные» есть во многих пакетах), бой привязывается к пакету целиком:
    # названия почти всех его тем должны найтись в одном пакете. Тогда темы боя
    # опознаются по вопросам этого пакета, а не по одному названию.
    for (roomid, gameindex), game in attributed_legacy(conn).items():
        key = (roomid, gameindex)
        if key in best:
            continue
        themes, seen, libraries = [], set(), []
        for libraryid in sorted(game["libraries"]):
            libraries.append(libraryid)
            for theme in game["libraries"][libraryid]["themes"]:
                signature = theme_signature(theme)
                if signature in seen:
                    continue
                seen.add(signature)
                themes.append(theme)
        best[key] = {
            "roomid": roomid, "gameindex": gameindex, "packageid": None,
            "themes": themes, "titles": game["titles"], "libraryids": libraries,
            "source": "log", "updated": None,
        }
    return list(best.values())


def protocol_is_empty(payload):
    """Протокол ничего не описывает: ни игроков, ни отметок, ни счёта, бой не закончен.

    Так выглядит заготовка следующего боя: пульт перешёл к бою 11, темы остались
    от боя 10, места за столом ещё пустые. Сохранять такое нечего.
    """
    if payload.get("finished") or protocol_has_scores(payload):
        return False
    if payload.get("marks"):
        return False
    for player in payload.get("players") or []:
        if isinstance(player, dict) and str(player.get("name") or "").strip():
            return False
    return True


def protocol_has_scores(payload):
    """В протоколе есть счёт: бой шёл, даже если отметку «закончен» не поставили."""
    scores = payload.get("scores")
    if isinstance(scores, dict):
        values = scores.values()
    elif isinstance(scores, list):
        values = scores
    else:
        return False
    for value in values:
        try:
            if float(value):
                return True
        except (TypeError, ValueError):
            continue
    return False


def played_battles(conn, strict=False):
    """{(roomid, gameindex)} — бои, которые действительно играли.

    Поводом считать бой состоявшимся служат результаты, запись в журнале
    «save game result» или непустой протокол. Пустой протокол — заготовка
    следующего боя (темы прошлого боя, места ещё пустые), а не бой: именно
    из-за неё в истории пакета появлялся бой, которого не было.

    strict — только явные признаки: результаты, журнал, «закончен» или счёт.
    Без strict хватает и протокола с игроками или отметками: бой идёт сейчас,
    и для подсказки «эта тема уже игралась» этого достаточно.
    """
    played = set()
    try:
        for row in conn.execute("SELECT DISTINCT roomid, gameindex FROM results"):
            try:
                played.add((int(row[0]), int(row[1])))
            except (TypeError, ValueError):
                continue
    except sqlite3.OperationalError:
        pass
    for roomid, gameindex in legacy_layouts(conn):
        try:
            played.add((int(roomid), int(gameindex)))
        except (TypeError, ValueError):
            continue
    try:
        rows = conn.execute(
            "SELECT roomid, gameindex, payload FROM game_protocols").fetchall()
    except sqlite3.OperationalError:
        rows = []
    for row in rows:
        payload = load_json(row["payload"])
        if protocol_is_empty(payload):
            continue
        if strict and not (payload.get("finished") or protocol_has_scores(payload)
                           or payload.get("marks")):
            continue
        try:
            played.add((int(row["roomid"]), int(row["gameindex"])))
        except (TypeError, ValueError):
            continue
    return played


def played_layouts(conn, strict=False):
    """Раскладки состоявшихся боёв.

    В обычном режиме берём все раскладки: для подсказки «эта тема уже игралась»
    хватает и плана, и протокола — бой раскладывают прямо сейчас.

    В strict остаются только бои с результатом, записью в журнале или явными
    признаками игры (закончен, счёт, отметки). Так история пакета не показывает
    бой, которого не было: пульт мог сохранить заготовку следующего боя с темами
    прошлого и пустыми местами.
    """
    if not strict:
        return theme_layouts(conn)
    played = played_battles(conn, strict=True)
    return [layout for layout in theme_layouts(conn)
            if (layout["roomid"], layout["gameindex"]) in played]


def library_signatures(conn, libraryid):
    """Подписи всех тем, которые есть в пакетах этой записи библиотеки."""
    signatures = set()
    for row in conn.execute("SELECT data FROM packages WHERE libraryid = ?", (libraryid,)):
        for theme in package_topics(row[0]):
            signature = theme_signature(theme)
            if signature:
                signatures.add(signature)
    return signatures


def library_usage(conn, libraryid, mode=EXPOSURE_HALL):
    """История пакета: где и когда игрались его темы и кто их мог слышать.

    Старые бои попадают сюда, только если весь набор их тем опознан как темы
    этого пакета: темой считается тема с вопросами, а не совпадение названия.
    """
    signatures = library_signatures(conn, libraryid)
    marks = battle_marks(conn)
    cache = {}
    usage = []
    for layout in sorted(played_layouts(conn, strict=True),
                         key=lambda item: (item["roomid"], item["gameindex"]),
                         reverse=True):
        themes = [theme for theme in layout["themes"]
                  if theme_signature(theme) in signatures]
        if not themes:
            continue
        roomid, gameindex = layout["roomid"], layout["gameindex"]
        if roomid not in cache:
            timeline = room_timeline(conn, roomid)
            cache[roomid] = (timeline, game_times(conn, roomid, timeline[2]))
        timeline, times = cache[roomid]
        listeners = game_listeners(conn, roomid, gameindex, mode, times, timeline)
        room = conn.execute(
            "SELECT r.date, u.name, v.venuename FROM activerooms r "
            "LEFT JOIN users u ON u.userid = r.ownerid "
            "LEFT JOIN venues v ON v.venueid = r.venueid WHERE r.roomid = ?", (roomid,)
        ).fetchone()
        usage.append({
            "roomid": roomid,
            "gameindex": gameindex,
            "packageid": layout["packageid"],
            "package": layout.get("package") or library_name(conn, libraryid),
            "source": layout["source"],
            # Бой известен только по названиям тем: вопросов в его записи нет.
            "by_title": layout["source"] == "log",
            # Пометка ведущего: "own" — привязали вручную, "foreign" — убрали.
            "marked": marks.get((roomid, gameindex), {}).get(libraryid),
            "topics": [theme_title(theme) for theme in themes],
            "theme_items": [{"title": theme_title(theme), "signature": theme_signature(theme),
                             "by_title": layout["source"] == "log"} for theme in themes],
            "when": format_time(times.get(gameindex)),
            "date": room[0] if room else "",
            "owner": room[1] if room else None,
            "venue": room[2] if room else None,
            "listeners": [item["name"] for item in listeners],
            "late": [item["name"] for item in listeners if item["late"]],
        })
    return usage


def rejected_battles(conn, libraryid):
    """Бои, которые ведущий убрал из пакета: их можно вернуть одной кнопкой."""
    marks = battle_marks(conn)
    rejected = []
    for (roomid, gameindex), game in legacy_layouts(conn).items():
        if marks.get((roomid, gameindex), {}).get(libraryid) != "foreign":
            continue
        room = conn.execute(
            "SELECT r.date, u.name FROM activerooms r LEFT JOIN users u ON u.userid = r.ownerid "
            "WHERE r.roomid = ?", (roomid,)).fetchone()
        timeline = room_timeline(conn, roomid)
        times = game_times(conn, roomid, timeline[2])
        rejected.append({
            "roomid": roomid, "gameindex": gameindex,
            "topics": list(game["titles"]),
            "when": format_time(times.get(gameindex)),
            "date": room[0] if room else "",
            "owner": room[1] if room else None,
        })
    rejected.sort(key=lambda item: (item["roomid"], item["gameindex"]), reverse=True)
    return rejected


def library_name(conn, libraryid):
    row = conn.execute(
        "SELECT name FROM package_library WHERE libraryid = ?", (libraryid,)
    ).fetchone()
    return row[0] if row else ""


def place_name(roomid, gameindex):
    return "комната %s, бой %s" % (roomid, gameindex)


def played_themes(conn):
    """Все темы, которые игрались: где, какая тема и с какой подписью.

    Темы старых боёв тоже приходят с вопросами: бой сначала целиком привязан
    к пакету по названиям тем, и только потом его темы считаются темами этого
    пакета. by_title отмечает, что сама запись боя вопросов не сохранила.
    """
    items = []
    # Только состоявшиеся бои: заготовка следующего боя не делает темы сыгранными.
    for layout in played_layouts(conn):
        by_title = layout["source"] == "log"
        for theme in layout["themes"]:
            signature = theme_signature(theme)
            if signature is None:
                continue
            items.append({
                "place": place_name(layout["roomid"], layout["gameindex"]),
                "roomid": layout["roomid"],
                "gameindex": layout["gameindex"],
                "source": layout["source"],
                "by_title": by_title,
                "theme": theme,
                "title": theme_title(theme),
                "signature": signature,
                "observed": question_texts_of(theme),
                "words": frozenset(theme_vocabulary(theme)),
            })
    return items


def question_texts_of(theme):
    return tuple(theme_questions(theme))


def theme_usage(conn, libraryid=None):
    """{подпись темы: {"title": …, "places": [...]}} — где тема уже игралась.

    Тема опознаётся по своим вопросам, поэтому считается по всем комнатам и
    пакетам: если та же тема (тот же набор вопросов) игралась в другом пакете,
    она уже засвечена. libraryid оставлен для совместимости и больше не сужает.
    """
    usage = {}
    for item in played_themes(conn):
        entry = usage.setdefault(item["signature"],
                                 {"title": item["title"], "places": []})
        if item["place"] not in entry["places"]:
            entry["places"].append(item["place"])
    return usage


def similar_plays(theme, played, skip=()):
    """Где игрались темы, отличающиеся от этой только правками текста."""
    mine = theme_questions(theme)
    if not mine:
        return []
    found = []
    seen_places = set()
    mine_words = frozenset(theme_vocabulary(theme))
    # Одну и ту же тему не сравниваем по второму разу: группируем по подписи.
    unique = {}
    for item in played:
        if not item.get("signature") or item.get("theme") is None:
            # У старых боёв нет вопросов — текстом их не сравнить.
            continue
        unique.setdefault(item["signature"], []).append(item)
    for items in unique.values():
        first = items[0]
        theirs = first.get("observed") or question_texts_of(first["theme"])
        # Быстрый отсев: темы заметно разной длины не бывают одной темой.
        if abs(len(mine) - len(theirs)) > max(1, len(mine) // 3):
            continue
        words = first.get("words") or frozenset(theme_vocabulary(first["theme"]))
        if len(mine_words & words) < 0.25 * max(1, min(len(mine_words), len(words))):
            continue
        match = theme_match(theme, first["theme"])
        if not same_theme(match) or match["exact"]:
            continue
        for item in items:
            if item["place"] in skip or item["place"] in seen_places:
                continue
            seen_places.add(item["place"])
            found.append({
                "place": item["place"],
                "roomid": item["roomid"],
                "gameindex": item["gameindex"],
                "title": item["title"],
                "matched": match["matched"],
                "total": match["total"],
            })
    return found


def heard_by_themes(conn, themes, names, mode=EXPOSURE_HALL, skip=None):
    """Кто из названных игроков уже слышал эти темы и где.

    Возвращает {имя: [{topic, place, changed}]}. Мелкие правки текста тему не
    меняют: повторы с исправленным текстом тоже считаются услышанными.
    skip — места (roomid, gameindex), которые считать не надо: сам бой, для
    которого подбираются темы, слышанным не бывает.
    """
    skip = set(skip or ())
    names = [name for name in (names or []) if name]
    themes = [theme for theme in (themes or []) if isinstance(theme, dict)]
    if not names or not themes:
        return {}
    wanted = set(names)
    signatures = {theme_signature(theme) for theme in themes}
    prepared = [(theme, question_texts_of(theme), frozenset(theme_vocabulary(theme)))
                for theme in themes]

    def close_to(theme):
        """Та же тема или та же тема с поправленным текстом."""
        if theme_signature(theme) in signatures:
            return True
        texts = question_texts_of(theme)
        words = frozenset(theme_vocabulary(theme))
        for other, other_texts, other_words in prepared:
            if abs(len(texts) - len(other_texts)) > max(1, len(other_texts) // 3):
                continue
            if len(words & other_words) < 0.25 * max(1, min(len(words), len(other_words))):
                continue
            match = theme_match(other, theme)
            if same_theme(match):
                return True
        return False

    heard = {}
    cache = {}
    finished = {}

    def is_finished(roomid, gameindex):
        """Бой уже сыгран: иначе темы в нём только разложены, а не услышаны."""
        if roomid not in finished:
            _, _, ends = room_timeline(conn, roomid)
            games = set(ends)
            try:
                rows = conn.execute(
                    "SELECT DISTINCT gameindex FROM results WHERE roomid = ?", (roomid,)
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
            for row in rows:
                try:
                    games.add(int(row[0]))
                except (TypeError, ValueError):
                    continue
            finished[roomid] = games
        return gameindex in finished[roomid]

    for layout in theme_layouts(conn):
        if not is_finished(layout["roomid"], layout["gameindex"]):
            continue
        if (layout["roomid"], layout["gameindex"]) in skip:
            continue
        by_title = layout["source"] == "log"
        topics = []
        for theme in layout["themes"]:
            if not close_to(theme):
                continue
            changed = theme_signature(theme) not in signatures
            topics.append((theme_title(theme), changed, by_title))
        if not topics:
            continue
        key = (layout["roomid"], layout["gameindex"])
        if key not in cache:
            timeline = room_timeline(conn, layout["roomid"])
            times = game_times(conn, layout["roomid"], timeline[2])
            listeners = game_listeners(conn, layout["roomid"], layout["gameindex"],
                                       mode, times, timeline)
            cache[key] = [item["name"] for item in listeners]
        place = place_name(layout["roomid"], layout["gameindex"])
        for name in cache[key]:
            if name not in wanted:
                continue
            entry = heard.setdefault(name, [])
            for title, changed, by_title in topics:
                item = {"topic": title, "place": place, "changed": changed,
                        "by_title": by_title}
                if item not in entry:
                    entry.append(item)
    return heard


def room_themes(conn, roomid):
    """Темы, разложенные в комнате."""
    themes = []
    for layout in theme_layouts(conn):
        if layout["roomid"] == roomid:
            themes.extend(layout["themes"])
    return themes


def heard_before(conn, roomid, names, mode=EXPOSURE_HALL, skip=None):
    """Что из разложенного в этой комнате её игроки уже слышали."""
    return heard_by_themes(conn, room_themes(conn, roomid), names, mode, skip)


def theme_use_by_index(conn, data, libraryid=None, played=None):
    """Где игралась каждая тема пакета — по порядку тем в пакете.

    Для каждой темы два яруса совпадений: точные повторы (те же вопросы) и
    похожие темы с поправленным текстом. by_title отмечает те места, где сам
    бой вопросов не сохранил и опознан по набору названий тем.
    """
    if played is None:
        played = played_themes(conn)
    usage = {}
    for item in played:
        if item.get("signature"):
            usage.setdefault(item["signature"], []).append(item)
    result = []
    for theme in package_topics(data):
        signature = theme_signature(theme)
        exact = []
        by_title = []
        for item in (usage.get(signature) or []) if signature else []:
            if item["place"] not in exact:
                exact.append(item["place"])
                if item.get("by_title"):
                    by_title.append(item["place"])
        similar = similar_plays(theme, played, skip=exact)
        result.append({"exact": exact, "similar": similar, "by_title": by_title})
    return result


def library_listeners(conn, libraryid, mode=EXPOSURE_HALL):
    """Все, кто мог слышать хоть что-то из пакета: имя → где слышал."""
    seen = {}
    late = set()
    for item in library_usage(conn, libraryid, mode):
        for name in item["listeners"]:
            place = seen.setdefault(name, [])
            key = "комната %s, бой %s" % (item["roomid"], item["gameindex"])
            if key not in place:
                place.append(key)
        late.update(item.get("late") or [])
    return [{"name": name, "places": places, "late": name in late}
            for name, places in sorted(seen.items())]


def package_themes(conn, libraryid):
    """Темы пакета по порядку — из самой свежей копии."""
    row = conn.execute(
        "SELECT data FROM packages WHERE libraryid = ? ORDER BY packageid DESC LIMIT 1",
        (libraryid,),
    ).fetchone()
    if row is None:
        return []
    return [topic for topic in package_topics(row[0]) if isinstance(topic, dict)]


def theme_history(conn, libraryid, mode=EXPOSURE_HALL):
    """История по темам пакета: где тема игралась и кто мог её слышать."""
    package = {}
    order, seen = [], set()
    for theme in package_themes(conn, libraryid):
        signature = theme_signature(theme)
        key = signature or ("title", theme_title(theme))
        if key in seen:
            continue
        seen.add(key)
        order.append((theme_title(theme), signature))
        package[signature] = theme
    blocks = {}
    for item in library_usage(conn, libraryid, mode):
        place = (item["roomid"], item["gameindex"])
        for entry in item.get("theme_items") or []:
            signature = entry.get("signature")
            if not signature:
                continue
            used = blocks.setdefault(signature, [])
            if any((block["roomid"], block["gameindex"]) == place for block in used):
                continue
            used.append({
                "roomid": item["roomid"], "gameindex": item["gameindex"],
                "when": item["when"], "listeners": item["listeners"],
                "by_title": bool(entry.get("by_title")),
            })
    titles = {}
    for title, _ in order:
        titles[title] = titles.get(title, 0) + 1
    everywhere = theme_usage(conn)
    played = played_themes(conn)
    themes = []
    for title, signature in order:
        used = list(blocks.get(signature, [])) if signature else []
        heard = set()
        for block in used:
            heard.update(block["listeners"])
        known = {place_name(block["roomid"], block["gameindex"]) for block in used}
        elsewhere = []
        if signature:
            for place in (everywhere.get(signature) or {}).get("places", []):
                if place not in known:
                    elsewhere.append(place)
        similar = []
        theme = package.get(signature)
        if theme is not None:
            similar = similar_plays(theme, played, skip=known | set(elsewhere))
        themes.append({
            "topic": title,
            "signature": signature,
            "used": used,
            "elsewhere": elsewhere,
            "similar": similar,
            "listeners": sorted(heard),
            # Одноимённые темы с разными вопросами — разные темы.
            "same_title": titles.get(title, 0) > 1,
            # Есть бои, которые своя запись вопросов не сохранила: их опознали
            # по набору названий тем этих пакетов.
            "by_title": any(block.get("by_title") for block in used),
        })
    return themes


# ------------------------------------------------------- похожие и подтверждение

def _theme_sets(conn):
    """libraryid → набор подписей тем пакета (тема = её вопросы)."""
    sets = {}
    for row in conn.execute(
        "SELECT libraryid, data FROM packages WHERE libraryid IS NOT NULL"
    ):
        signatures = set()
        for theme in package_topics(row[1]):
            signature = theme_signature(theme)
            if signature:
                signatures.add(signature)
        if signatures:
            sets.setdefault(row[0], set()).update(signatures)
    return sets


def decided_links(conn):
    pairs = {}
    for row in conn.execute("SELECT libraryid_a, libraryid_b, status FROM package_links"):
        pairs[(row[0], row[1])] = row[2]
    return pairs


def similar_libraries(conn, libraryid):
    """Какие ещё записи библиотеки похожи на эту (для подтверждения админом)."""
    sets = _theme_sets(conn)
    mine = sets.get(libraryid) or set()
    if not mine:
        return []
    decisions = decided_links(conn)
    mine_hash = conn.execute(
        "SELECT hash FROM package_library WHERE libraryid = ?", (libraryid,)
    ).fetchone()
    mine_hash = mine_hash[0] if mine_hash else None
    result = []
    for other, theirs in sets.items():
        if other == libraryid:
            continue
        key = (min(libraryid, other), max(libraryid, other))
        if decisions.get(key) == "rejected":
            continue
        shared = mine & theirs
        if not shared:
            continue
        other_hash = conn.execute(
            "SELECT hash, name FROM package_library WHERE libraryid = ?", (other,)
        ).fetchone()
        if other_hash is None:
            continue
        same = bool(mine_hash) and mine_hash == other_hash[0]
        ratio = len(shared) / float(max(len(mine), len(theirs)))
        if not same and ratio < 0.3:
            continue
        result.append({
            "libraryid": other,
            "name": other_hash[1],
            "shared": len(shared),
            "total": len(theirs),
            "same_content": same,
            "ratio": round(ratio * 100),
            "merged": decisions.get(key) == "merged",
        })
    result.sort(key=lambda item: (not item["same_content"], -item["shared"]))
    return result


def merge_libraries(conn, target, source, author=None):
    """Подтверждение «это один пакет»: копии переносим в целевую запись."""
    if int(target) == int(source):
        raise LibraryError("Это одна и та же запись")
    target_row = conn.execute(
        "SELECT libraryid FROM package_library WHERE libraryid = ?", (target,)).fetchone()
    source_row = conn.execute(
        "SELECT libraryid FROM package_library WHERE libraryid = ?", (source,)).fetchone()
    if target_row is None or source_row is None:
        raise LibraryError("Пакета нет в библиотеке")
    moved = conn.execute(
        "UPDATE packages SET libraryid = ? WHERE libraryid = ?", (target, source)
    ).rowcount
    conn.execute("DELETE FROM package_library WHERE libraryid = ?", (source,))
    key = (min(int(target), int(source)), max(int(target), int(source)))
    conn.execute(
        "INSERT OR REPLACE INTO package_links (libraryid_a, libraryid_b, status, by) "
        "VALUES (?, ?, 'merged', ?)", (key[0], key[1], author),
    )
    return moved


def reject_link(conn, one, other, author=None):
    """«Это разные пакеты»: больше не предлагать связать их."""
    key = (min(int(one), int(other)), max(int(one), int(other)))
    conn.execute(
        "INSERT OR REPLACE INTO package_links (libraryid_a, libraryid_b, status, by) "
        "VALUES (?, ?, 'rejected', ?)", (key[0], key[1], author),
    )
    return True


def pending_links(conn):
    """Сколько пар ждут решения администратора."""
    decisions = decided_links(conn)
    seen = set()
    for row in conn.execute(
        "SELECT DISTINCT libraryid FROM packages WHERE libraryid IS NOT NULL"
    ):
        libraryid = row[0]
        for item in similar_libraries(conn, libraryid):
            seen.add((min(libraryid, item["libraryid"]), max(libraryid, item["libraryid"])))
    return sum(1 for key in seen if decisions.get(key) != "merged")
