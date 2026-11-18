"""Библиотека пакетов: одна и та же колода в разных комнатах — один пакет.

Пакет по-прежнему хранится в комнате: содержимое видит только владелец комнаты
и администратор. Поверх этого библиотека связывает одинаковые пакеты из разных
комнат в одну запись, чтобы видеть всю историю: в каких боях игрались темы,
когда и кто мог их слышать.

Связь — по содержимому: загрузили тот же файл в другую комнату, и это тот же
пакет. Похожие, но не совпадающие пакеты библиотека показывает
администратору как кандидатов: подтвердить, что это один пакет, может он.
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
    conn.execute(
        "CREATE TABLE IF NOT EXISTS package_links ("
        " libraryid_a INTEGER NOT NULL,"
        " libraryid_b INTEGER NOT NULL,"
        " status TEXT NOT NULL,"
        " by TEXT,"
        " created_at TEXT DEFAULT CURRENT_TIMESTAMP,"
        " PRIMARY KEY (libraryid_a, libraryid_b))"
    )
    if has_table(conn, "packages") and "libraryid" not in table_columns(conn, "packages"):
        conn.execute("ALTER TABLE packages ADD COLUMN libraryid INTEGER")
        changed = True
    changed = backfill(conn) or changed
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
    conn.execute("UPDATE packages SET libraryid = ? WHERE packageid = ?", (libraryid, packageid))
    return libraryid


# ---------------------------------------------------------------- просмотр

def visible_clause(ownerid):
    """Пакет видит его владелец и тот, у кого в комнате этот пакет лежит."""
    return (
        "(l.ownerid = ? OR EXISTS (SELECT 1 FROM packages p "
        "JOIN activerooms r ON r.roomid = p.roomid "
        "WHERE p.libraryid = l.libraryid AND r.ownerid = ?))"
    )


def library_rows(conn, ownerid=None, query=None, limit=200):
    """Список пакетов библиотеки: сколько комнат, боёв и тем."""
    sql = (
        "SELECT l.libraryid, l.name, l.ownerid, l.hash, "
        "(SELECT COUNT(*) FROM packages p WHERE p.libraryid = l.libraryid) AS copies, "
        "(SELECT COUNT(DISTINCT p.roomid) FROM packages p "
        " WHERE p.libraryid = l.libraryid) AS rooms, "
        "(SELECT COUNT(*) FROM game_questions gq JOIN packages p "
        " ON p.packageid = gq.packageid WHERE p.libraryid = l.libraryid) AS games "
        "FROM package_library l "
    )
    where, params = [], []
    if ownerid is not None:
        where.append(visible_clause(ownerid))
        params.extend([ownerid, ownerid])
    if query:
        where.append("l.name LIKE ?")
        params.append("%" + query + "%")
    if where:
        sql += "WHERE " + " AND ".join(where) + " "
    sql += "ORDER BY l.libraryid DESC LIMIT ?"
    params.append(limit)
    rows = []
    for row in conn.execute(sql, params):
        item = {
            "libraryid": row[0], "name": row[1], "ownerid": row[2], "hash": row[3],
            "copies": row[4], "rooms": row[5], "games": row[6], "topics": 0,
            "updated": "",
        }
        first = conn.execute(
            "SELECT packageid, data, updated_at, created_at FROM packages "
            "WHERE libraryid = ? ORDER BY packageid DESC LIMIT 1", (item["libraryid"],)
        ).fetchone()
        if first is not None:
            item["packageid"] = first[0]
            item["topics"] = len(package_topics(first[1]))
            item["updated"] = (first[2] or first[3] or "")[:16]
        rows.append(item)
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
    # Если бой есть и в плане, и в пульте — берём тот, где тем больше: так
    # сохраняется и плановая раскладка, и то, что ведущий реально играл.
    best = {}
    for layout in layouts:
        key = (layout["roomid"], layout["gameindex"])
        current = best.get(key)
        if current is None or len(layout["themes"]) > len(current["themes"]):
            best[key] = layout
        elif len(layout["themes"]) == len(current["themes"]) and layout["source"] == "plan":
            best[key] = layout
    # Старые бои известны только по названиям тем — и только там, где раскладки
    # в плане или в пульте нет: иначе знание о вопросах точнее.
    for (roomid, gameindex), game in legacy_layouts(conn).items():
        key = (roomid, gameindex)
        if key in best or not game["titles"]:
            continue
        best[key] = {
            "roomid": roomid, "gameindex": gameindex, "packageid": None,
            "themes": [], "titles": game["titles"],
            "source": "log", "updated": None,
        }
    return list(best.values())


def library_signatures(conn, libraryid):
    """Подписи всех тем, которые есть в пакетах этой записи библиотеки."""
    signatures = set()
    for row in conn.execute("SELECT data FROM packages WHERE libraryid = ?", (libraryid,)):
        for theme in package_topics(row[0]):
            signature = theme_signature(theme)
            if signature:
                signatures.add(signature)
    return signatures


def library_titles(conn, libraryid):
    """Названия тем пакетов этой записи библиотеки — для старых боёв."""
    titles = set()
    for row in conn.execute("SELECT data FROM packages WHERE libraryid = ?", (libraryid,)):
        for theme in package_topics(row[0]):
            key = theme_title_key(theme_title(theme))
            if key:
                titles.add(key)
    return titles


def library_usage(conn, libraryid, mode=EXPOSURE_HALL):
    """История пакета: где и когда игрались его темы и кто их мог слышать."""
    signatures = library_signatures(conn, libraryid)
    titles = library_titles(conn, libraryid)
    cache = {}
    usage = []
    for layout in sorted(theme_layouts(conn),
                         key=lambda item: (item["roomid"], item["gameindex"]),
                         reverse=True):
        themes = [theme for theme in layout["themes"]
                  if theme_signature(theme) in signatures]
        # Старые бои: вопросов в них нет, узнаём тему только по названию.
        legacy = [title for title in layout.get("titles") or []
                  if theme_title_key(title) in titles]
        if not themes and not legacy:
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
            "by_title": not themes,
            "topics": [theme_title(theme) for theme in themes] + legacy,
            "theme_items": [{"title": theme_title(theme), "signature": theme_signature(theme),
                             "by_title": False} for theme in themes] +
                           [{"title": title, "signature": None,
                             "by_title": True} for title in legacy],
            "when": format_time(times.get(gameindex)),
            "date": room[0] if room else "",
            "owner": room[1] if room else None,
            "venue": room[2] if room else None,
            "listeners": [item["name"] for item in listeners],
            "late": [item["name"] for item in listeners if item["late"]],
        })
    return usage


def library_name(conn, libraryid):
    row = conn.execute(
        "SELECT name FROM package_library WHERE libraryid = ?", (libraryid,)
    ).fetchone()
    return row[0] if row else ""


def place_name(roomid, gameindex):
    return "комната %s, бой %s" % (roomid, gameindex)


def played_themes(conn):
    """Все темы, которые раскладывались по боям: где, что и с какой подписью.

    У старых боёв известны только названия тем: у таких записей нет ни вопросов,
    ни подписи, зато есть ключ названия — с ним тема опознаётся по имени.
    """
    items = []
    for layout in theme_layouts(conn):
        for theme in layout["themes"]:
            signature = theme_signature(theme)
            if signature is None:
                continue
            items.append({
                "place": place_name(layout["roomid"], layout["gameindex"]),
                "roomid": layout["roomid"],
                "gameindex": layout["gameindex"],
                "source": layout["source"],
                "theme": theme,
                "title": theme_title(theme),
                "signature": signature,
                "title_key": None,
                "observed": question_texts_of(theme),
                "words": frozenset(theme_vocabulary(theme)),
            })
        for title in layout.get("titles") or []:
            key = theme_title_key(title)
            if not key:
                continue
            items.append({
                "place": place_name(layout["roomid"], layout["gameindex"]),
                "roomid": layout["roomid"],
                "gameindex": layout["gameindex"],
                "source": layout["source"],
                "theme": None,
                "title": title,
                "signature": None,
                "title_key": key,
                "observed": (),
                "words": frozenset(),
            })
    return items


def question_texts_of(theme):
    return tuple(theme_questions(theme))


def played_item_key(item):
    """Ключ записи о раскладке: вопросы, если известны, иначе название."""
    if item.get("signature"):
        return ("sig", item["signature"])
    return ("title", item.get("title_key") or theme_title_key(item.get("title")))


def theme_usage(conn, libraryid=None):
    """{(«sig», подпись) или («title», название): {"title": …, "places": [...]}}.

    Тема опознаётся по своим вопросам, поэтому считается по всем комнатам и
    пакетам: если та же тема (тот же набор вопросов) игралась в другом пакете,
    она уже засвечена. У старых боёв вопросов нет — такие записи лежат под
    ключом названия. libraryid оставлен для совместимости и больше не сужает.
    """
    usage = {}
    for item in played_themes(conn):
        entry = usage.setdefault(played_item_key(item),
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


def heard_by_themes(conn, themes, names, mode=EXPOSURE_HALL):
    """Кто из названных игроков уже слышал эти темы и где.

    Возвращает {имя: [{topic, place, changed}]}. Мелкие правки текста тему не
    меняют: повторы с исправленным текстом тоже считаются услышанными.
    """
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

    wanted_titles = {theme_title_key(theme_title(theme)) for theme in themes}
    for layout in theme_layouts(conn):
        if not is_finished(layout["roomid"], layout["gameindex"]):
            continue
        topics = []
        for theme in layout["themes"]:
            if not close_to(theme):
                continue
            changed = theme_signature(theme) not in signatures
            topics.append((theme_title(theme), changed, False))
        # Старые бои: вопросов нет, тема узнаётся только по названию.
        for title in layout.get("titles") or []:
            if theme_title_key(title) in wanted_titles:
                topics.append((title, False, True))
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


def heard_before(conn, roomid, names, mode=EXPOSURE_HALL):
    """Что из разложенного в этой комнате её игроки уже слышали."""
    return heard_by_themes(conn, room_themes(conn, roomid), names, mode)


def theme_use_by_index(conn, data, libraryid=None, played=None):
    """Где игралась каждая тема пакета — по порядку тем в пакете.

    Для каждой темы три яруса совпадений: точные повторы, темы с поправленным
    текстом и старые бои, где от темы осталось одно название.
    """
    if played is None:
        played = played_themes(conn)
    usage = {}
    by_title = {}
    for item in played:
        if item.get("signature"):
            usage.setdefault(item["signature"], []).append(item)
        elif item.get("title_key"):
            by_title.setdefault(item["title_key"], []).append(item)
    result = []
    for theme in package_topics(data):
        signature = theme_signature(theme)
        exact = []
        for item in (usage.get(signature) or []) if signature else []:
            if item["place"] not in exact:
                exact.append(item["place"])
        similar = similar_plays(theme, played, skip=exact)
        known = set(exact) | {item["place"] for item in similar}
        titles = []
        for item in by_title.get(theme_title_key(theme_title(theme)), []):
            if item["place"] in known or item["place"] in titles:
                continue
            titles.append(item["place"])
        result.append({"exact": exact, "similar": similar, "title": titles})
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
            keys = []
            if entry.get("signature"):
                keys.append(("sig", entry["signature"]))
            elif entry.get("by_title"):
                # Название заменяет вопросы только там, где вопросов нет:
                # у одноимённых тем из пакета вопросы разные.
                title_key = theme_title_key(entry.get("title"))
                if title_key:
                    keys.append(("title", title_key))
            for key in keys:
                used = blocks.setdefault(key, [])
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
        used = list(blocks.get(("sig", signature), [])) if signature else []
        seen_places = {(block["roomid"], block["gameindex"]) for block in used}
        for block in blocks.get(("title", theme_title_key(title)), []):
            if (block["roomid"], block["gameindex"]) in seen_places:
                continue
            used.append(dict(block, by_title=True))
        heard = set()
        for block in used:
            heard.update(block["listeners"])
        known = {place_name(block["roomid"], block["gameindex"]) for block in used}
        elsewhere = []
        if signature:
            for place in (everywhere.get(("sig", signature)) or {}).get("places", []):
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
            # Есть бои, где тему узнали только по названию: вопросов там нет.
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
