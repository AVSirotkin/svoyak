import os
import sqlite3
from flask import Flask, render_template, request, redirect, abort
import datetime
import time
import json
from shuffle import * 
from player_state import player_state
import pandas as pd
from statistics import median
from collections import defaultdict


app = Flask(__name__)

# Путь к базе можно переопределить переменной окружения (удобно для тестов
# на копии базы, не трогая рабочую).
DB_PATH = os.environ.get("SVOYAK_DB", "./svoyak.db")

game_states = {}

def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def get_tx_connection():
    """Соединение с ручным управлением транзакциями (BEGIN IMMEDIATE)."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.isolation_level = None
    return conn


def log_game_events(room, event, data, conn=None):
    """Пишет событие в лог. Если передан conn — в его транзакции, без commit."""
    payload = json.dumps(data, ensure_ascii=False)
    cur_time = time.ctime()
    own_conn = conn is None
    if own_conn:
        conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO log (date, roomid, event, data) VALUES (?, ?, ?, ?)",
            (cur_time, room, event, payload),
        )
        if own_conn:
            conn.commit()
    finally:
        if own_conn:
            conn.close()
    print(f"{cur_time}: log {event} room={room}")


def ensure_schema():
    """Создаёт индексы. Не падает, если в данных ещё есть дубли."""
    conn = get_db_connection()
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_results_room_game ON results(roomid, gameindex)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_log_room ON log(roomid)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_activeplayers_room ON activeplayers(roomid)")
        conn.commit()
        for name, sql in (
            ("ux_results_game_player",
             "CREATE UNIQUE INDEX IF NOT EXISTS ux_results_game_player "
             "ON results(roomid, gameindex, playerid)"),
            ("ux_players_id",
             "CREATE UNIQUE INDEX IF NOT EXISTS ux_players_id ON players(playerid)"),
        ):
            try:
                conn.execute(sql)
                conn.commit()
            except sqlite3.IntegrityError:
                print(f"WARNING: {name} not created: duplicate rows still in data")
            except sqlite3.OperationalError as exc:
                print(f"WARNING: {name} not created: {exc}")
    finally:
        conn.close()


@app.route('/', subdomain = "svoyak")
def SvoyakWelcomePage():
    return render_template("start.html")
    # return "Страничка на которой живет своячное приложение, однажды она станет интерактивной"
    

@app.route('/allplayers', subdomain = "svoyak")
def AllPalayers():
    conn = get_db_connection()
    all_players = conn.execute('SELECT playerid, name FROM players').fetchall()
    return json.dumps( [dict(ix) for ix in all_players])

@app.route('/oldroom/<int:roomid>', subdomain = "svoyak")
def SvoyakMainPage(roomid):
    conn = get_db_connection()
    roomstate = conn.execute(f"SELECT * FROM activerooms WHERE roomid={roomid}").fetchone()
    print(roomstate)
    if roomstate is None:
        return redirect(f"/init/{roomid}", code=302)
    if roomstate["finished"]:
        return redirect(f"/view/{roomid}", code=302)

    all_players = conn.execute('SELECT playerid, name FROM players ORDER BY name').fetchall()
    gi = conn.execute('SELECT max(gameindex) FROM results WHERE roomid == '+str(roomid)).fetchone()
    if gi["max(gameindex)"] is None:
        gameindex = 1
    else:
        gameindex = gi["max(gameindex)"] + 1
    ActivePalayers = []
    return render_template("SvoyakRoom.html", roomid = roomid, Players = all_players, gameindex = gameindex)

@app.route('/room/<int:roomid>', subdomain = "svoyak")
def SvoyakNewMainPage(roomid):
    conn = get_db_connection()
    roomstate = conn.execute(f"SELECT * FROM activerooms WHERE roomid={roomid}").fetchone()
    print(roomstate)
    if roomstate is None:
        return redirect(f"/init/{roomid}", code=302)
    if roomstate["finished"]:
        return redirect(f"/view/{roomid}", code=302)

    all_players = conn.execute('SELECT playerid, name FROM players ORDER BY name').fetchall()
    gi = conn.execute('SELECT max(gameindex) FROM results WHERE roomid == '+str(roomid)).fetchone()
    if gi["max(gameindex)"] is None:
        gameindex = 1
    else:
        gameindex = gi["max(gameindex)"] + 1
    ActivePalayers = []

    rules = get_room_rules(roomid)
    # with open("12-2-azart.json", "rt", encoding="UTF8") as fo:
    #     rules["brackets"] = json.load(fo)    

    return render_template("FullRoom.html", roomid = roomid, Players = all_players, gameindex = gameindex, rules = rules)

@app.route('/fullroom2/<int:roomid>', subdomain = "svoyak")
def SvoyakNewMainPage2(roomid):
    # Отдельного шаблона FullRoom2.html нет — ведём в обычную комнату,
    # иначе маршрут падал с TemplateNotFound.
    return redirect(f"/room/{roomid}", code=302)


@app.route('/view/<int:roomid>', subdomain = "svoyak")
def SvoyakViewPage(roomid):
    
    rules = get_room_rules(roomid)

    if "brackets" in rules:
        if not rules["brackets"] is None:
            return SvoyakViewBracketsPage(roomid)


    conn = get_db_connection()
    all_players_stats = conn.execute('SELECT playerid, name, COUNT(position) as games, sum(position) as position, sum(score) as score, sum(points) as points FROM results WHERE roomid == '+str(roomid)+' AND gamenumber <= '+ str(rules["parameters"]["basic_game_number"]) +' GROUP BY playerid, name ORDER BY points DESC').fetchall()
    
    all_places = pd.Series([r["points"] for r in all_players_stats]).rank(ascending=False).to_list()
    return render_template("view_new.html", roomid = roomid, PlayersStat = all_players_stats, places = all_places, rules = rules)

@app.route('/viewbrackets/<int:roomid>', subdomain = "svoyak")
def SvoyakViewBracketsPage(roomid):
    rules = get_room_rules(roomid)

    # print(rules)
    if "brackets" in rules:
        if not rules["brackets"] is None:
            return render_template("view_brackets.html", roomid = roomid, rules = rules)

    return "Нет сетки для этой комнаты"



@app.route('/view_old/<int:roomid>', subdomain = "svoyak")
def SvoyakViewOldPage(roomid):
    # gamehistory = json.loads(GetResult(roomid)
    
    rules = get_room_rules(roomid)
    print(rules)
    
    conn = get_db_connection()
    all_players_stats = conn.execute('SELECT name, COUNT(position) as games, sum(position) as position, sum(score) as score, sum(points) as points FROM results WHERE roomid == '+str(roomid)+' AND gamenumber <= '+ str(rules["parameters"]["basic_game_number"]) +' GROUP BY name ORDER BY points DESC').fetchall()
    
    all_places = pd.Series([r["points"] for r in all_players_stats]).rank(ascending=False).to_list()
    # for i in range(len(all_places)):
    #     all_players_stats[i]["place"] = all_places[i]
    return render_template("SvoyakView.html", roomid = roomid, PlayersStat = all_players_stats, places = all_places, rules = rules)



@app.route('/activeplayers/<int:roomid>', subdomain = "svoyak", methods = ["GET"])
def ActivePalayers(roomid):
    conn = get_db_connection()
    all_players = conn.execute('SELECT playerid, name FROM activeplayers where roomid =='+str(roomid)).fetchall()
    return json.dumps( [dict(ix) for ix in all_players])


def get_id(name, force_new=True, conn=None):
    """Возвращает playerid по имени, при необходимости создавая игрока.

    Вставка идёт в транзакции BEGIN IMMEDIATE, поэтому два одновременных
    добавления не могут получить один и тот же playerid.
    """
    name = (name or "").strip()
    if not name:
        return None

    own_conn = conn is None
    if own_conn:
        conn = get_tx_connection()
        conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT playerid FROM players WHERE name = ?", (name,)).fetchone()
        if row is not None:
            playerid = row["playerid"]
        elif not force_new:
            playerid = None
        else:
            max_id = conn.execute("SELECT COALESCE(MAX(playerid), 0) AS m FROM players").fetchone()["m"]
            playerid = max_id + 1
            conn.execute(
                "INSERT INTO players (playerid, name, fullname) VALUES (?, ?, ?)",
                (playerid, name, name),
            )
        if own_conn:
            conn.execute("COMMIT")
        return playerid
    except Exception:
        if own_conn:
            conn.execute("ROLLBACK")
        raise
    finally:
        if own_conn:
            conn.close()

def get_name(playerid):
    conn = get_db_connection()
    names = conn.execute('SELECT name FROM players where playerid =='+str(playerid)).fetchone()
    if names:
        return(names["name"])
    return ""


def _active_players(roomid):
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT playerid, name FROM activeplayers WHERE roomid = ? ORDER BY name", (roomid,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.route('/activeplayers/<int:roomid>', subdomain = "svoyak", methods = ["POST"])
def AddActivePalayers(roomid):
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()
    raw_id = data.get("playerid")
    if not name and raw_id in (None, ""):
        return json.dumps(_active_players(roomid), ensure_ascii=False)

    conn = get_tx_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        playerid = None
        if raw_id not in (None, ""):
            try:
                pid = int(raw_id)
            except (TypeError, ValueError):
                pid = None
            if pid is not None:
                known = conn.execute("SELECT name FROM players WHERE playerid = ?", (pid,)).fetchone()
                if known is not None:
                    playerid = pid
                    name = known["name"]
        if playerid is None:
            if not name:
                conn.execute("COMMIT")
                return json.dumps(_active_players(roomid), ensure_ascii=False)
            playerid = get_id(name, conn=conn)

        exists = conn.execute(
            "SELECT 1 FROM activeplayers WHERE roomid = ? AND playerid = ?", (roomid, playerid)
        ).fetchone()
        if exists is None:
            conn.execute(
                "INSERT INTO activeplayers (roomid, playerid, name) VALUES (?, ?, ?)",
                (roomid, playerid, name),
            )
            log_game_events(roomid, "add active player",
                            {"name": name, "playerid": playerid}, conn=conn)
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()

    game_states.pop(roomid, None)
    return json.dumps(_active_players(roomid), ensure_ascii=False)


@app.route('/removeactiveplayer/<int:roomid>', subdomain = "svoyak", methods = ["POST"])
def RemoveActivePalayers(roomid):
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()
    raw_id = data.get("playerid")
    playerid = None
    if raw_id not in (None, ""):
        try:
            playerid = int(raw_id)
        except (TypeError, ValueError):
            playerid = None
    if playerid is None:
        if not name:
            return json.dumps(_active_players(roomid), ensure_ascii=False)
        playerid = get_id(name, False)
        if playerid is None:
            return json.dumps(_active_players(roomid), ensure_ascii=False)

    conn = get_tx_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        known = conn.execute("SELECT name FROM players WHERE playerid = ?", (playerid,)).fetchone()
        if known is not None:
            name = known["name"]
        removed = conn.execute(
            "DELETE FROM activeplayers WHERE roomid = ? AND playerid = ?", (roomid, playerid)
        ).rowcount
        if removed:
            log_game_events(roomid, "remove active player",
                            {"name": name, "playerid": playerid}, conn=conn)
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()

    game_states.pop(roomid, None)
    return json.dumps(_active_players(roomid), ensure_ascii=False)


@app.route('/init/<int:roomid>', subdomain = "svoyak")
def InitRoom(roomid):
    conn = get_db_connection()
    venues = conn.execute(f"SELECT * FROM venues").fetchall()
    # print(venues)
    rules = conn.execute(f"SELECT * FROM basic_rules").fetchall()
    return render_template("InitRoom.html", roomid = roomid, venues = venues,
                           rules = rules, title = "Инициализация комнаты")


@app.route('/admin/<int:roomid>', subdomain = "svoyak")
def AdminLogView(roomid):
    conn = get_db_connection()
    log_info = conn.execute(f"SELECT rowid as id, date, roomid as room, event as name, data as json FROM log WHERE roomid = {roomid}").fetchall()
    venues = conn.execute(f"SELECT * FROM venues").fetchall()
    # print(venues)
    return render_template("RoomAdmin.html", roomid = roomid, gamelog = log_info,
                           venues = venues, title = f"Лог комнаты {roomid}")


@app.route('/removegamelog/<int:roomid>', subdomain = "svoyak", methods = ["POST"])
def RemoveFromLog(roomid):
    data = request.get_json(silent=True) or {}
    try:
        rowid = int(data.get("rowid"))
    except (TypeError, ValueError):
        return json.dumps({"status": "error", "message": "Нужен rowid"}, ensure_ascii=False), 400

    conn = get_tx_connection()
    changed = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        event = conn.execute(
            "SELECT event, data FROM log WHERE roomid = ? AND rowid = ?", (roomid, rowid)
        ).fetchone()
        if event is not None:
            if event["event"] == "save game result":
                try:
                    gameindex = int(json.loads(event["data"]).get("gameindex"))
                except (TypeError, ValueError):
                    gameindex = None
                if gameindex is not None:
                    conn.execute(
                        "DELETE FROM results WHERE roomid = ? AND gameindex = ?",
                        (roomid, gameindex),
                    )
                    recompute_gamenumbers(conn, roomid)
            conn.execute("DELETE FROM log WHERE roomid = ? AND rowid = ?", (roomid, rowid))
            changed = True
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()

    if changed:
        game_states.pop(roomid, None)
    return json.dumps({"status": "ok", "deleted": changed}, ensure_ascii=False)


def _rename_in_log(conn, playerid, name):
    """Обновляет имя игрока внутри JSON-событий лога.

    Иначе после переименования restore_states продолжит поднимать старое имя,
    а в админке останется прежний вариант.
    """
    rows = conn.execute(
        "SELECT rowid, data FROM log WHERE data LIKE ?", (f'%"playerid": {playerid}%',)
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["data"])
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        changed = False
        if payload.get("playerid") == playerid and "name" in payload:
            payload["name"] = name
            changed = True
        for item in payload.get("scores") or []:
            if isinstance(item, dict) and item.get("playerid") == playerid:
                item["name"] = name
                changed = True
        if changed:
            conn.execute(
                "UPDATE log SET data = ? WHERE rowid = ?",
                (json.dumps(payload, ensure_ascii=False), row["rowid"]),
            )


@app.route('/system/renameplayer', subdomain = "svoyak", methods = ["POST"])
def RenamePlayer():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()
    fullname = str(data.get("fullname") or "").strip() or name
    try:
        playerid = int(data.get("playerid"))
    except (TypeError, ValueError):
        return json.dumps({"status": "error", "message": "Нужен playerid"},
                          ensure_ascii=False), 400
    if not name:
        return json.dumps({"status": "error", "message": "Пустое имя"},
                          ensure_ascii=False), 400

    conn = get_tx_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        exists = conn.execute(
            "SELECT 1 FROM players WHERE playerid = ?", (playerid,)
        ).fetchone()
        if exists is None:
            conn.execute("COMMIT")
            return json.dumps({"status": "error", "message": "Игрок не найден"},
                              ensure_ascii=False), 404
        conn.execute(
            "UPDATE players SET name = ?, fullname = ? WHERE playerid = ?",
            (name, fullname, playerid),
        )
        conn.execute("UPDATE results SET name = ? WHERE playerid = ?", (name, playerid))
        conn.execute("UPDATE activeplayers SET name = ? WHERE playerid = ?", (name, playerid))
        _rename_in_log(conn, playerid, name)
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()

    for roomid in [r["roomid"] for r in _rooms_of_player(playerid)]:
        game_states.pop(roomid, None)

    return json.dumps({"status": "ok", "playerid": playerid, "name": name},
                      ensure_ascii=False)


def _rooms_of_player(playerid):
    conn = get_db_connection()
    try:
        return conn.execute(
            "SELECT DISTINCT roomid FROM results WHERE playerid = ? "
            "UNION SELECT DISTINCT roomid FROM activeplayers WHERE playerid = ?",
            (playerid, playerid),
        ).fetchall()
    finally:
        conn.close()





def _parse_score(raw):
    """'40', ' 40 ', '40.5', 40 -> число; мусор -> None."""
    if raw is None:
        return None
    text = str(raw).strip().replace(",", ".")
    if text == "":
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return int(value) if value.is_integer() else value


def _average_positions(scores):
    """Места как pandas rank(method='average'): ничьи делят место."""
    result = []
    for score in scores:
        greater = sum(1 for x in scores if x > score)
        greater_or_equal = sum(1 for x in scores if x >= score)
        result.append((greater + 1 + greater_or_equal) / 2.0)
    return result


def recompute_gamenumbers(conn, roomid):
    """Пересчитывает номер игры каждого игрока в комнате.

    Номер — это порядковый номер игры игрока внутри комнаты, а не gameindex.
    Раньше он считался один раз при вставке и ломался после удаления игры
    или повторного сохранения.
    """
    conn.execute(
        """
        WITH ordered AS (
            SELECT rowid AS rid,
                   ROW_NUMBER() OVER (PARTITION BY playerid ORDER BY gameindex, rowid) AS gn
            FROM results
            WHERE roomid = ?
        )
        UPDATE results
        SET gamenumber = (SELECT gn FROM ordered WHERE ordered.rid = results.rowid)
        WHERE roomid = ?
        """,
        (roomid, roomid),
    )


@app.route('/gameresult/<int:roomid>', subdomain="svoyak", methods=["POST"])
def SaveResult(roomid):
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return json.dumps({"status": "error", "message": "Ожидается JSON"}), 400
    if "gameindex" not in data or "scores" not in data:
        return json.dumps({"status": "error", "message": "Некорректный запрос"}), 400
    try:
        payload_room = int(data.get("roomid"))
    except (TypeError, ValueError):
        payload_room = None
    if payload_room != roomid:
        return json.dumps({"status": "error", "message": "Некорректный номер комнаты"}), 400
    try:
        gameindex = int(data["gameindex"])
    except (TypeError, ValueError):
        return json.dumps({"status": "error", "message": "Некорректный номер игры"}), 400

    scores = []
    for item in data["scores"]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        raw = item.get("score")
        if not name or raw is None or str(raw).strip() == "":
            continue
        score = _parse_score(raw)
        if score is None:
            return json.dumps(
                {"status": "error", "message": f"Некорректный счёт у игрока {name}"},
                ensure_ascii=False,
            ), 400
        scores.append({"name": name, "score": score, "playerid": item.get("playerid")})
    if not scores:
        return json.dumps({"status": "error", "message": "Нет игроков для сохранения"},
                          ensure_ascii=False), 400

    conn = get_tx_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")

        room = conn.execute(
            "SELECT finished FROM activerooms WHERE roomid = ?", (roomid,)
        ).fetchone()
        if room is not None and room["finished"]:
            conn.execute("COMMIT")
            return json.dumps({"status": "error", "message": "Комната уже завершена"},
                             ensure_ascii=False), 409

        # Приводим имена к каноническим из players, чтобы results.name
        # не расходился с players.name после переименований.
        rows = {}
        for item in scores:
            playerid = item["playerid"]
            playerid = int(playerid) if playerid not in (None, "") else get_id(item["name"], conn=conn)
            known = conn.execute("SELECT name FROM players WHERE playerid = ?", (playerid,)).fetchone()
            if known is None:
                playerid = get_id(item["name"], conn=conn)
                known = conn.execute("SELECT name FROM players WHERE playerid = ?", (playerid,)).fetchone()
            rows[playerid] = {"playerid": playerid, "name": known["name"], "score": item["score"]}
        rows = list(rows.values())
        positions = _average_positions([r["score"] for r in rows])

        # Повторное сохранение этой же игры заменяет её, а не игнорируется молча.
        conn.execute("DELETE FROM results WHERE roomid = ? AND gameindex = ?", (roomid, gameindex))
        for row, position in zip(rows, positions):
            points = round(5 - position + row["score"] / 1000.0, 3)
            conn.execute(
                "INSERT INTO results (roomid, gameindex, playerid, name, score, position, points, gamenumber) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                (roomid, gameindex, row["playerid"], row["name"], row["score"], position, points),
            )
        recompute_gamenumbers(conn, roomid)
        log_game_events(roomid, "save game result", data, conn=conn)
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()

    # Состояние пересоберётся из базы при следующем запросе.
    game_states.pop(roomid, None)

    saved = conn_rows(roomid, gameindex)
    return json.dumps(saved, ensure_ascii=False)


def conn_rows(roomid, gameindex):
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT playerid, name, score, position, points FROM results "
            "WHERE roomid = ? AND gameindex = ? ORDER BY position, name",
            (roomid, gameindex),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.route('/gameresult/<int:roomid>/<int:gameindex>', subdomain="svoyak", methods=["DELETE"])
def DeleteResult(roomid, gameindex):
    """Удаляет сохранённый бой (кнопка «Очистить бой»)."""
    conn = get_tx_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        room = conn.execute(
            "SELECT finished FROM activerooms WHERE roomid = ?", (roomid,)
        ).fetchone()
        if room is not None and room["finished"]:
            conn.execute("COMMIT")
            return json.dumps({"status": "error", "message": "Комната уже завершена"},
                             ensure_ascii=False), 409
        deleted = conn.execute(
            "DELETE FROM results WHERE roomid = ? AND gameindex = ?", (roomid, gameindex)
        ).rowcount
        if deleted:
            recompute_gamenumbers(conn, roomid)
            log_game_events(
                roomid, "delete game result",
                {"roomid": roomid, "gameindex": gameindex}, conn=conn,
            )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
    game_states.pop(roomid, None)
    return json.dumps({"status": "ok", "deleted": deleted}, ensure_ascii=False)


@app.route('/gameresult/<int:roomid>', subdomain = "svoyak", methods = ["GET"])
def GetResult(roomid):
    data = request.get_json(silent=True)
    gameindex = None
    if isinstance(data, dict):
        gameindex = data.get("gameindex", data.get("game_index"))
        try:
            gameindex = int(gameindex) if gameindex is not None else None
        except (TypeError, ValueError):
            gameindex = None

    conn = get_db_connection()
    try:
        if gameindex is None:
            req = conn.execute(
                "SELECT gameindex, playerid, name, score, position, points FROM results "
                "WHERE roomid = ? ORDER BY gameindex", (roomid,))
        else:
            req = conn.execute(
                "SELECT gameindex, playerid, name, score, position, points FROM results "
                "WHERE roomid = ? AND gameindex = ?", (roomid, gameindex))
        rows = req.fetchall()
    finally:
        conn.close()

    res = []
    prev_index = -1
    for r in rows:
        if r["gameindex"] != prev_index:
            res.append({"gameindex": r["gameindex"], "scores": []})
            prev_index = r["gameindex"]
        res[-1]["scores"].append(dict(r))

    return json.dumps(res, ensure_ascii=False)


@app.route('/lastgames/<int:lastcount>', subdomain = "svoyak", methods = ["GET"])
def GetLastResult(lastcount):
    """Последние бои для главной страницы.

    Берём последние события лога, но каждый бой показываем один раз:
    повторное сохранение больше не удваивает его в списке.
    """
    conn = get_db_connection()
    try:
        events = conn.execute(
            "SELECT data FROM log WHERE event = 'save game result' ORDER BY rowid DESC"
        ).fetchall()
        res = []
        seen = set()
        for event in events:
            try:
                payload = json.loads(event["data"])
                roomid = int(payload.get("roomid"))
                gameindex = int(payload.get("gameindex"))
            except (TypeError, ValueError):
                continue
            key = (roomid, gameindex)
            if key in seen:
                continue
            seen.add(key)
            scores = conn.execute(
                "SELECT gameindex, playerid, name, score, position, points FROM results "
                "WHERE roomid = ? AND gameindex = ? ORDER BY position, name",
                (roomid, gameindex),
            ).fetchall()
            if not scores:
                continue
            # Название площадки и дату показываем на главной вместо номера комнаты.
            room = conn.execute(
                "SELECT a.date, v.venuename FROM activerooms a "
                "LEFT JOIN venues v ON v.venueid = a.venueid WHERE a.roomid = ?",
                (roomid,),
            ).fetchone()
            res.append({
                "gameindex": gameindex,
                "roomid": roomid,
                "roomname": (room["venuename"] if room and room["venuename"] else None),
                "date": (room["date"] if room else None),
                "scores": [dict(r) for r in scores],
            })
            if len(res) >= lastcount:
                break
        return json.dumps(res, ensure_ascii=False)
    finally:
        conn.close()


@app.route('/tournaments/active', subdomain = "svoyak", methods = ["GET"])
def GetActiveTournaments():
    conn = get_db_connection()
    try:
        # Показываем только комнаты, где уже есть игроки или игры:
        # пустые комнаты, созданные «на пробу», больше не висят на главной.
        rows = conn.execute(
            """
            SELECT a.roomid, a.date, v.venuename,
                   (SELECT COUNT(*) FROM activeplayers ap WHERE ap.roomid = a.roomid) AS players,
                   (SELECT COUNT(DISTINCT r.gameindex) FROM results r WHERE r.roomid = a.roomid) AS games
            FROM activerooms a
            LEFT JOIN venues v ON v.venueid = a.venueid
            WHERE a.finished = 0
              AND a.date >= date('now', '-30 day')
              AND (EXISTS (SELECT 1 FROM results r WHERE r.roomid = a.roomid)
                   OR (SELECT COUNT(*) FROM activeplayers ap WHERE ap.roomid = a.roomid) >= 4)
            ORDER BY a.date DESC
            LIMIT 3
            """
        ).fetchall()
    finally:
        conn.close()

    res = []
    for t in rows:
        venue = t["venuename"] or "Свояк"
        res.append({
            "id": t["roomid"],
            "name": f"{venue} {t['date']}",
            "participants": t["players"],
            "games": t["games"],
        })
    return json.dumps(res, ensure_ascii=False)

@app.route('/tournaments/completed', subdomain = "svoyak", methods = ["GET"])
def GetLastTournaments():

    # res = []{"id": 2,
    #         "name": 'Зимний кубок',
    #         "winners": [
    #                     {"name": 'Профессионал123'},
    #                     {"name": 'Игрок2000'},
    #                     {"name": 'НовыйЧемпион'}
    #                 ]}]
    conn = get_db_connection()
    GameHistory = conn.execute("SELECT * FROM roomhistory LEFT JOIN venues ON roomhistory.venueid==venues.venueid LEFT JOIN players ON roomhistory.winerplayerid==players.playerid WHERE roomhistory.winerplayerid IS NOT NULL ORDER BY date DESC;").fetchmany(5)

    res = []
    for t in GameHistory:
        res += [{"id":t["roomid"], "name": (t["venuename"] if not t["venuename"] is None else "Безымянный") +" "+t["date"], "winners":[{"name": t["name"]}]}]

    return json.dumps(res, ensure_ascii=False)



@app.route('/weigth/<int:roomid>', subdomain = "svoyak", methods = ["POST"])
def EstimateWeigth(roomid):
    data = request.get_json(silent=True) or []
    choused = []
    for pl in data:
        if isinstance(pl, dict) and pl.get("name"):
            choused.append(pl["name"])
    all_players = rate_all(roomid, choused = choused)

    return json.dumps( [{"name":ix[0], "weight":ix[1], "skiped":ix[2], "played":ix[3]} for ix in all_players])



@app.route('/fillpositions/<int:roomid>', subdomain = "svoyak", methods = ["POST"])
def ReturnPositions(roomid):
    data = request.get_json(silent=True) or {}
    if not roomid in game_states:
        game_states[roomid] = restore_states(roomid)
    
    rules = get_room_rules(roomid)
    sub = []
    pre = []
    forbiden = []
    if "final_players" in rules["parameters"]:
        if rules["parameters"]["final_participants"] == "best_active":
            final_time = True
            for a in game_states[roomid].active_players:
                if game_states[roomid].played_games[a] < rules["parameters"]["basic_game_number"]:
                    final_time = False
                    break
            if final_time:
                conn = get_db_connection()
                #Возможно лучше сделать join с активными игроками
                players_stat = conn.execute('SELECT playerid, name, COUNT(position) as games, sum(position) as position, sum(score) as score, sum(points) as points FROM results WHERE roomid == '+str(roomid)+' AND gamenumber <= '+ str(rules["parameters"]["basic_game_number"]) +' GROUP BY name ORDER BY points DESC').fetchall()
                player_pos = 0
                if len(players_stat)>0:
                    assigned = []
                    for p in players_stat:
                        if p["name"] in game_states[roomid].active_players:
                            assigned.append({"name":p["name"]})
                        if len(assigned) >= rules["parameters"]["final_players"]:
                            break
                    if len(assigned)>0:
                        return json.dumps(assigned)

    if "assigned" in data:
        for i in range(len(data["assigned"])):
            if data["assigned"][i]["name"] == "":
                sub.append(i)
            else:
                pre.append(data["assigned"][i]["name"])
    if "one_game_forbiden" in data:
        for pl in data["one_game_forbiden"]:
            forbiden.append(pl["name"])

    player_in_game = game_states[roomid].shuffle_players(pre, forbiden=forbiden, rules = rules["name"])
    print(player_in_game)
    for i in range(len(player_in_game) - len(pre)):
        data["assigned"][sub[i]]["name"] = player_in_game[i+len(pre)]

    # data_all = [{"shift":0, "data": data["assigned"]}]
    return json.dumps(data["assigned"])

@app.route('/predictgames/<int:roomid>/<int:games_num>', subdomain = "svoyak", methods = ["POST"])
def ReturnFuturePositions(roomid, games_num):
    data = request.get_json(silent=True) or {}
    if not roomid in game_states:
        game_states[roomid] = restore_states(roomid)
    
    rules = get_room_rules(roomid)

    sub = []
    pre = []
    forbiden = []
    if "assigned" in data:
        for i in range(len(data["assigned"])):
            if data["assigned"][i]["name"] == "":
                sub.append(i)
            else:
                pre.append(data["assigned"][i]["name"])
    if "one_game_forbiden" in data:
        for pl in data["one_game_forbiden"]:
            forbiden.append(pl["name"])

    player_in_game = game_states[roomid].shuffle_players(pre, forbiden=forbiden, rules = rules["name"])
    print(player_in_game)
    for i in range(len(player_in_game) - len(pre)):
        data["assigned"][sub[i]]["name"] = player_in_game[i+len(pre)]

    data_all = [{"shift":0, "data": data["assigned"]}]
    
    if games_num > 1:
        loc_state = game_states[roomid].get_copy()        
        for i in range(1, games_num):
            data_all.append({"shift":i, "data": []})
            loc_state.process_one_match(player_in_game)
            player_in_game = loc_state.shuffle_players(rules = rules["name"])
            for n in player_in_game:
                data_all[-1]["data"].append({"name":n})

    return json.dumps(data_all)



@app.route('/sw.js', subdomain = "svoyak")
def ServiceWorker():
    """Service worker для офлайн-перезагрузки страницы комнаты."""
    response = app.send_static_file('sw.js')
    response.headers['Service-Worker-Allowed'] = '/'
    response.headers['Cache-Control'] = 'no-cache'
    return response


@app.route('/api/ping', subdomain = "svoyak", methods = ["GET", "HEAD"])
def PingApi():
    """Лёгкая проверка доступности сервера для офлайн-режима страницы."""
    response = app.response_class(json.dumps({"status": "ok"}), mimetype="application/json")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route('/api/addroom', subdomain = "svoyak", methods = ["POST"])
def CreateRoomApi():
    data = request.get_json(silent=True) or {}
    roomdate = datetime.datetime.today().strftime('%Y-%m-%d')
    if "date" in data:
        try:
            roomdate = datetime.datetime.strptime(str(data["date"]).strip(), "%Y-%m-%d").strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            roomdate = datetime.datetime.today().strftime('%Y-%m-%d')

    try:
        venueid = int(data.get("venueid", 0))
    except (TypeError, ValueError):
        venueid = 0

    rules_name = None
    rules_param = None
    rules_brackets = None
    
    if "rules_name" in data:
        rules_name = data["rules_name"]

    conn = get_db_connection()

    if "rulesid" in data:
        try:
            rulesid = int(data["rulesid"])
        except (TypeError, ValueError):
            rulesid = 0
        if rulesid > 0:
            r = conn.execute(
                "SELECT name, parameters, brackets FROM basic_rules WHERE rulesid = ?",
                (rulesid,),
            ).fetchone()
            if not r is None:
                rules_name = r["name"]
                rules_param = r["parameters"]
                rules_brackets = r["brackets"]


    new_room_id = 0
    if "roomid" in data:
        new_room_id = int(data["roomid"])
    
    force_room_overwrite = False
    if "force_room_overwrite" in data:
        if data["force_room_overwrite"]:
            force_room_overwrite = True

    if new_room_id != 0:
        check_room_id = conn.execute('SELECT roomid FROM roomhistory WHERE roomid == '+str(new_room_id)).fetchall()
        if len(check_room_id) > 0:
            if force_room_overwrite:
                conn.executescript('DELETE FROM roomhistory WHERE roomid == '+str(new_room_id))
            else:
                return json.dumps({"Status": "Room already in use", "RoomId": new_room_id})
    
    if new_room_id != 0:
        check_room_id = conn.execute('SELECT roomid FROM activerooms WHERE roomid == '+str(new_room_id)).fetchall()
        if len(check_room_id) > 0:
            if force_room_overwrite:
                conn.executescript('DELETE FROM activerooms WHERE roomid == '+str(new_room_id))
            else:
                return json.dumps({"Status": "Room already in use", "RoomId": new_room_id})

    if new_room_id == 0:
        max_room_id = 0
        max_room_h = conn.execute('SELECT max(roomid) as roomid FROM roomhistory').fetchall()
        if len(max_room_h):
            if max_room_id < max_room_h[0]["roomid"]:
                max_room_id = max_room_h[0]["roomid"]
        max_room_a = conn.execute('SELECT max(roomid) as roomid FROM activerooms').fetchall()
        if len(max_room_a):
            if max_room_id < max_room_a[0]["roomid"]:
                max_room_id = max_room_a[0]["roomid"]
        
        new_room_id = max_room_id + 1

    conn.execute(
        "INSERT INTO roomhistory (roomid, date, venueid, winerplayerid) VALUES (?, ?, ?, NULL)",
        (new_room_id, roomdate, venueid),
    )
    conn.execute(
        "INSERT INTO activerooms (roomid, date, venueid, finished) VALUES (?, ?, ?, 0)",
        (new_room_id, roomdate, venueid),
    )

    conn.execute("DELETE FROM rules WHERE roomid = ?", (new_room_id,))
    if not rules_name is None:
        conn.execute("INSERT INTO rules(roomid, name, parameters, brackets) VALUES(?,?,?,?)",(new_room_id, rules_name, rules_param, rules_brackets))
    conn.commit()
    return json.dumps({"Status": "Ok", "RoomId": new_room_id})

def _winner_from_bracket(conn, roomid, rules):
    """Победитель турнирной сетки — первый в финальном матче.

    Раньше для bracket-комнат parameters = NULL и код считал победителя
    по сумме очков за первые basic_game_number игр, игнорируя финал.
    """
    brackets = rules.get("brackets")
    if not brackets:
        return None
    try:
        schema = json.loads(brackets)
        final_id = schema.get("final_id")
    except (TypeError, ValueError):
        return None
    if final_id in (None, ""):
        return None
    try:
        final_game = int(final_id)
    except (TypeError, ValueError):
        return None
    return conn.execute(
        "SELECT playerid, name FROM results "
        "WHERE roomid = ? AND gameindex = ? AND position = 1",
        (roomid, final_game),
    ).fetchone()


@app.route('/api/finalizeroom/<int:roomid>', subdomain = "svoyak", methods = ["GET", "POST"])
def FinalizeRoomApi(roomid):
    conn = get_db_connection()
    try:
        room = conn.execute(
            "SELECT roomid, finished FROM activerooms WHERE roomid = ?", (roomid,)
        ).fetchone()
        if room is None:
            return json.dumps({"status": "Комната не найдена"}, ensure_ascii=False)
        if room["finished"]:
            return json.dumps({"status": "Комната уже завершена"}, ensure_ascii=False)

        rules = get_room_rules(roomid)
        params = rules.get("parameters") or {}

        if rules.get("brackets"):
            winner = _winner_from_bracket(conn, roomid, rules)
            if winner is None:
                return json.dumps({"status": "Финал ещё не сыгран"}, ensure_ascii=False)
        elif "final_players" in params:
            last_game = conn.execute(
                "SELECT MAX(gameindex) AS last FROM results WHERE roomid = ?", (roomid,)
            ).fetchone()["last"]
            if last_game is None:
                return json.dumps({"status": "Победитель не определён"}, ensure_ascii=False)
            winner = conn.execute(
                "SELECT playerid, name FROM results "
                "WHERE roomid = ? AND gameindex = ? AND position = 1",
                (roomid, last_game),
            ).fetchone()
        else:
            winner = conn.execute(
                "SELECT playerid, name, SUM(points) AS points FROM results "
                "WHERE roomid = ? AND gamenumber <= ? "
                "GROUP BY playerid, name ORDER BY points DESC, playerid LIMIT 1",
                (roomid, int(params.get("basic_game_number", 3))),
            ).fetchone()

        if winner is None:
            return json.dumps({"status": "Победитель не определён"}, ensure_ascii=False)

        conn.execute(
            "UPDATE roomhistory SET winerplayerid = ? WHERE roomid = ?",
            (winner["playerid"], roomid),
        )
        conn.execute("UPDATE activerooms SET finished = 1 WHERE roomid = ?", (roomid,))
        conn.commit()
        return json.dumps({"status": "Ok", "winer": winner["name"]}, ensure_ascii=False)
    finally:
        conn.close()
    

def rate_all(roomid, choused = []):
    global game_states
    print("rate all ", roomid)
    if not roomid in game_states:
        game_states[roomid] = restore_states(roomid)
    
    rules = get_room_rules(roomid)

    rt = estimate_rates(game_states[roomid].active_players, game_states[roomid], choused = choused, rules = rules["name"])
    if len(rt) == 0:
        return([])
    else:
        rt_extra = [(x[0], x[1], game_states[roomid].skipped_games[x[0]], game_states[roomid].played_games[x[0]]) for x in rt.items()]
        return(sorted(rt_extra, key=lambda x:-x[1]))

def get_room_rules(roomid):
    conn = get_db_connection()
    try:
        rules_base = conn.execute(
            "SELECT * FROM rules WHERE roomid = ?", (roomid,)
        ).fetchone()
    finally:
        conn.close()
    if rules_base is None:
        rules = {"name": "Spontan", "parameters":{"basic_game_number":3}}
    else:
        rules = dict(rules_base)
        if rules["parameters"] is None:
            if rules["name"] == "Tumen":
                rules["parameters"] = {"basic_game_number":4}
            else:
                rules["parameters"] = {"basic_game_number":3}
        else:
            rules["parameters"] = json.loads(rules["parameters"])    
    return rules


def restore_states(room):
    """Собирает состояние комнаты для подбора составов.

    Раньше лог проигрывался целиком, поэтому задвоенная запись «save game result»
    считалась дважды, а удалённая игра продолжала влиять на веса. Теперь:
      * события читаются в порядке вставки (rowid);
      * игры берутся из results, а не из текста события;
      * для повторов одного gameindex применяется только последнее событие.
    """
    loc_states = player_state()
    conn = get_db_connection()
    try:
        history = conn.execute(
            "SELECT rowid, event, data FROM log WHERE roomid = ? ORDER BY rowid", (room,)
        ).fetchall()
        results_rows = conn.execute(
            "SELECT gameindex, name FROM results WHERE roomid = ? ORDER BY gameindex, rowid", (room,)
        ).fetchall()
    finally:
        conn.close()

    games = defaultdict(list)
    seen_pairs = set()
    for row in results_rows:
        gameindex = int(row["gameindex"])
        name = row["name"]
        if (gameindex, name) in seen_pairs:
            continue
        seen_pairs.add((gameindex, name))
        games[gameindex].append(name)

    latest_save = {}
    for event in history:
        if event["event"] != "save game result":
            continue
        try:
            payload = json.loads(event["data"])
            gameindex = int(payload.get("gameindex"))
        except (TypeError, ValueError):
            continue
        latest_save[gameindex] = event["rowid"]

    for event in history:
        try:
            payload = json.loads(event["data"])
        except (TypeError, ValueError):
            continue
        name = str(payload.get("name") or "").strip()
        if event["event"] == "add active player":
            if name:
                loc_states.add_player(name)
        elif event["event"] == "remove active player":
            if name:
                loc_states.remove_player(name)
        elif event["event"] == "save game result":
            try:
                gameindex = int(payload.get("gameindex"))
            except (TypeError, ValueError):
                continue
            if latest_save.get(gameindex) == event["rowid"] and gameindex in games:
                loc_states.process_one_match(games[gameindex])

    return loc_states.get_copy()

@app.route('/spontan', subdomain = "svoyak")
def SpontanPage():
    conn = get_db_connection()
    GameHistory = conn.execute("SELECT * FROM roomhistory LEFT JOIN players ON roomhistory.winerplayerid==players.playerid WHERE venueid==1 ORDER BY date DESC;").fetchall()
    # print(GameHistory)
    return render_template("VenuesView.html", GameHistory = GameHistory, VenueName = "Бар Спонтан")
    # return " "

@app.route('/venue/<int:venueid>', subdomain = "svoyak")
def VenuePage(venueid):
    conn = get_db_connection()
    VenueNameRec = conn.execute(f"SELECT venuename FROM venues WHERE venueid=={venueid};").fetchall()
    VenueName = ""
    if len(VenueNameRec) >0:
        VenueName = VenueNameRec[0]["venuename"]

    GameHistory = conn.execute(f"SELECT * FROM roomhistory LEFT JOIN players ON roomhistory.winerplayerid==players.playerid WHERE venueid=={venueid} ORDER BY date DESC;").fetchall()
    # print(GameHistory)
    return render_template("VenuesView.html", GameHistory = GameHistory, VenueName = VenueName)
    # return " "



@app.route('/active', subdomain = "svoyak")
def ActivePage():
    conn = get_db_connection()
    GameHistory = conn.execute("SELECT * FROM activerooms LEFT JOIN venues ON activerooms.venueid==venues.venueid ORDER BY date DESC;").fetchall()
    # print(GameHistory)
    return render_template("ActiveView.html", GameHistory = GameHistory, VenueName = "Бар Спонтан")
    # return " "

@app.route('/api/detailed/<int:roomid>/<int:gameindex>', subdomain = "svoyak")
def detailed(roomid, gameindex, return_json = True):
    conn = get_db_connection()
    #todo: move detailed info into other table
    play_hist = conn.execute("SELECT data FROM log WHERE roomid = ? and event = 'save game result'", (roomid,)).fetchall()
    res = None
    for rs in play_hist:
        rj = json.loads(rs["data"])
        if rj["gameindex"] == gameindex:
            if "detailed" in rj:
                res = rj["detailed"]    
            break 
    return(json.dumps(res))



@app.route('/room/<int:roomid>/player/<int:playerid>', subdomain = "svoyak")
def player_stats(roomid, playerid):
    conn = get_db_connection()
    
    # 1. Получить имя игрока и все его игры
    player_info = conn.execute(
        'SELECT name FROM results WHERE roomid = ? AND playerid = ? LIMIT 1',
        (roomid, playerid)
    ).fetchone()
    
    if not player_info:
        conn.close()
        abort(404, description="Игрок не найден в этой комнате")
    
    player_name = player_info['name']
    
    # Список уникальных игр, в которых участвовал игрок
    games_list = conn.execute(
        'SELECT DISTINCT gameindex FROM results WHERE roomid = ? AND playerid = ?',
        (roomid, playerid)
    ).fetchall()
    
    games_data = []
    total_score = 0
    total_points = 0.0
    total_positions_sum = 0.0
    best_position = float('inf')
    worst_position = float('-inf')
    
    # Для каждого gameindex получим всех участников
    for row in games_list:
        game_idx = row['gameindex']
        players = conn.execute(
            'SELECT playerid, name, score, position, points FROM results WHERE roomid = ? AND gameindex = ?',
            (roomid, game_idx)
        ).fetchall()
        
        # Преобразуем в список словарей
        players_list = []
        for p in players:
            pl = dict(p)
            # Для подсчёта статистики по текущему игроку
            if pl['playerid'] == playerid:
                total_score += pl['score']
                total_points += pl['points']
                total_positions_sum += pl['position']
                best_position = min(best_position, pl['position'])
                worst_position = max(worst_position, pl['position'])
            players_list.append(pl)
        
        games_data.append({
            'gameindex': game_idx,
            'players': players_list
        })
    
    total_games = len(games_data)
    avg_position = total_positions_sum / total_games if total_games else 0
    best_position = best_position if total_games else None
    worst_position = worst_position if total_games else None
    
    # 2. Статистика против соперников
    opponents = defaultdict(lambda: {
        'name': None,
        'games_count': 0,
        'wins': 0,
        'losses': 0,
        'draws': 0,
        'score_diffs': []
    })
    
    for game in games_data:
        current_player_info = None
        other_players = []
        for p in game['players']:
            if p['playerid'] == playerid:
                current_player_info = p
            else:
                other_players.append(p)
        
        if not current_player_info:
            continue
        
        for opp in other_players:
            opp_id = opp['playerid']
            opp_name = opp['name']
            data = opponents[opp_id]
            data['name'] = opp_name
            data['games_count'] += 1
            
            # Сравнение мест (чем меньше число – тем лучше)
            if current_player_info['position'] < opp['position']:
                data['wins'] += 1
            elif current_player_info['position'] > opp['position']:
                data['losses'] += 1
            else:
                data['draws'] += 1
            
            # Разница в счёте
            score_diff = current_player_info['score'] - opp['score']
            data['score_diffs'].append(score_diff)
    
    # Постобработка: вычисляем среднее и медиану
    opponents_stats = []
    for opp_id, data in opponents.items():
        diffs = data['score_diffs']
        avg_diff = sum(diffs) / len(diffs) if diffs else 0
        median_diff = median(diffs) if diffs else 0
        opponents_stats.append({
            'name': data['name'],
            'games_count': data['games_count'],
            'wins': data['wins'],
            'losses': data['losses'],
            'draws': data['draws'],
            'avg_score_diff': avg_diff,
            'median_score_diff': median_diff
        })
    
    # Сортируем соперников по количеству совместных игр (по убыванию)
    opponents_stats.sort(key=lambda x: x['games_count'], reverse=True)
    
    conn.close()
    
    return render_template('player_stats.html',
                           roomid=roomid,
                           player_id=playerid,
                           player_name=player_name,
                           total_games=total_games,
                           total_score=total_score,
                           total_points=total_points,
                           avg_position=avg_position,
                           best_position=best_position,
                           worst_position=worst_position,
                           games=games_data,
                           opponents_stats=opponents_stats)

@app.route('/player/<int:playerid>/global', subdomain = "svoyak")
def global_player_stats(playerid):
    # Получаем параметр фильтра по сопернику (опционально)
    opponent_filter = request.args.get('opponent', type=int)
    clear = request.args.get('clear')
    if clear:
        opponent_filter = None

    conn = get_db_connection()  # адаптируйте под своё подключение

    # 1. Информация об игроке (имя)
    player_info = conn.execute(
        'SELECT name FROM results WHERE playerid = ? LIMIT 1',
        (playerid,)
    ).fetchone()
    if not player_info:
        conn.close()
        abort(404, description="Игрок не найден")
    player_name = player_info['name']

    # 2. Получить все уникальные (roomid, gameindex) где участвовал игрок
    games_rows = conn.execute('''
        SELECT DISTINCT roomid, gameindex
        FROM results
        WHERE playerid = ?
        ORDER BY roomid, gameindex
    ''', (playerid,)).fetchall()

    # Словарь для сбора данных о каждой игре
    games_data = []
    total_score = 0
    total_points = 0.0
    total_positions_sum = 0.0
    best_position = float('inf')
    worst_position = float('-inf')
    rooms_set = set()

    # Для фильтрации: если задан opponent_filter, будем пропускать игры без этого соперника
    for row in games_rows:
        roomid = row['roomid']
        game_idx = row['gameindex']
        rooms_set.add(roomid)

        # Получаем всех участников этой игры
        players = conn.execute('''
            SELECT playerid, name, score, position, points
            FROM results
            WHERE roomid = ? AND gameindex = ?
        ''', (roomid, game_idx)).fetchall()

        # Преобразуем в список словарей
        players_list = []
        current_player_info = None
        other_player_ids = set()

        for p in players:
            pl = dict(p)
            players_list.append(pl)
            if pl['playerid'] == playerid:
                current_player_info = pl
            else:
                other_player_ids.add(pl['playerid'])

        # Применяем фильтр по сопернику
        if opponent_filter is not None and opponent_filter not in other_player_ids:
            continue

        # Обновляем общую статистику игрока
        if current_player_info:
            total_score += current_player_info['score']
            total_points += current_player_info['points']
            total_positions_sum += current_player_info['position']
            best_position = min(best_position, current_player_info['position'])
            worst_position = max(worst_position, current_player_info['position'])

        games_data.append({
            'roomid': roomid,
            'gameindex': game_idx,
            'players': players_list
        })

    total_games = len(games_data)
    total_rooms = len(rooms_set)
    avg_position = total_positions_sum / total_games if total_games else 0
    best_position = best_position if total_games else None
    worst_position = worst_position if total_games else None

    # 3. Статистика против соперников (на основе отфильтрованных игр или всех? Лучше по всем, но с учётом фильтра? 
    #    Для единообразия будем считать статистику против соперников только по отфильтрованным играм,
    #    чтобы таблица отражала контекст текущего фильтра. Если фильтра нет – по всем играм.
    #    Альтернативно: всегда показывать полную таблицу, но это может сбивать с толку. Выберем логику: таблица соответствует отфильтрованным боям.
    opponents = defaultdict(lambda: {
        'name': None,
        'playerid': None,
        'games_count': 0,
        'wins': 0,
        'losses': 0,
        'draws': 0,
        'score_diffs': []
    })

    for game in games_data:
        current_player = None
        others = []
        for p in game['players']:
            if p['playerid'] == playerid:
                current_player = p
            else:
                others.append(p)
        if not current_player:
            continue

        for opp in others:
            opp_id = opp['playerid']
            data = opponents[opp_id]
            data['playerid'] = opp_id
            data['name'] = opp['name']
            data['games_count'] += 1

            if current_player['position'] < opp['position']:
                data['wins'] += 1
            elif current_player['position'] > opp['position']:
                data['losses'] += 1
            else:
                data['draws'] += 1

            score_diff = current_player['score'] - opp['score']
            data['score_diffs'].append(score_diff)

    opponents_stats = []
    for opp_id, data in opponents.items():
        diffs = data['score_diffs']
        avg_diff = sum(diffs) / len(diffs) if diffs else 0
        median_diff = median(diffs) if diffs else 0
        opponents_stats.append({
            'playerid': opp_id,
            'name': data['name'],
            'games_count': data['games_count'],
            'wins': data['wins'],
            'losses': data['losses'],
            'draws': data['draws'],
            'avg_score_diff': avg_diff,
            'median_score_diff': median_diff
        })

    # Сортируем по количеству встреч (убывание)
    opponents_stats.sort(key=lambda x: x['games_count'], reverse=True)

    # Имя соперника для отображения фильтра
    filter_opponent_name = None
    if opponent_filter:
        opp_info = next((o for o in opponents_stats if o['playerid'] == opponent_filter), None)
        filter_opponent_name = opp_info['name'] if opp_info else str(opponent_filter)

    conn.close()

    return render_template('player_global_stats.html',
                           player_id=playerid,
                           player_name=player_name,
                           total_rooms=total_rooms,
                           total_games=total_games,
                           total_score=total_score,
                           total_points=total_points,
                           avg_position=avg_position,
                           best_position=best_position,
                           worst_position=worst_position,
                           games=games_data,
                           opponents_stats=opponents_stats,
                           current_filter=opponent_filter,
                           filter_opponent_name=filter_opponent_name)

# ============================================================
# Пакеты вопросов (привязка к комнате)
# ============================================================
def ensure_packages_table():
    conn = get_db_connection()
    conn.execute('''
        CREATE TABLE IF NOT EXISTS packages (
            packageid   INTEGER PRIMARY KEY AUTOINCREMENT,
            roomid      INTEGER NOT NULL,
            name        TEXT NOT NULL,
            data        TEXT NOT NULL,
            created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_packages_room ON packages(roomid)')
    conn.commit()
    conn.close()


@app.route('/api/packages/<int:roomid>', subdomain="svoyak", methods=["GET"])
def list_packages(roomid):
    """Список пакетов комнаты (без содержимого)."""
    ensure_packages_table()
    conn = get_db_connection()
    rows = conn.execute(
        'SELECT packageid, name, created_at, updated_at FROM packages WHERE roomid = ? ORDER BY created_at DESC',
        (roomid,)
    ).fetchall()
    return json.dumps([dict(r) for r in rows], ensure_ascii=False)


@app.route('/api/packages/<int:roomid>/<int:packageid>', subdomain="svoyak", methods=["GET"])
def get_package(roomid, packageid):
    ensure_packages_table()
    conn = get_db_connection()
    row = conn.execute(
        'SELECT * FROM packages WHERE packageid = ? AND roomid = ?',
        (packageid, roomid)
    ).fetchone()
    if not row:
        return json.dumps({"status": "error", "message": "Not found"})
    result = dict(row)
    try:
        result['data'] = json.loads(result['data'])
    except Exception:
        result['data'] = {}
    return json.dumps(result, ensure_ascii=False)


@app.route('/api/packages/<int:roomid>/<int:packageid>', subdomain="svoyak", methods=["DELETE"])
def delete_package(roomid, packageid):
    ensure_packages_table()
    conn = get_db_connection()
    conn.execute('DELETE FROM packages WHERE packageid = ? AND roomid = ?', (packageid, roomid))
    conn.commit()
    return json.dumps({"status": "ok", "packageid": packageid})


@app.route('/api/packages/<int:roomid>/sync', subdomain="svoyak", methods=["POST"])
def sync_packages(roomid):
    """
    Принимает {packages: [{localId, serverId, name, data}]},
    для каждого: update (если serverId существует) либо insert.
    Возвращает mapping localId -> serverId.
    """
    ensure_packages_table()
    payload = request.get_json(silent=True) or {}
    packages = payload.get('packages', [])
    conn = get_db_connection()

    results = []
    for pkg in packages:
        local_id = pkg.get('localId')
        server_id = pkg.get('serverId')
        name = pkg.get('name') or 'Без названия'
        data_json = json.dumps(pkg.get('data') or {}, ensure_ascii=False)

        if server_id:
            existing = conn.execute(
                'SELECT packageid FROM packages WHERE packageid = ? AND roomid = ?',
                (server_id, roomid)
            ).fetchone()
            if existing:
                conn.execute(
                    'UPDATE packages SET name = ?, data = ?, updated_at = CURRENT_TIMESTAMP WHERE packageid = ?',
                    (name, data_json, server_id)
                )
                results.append({"localId": local_id, "serverId": server_id, "status": "updated"})
                continue

        cur = conn.execute(
            'INSERT INTO packages (roomid, name, data) VALUES (?, ?, ?)',
            (roomid, name, data_json)
        )
        results.append({"localId": local_id, "serverId": cur.lastrowid, "status": "created"})

    conn.commit()
    conn.close()
    return json.dumps({"status": "ok", "results": results}, ensure_ascii=False)



def read_cfg():
    website_url = ""
    port = 80
    try:
        with open("site.cfg") as cfg_file:
            website_url = cfg_file.readline().strip()
            port = int(cfg_file.readline().strip())
    except Exception:
        pass    
    if website_url == "":
        website_url = 'chgk.test'
        port = 80
    return website_url, port

# SERVER_NAME нужен маршрутам с subdomain="svoyak" и должен быть задан
# до первого запроса, а не только при запуске через __main__.
website_url, port = read_cfg()
app.config['SERVER_NAME'] = os.environ.get("SVOYAK_SERVER_NAME", website_url)
app.jinja_env.filters['zip'] = zip

if __name__ == "__main__":
    ensure_schema()
    debug = os.environ.get("SVOYAK_DEBUG", "").lower() in ("1", "true", "yes")
    print(f"svoyak on {app.config['SERVER_NAME']}:{port}, debug={debug}, db={DB_PATH}")
    app.run(debug=debug, port=port, threaded=True)