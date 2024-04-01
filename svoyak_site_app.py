import sqlite3
from flask import Flask, render_template, request
import datetime
import time
import json
from shuffle import * 
from player_state import player_state


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
    return "Страничка на которой живет своячное приложение, однажды она станет интерактивной"
    



@app.route('/allplayers', subdomain = "svoyak")
def AllPalayers():
    conn = get_db_connection()
    all_players = conn.execute('SELECT playerid, name FROM players').fetchall()
    return json.dumps( [dict(ix) for ix in all_players])

@app.route('/room/<int:roomid>', subdomain = "svoyak")
def SvoyakMainPage(roomid):
    conn = get_db_connection()
    all_players = conn.execute('SELECT playerid, name FROM players ORDER BY name').fetchall()
    gi = conn.execute('SELECT max(gameindex) FROM results WHERE roomid == '+str(roomid)).fetchone()
    if gi["max(gameindex)"] is None:
        gameindex = 1
    else:
        gameindex = gi["max(gameindex)"] + 1
    ActivePalayers = []
    return render_template("SvoyakRoom.html", roomid = roomid, Players = all_players, gameindex = gameindex)


@app.route('/view/<int:roomid>', subdomain = "svoyak")
def SvoyakViewPage(roomid):
    gamehistory = GetResult(roomid)
    all_players_stats = []
    # conn = get_db_connection()
    # all_players = conn.execute('SELECT playerid, name FROM players ORDER BY name').fetchall()
    # gi = conn.execute('SELECT max(gameindex) FROM results WHERE roomid == '+str(roomid)).fetchone()
    # if gi["max(gameindex)"] is None:
    #     gameindex = 1
    # else:
    #     gameindex = gi["max(gameindex)"] + 1
    # ActivePalayers = []

    return render_template("SvoyakView.html", roomid = roomid, PlayersStat = all_players_stats, gamehistory = gamehistory)



@app.route('/activeplayers/<int:roomid>', subdomain = "svoyak", methods = ["GET"])
def ActivePalayers(roomid):
    conn = get_db_connection()
    all_players = conn.execute('SELECT playerid, name FROM activeplayers where roomid =='+str(roomid)).fetchall()
    return json.dumps( [dict(ix) for ix in all_players])


def get_id(name):
    conn = get_db_connection()
    ids = conn.execute(f'SELECT playerid FROM players where name == "{name}"').fetchall()
    if len(ids) == 0:
        m = conn.execute('SELECT max(playerid) FROM players').fetchone()["max(playerid)"]
        conn.execute(f'INSERT INTO players (playerid, name, fullname) VALUES ({m+1}, "{name}", "{name}")')
        conn.commit()
        return (m+1)
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
        data["playerid"] = get_id(data["name"])

    if "playerid" in data and "name" in data:
        conn.execute(f"DELETE FROM activeplayers WHERE roomid = {roomid} AND playerid = {data['playerid']} AND name =\"{data['name']}\" ")
        conn.commit()
        log_game_events(roomid, "remove active player", data)
        if not roomid in game_states:
            game_states[roomid] = restore_states(roomid)
        game_states[roomid].remove_player(data['name'])
    all_players = conn.execute('SELECT playerid, name FROM activeplayers where roomid =='+str(roomid)).fetchall()
    return json.dumps([dict(ix) for ix in all_players])


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
        for v in data["scores"]:
            if v["name"] == "":
                continue
            if v["score"] == "":
                continue
            player_in_game.append(v["name"])
            if not "playerid" in v:
                v["playerid"] = get_id(v['name'])
            conn.execute(f"INSERT OR IGNORE INTO results VALUES ({roomid},{data['gameindex']}, {v['playerid']}, \"{v['name']}\", {v['score']} )")
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
        req = conn.execute(f"SELECT gameindex, playerid, name, score FROM results WHERE roomid == {roomid} ORDER BY gameindex")
    elif "game_index" in data:
        req = conn.execute(f"SELECT gameindex, playerid, name, score FROM results WHERE roomid == {roomid} AND gameindex == {data['gameindex']}")
    else:
        req = conn.execute(f"SELECT gameindex, playerid, name, score FROM results WHERE roomid == {roomid} ORDER BY gameindex")

    res = []
    prev_index = -1
    for r in req.fetchall():
        if r["gameindex"] != prev_index:
            res.append({"gameindex":r["gameindex"], "scores":[]})
            prev_index = r["gameindex"]
        res[-1]["scores"].append(dict(r))

    return json.dumps(res)




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
    return json.dumps( [{"name":ix[0], "weight":ix[1]} for ix in all_players])



@app.route('/fillpositions/<int:roomid>', subdomain = "svoyak", methods = ["POST"])
def ReturnPositions(roomid):
    data = request.json
    print(data)
    if not roomid in game_states:
        game_states[roomid] = restore_states(roomid)
    
    
    sub = []
    pre = []
    for i in range(len(data)):
        if data[i]["name"] == "":
            sub.append(i)
        else:
            pre.append(data[i]["name"])
            
    player_in_game = game_states[roomid].shuffle_players(pre)
    print(player_in_game)
    for i in range(len(player_in_game) - len(pre)):
        data[sub[i]]["name"] = player_in_game[i+len(pre)]
        
    return json.dumps(data)

@app.route('/predictgames/<int:roomid>/<int:games_num>', subdomain = "svoyak", methods = ["POST"])
def ReturnFuturePositions(roomid, games_num):
    data = request.json
    print(data)
    if not roomid in game_states:
        game_states[roomid] = restore_states(roomid)
    
    
    sub = []
    pre = []
    for i in range(len(data)):
        if data[i]["name"] == "":
            sub.append(i)
        else:
            pre.append(data[i]["name"])
            
    player_in_game = game_states[roomid].shuffle_players(pre)
    print(player_in_game)
    for i in range(len(player_in_game) - len(pre)):
        data[sub[i]]["name"] = player_in_game[i+len(pre)]

    data_all = [{"shift":0, "data": data}]
    
    if games_num > 1:
        loc_state = game_states[roomid].get_copy()        
        for i in range(1, games_num):
            data_all.append({"shift":i, "data": []})
            loc_state.process_one_match(player_in_game)
            player_in_game = loc_state.shuffle_players()
            for n in player_in_game:
                data_all[-1]["data"].append({"name":n})

    return json.dumps(data_all)


def rate_all(room, choused = []):
    global game_states
    print("rate all ", room)
    if not room in game_states:
        game_states[room] = restore_states(room)

    rt = estimate_rates(game_states[room].active_players, game_states[room], choused = choused)
    if len(rt) == 0:
        return([])
    else:
        return(sorted(rt.items(), key=lambda x:-x[1]))

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
    app.run(debug=True, port=port)