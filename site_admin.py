"""Инструменты администратора сайта: удаление, перенос номеров, резервные копии комнат.

Комната в этой базе — это не одна строка: правила, состав, результаты,
протоколы, журнал, вопросы и назначения ведущих лежат в разных таблицах с
ключом roomid. Поэтому здесь собраны операции, которые трогают их все разом.

Транзакцию открывает и закрывает вызывающий код: функции только меняют данные
в переданном соединении, чтобы удаление и перенос номера были атомарными.
"""

import datetime
import json
import os
import sqlite3

# Все таблицы, где есть roomid. Порядок важен при восстановлении: сначала
# комната и правила, потом всё, что на них ссылается.
ROOM_TABLES = (
    "activerooms",
    "roomhistory",
    "rules",
    "activeplayers",
    "results",
    "log",
    "area_hosts",
    "packages",
    "game_questions",
    "game_protocols",
    "game_protocol_history",
    "game_protocol_conflicts",
    "game_overrides",
    "tournament_seed",
)

# У этих таблиц id выдаёт сама база: при восстановлении переносим строки без id,
# иначе восстановленная комната перетёрла бы чужую запись.
GENERATED_IDS = ("game_protocol_history", "game_protocol_conflicts")


class AdminError(Exception):
    """Операция не выполнена: текст пригоден для показа администратору."""


def has_table(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone() is not None


def table_columns(conn, table):
    return [row[1] for row in conn.execute("PRAGMA table_info(%s)" % table)]


def select_rows(conn, sql, params=()):
    """Строки как словари — независимо от row_factory соединения."""
    cursor = conn.execute(sql, params)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, values)) for values in cursor.fetchall()]


def room_taken(conn, roomid):
    for table in ("activerooms", "roomhistory"):
        if not has_table(conn, table):
            continue
        row = conn.execute(
            "SELECT 1 FROM %s WHERE roomid = ?" % table, (roomid,)
        ).fetchone()
        if row is not None:
            return True
    return False


def next_roomid(conn):
    """Следующий свободный номер: максимальный по живым и архивным комнатам."""
    best = 0
    for table in ("activerooms", "roomhistory"):
        if not has_table(conn, table):
            continue
        row = conn.execute("SELECT MAX(roomid) FROM %s" % table).fetchone()
        if row is not None and row[0] is not None:
            best = max(best, int(row[0]))
    return best + 1


def room_dump(conn, roomid):
    """Всё, что относится к комнате, одним словарём для резервной копии."""
    dump = {
        "version": 1,
        "roomid": int(roomid),
        "created_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "tables": {},
    }
    for table in ROOM_TABLES:
        if not has_table(conn, table):
            continue
        dump["tables"][table] = select_rows(
            conn, "SELECT * FROM %s WHERE roomid = ?" % table, (roomid,)
        )
    if not dump["tables"].get("activerooms"):
        raise AdminError("Комнаты %s нет" % roomid)
    return dump


def delete_room(conn, roomid):
    """Удаляет все записи комнаты. Возвращает число удалённых строк по таблицам."""
    removed = {}
    for table in ROOM_TABLES:
        if not has_table(conn, table):
            continue
        cursor = conn.execute("DELETE FROM %s WHERE roomid = ?" % table, (roomid,))
        if cursor.rowcount:
            removed[table] = cursor.rowcount
    if not removed:
        raise AdminError("Комнаты %s нет" % roomid)
    return removed


def renumber_room(conn, roomid, new_roomid):
    """Переносит комнату на другой номер вместе со всеми записями."""
    if int(roomid) == int(new_roomid):
        raise AdminError("Новый номер совпадает со старым")
    if room_taken(conn, new_roomid):
        raise AdminError("Номер %s занят" % new_roomid)
    moved = {}
    for table in ROOM_TABLES:
        if not has_table(conn, table):
            continue
        cursor = conn.execute(
            "UPDATE %s SET roomid = ? WHERE roomid = ?" % table, (new_roomid, roomid)
        )
        if cursor.rowcount:
            moved[table] = cursor.rowcount
    if not moved:
        raise AdminError("Комнаты %s нет" % roomid)
    return moved


