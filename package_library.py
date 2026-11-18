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
import hashlib
import json
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


def question_texts(data):
    """Тексты вопросов пакета: по ним ищем пакеты, где вопросы пересекаются."""
    texts = set()
    for topic in package_topics(data):
        if not isinstance(topic, dict):
            continue
        for question in topic.get("questions") or []:
            if isinstance(question, dict):
                text = (question.get("text") or "").strip().lower()
                if text:
                    texts.add(" ".join(text.split()))
    return texts


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


def library_usage(conn, libraryid, mode=EXPOSURE_HALL):
    """История пакета: где и когда игрались темы и кто их мог слышать."""
    rows = conn.execute(
        "SELECT gq.roomid, gq.gameindex, gq.payload, gq.updated_at, p.packageid, p.name "
        "FROM game_questions gq JOIN packages p ON p.packageid = gq.packageid "
        "WHERE p.libraryid = ? ORDER BY gq.roomid DESC, gq.gameindex", (libraryid,)
    ).fetchall()
    cache = {}
    usage = []
    for row in rows:
        roomid, gameindex = row[0], row[1]
        try:
            payload = json.loads(row[2] or "{}")
        except ValueError:
            payload = {}
        topics = []
        for theme in payload.get("themes") or []:
            if isinstance(theme, dict):
                topics.append(theme.get("topic") or "без названия")
            elif isinstance(theme, str):
                topics.append(theme)
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
            "packageid": row[4],
            "package": row[5],
            "topics": topics,
            "when": format_time(times.get(gameindex)),
            "date": room[0] if room else "",
            "owner": room[1] if room else None,
            "venue": room[2] if room else None,
            "listeners": [item["name"] for item in listeners],
            "late": [item["name"] for item in listeners if item["late"]],
        })
    return usage


def theme_usage(conn, libraryid):
    """{тема: [«комната X, бой Y»]} — где тема уже игралась, без подсчёта слышавших."""
    usage = {}
    rows = conn.execute(
        "SELECT gq.roomid, gq.gameindex, gq.payload FROM game_questions gq "
        "JOIN packages p ON p.packageid = gq.packageid WHERE p.libraryid = ?",
        (libraryid,),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row[2] or "{}")
        except ValueError:
            continue
        place = "комната %s, бой %s" % (row[0], row[1])
        for theme in payload.get("themes") or []:
            topic = theme.get("topic") if isinstance(theme, dict) else theme
            if not topic:
                continue
            places = usage.setdefault(topic, [])
            if place not in places:
                places.append(place)
    return usage


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
    """Темы пакета по порядку: из самой свежей копии."""
    row = conn.execute(
        "SELECT data FROM packages WHERE libraryid = ? ORDER BY packageid DESC LIMIT 1",
        (libraryid,),
    ).fetchone()
    if row is None:
        return []
    topics = []
    for topic in package_topics(row[0]):
        if isinstance(topic, dict) and topic.get("topic"):
            topics.append(topic["topic"])
    return topics


def theme_history(conn, libraryid, mode=EXPOSURE_HALL):
    """История по темам: что игралось, где и кто слышал. Темы — все темы пакета."""
    order = package_themes(conn, libraryid)
    blocks = {}
    for item in library_usage(conn, libraryid, mode):
        for topic in item["topics"]:
            if topic not in order:
                order.append(topic)
            blocks.setdefault(topic, []).append({
                "roomid": item["roomid"], "gameindex": item["gameindex"],
                "when": item["when"], "listeners": item["listeners"],
            })
    themes = []
    for topic in order:
        used = blocks.get(topic, [])
        heard = set()
        for block in used:
            heard.update(block["listeners"])
        themes.append({"topic": topic, "used": used, "listeners": sorted(heard)})
    return themes


# ------------------------------------------------------- похожие и подтверждение

def _question_sets(conn):
    """libraryid → набор текстов вопросов."""
    sets = {}
    for row in conn.execute(
        "SELECT libraryid, data FROM packages WHERE libraryid IS NOT NULL"
    ):
        texts = question_texts(row[1])
        if texts:
            sets.setdefault(row[0], set()).update(texts)
    return sets


def decided_links(conn):
    pairs = {}
    for row in conn.execute("SELECT libraryid_a, libraryid_b, status FROM package_links"):
        pairs[(row[0], row[1])] = row[2]
    return pairs


def similar_libraries(conn, libraryid):
    """Какие ещё записи библиотеки похожи на эту (для подтверждения админом)."""
    sets = _question_sets(conn)
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
