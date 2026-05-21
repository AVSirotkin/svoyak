import sqlite3
from flask import Flask, render_template, request, redirect
import datetime
import time
import json
from shuffle import * 
from player_state import player_state
import pandas as pd
from statistics import median
from collections import defaultdict


app = Flask(__name__)

game_states = {}

def get_db_connection():
    conn = sqlite3.connect('./svoyak.db')
    conn.row_factory = sqlite3.Row
    return conn

def log_game_events(room, event, data):
    cur_time = time.ctime()
    print(f"{cur_time}: log {event} at room {room} with data {data}")
    print(json.dumps(data, ensure_ascii=False).encode('utf8'))
    conn = get_db_connection()
    print(f"INSERT OR IGNORE INTO log VALUES ('{cur_time}', {room}, '{event}', '{json.dumps(data,ensure_ascii=False)}' )")
    conn.execute(f"INSERT OR IGNORE INTO log VALUES ('{cur_time}', {room}, '{event}', '{json.dumps(data,ensure_ascii=False)}')")
    conn.commit()


@app.route('/', subdomain = "svoyak")
def SvoyakWelcomePage():
    return render_template("start.html")
    # return "Страничка на которой живет своячное приложение, однажды она станет интерактивной"
    

@app.route('/allplayers', subdomain = "svoyak")
def AllPalayers():
    conn = get_db_connection()
    all_players = conn.execute('SELECT playerid, name FROM players').fetchall()
    return json.dumps( [dict(ix) for ix in all_players])

@app.route('/room/<int:roomid>', subdomain = "svoyak")
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

@app.route('/fullroom/<int:roomid>', subdomain = "svoyak")
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
    return render_template("FullRoomv2.html", roomid = roomid, Players = all_players, gameindex = gameindex)



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


def get_id(name, force_new = True):
    conn = get_db_connection()
    ids = conn.execute(f'SELECT playerid FROM players where name == "{name}"').fetchall()
    if len(ids) == 0:
        if force_new:
            m = conn.execute('SELECT max(playerid) FROM players').fetchone()["max(playerid)"]
            conn.execute(f'INSERT INTO players (playerid, name, fullname) VALUES ({m+1}, "{name}", "{name}")')
            conn.commit()
            return (m+1)
        else:
            return None
    return(ids[0]["playerid"])

def get_name(playerid):
    conn = get_db_connection()
    names = conn.execute('SELECT name FROM players where playerid =='+str(playerid)).fetchone()
    if names:
        return(names["name"])
    return ""


@app.route('/activeplayers/<int:roomid>', subdomain = "svoyak", methods = ["POST"])
def AddActivePalayers(roomid):
    data = request.json
    conn = get_db_connection()

    print(data)

    if "playerid" in data or "name" in data:
        if not "name" in data:
            data["name"] = get_name(data['playerid'])
        if not data["name"] == "":
            if not "playerid" in data:
                data["playerid"] = get_id(data['name'])
            
            if conn.execute(f"select * from activeplayers where roomid == {roomid} and playerid == {data['playerid']}").fetchone():
                pass
            else:
                conn.execute(f"INSERT OR IGNORE INTO activeplayers VALUES ({roomid},{data['playerid']},\"{data['name']}\" )")
                conn.commit()
                log_game_events(roomid, "add active player", data)
                if not roomid in game_states:
                    game_states[roomid] = restore_states(roomid)
                game_states[roomid].add_player(data['name'])
    
    all_players = conn.execute('SELECT playerid, name FROM activeplayers where roomid =='+str(roomid)).fetchall()
    return json.dumps([dict(ix) for ix in all_players])


@app.route('/removeactiveplayer/<int:roomid>', subdomain = "svoyak", methods = ["POST"])
def RemoveActivePalayers(roomid):
    data = request.json
    conn = get_db_connection()

    print(data)
    if not "playerid" in data:
        data["playerid"] = get_id(data["name"], False)

    if "playerid" in data and "name" in data:
        if data["playerid"] is not None:
            conn.execute(f"DELETE FROM activeplayers WHERE roomid = {roomid} AND playerid = {data['playerid']} AND name =\"{data['name']}\" ")
            conn.commit()
            log_game_events(roomid, "remove active player", data)
            if not roomid in game_states:
                game_states[roomid] = restore_states(roomid)
            game_states[roomid].remove_player(data['name'])

    all_players = conn.execute('SELECT playerid, name FROM activeplayers where roomid =='+str(roomid)).fetchall()
    return json.dumps([dict(ix) for ix in all_players])