def next_packageid(conn):
    row = conn.execute("SELECT COALESCE(MAX(packageid), 0) + 1 FROM packages").fetchone()
    return int(row[0])


def restore_room(conn, dump, roomid=None):
    """Возвращает комнату из резервной копии. Номер — указанный или исходный."""
    tables = dump.get("tables") or {}
    source = int(dump.get("roomid") or 0)
    target = int(roomid) if roomid else source
    if not target:
        raise AdminError("В копии не указан номер комнаты")
    if room_taken(conn, target):
        raise AdminError("Номер %s занят: укажите другой" % target)
    if not tables.get("activerooms"):
        raise AdminError("В копии нет данных комнаты")

    package_map = {}
    for table in ROOM_TABLES:
        rows = tables.get(table) or []
        if not rows or not has_table(conn, table):
            continue
        columns = set(table_columns(conn, table))
        skip = {"roomid"}
        if table in GENERATED_IDS:
            skip.add("id")
        for row in rows:
            values = {key: value for key, value in row.items()
                      if key in columns and key not in skip}
            values["roomid"] = target
            if table == "packages":
                old_id = row.get("packageid")
                new_id = old_id
                if new_id is not None and conn.execute(
                    "SELECT 1 FROM packages WHERE packageid = ?", (new_id,)
                ).fetchone() is not None:
                    new_id = next_packageid(conn)
                values["packageid"] = new_id
                package_map[old_id] = new_id
            if table == "game_questions" and row.get("packageid") is not None:
                values["packageid"] = package_map.get(row["packageid"], row["packageid"])
            names = ", ".join(values)
            marks = ", ".join("?" for _ in values)
            conn.execute(
                "INSERT INTO %s (%s) VALUES (%s)" % (table, names, marks),
                list(values.values()),
            )
    return target


def backup_dir(db_path):
    """Резервные копии комнат лежат рядом с базой, вне её самой."""
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "backups", "rooms")


def save_dump(db_path, dump, author=None):
    folder = backup_dir(db_path)
    os.makedirs(folder, exist_ok=True)
    if author:
        dump["deleted_by"] = author
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    name = "room-%s-%s.json" % (dump["roomid"], stamp)
    path = os.path.join(folder, name)
    counter = 1
    while os.path.exists(path):
        name = "room-%s-%s-%d.json" % (dump["roomid"], stamp, counter)
        path = os.path.join(folder, name)
        counter += 1
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(dump, handle, ensure_ascii=False, indent=1)
    return path


def backup_path(db_path, name):
    """Путь к копии по имени файла: выйти за пределы каталога нельзя."""
    folder = os.path.abspath(backup_dir(db_path))
    path = os.path.abspath(os.path.join(folder, name))
    if os.path.dirname(path) != folder or not os.path.isfile(path):
        raise AdminError("Резервной копии %s нет" % name)
    return path


def load_dump(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as error:
        raise AdminError("Копия не читается: %s" % error)
    if not isinstance(data, dict) or "tables" not in data:
        raise AdminError("Это не резервная копия комнаты")
    return data


def list_backups(db_path):
    folder = backup_dir(db_path)
    if not os.path.isdir(folder):
        return []
    items = []
    for name in os.listdir(folder):
        if not name.endswith(".json"):
            continue
        path = os.path.join(folder, name)
        try:
            stat = os.stat(path)
            data = load_dump(path)
        except (OSError, AdminError):
            continue
        items.append({
            "name": name,
            "roomid": data.get("roomid"),
            "created_at": data.get("created_at") or "",
            "deleted_by": data.get("deleted_by") or "",
            "size": stat.st_size,
            "modified": datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%d.%m.%Y %H:%M"),
            "stamp": stat.st_mtime,
            "counts": {table: len(rows) for table, rows in (data.get("tables") or {}).items()},
        })
    items.sort(key=lambda item: item["stamp"], reverse=True)
    return items
