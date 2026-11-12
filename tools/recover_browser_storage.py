# -*- coding: utf-8 -*-
"""Достаёт прежние версии состояния матчей из хранилища браузера.

Браузер не стирает старое значение сразу: Chrome и Edge пишут Local Storage в
LevelDB и дописывают новые записи в журнал, Firefox держит их в
webappsstore.sqlite. Поэтому сброс в интерфейсе не обязательно означает, что
прежняя расплюсовка пропала с диска.

Что подавать на вход:

    Chrome/Edge: папку  ...\\User Data\\Default\\Local Storage\\leveldb
    Firefox:     файл   ...\\Profiles\\xxxx.default-release\\webappsstore.sqlite

Пример:

    python tools/recover_browser_storage.py --path "C:\\...\\leveldb" --room 481 --game 8
    python tools/recover_browser_storage.py --path webappsstore.sqlite --room 481 --game 8 --dump out.json
"""

import argparse
import json
import os
import sqlite3
import struct
import sys
import zlib

try:
    sys.stdout.reconfigure(encoding='utf-8')
except (AttributeError, ValueError):
    pass

BLOCK_SIZE = 32768
HEADER_SIZE = 7
FULL, FIRST, MIDDLE, LAST = 1, 2, 3, 4


def read_leveldb_log(path):
    """Логические записи журнала LevelDB: склеиваем куски по заголовкам."""
    with open(path, 'rb') as handle:
        blob = handle.read()
    records, current = [], b''
    position = 0
    while position + HEADER_SIZE <= len(blob):
        block_end = min(len(blob), (position // BLOCK_SIZE + 1) * BLOCK_SIZE)
        header = blob[position:position + HEADER_SIZE]
        if len(header) < HEADER_SIZE:
            break
        crc, length, kind = struct.unpack('<IHB', header)
        if length == 0 and kind == 0:
            # добивка до конца блока
            position = block_end
            continue
        payload = blob[position + HEADER_SIZE:position + HEADER_SIZE + length]
        position += HEADER_SIZE + length
        if kind == FULL:
            records.append(payload)
            current = b''
        elif kind == FIRST:
            current = payload
        elif kind == MIDDLE:
            current += payload
        elif kind == LAST:
            current += payload
            records.append(current)
            current = b''
    return records


def json_candidates(text, limit=40):
    """Все JSON-объекты, которые удаётся разобрать в строке."""
    decoder = json.JSONDecoder()
    found = []
    position = text.find('{"')
    attempts = 0
    while position != -1 and attempts < limit:
        attempts += 1
        try:
            value, end = decoder.raw_decode(text[position:])
        except ValueError:
            value = None
        if isinstance(value, dict):
            found.append(value)
        position = text.find('{"', position + 1)
    return found


def matches_from_value(value, room, game):
    """Достаёт состояние нужного матча из объекта tournament_matches."""
    if not isinstance(value, dict):
        return None
    room_data = value.get(str(room))
    if not isinstance(room_data, dict):
        return None
    item = room_data.get(str(game))
    if not isinstance(item, dict):
        return None
    return item


def filled_count(item):
    answers = ((item or {}).get('gameState') or {}).get('answers') or []
    total = 0
    for row in answers:
        for mark in row or []:
            if mark is True or mark is False:
                total += 1
    return total


def grid_from_item(item):
    """Сетка ответов: список (тема, номинал, [плюс/минус/пусто])."""
    state = (item or {}).get('gameState') or {}
    data = (item or {}).get('gameData') or {}
    players = data.get('players') or []
    rows = []
    index = 0
    for block in data.get('questionBlocks') or []:
        for question in block.get('questions') or []:
            marks = (state.get('answers') or [None] * (index + 1))[index] if index < len(state.get('answers') or []) else None
            rows.append((block.get('theme'), question.get('value'), marks))
            index += 1
    return players, rows


def print_grid(item, indent='   '):
    players, rows = grid_from_item(item)
    if not rows:
        print(indent + 'сетки нет')
        return
    short = [str(name).split()[0][:6] for name in players]
    header = indent + '%-3s %-26s %-5s ' % ('#', 'тема', 'ном.') + ' '.join('%-7s' % s for s in short)
    print(header)
    print(indent + '-' * (len(header) - len(indent)))
    totals = [0] * len(players)
    for number, (theme, value, marks) in enumerate(rows, 1):
        cells = []
        for position in range(len(players)):
            mark = (marks or [])[position] if position < len(marks or []) else None
            if mark is True:
                cells.append('+%-6d' % (value or 0))
                totals[position] += value or 0
            elif mark is False:
                cells.append('-%-6d' % (value or 0))
                totals[position] -= value or 0
            else:
                cells.append('·      ')
        print(indent + '%-3d %-26s %-5s ' % (number, str(theme)[:26], value) + ' '.join(cells))
    print(indent + '-' * (len(header) - len(indent)))
    print(indent + '%-3s %-26s %-5s ' % ('', 'итого', '') + ' '.join('%-7d' % t for t in totals))


def scan_sqlite(path, room, game):
    """Firefox: текущие строки и то, что осталось в свободных страницах."""
    versions = []
    conn = sqlite3.connect('file:%s?mode=ro' % path.replace('\\', '/'), uri=True)
    conn.row_factory = sqlite3.Row
    tables = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'webappsstore%'")]
    for table in tables:
        try:
            rows = conn.execute('SELECT * FROM %s' % table).fetchall()
        except sqlite3.Error:
            continue
        for row in rows:
            raw = row['value'] if 'value' in row.keys() else None
            if not isinstance(raw, (str, bytes)):
                continue
            text = raw.decode('utf-8', 'ignore') if isinstance(raw, bytes) else raw
            for value in json_candidates(text):
                item = matches_from_value(value, room, game)
                if item is not None:
                    versions.append(('таблица ' + table, item))
    conn.close()

    # Свободные страницы и журналы: значения, которые браузер уже перезаписал.
    extra = [path, path + '-wal', path + '-journal', path + '-shm']
    for extra_path in extra:
        if not os.path.exists(extra_path):
            continue
        with open(extra_path, 'rb') as handle:
            blob = handle.read()
        for text in (blob.decode('utf-8', 'ignore'), blob.decode('utf-16-le', 'ignore')):
            for value in json_candidates(text, limit=400):
                item = matches_from_value(value, room, game)
                if item is not None:
                    versions.append((os.path.basename(extra_path), item))
    return versions


def scan_leveldb(directory, room, game):
    """Chrome/Edge: журналы и таблицы LevelDB."""
    versions = []
    files = []
    for folder, _, names in os.walk(directory):
        for name in sorted(names):
            if name.endswith(('.log', '.ldb', '.sst')):
                files.append(os.path.join(folder, name))
    for path in files:
        if path.endswith('.log'):
            chunks = read_leveldb_log(path)
        else:
            with open(path, 'rb') as handle:
                chunks = [handle.read()]
        for chunk in chunks:
            for text in (chunk.decode('utf-16-le', 'ignore'), chunk.decode('utf-8', 'ignore')):
                for value in json_candidates(text):
                    item = matches_from_value(value, room, game)
                    if item is not None:
                        versions.append((os.path.basename(path), item))
    return versions


def main():
    parser = argparse.ArgumentParser(description='Поиск прежних версий матчей в хранилище браузера')
    parser.add_argument('--path', required=True, help='папка leveldb или файл webappsstore.sqlite')
    parser.add_argument('--room', type=int, required=True)
    parser.add_argument('--game', type=int, required=True)
    parser.add_argument('--dump', help='куда сохранить найденные версии (JSON)')
    args = parser.parse_args()

    if not os.path.exists(args.path):
        print('Нет такого пути: %s' % args.path)
        return 2

    if os.path.isdir(args.path):
        versions = scan_leveldb(args.path, args.room, args.game)
    else:
        versions = scan_sqlite(args.path, args.room, args.game)

    if not versions:
        print('Ничего не нашлось: похоже, старые записи уже уплотнены.')
        return 1

    # Разные файлы могут содержать одни и те же записи — убираем повторы.
    unique, seen = [], set()
    for source, item in versions:
        key = json.dumps(item, sort_keys=True, ensure_ascii=False)
        if key in seen:
            continue
        seen.add(key)
        unique.append((source, item))
    unique.sort(key=lambda pair: filled_count(pair[1]), reverse=True)

    print('Найдено версий: %d' % len(unique))
    for number, (source, item) in enumerate(unique, 1):
        print()
        print('=' * 78)
        print('Версия %d (%s): сохранена %s, заполнено ответов: %d' % (
            number, source, item.get('timestamp'), filled_count(item)))
        print_grid(item)

    if args.dump:
        with open(args.dump, 'w', encoding='utf-8') as handle:
            json.dump([{'source': source, 'item': item} for source, item in unique],
                      handle, ensure_ascii=False, indent=1)
        print('\nСохранено в %s' % args.dump)
    return 0


if __name__ == '__main__':
    sys.exit(main())
