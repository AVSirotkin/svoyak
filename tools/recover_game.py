# -*- coding: utf-8 -*-
"""Что осталось от боя после случайного сброса и как это вернуть.

Читает базу только на чтение, если не передан --yes. Три источника:

1. log — каждое сохранение боя пишет туда полную таблицу очков;
2. game_protocol_history — снимки протокола, которые затёрла следующая запись
   (появляется после обновления сайта);
3. сам файл базы — SQLite не всегда затирает старые страницы, поэтому прежняя
   расплюсовка часто остаётся в файле и находится побайтовым поиском.

Примеры:

    python tools/recover_game.py --db svoyak.db --recent
    python tools/recover_game.py --db svoyak.db --room 2031 --game 5
    python tools/recover_game.py --db svoyak.db --room 2031 --game 5 --scan
    python tools/recover_game.py --db svoyak.db --room 2031 --game 5 --apply-marks scan:2 --yes
    python tools/recover_game.py --db svoyak.db --room 2031 --game 5 --apply-scores 7230 --yes
"""

import argparse
import datetime
import json
import os
import sqlite3
import sys

try:  # консоль Windows иначе ругается на кириллицу
    sys.stdout.reconfigure(encoding='utf-8')
except (AttributeError, ValueError):
    pass


def connect(path, readonly=True):
    if readonly:
        conn = sqlite3.connect('file:%s?mode=ro' % path.replace('\\', '/'), uri=True)
    else:
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def has_table(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def parse_json(raw):
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def room_players(conn, roomid):
    """Игроки комнаты: playerid -> имя."""
    rows = conn.execute(
        "SELECT p.playerid, p.name FROM activeplayers a "
        "JOIN players p ON p.playerid = a.playerid WHERE a.roomid = ?",
        (roomid,),
    ).fetchall()
    return {int(row['playerid']): row['name'] for row in rows}


def marks_summary(payload, players):
    """Плюсы и минусы по игрокам из протокола."""
    marks = (payload or {}).get('marks') or {}
    plus, minus = {}, {}
    for key, value in marks.items():
        try:
            _, playerid = key.split(':')
            playerid = int(playerid)
        except (ValueError, AttributeError):
            continue
        if value == 'plus':
            plus[playerid] = plus.get(playerid, 0) + 1
        elif value == 'minus':
            minus[playerid] = minus.get(playerid, 0) + 1
    ids = sorted(set(plus) | set(minus))
    return [(players.get(pid, 'игрок %s' % pid), plus.get(pid, 0), minus.get(pid, 0)) for pid in ids]


def print_marks(payload, players, indent='   '):
    summary = marks_summary(payload, players)
    if not summary:
        print(indent + 'отметок нет')
        return
    marks = (payload or {}).get('marks') or {}
    print(indent + 'отметок: %d, указатель: %s' % (len(marks), (payload or {}).get('pointer')))
    for name, plus, minus in summary:
        print(indent + '  %-28s +%-3d −%-3d' % (name[:28], plus, minus))


def print_scores(scores, indent='   '):
    for item in scores:
        print(indent + '  %-28s %s' % (str(item.get('name'))[:28], item.get('score')))


def log_entries(conn, roomid, gameindex):
    """События лога по этому бою, старые сверху."""
    rows = conn.execute(
        "SELECT rowid, date, event, data FROM log WHERE roomid = ? ORDER BY rowid",
        (roomid,),
    ).fetchall()
    result = []
    for row in rows:
        data = parse_json(row['data'])
        if data is None:
            continue
        if 'gameindex' in data and int(data.get('gameindex') or 0) != int(gameindex):
            continue
        if 'gameindex' not in data and row['event'] != 'protocol restored':
            continue
        result.append((row['rowid'], row['date'], row['event'], data))
    return result


def scan_file(path, players, roomid, gameindex, limit=40):
    """Ищет в файле базы прежние версии протокола (JSON с marks)."""
    with open(path, 'rb') as handle:
        blob = handle.read()
    decoder = json.JSONDecoder()
    found, seen = [], set()
    for needle in (b'{"themes"', b'{"marks"', b'{"placeholder"'):
        start = blob.find(needle)
        while start != -1 and len(found) < limit:
            chunk = blob[start:start + 2 * 1024 * 1024]
            try:
                text = chunk.decode('utf-8', 'ignore')
                payload, _ = decoder.raw_decode(text)
            except (UnicodeDecodeError, ValueError):
                payload = None
            if isinstance(payload, dict) and 'marks' in payload:
                marks = payload.get('marks') or {}
                ids = set()
                for key in marks:
                    try:
                        ids.add(int(key.split(':')[1]))
                    except (ValueError, IndexError):
                        pass
                # Игроки должны быть из этой комнаты: иначе это чужой бой.
                if ids and ids <= set(players):
                    digest = json.dumps(payload, sort_keys=True, ensure_ascii=False)
                    if digest not in seen:
                        seen.add(digest)
                        found.append((start, payload))
            start = blob.find(needle, start + 1)
    return found


def parse_log_date(raw):
    """Дата в логе записана текстом: 'Wed Nov 12 18:16:10 2026'."""
    for fmt in ('%a %b %d %H:%M:%S %Y', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.datetime.strptime(str(raw).strip(), fmt)
        except (TypeError, ValueError):
            continue
    return None


def show_recent(conn, days=None):
    """Что менялось недавно: чтобы найти нужный бой, не помня номер."""
    limit = 2000 if days else 400
    rows = conn.execute(
        "SELECT rowid, date, roomid, event, data FROM log ORDER BY rowid DESC LIMIT %d" % limit
    ).fetchall()
    if days:
        border = datetime.datetime.now() - datetime.timedelta(days=days)
        filtered = []
        for row in rows:
            moment = parse_log_date(row['date'])
            if moment is None or moment >= border:
                filtered.append(row)
        rows = filtered
        print('События за последние %d дн.:' % days)
    else:
        print('Последние события по комнатам:')
    seen = {}
    for row in rows:
        key = (row['roomid'], row['event'])
        seen.setdefault(key, row)
    for (roomid, event), row in list(seen.items())[:25]:
        data = parse_json(row['data']) or {}
        game = data.get('gameindex')
        print('   комната %-6s %-22s бой %-4s %s' % (roomid, event, game if game is not None else '—', row['date']))
    print()
    if has_table(conn, 'game_protocols'):
        print('Протоколы в базе:')
        for row in conn.execute(
            "SELECT roomid, gameindex, length(payload) AS size, updated_at, updated_by "
            "FROM game_protocols ORDER BY updated_at DESC LIMIT 15"
        ):
            print('   комната %-6s бой %-4s %7s байт  %s  %s' % (
                row['roomid'], row['gameindex'], row['size'], row['updated_at'], row['updated_by'] or ''))


def report(path, roomid, gameindex, scan):
    conn = connect(path)
    players = room_players(conn, roomid)
    room = conn.execute("SELECT * FROM activerooms WHERE roomid = ?", (roomid,)).fetchone()
    print('=' * 70)
    if room is None:
        print('Комнаты %s в базе нет' % roomid)
        return 1
    print('Комната %s, дата %s, завершена: %s' % (roomid, room['date'], 'да' if room['finished'] else 'нет'))
    print('Игроков в комнате: %d' % len(players))
    print('Бой: %s' % gameindex)

    results = conn.execute(
        "SELECT name, score, position FROM results WHERE roomid = ? AND gameindex = ? ORDER BY position",
        (roomid, gameindex),
    ).fetchall() if has_table(conn, 'results') else []
    print('\nРезультаты в базе сейчас:')
    if results:
        for row in results:
            print('   %-28s %-6s место %s' % (row['name'][:28], row['score'], row['position']))
    else:
        print('   нет')

    print('\nПротокол сейчас:')
    if not has_table(conn, 'game_protocols'):
        protocol = None
        print('   таблицы протокола в этой базе нет (сайт старой версии)')
    else:
        protocol = conn.execute(
            "SELECT payload, updated_at, updated_by FROM game_protocols WHERE roomid = ? AND gameindex = ?",
            (roomid, gameindex),
        ).fetchone()
    if protocol is None:
        if has_table(conn, 'game_protocols'):
            print('   протокола нет')
    else:
        payload = parse_json(protocol['payload']) or {}
        print_marks(payload, players)
        print('   сохранён: %s (%s), тем: %d' % (
            protocol['updated_at'], protocol['updated_by'] or '—', len(payload.get('themes') or [])))

    if has_table(conn, 'game_protocol_history'):
        history = conn.execute(
            "SELECT id, payload, saved_at, saved_by FROM game_protocol_history "
            "WHERE roomid = ? AND gameindex = ? ORDER BY id DESC LIMIT 20",
            (roomid, gameindex),
        ).fetchall()
        print('\nИстория протокола (снимки «до» перезаписи): %d' % len(history))
        for row in history:
            payload = parse_json(row['payload']) or {}
            marks = payload.get('marks') or {}
            plus = sum(1 for value in marks.values() if value == 'plus')
            print('   #%s  %s  %s  отметок %d (плюсов %d)  → --apply-marks history:%s' % (
                row['id'], row['saved_at'], row['saved_by'] or '—', len(marks), plus, row['id']))
    else:
        print('\nТаблицы истории протокола нет: сайт ещё не обновлён.')

    entries = log_entries(conn, roomid, gameindex)
    print('\nЛог по бою: %d записей' % len(entries))
    for rowid, date, event, data in entries:
        print('   [%s] %s  %s' % (rowid, date, event))
        if event == 'save game result' and data.get('scores'):
            print_scores(data['scores'], indent='      ')

    # Ищем подозрительное: сохранение, где все очки нулевые, после ненулевых.
    previous = None
    for rowid, date, event, data in entries:
        if event != 'save game result':
            continue
        scores = data.get('scores') or []
        total = sum(abs(float(item.get('score') or 0)) for item in scores)
        if previous and total == 0 and previous[1] > 0:
            print('\n⚠️  Похоже на сброс: бой сохранён нулями [%s] %s, до него было %s' % (
                rowid, date, previous[0]))
            print('   Вернуть очки: --apply-scores %s --yes' % previous[0])
        previous = (rowid, total)

    if scan:
        print('\nПоиск прежних версий протокола в файле базы (побайтово):')
        candidates = scan_file(path, players, roomid, gameindex)
        if not candidates:
            print('   ничего не нашлось: старые страницы уже перезаписаны')
        current_themes = len((parse_json(protocol['payload']) or {}).get('themes') or []) if protocol else 0
        current_marks = len(((parse_json(protocol['payload']) or {}).get('marks') or {})) if protocol else 0
        scored = []
        for index, (offset, payload) in enumerate(candidates):
            themes = len(payload.get('themes') or [])
            marks_count = len(payload.get('marks') or {})
            # Ближе к текущему протоколу по числу тем — тот же бой и пакет.
            score = (1 if current_themes and themes == current_themes else 0) * 2 + (marks_count > current_marks)
            scored.append((score, marks_count, index, offset, payload))
        scored.sort(key=lambda item: (-item[0], -item[1]))
        for score, marks_count, index, offset, payload in scored:
            print('   снимок #%d, смещение %d байт  → --apply-marks scan:%d' % (index, offset, index))
            if current_themes and len(payload.get('themes') or []) != current_themes:
                print('      (тем %d, а в текущем протоколе %d — возможно, другой бой)' % (
                    len(payload.get('themes') or []), current_themes))
            print_marks(payload, players, indent='      ')
    conn.close()
    return 0


def apply_marks(path, roomid, gameindex, source, players):
    kind, _, ident = source.partition(':')
    if kind == 'history':
        conn = connect(path)
        row = conn.execute(
            "SELECT payload FROM game_protocol_history WHERE id = ?", (int(ident),)
        ).fetchone()
        conn.close()
        if row is None:
            print('Снимок history:%s не найден' % ident)
            return 1
        raw = row['payload']
    elif kind == 'scan':
        conn = connect(path)
        candidates = scan_file(path, players, roomid, gameindex)
        conn.close()
        index = int(ident)
        if index >= len(candidates):
            print('Снимок scan:%s не найден (нашлось %d)' % (ident, len(candidates)))
            return 1
        raw = json.dumps(candidates[index][1], ensure_ascii=False)
    else:
        print('Непонятный источник: %s (нужно history:ID или scan:ID)' % source)
        return 1

    payload = parse_json(raw) or {}
    print('Восстанавливаю протокол комнаты %s, боя %s:' % (roomid, gameindex))
    print_marks(payload, players)

    conn = connect(path, readonly=False)
    try:
        conn.execute("BEGIN IMMEDIATE")
        current = conn.execute(
            "SELECT payload FROM game_protocols WHERE roomid = ? AND gameindex = ?",
            (roomid, gameindex),
        ).fetchone()
        if current is not None and current['payload'] == raw:
            print('В базе уже эта версия — ничего не меняю')
            conn.execute("ROLLBACK")
            return 0
        if has_table(conn, 'game_protocol_history') and current is not None:
            conn.execute(
                "INSERT INTO game_protocol_history (roomid, gameindex, payload, saved_by) "
                "VALUES (?, ?, ?, 'recover_game.py')",
                (roomid, gameindex, current['payload']),
            )
        conn.execute(
            "INSERT OR REPLACE INTO game_protocols (roomid, gameindex, payload, updated_at, updated_by) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP, 'recover_game.py')",
            (roomid, gameindex, raw),
        )
        conn.execute(
            "INSERT INTO log (date, roomid, event, data) VALUES (datetime('now'), ?, ?, ?)",
            (roomid, 'protocol restored',
             json.dumps({'gameindex': gameindex, 'by': 'recover_game.py', 'source': source},
                        ensure_ascii=False)),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    print('Готово: протокол восстановлен')
    return 0


def apply_scores(path, roomid, gameindex, log_rowid):
    conn = connect(path, readonly=False)
    try:
        row = conn.execute("SELECT data FROM log WHERE rowid = ?", (log_rowid,)).fetchone()
        if row is None:
            print('Записи лога %s нет' % log_rowid)
            return 1
        data = parse_json(row['data']) or {}
        scores = data.get('scores') or []
        if not scores:
            print('В записи %s нет таблицы очков' % log_rowid)
            return 1
        print('Восстанавливаю очки комнаты %s, боя %s из лога [%s]:' % (roomid, gameindex, log_rowid))
        print_scores(scores)

        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        os.environ.setdefault('SVOYAK_DB', path)
        os.environ.setdefault('SVOYAK_SECRET_KEY', 'recover-game-tool')
        import svoyak_site_app as site

        conn.execute("BEGIN IMMEDIATE")
        rows = site._score_rows(conn, [
            {'playerid': item.get('playerid'), 'name': item.get('name'), 'score': item.get('score')}
            for item in scores
        ])
        site._write_game_results(conn, roomid, gameindex, rows)
        # На старой версии сайта этой функции может не быть.
        recompute = getattr(site, 'recompute_gamenumbers', None)
        if recompute is not None:
            recompute(conn, roomid)
        conn.execute(
            "INSERT INTO log (date, roomid, event, data) VALUES (datetime('now'), ?, ?, ?)",
            (roomid, 'save game result',
             json.dumps({'roomid': roomid, 'gameindex': gameindex, 'scores': scores,
                         'by': 'recover_game.py'}, ensure_ascii=False)),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    print('Готово: очки восстановлены')
    return 0


def main():
    parser = argparse.ArgumentParser(description='Восстановление боя после сброса')
    parser.add_argument('--db', required=True, help='путь к базе')
    parser.add_argument('--room', type=int, help='номер комнаты')
    parser.add_argument('--game', type=int, help='номер боя')
    parser.add_argument('--recent', action='store_true', help='что менялось недавно')
    parser.add_argument('--since', type=int, metavar='ДНЕЙ',
                        help='только события за последние N дней (вместе с --recent)')
    parser.add_argument('--scan', action='store_true', help='искать прежние версии протокола в файле')
    parser.add_argument('--apply-marks', help='history:ID или scan:ID')
    parser.add_argument('--apply-scores', help='rowid записи лога с нужными очками')
    parser.add_argument('--yes', action='store_true', help='подтвердить запись в базу')
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print('Нет такого файла: %s' % args.db)
        return 2

    if args.recent or args.since:
        conn = connect(args.db)
        show_recent(conn, args.since)
        conn.close()
        return 0

    if args.room is None or args.game is None:
        parser.print_help()
        return 2

    writes = bool(args.apply_marks or args.apply_scores)
    if writes and not args.yes:
        print('Это запись в базу. Добавьте --yes, если уверены.')
        return 2

    conn = connect(args.db)
    players = room_players(conn, args.room)
    conn.close()

    if args.apply_marks:
        return apply_marks(args.db, args.room, args.game, args.apply_marks, players)
    if args.apply_scores:
        return apply_scores(args.db, args.room, args.game, int(args.apply_scores))
    return report(args.db, args.room, args.game, args.scan)


if __name__ == '__main__':
    sys.exit(main())