@app.route('/init/<int:roomid>', subdomain = "svoyak")
def InitRoom(roomid):
    conn = get_db_connection()
    venues = conn.execute(f"SELECT * FROM venues").fetchall()
    # print(venues)
    rules = conn.execute(f"SELECT * FROM basic_rules").fetchall()
    return render_template("InitRoom.html", roomid = roomid, venues = venues, rules=rules)


@app.route('/admin/<int:roomid>', subdomain = "svoyak")
def AdminLogView(roomid):
    conn = get_db_connection()
    log_info = conn.execute(f"SELECT rowid as id, date, roomid as room, event as name, data as json FROM log WHERE roomid = {roomid}").fetchall()
    venues = conn.execute(f"SELECT * FROM venues").fetchall()
    # print(venues)
    return render_template("RoomAdmin.html", roomid = roomid, gamelog = log_info, venues = venues)


@app.route('/removegamelog/<int:roomid>', subdomain = "svoyak", methods = ["POST"])
def RemoveFromLog(roomid):
    data = request.json
    print("DATA", data)
    if not "rowid" in data:
        return "{}"
    conn = get_db_connection()
    event_list = conn.execute(f"SELECT event, data FROM log WHERE roomid = {roomid} AND rowid = {data['rowid']}").fetchall()
    if len(event_list) > 0:
        if event_list[0]["event"] == "save game result":
            gamedata = json.loads(event_list[0]["data"])
            conn.execute(f"DELETE FROM results WHERE roomid = {roomid} AND gameindex = {gamedata['gameindex']}")
    sql_del = conn.execute(f"DELETE FROM log WHERE roomid = {roomid} AND rowid = {data['rowid']}")
    conn.commit()
    if sql_del.rowcount == 0:
        return "{}"
    if roomid in game_states:
        game_states[roomid] = restore_states(roomid)
    return json.dumps(request.json)


@app.route('/system/renameplayer', subdomain = "svoyak", methods = ["POST"])
def RenamePlayer():
    data = request.json
    print("DATA", data)
    if not "playerid" in data:
        return "{}"
    if not "name" in data:
        return "{}"
    if not "fullname" in data:
        return "{}"
    
    conn = get_db_connection()
    conn.execute(f"UPDATE players SET name = ? WHERE playerid = ?", (data["name"], data["playerid"]))
    conn.execute(f"UPDATE players SET fullname = ? WHERE playerid = ?", (data["fullname"], data["playerid"]))
    conn.execute(f"UPDATE results SET name = ? WHERE playerid = ?", (data["name"], data["playerid"]))
    conn.execute(f"UPDATE activeplayers SET name = ? WHERE playerid = ?", (data["name"], data["playerid"]))
    conn.commit()
    return json.dumps(request.json)





@app.route('/gameresult/<int:roomid>', subdomain = "svoyak", methods = ["POST"])
def SaveResult(roomid):
    data = request.json

    print("DATA", data)
    conn = get_db_connection()
    if not "roomid" in data:
        return "{}"
    if not "gameindex" in data:
        return "{}"
    if not "scores" in data:
        return "{}"
    if data["roomid"] != roomid:
        return "{}"
    player_in_game = []
    if conn.execute(f"SELECT count(playerid) FROM results WHERE roomid == {roomid} AND gameindex == {data['gameindex']}").fetchone()["count(playerid)"] == 0:

        non_empty = []
        
        for v in data["scores"]:
            if v["name"] == "":
                continue
            if v["score"] == "":
                continue
            player_in_game.append(v["name"])
            if not "playerid" in v:
                v["playerid"] = get_id(v['name'])
            
            non_empty.append(v)

        if len(non_empty) == 0:
            return {}
        
        local_res = pd.DataFrame(non_empty)

        print(local_res)

        game_num = [len(conn.execute(f"SELECT name FROM results WHERE roomid = {roomid} AND name = '{x}' AND gameindex < {data['gameindex']}").fetchall()) + 1 for x in local_res["name"]]
        print(game_num)
        for x in local_res["name"]:
            print(f"SELECT name FROM results WHERE roomid = {roomid} AND name = '{x}' AND gameindex < {data['gameindex']}")

        local_res["score"] = pd.to_numeric(local_res["score"])
        local_res["position"] = local_res["score"].rank(ascending = False)
        local_res["points"] = 5 - local_res["position"] + local_res["score"]/1000
        local_res["points"] = local_res["points"].round(3)
        local_res["roomid"] = roomid
        local_res["gameindex"] = data['gameindex']
        local_res["gamenumber"] = game_num

        
        local_res.to_sql('results', conn, if_exists='append', index=False)
        conn.commit()

        log_game_events(roomid, "save game result", data)
        
        if not roomid in game_states:
            game_states[roomid] = restore_states(roomid)
        
        if len(player_in_game) >0:
            game_states[roomid].process_one_match(player_in_game)

    all_players = conn.execute('SELECT playerid, name, score FROM results where roomid =='+str(roomid)+" and gameindex == "+str(data['gameindex']) ).fetchall()
    return json.dumps([dict(ix) for ix in all_players])


@app.route('/gameresult/<int:roomid>', subdomain = "svoyak", methods = ["GET"])
def GetResult(roomid):
    print("GetResultReques")
    try:
        data = request.json
    except:
        data = None

    print("DATA", data)

    conn = get_db_connection()

    if data is None:
        req = conn.execute(f"SELECT gameindex, playerid, name, score, position, points FROM results WHERE roomid == {roomid} ORDER BY gameindex")
    elif "game_index" in data:
        req = conn.execute(f"SELECT gameindex, playerid, name, score, position, points FROM results WHERE roomid == {roomid} AND gameindex == {data['gameindex']}")
    else:
        req = conn.execute(f"SELECT gameindex, playerid, name, score, position, points FROM results WHERE roomid == {roomid} ORDER BY gameindex")

    res = []
    prev_index = -1
    for r in req.fetchall():
        if r["gameindex"] != prev_index:
            res.append({"gameindex":r["gameindex"], "scores":[]})
            prev_index = r["gameindex"]
        res[-1]["scores"].append(dict(r))

    return json.dumps(res, ensure_ascii=False)


@app.route('/lastgames/<int:lastcount>', subdomain = "svoyak", methods = ["GET"])
def GetLastResult(lastcount):
    print("GetLastResultReques")
    try:
        data = request.json
    except:
        data = None

    print("DATA", data)

    conn = get_db_connection()

    events_list = conn.execute(f'SELECT data FROM log WHERE event=="save game result" ORDER BY rowid DESC').fetchmany(lastcount)

    res = []
    if len(events_list) > 0:
        print(events_list)
        for j in events_list:
            v = json.loads(j["data"])
            req = conn.execute(f"SELECT gameindex, playerid, name, score, position, points FROM results WHERE roomid == {v['roomid']} AND gameindex == {v['gameindex']}")
            res.append({"gameindex":v["gameindex"], "roomid":v["roomid"], "scores":[]})

            for r in req.fetchall():
                res[-1]["scores"].append(dict(r))

    return json.dumps(res, ensure_ascii=False)


@app.route('/tournaments/active', subdomain = "svoyak", methods = ["GET"])
def GetActiveTournaments():
    conn = get_db_connection()
    GameHistory = conn.execute("SELECT * FROM activerooms LEFT JOIN venues ON activerooms.venueid==venues.venueid WHERE finished == 0 ORDER BY date DESC;").fetchmany(3)

    res = []
    # {"id": 1,
    #        "name": 'Весенний чемпионат',
    #        "timeLeft": '3 дня осталось',
    #         "participants": 12
    #     }]
    for t in GameHistory:
        res += [{"id":t["roomid"], "name": (t["venuename"] if not t["venuename"] is None else "Безымянный") +" "+t["date"]}]

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
    data = request.json
    print("weigth", data)
    choused = []
    for pl in data:
        if pl["name"] != "":
            choused.append(pl["name"])
    all_players = rate_all(roomid, choused = choused)

    print(game_states)

    return json.dumps( [{"name":ix[0], "weight":ix[1], "skiped":ix[2], "played":ix[3]} for ix in all_players])



@app.route('/fillpositions/<int:roomid>', subdomain = "svoyak", methods = ["POST"])
def ReturnPositions(roomid):
    data = request.json
    print(data)
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
    data = request.json
    print(data)
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



@app.route('/api/addroom', subdomain = "svoyak", methods = ["POST"])
def CreateRoomApi():
    data = request.json
    print(data)
    roomdate = datetime.datetime.today().strftime('%Y-%m-%d')
    if "date" in data:
        roomdate = data["date"]

    venueid = 0
    if "venueid" in data:
        venueid = data["venueid"]
    
    rules_name = None
    rules_param = None
    rules_brackets = None
    
    if "rules_name" in data:
        rules_name = data["rules_name"]

    conn = get_db_connection()

    if "rulesid" in data:
        if int(data["rulesid"]) > 0:
            r = conn.execute(f"SELECT name, parameters, brackets FROM basic_rules WHERE rulesid={data['rulesid']}").fetchone()
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

    print(f'INSERT INTO roomhistory(roomid, date, venueid, winerplayerid) VALUES({new_room_id}, "{roomdate}", {venueid}, Null)')
    conn.executescript(f'INSERT INTO roomhistory(roomid, date, venueid, winerplayerid) VALUES({new_room_id}, "{roomdate}", {venueid}, Null)')
    conn.executescript(f'INSERT INTO activerooms(roomid, date, venueid, finished) VALUES({new_room_id}, "{roomdate}", {venueid}, 0)')
    
    check_room_id = conn.execute('SELECT roomid FROM rules WHERE roomid == '+str(new_room_id)).fetchall()
    if len(check_room_id) > 0:
        conn.executescript('DELETE FROM rules WHERE roomid == '+str(new_room_id))

    if not rules_name is None:
        conn.execute("INSERT INTO rules(roomid, name, parameters, brackets) VALUES(?,?,?,?)",(new_room_id, rules_name, rules_param, rules_brackets))
        conn.commit()
    return json.dumps({"Status": "Ok", "RoomId": new_room_id})

@app.route('/api/finalizeroom/<int:roomid>', subdomain = "svoyak")
def FinalizeRoomApi(roomid):
    # data = request.json
    conn = get_db_connection()
    room_info = conn.execute('SELECT * FROM activerooms WHERE roomid == '+str(roomid)).fetchall()
    rules = get_room_rules(roomid)
    if len(room_info) == 1:
        if not room_info[0]["finished"]:
    
            if "final_players" in rules["parameters"]:
                final_id = conn.execute('SELECT MAX(gameindex) as last FROM results WHERE roomid == '+str(roomid)).fetchone()
                if final_id is None:
                    return json.dumps({"status":"No winer detected"})
                winer = conn.execute('SELECT playerid, name FROM results WHERE roomid == '+str(roomid)+' AND gameindex == '+ str(final_id["last"]) +' AND position == 1').fetchone()
                if winer is None:
                    return json.dumps({"status":"No winer detected"})
                playerid = winer["playerid"]
                winer_name = winer["name"]
                conn.executescript(f'UPDATE roomhistory SET winerplayerid={playerid} WHERE roomid == {roomid}')
                conn.executescript('UPDATE activerooms SET finished=1 WHERE roomid == '+str(roomid))
                return json.dumps({"status":"Ok", "winer":winer_name}, ensure_ascii=False)
                

            else:
                players_stat = conn.execute('SELECT playerid, name, COUNT(position) as games, sum(position) as position, sum(score) as score, sum(points) as points FROM results WHERE roomid == '+str(roomid)+' AND gamenumber <= '+ str(rules["parameters"]["basic_game_number"]) +' GROUP BY name ORDER BY points DESC').fetchall()
                    
                if len(players_stat)>0:
                    playerid = players_stat[0]["playerid"]
                    winer_name = players_stat[0]["name"]
                    conn.executescript(f'UPDATE roomhistory SET winerplayerid={playerid} WHERE roomid == {roomid}')
                    conn.executescript('UPDATE activerooms SET finished=1 WHERE roomid == '+str(roomid))
                    return json.dumps({"status":"Ok", "winer":winer_name}, ensure_ascii=False)
                return json.dumps({"status":"No winer detected"})
        return json.dumps({"status":"Room arleady finished"})
    return json.dumps({"status":"No such room"})
    

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
    rules_base = conn.execute(f'SELECT * FROM rules WHERE roomid={roomid}').fetchone()
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
    print("restore state ", room)
    
    loc_states = player_state()
    print(loc_states.active_players, loc_states.played_games)
    conn = get_db_connection()
    history = conn.execute(f"SELECT date, roomid, event, data FROM log WHERE roomid == {room}").fetchall()
    print("len history ", len(history))
    for event in history:
        loc_data = json.loads(event["data"])
        if event["event"] == "add active player":
            loc_states.add_player(loc_data['name'])
        elif event["event"] == "remove active player":
            loc_states.remove_player(loc_data['name'])
        elif event["event"] == "save game result":
            player_in_game = []
            for v in loc_data["scores"]:
                if v["name"] == "":
                    continue
                if v["score"] == "":
                    continue
                player_in_game.append(v["name"])
                if not "playerid" in v:
                    v["playerid"] = get_id(v['name'])
            if len(player_in_game) >0:
                print("from game log: ", player_in_game)
                loc_states.process_one_match(player_in_game)
    print(loc_states.played_games)
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

if __name__ == "__main__":
    website_url, port = read_cfg()
    print(website_url)
    app.config['SERVER_NAME'] = website_url
    app.jinja_env.filters['zip'] = zip
    app.run(debug=True, port=port)