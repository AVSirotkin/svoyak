import os
import sqlite3
import functools
from flask import Flask, render_template, request, redirect, abort, session, g, send_from_directory
import datetime
import time
import json
import random
from shuffle import * 
from player_state import player_state
import tournament_plan
import pandas as pd
from statistics import median
from collections import defaultdict


app = Flask(__name__)
# Кириллица в JSON, который отдаётся в шаблоны, должна оставаться читаемой.
try:
    app.json.ensure_ascii = False
except AttributeError:
    pass

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


# ============================================================
# Пользователи и вход
# ============================================================

ROLE_ADMIN = "администратор"
ROLE_HOST = "ведущий"


def get_secret_key():
    """Ключ подписи сессий: из окружения, из файла рядом с приложением, иначе создаём."""
    value = os.environ.get("SVOYAK_SECRET_KEY", "").strip()
    if value:
        return value
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".secret_key")
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as handle:
                value = handle.read().strip()
            if value:
                return value
        value = os.urandom(32).hex()
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(value)
        return value
    except OSError:
        print("WARNING: .secret_key не сохранился, после перезапуска входы слетят")
        return os.urandom(32).hex()


app.secret_key = get_secret_key()


def hash_password(password):
    from werkzeug.security import generate_password_hash
    # Метод фиксируем: рабочая версия Flask старее, чем может оказаться здесь.
    return generate_password_hash(password, method="pbkdf2:sha256")


def check_password(password_hash, password):
    from werkzeug.security import check_password_hash
    try:
        return check_password_hash(password_hash, password)
    except (ValueError, TypeError):
        return False


def ensure_auth_tables():
    """Пользователи и владелец комнаты. Можно звать сколько угодно раз."""
    conn = get_db_connection()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS users ("
            " userid INTEGER PRIMARY KEY AUTOINCREMENT,"
            " login TEXT NOT NULL UNIQUE,"
            " name TEXT NOT NULL,"
            " password_hash TEXT NOT NULL,"
            " role TEXT NOT NULL DEFAULT 'ведущий',"
            " created_at TEXT)"
        )
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(activerooms)")]
        if "ownerid" not in columns:
            conn.execute("ALTER TABLE activerooms ADD COLUMN ownerid INTEGER")
        conn.commit()
    finally:
        conn.close()


def current_user():
    """Пользователь из сессии. None, если не вошёл."""
    if "_current_user" in g:
        return g._current_user
    user = None
    userid = session.get("userid")
    if userid:
        try:
            conn = get_db_connection()
            try:
                row = conn.execute(
                    "SELECT userid, login, name, role FROM users WHERE userid = ?", (userid,)
                ).fetchone()
            finally:
                conn.close()
        except sqlite3.OperationalError:
            row = None
        if row is None:
            session.pop("userid", None)
        else:
            user = dict(row)
    g._current_user = user
    return user


def is_admin(user=None):
    user = user if user is not None else current_user()
    return user is not None and user["role"] == ROLE_ADMIN


@app.context_processor
def inject_user():
    return {"current_user": current_user()}


def login_required(view):
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if current_user() is None:
            return redirect("/login?next=" + request.path)
        return view(*args, **kwargs)

    return wrapper


def api_login_required(view):
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if current_user() is None:
            return json.dumps(
                {"status": "error", "needs_login": True, "message": "Нужно войти в систему"},
                ensure_ascii=False,
            ), 401
        return view(*args, **kwargs)

    return wrapper


def room_is_finished(roomid):
    """Завершённая комната — это витрина: её смотрит кто угодно."""
    try:
        conn = get_db_connection()
        try:
            row = conn.execute(
                "SELECT finished FROM activerooms WHERE roomid = ?", (roomid,)
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return False
    return row is not None and bool(row["finished"])


def room_access_required(view=None, allow_finished=False):
    """К пульту комнаты: владелец, администратор или комната без владельца."""

    def decorator(function):
        @functools.wraps(function)
        def wrapper(*args, **kwargs):
            user = current_user()
            roomid = kwargs.get("roomid")
            if user is None:
                # Старый консольный адрес завершённого турнира уводит на витрину.
                if allow_finished and roomid is not None and room_is_finished(roomid):
                    return function(*args, **kwargs)
                return redirect("/login?next=" + request.path)
            if not can_manage_room(roomid, user):
                abort(403)
            return function(*args, **kwargs)

        return wrapper

    if view is not None:
        return decorator(view)
    return decorator


def api_room_access_required(view):
    """То же для JSON-ручек: вместо редиректа честный 401/403."""

    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        user = current_user()
        if user is None:
            return json.dumps(
                {"status": "error", "needs_login": True, "message": "Нужно войти в систему"},
                ensure_ascii=False,
            ), 401
        if not can_manage_room(kwargs.get("roomid"), user):
            return json.dumps(
                {"status": "error", "message": "Нет прав на эту комнату"}, ensure_ascii=False
            ), 403
        return view(*args, **kwargs)

    return wrapper


def can_manage_room(roomid, user=None):
    user = user if user is not None else current_user()
    if user is None:
        return False
    if user["role"] == ROLE_ADMIN:
        return True
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT ownerid FROM activerooms WHERE roomid = ?", (roomid,)
        ).fetchone()
    except sqlite3.OperationalError:
        # Колонки ownerid ещё нет: база старая, комнаты без владельца.
        return True
    finally:
        conn.close()
    if row is None:
        return True
    return row["ownerid"] is None or row["ownerid"] == user["userid"]


@app.route('/login', subdomain="svoyak", methods=["GET", "POST"])
def LoginPage():
    ensure_auth_tables()
    next_url = request.args.get("next") or request.form.get("next") or "/"
    if not next_url.startswith("/"):
        next_url = "/"
    if current_user() is not None:
        return redirect(next_url)

    error = None
    login = ""
    if request.method == "POST":
        login = str(request.form.get("login") or "").strip()
        password = str(request.form.get("password") or "")
        conn = get_db_connection()
        try:
            row = conn.execute(
                "SELECT userid, password_hash FROM users WHERE login = ?", (login,)
            ).fetchone()
        finally:
            conn.close()
        if row is None or not check_password(row["password_hash"], password):
            error = "Неверный логин или пароль"
        else:
            session.clear()
            session["userid"] = row["userid"]
            session.permanent = True
            return redirect(next_url)

    return render_template("login.html", error=error, login=login, next_url=next_url,
                           title="Вход")


@app.route('/register', subdomain="svoyak", methods=["GET", "POST"])
def RegisterPage():
    ensure_auth_tables()
    error = None
    form = {"login": "", "name": ""}
    if request.method == "POST":
        form["login"] = str(request.form.get("login") or "").strip()
        form["name"] = str(request.form.get("name") or "").strip()
        password = str(request.form.get("password") or "")
        password2 = str(request.form.get("password2") or "")
        if not form["login"] or not form["name"]:
            error = "Логин и имя обязательны"
        elif len(password) < 6:
            error = "Пароль короче шести символов"
        elif password != password2:
            error = "Пароли не совпадают"
        else:
            conn = get_db_connection()
            try:
                taken = conn.execute(
                    "SELECT 1 FROM users WHERE login = ?", (form["login"],)
                ).fetchone()
                first = conn.execute("SELECT COUNT(*) AS total FROM users").fetchone()["total"] == 0
                if taken is not None:
                    error = "Такой логин уже занят"
                else:
                    role = ROLE_ADMIN if first else ROLE_HOST
                    cursor = conn.execute(
                        "INSERT INTO users (login, name, password_hash, role, created_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (form["login"], form["name"], hash_password(password), role,
                         datetime.datetime.now().isoformat(timespec="seconds")),
                    )
                    conn.commit()
                    userid = cursor.lastrowid
            finally:
                conn.close()
            if error is None:
                session.clear()
                session["userid"] = userid
                session.permanent = True
                return redirect("/")

    return render_template("register.html", error=error, form=form, title="Регистрация")


@app.route('/logout', subdomain="svoyak", methods=["POST", "GET"])
def LogoutPage():
    session.clear()
    return redirect("/")


# ============================================================
# План турнира: жеребьёвка, пульт стола, ввод результатов
# ============================================================


def room_schema(conn, roomid):
    """Снимок правил комнаты в нормализованном виде. None, если схемы нет."""
    row = conn.execute("SELECT brackets FROM rules WHERE roomid = ?", (roomid,)).fetchone()
    if row is None or not row["brackets"]:
        return None
    try:
        return tournament_plan.normalize_schema(row["brackets"])
    except tournament_plan.PlanError:
        return None


def room_seed(conn, roomid):
    """Сохранённая жеребьёвка: место -> игрок."""
    try:
        rows = conn.execute(
            "SELECT place, playerid, name FROM tournament_seed WHERE roomid = ? ORDER BY place",
            (roomid,),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {row["place"]: tournament_plan.Player(row["playerid"], row["name"]) for row in rows}


def room_results(conn, roomid):
    """Результаты комнаты, сгруппированные по номеру боя."""
    grouped = defaultdict(list)
    for row in conn.execute(
        "SELECT gameindex, playerid, name, score, position, points FROM results "
        "WHERE roomid = ? ORDER BY gameindex, position",
        (roomid,),
    ):
        grouped[row["gameindex"]].append(dict(row))
    return grouped


def build_room_plan(conn, roomid):
    """План комнаты или None, если схема не задана."""
    schema = room_schema(conn, roomid)
    if schema is None:
        return None
    return tournament_plan.build_plan(
        schema, room_seed(conn, roomid) or None, room_results(conn, roomid)
    )


def room_overview(conn, roomid):
    room = conn.execute("SELECT * FROM activerooms WHERE roomid = ?", (roomid,)).fetchone()
    if room is None:
        return None
    rule = conn.execute("SELECT name, parameters FROM rules WHERE roomid = ?", (roomid,)).fetchone()
    params = {}
    if rule is not None and rule["parameters"]:
        try:
            params = json.loads(rule["parameters"])
        except (TypeError, ValueError):
            params = {}
    players = conn.execute(
        "SELECT playerid, name FROM activeplayers "
        "WHERE roomid = ? AND trim(name) <> '' ORDER BY rowid", (roomid,)
    ).fetchall()
    owner = None
    ownerid = room["ownerid"] if "ownerid" in room.keys() else None
    if ownerid:
        row = conn.execute("SELECT name FROM users WHERE userid = ?", (ownerid,)).fetchone()
        owner = row["name"] if row is not None else None
    return {
        "room": dict(room),
        "owner": owner,
        "rules": dict(rule) if rule is not None else None,
        "params": params if isinstance(params, dict) else {},
        "players": [dict(player) for player in players],
    }


def plan_view(plan):
    """Готовые к рендеру стадии: игры, ранжирование, ничьи."""
    tie_sizes = {}
    for groups in plan.ties.values():
        for group in groups:
            for player in group:
                tie_sizes[player.key] = len(group)

    stages = []
    for stage in plan.stages:
        item = {
            "number": stage.number,
            "kind": stage.kind,
            "title": stage.title,
            "games": [],
            "rows": [],
        }
        if stage.kind == "aggregator":
            totals = plan.totals.get(stage.match_id, {})
            for place, player in enumerate(plan.standings.get(stage.match_id, []), start=1):
                item["rows"].append({
                    "place": place,
                    "name": player.name,
                    "points": round(totals.get(player.key, 0), 2),
                    "tie": tie_sizes.get(player.key, 0),
                })
        else:
            item["games"] = [game for game in plan.games if game.stage == stage.number]
        stages.append(item)
    return stages


@app.route('/plan/<int:roomid>', subdomain="svoyak")
@room_access_required
def PlanPage(roomid):
    conn = get_db_connection()
    try:
        overview = room_overview(conn, roomid)
        if overview is None:
            return redirect(f"/init/{roomid}", code=302)
        plan = build_room_plan(conn, roomid)
        seed = room_seed(conn, roomid)
    finally:
        conn.close()
    return render_template(
        "plan.html", roomid=roomid, overview=overview, plan=plan, seed=seed,
        stages=plan_view(plan) if plan is not None else [],
        queues=[{"table": table, "games": plan.queue(table)} for table in sorted(plan.queues)]
        if plan is not None else [],
        title=f"Турнир {roomid}",
    )


@app.route('/room/<int:roomid>/<int:table>', subdomain="svoyak")
@room_access_required
def PlanTablePage(roomid, table):
    conn = get_db_connection()
    try:
        overview = room_overview(conn, roomid)
        if overview is None:
            return redirect(f"/init/{roomid}", code=302)
        plan = build_room_plan(conn, roomid)
    finally:
        conn.close()
    if plan is None:
        return redirect(f"/room/{roomid}", code=302)
    if table not in plan.queues:
        abort(404)
    allow_any_order = bool(overview["params"].get("allow_any_order"))
    queue_games = [plan.game(item) for item in plan.queue(table)]
    current = plan.current_game(table)

    # Вне очереди — только если админ турнира это разрешил. Сыгранный бой можно
    # открыть всегда: это исправление результата, а не игра вперёд.
    chosen = request.args.get("game", type=int)
    if chosen is not None:
        picked = plan.game(chosen)
        if picked is not None and picked.table == table and (
            picked.done or (allow_any_order and picked.ready)
        ):
            current = picked

    questions = None
    protocol = None
    if current is not None:
        conn = get_db_connection()
        try:
            protocol = room_protocol(conn, roomid, current.gameindex)
            questions = room_questions(conn, roomid, current.gameindex)
        finally:
            conn.close()

    # Начатый бой живёт по своему протоколу: перепривязка тем его не меняет.
    # Протокол без тем — это бой на заглушке, ему можно подставить настоящий пакет.
    topics = []
    if protocol is not None:
        topics = protocol.get("themes") or []
    if not topics and questions is not None:
        topics = questions.get("themes") or []

    # Пакет не загружен — не оставляем ведущего без пульта: подставляем
    # заглушку «Тема N / Вопрос M», чтобы ответы можно было отмечать.
    placeholder = False
    if not topics and current is not None:
        topics = placeholder_topics()
        placeholder = True

    return render_template(
        "plan_table.html", roomid=roomid, table=table, plan=plan, overview=overview,
        current=current, allow_any_order=allow_any_order, queue_games=queue_games,
        topics=topics, protocol=protocol,
        half_minus=bool(overview["params"].get("half_minus")),
        can_bind=can_manage_room(roomid),
        placeholder=placeholder,
        title=f"Стол {table} — турнир {roomid}",
    )


@app.route('/api/plan/<int:roomid>/order', subdomain="svoyak", methods=["POST"])
@api_login_required
def PlanOrderRule(roomid):
    """Админ турнира решает, можно ли играть бои стола не по порядку."""
    if not can_manage_room(roomid):
        return json.dumps(
            {"status": "error", "message": "Нет прав на эту комнату"}, ensure_ascii=False
        ), 403

    data = request.get_json(silent=True) or {}
    allow = bool(data.get("allow"))

    conn = get_tx_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT parameters FROM rules WHERE roomid = ?", (roomid,)).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return json.dumps(
                {"status": "error", "message": "У комнаты нет схемы правил"}, ensure_ascii=False
            ), 400
        params = {}
        if row["parameters"]:
            try:
                params = json.loads(row["parameters"])
            except (TypeError, ValueError):
                params = {}
        if not isinstance(params, dict):
            params = {}
        params["allow_any_order"] = allow
        conn.execute(
            "UPDATE rules SET parameters = ? WHERE roomid = ?",
            (json.dumps(params, ensure_ascii=False), roomid),
        )
        log_game_events(roomid, "plan order rule", {"allow_any_order": allow}, conn=conn)
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()

    return json.dumps({"status": "ok", "allow_any_order": allow}, ensure_ascii=False)


def PublicPlanPage(roomid):
    """Публичная страница турнира: стадии, столы, места. Без управления."""
    conn = get_db_connection()
    try:
        overview = room_overview(conn, roomid)
        if overview is None:
            abort(404)
        plan = build_room_plan(conn, roomid)
        seed = room_seed(conn, roomid)
        venue = None
        venueid = overview["room"].get("venueid")
        if venueid:
            row = conn.execute(
                "SELECT venuename FROM venues WHERE venueid = ?", (venueid,)
            ).fetchone()
            venue = row["venuename"] if row is not None else None
    finally:
        conn.close()

    if plan is None:
        # Схема не разобралась: показываем старую сетку, чтобы страница не пропала.
        return SvoyakViewBracketsPage(roomid)

    tables_view = [
        {"table": table, "current": plan.next_game(table), "games": plan.queue(table)}
        for table in sorted(plan.queues)
    ]

    return render_template(
        "view_plan.html", roomid=roomid, overview=overview, plan=plan, seed=seed,
        stages=plan_view(plan), tables_view=tables_view, venue=venue,
        winner=plan.winner(),
        title=f"Турнир {roomid}",
    )


@app.route('/api/plan/<int:roomid>/draw', subdomain="svoyak", methods=["POST"])
@api_login_required
def PlanDraw(roomid):
    """Жеребьёвка: раскладывает активных игроков по местам посева."""
    data = request.get_json(silent=True) or {}
    force = bool(data.get("force"))
    if not can_manage_room(roomid):
        return json.dumps(
            {"status": "error", "message": "Нет прав на эту комнату"}, ensure_ascii=False
        ), 403

    conn = get_tx_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        schema = room_schema(conn, roomid)
        if schema is None:
            conn.execute("ROLLBACK")
            return json.dumps(
                {"status": "error", "message": "У комнаты нет схемы правил"}, ensure_ascii=False
            ), 400

        played = conn.execute(
            "SELECT COUNT(*) AS played FROM results WHERE roomid = ?", (roomid,)
        ).fetchone()["played"]
        finished = conn.execute(
            "SELECT finished FROM activerooms WHERE roomid = ?", (roomid,)
        ).fetchone()
        if finished is not None and finished["finished"]:
            conn.execute("ROLLBACK")
            return json.dumps(
                {"status": "error", "message": "Комната уже завершена: жеребьёвка недоступна"},
                ensure_ascii=False,
            ), 409
        if played and not force:
            conn.execute("ROLLBACK")
            return json.dumps({
                "status": "error",
                "needs_force": True,
                "message": "В комнате уже есть результаты: новая жеребьёвка изменит план",
            }, ensure_ascii=False), 409

        seed_node = next(
            (node for node in schema["matches"] if node["type"] == tournament_plan.SEED), None
        )
        slots = seed_node["participantSlots"] if seed_node else 0
        players = conn.execute(
            "SELECT playerid, name FROM activeplayers "
            "WHERE roomid = ? AND trim(name) <> '' ORDER BY rowid", (roomid,)
        ).fetchall()
        if len(players) < 2:
            conn.execute("ROLLBACK")
            return json.dumps(
                {"status": "error", "message": "Для жеребьёвки нужно хотя бы два игрока"},
                ensure_ascii=False,
            ), 400

        if slots and len(players) > slots:
            players = players[:slots]

        seed_value = data.get("seed")
        try:
            seed_value = int(seed_value) if seed_value not in (None, "") else random.randrange(1, 10 ** 6)
        except (TypeError, ValueError):
            seed_value = random.randrange(1, 10 ** 6)

        ordered = [dict(player) for player in players]
        random.Random(seed_value).shuffle(ordered)

        conn.execute("DELETE FROM tournament_seed WHERE roomid = ?", (roomid,))
        for place, player in enumerate(ordered, start=1):
            conn.execute(
                "INSERT INTO tournament_seed (roomid, place, playerid, name, seed_value) "
                "VALUES (?, ?, ?, ?, ?)",
                (roomid, place, player["playerid"], player["name"], seed_value),
            )
        log_game_events(
            roomid, "draw", {"seed": seed_value, "places": [p["name"] for p in ordered]}, conn=conn
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
    return json.dumps({
        "status": "ok",
        "seed": seed_value,
        "places": [player["name"] for player in ordered],
    }, ensure_ascii=False)


def room_questions(conn, roomid, gameindex):
    """Темы, привязанные к бою. None, если админ ничего не назначал."""
    try:
        row = conn.execute(
            "SELECT packageid, payload FROM game_questions WHERE roomid = ? AND gameindex = ?",
            (roomid, gameindex),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    try:
        payload = json.loads(row["payload"])
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    payload["packageid"] = row["packageid"]
    return payload


PLACEHOLDER_THEMES = 6
PLACEHOLDER_VALUES = (10, 20, 30, 40, 50)


def placeholder_topics():
    """Заглушка, когда пакет не загружен: 6 тем по пять вопросов 10–50.

    Вопросы могут быть у ведущего на другом устройстве — тогда он только
    отмечает ответы. Отметки привязаны к номеру вопроса, поэтому настоящий
    пакет с такой же сеткой подхватит их без потерь.
    """
    themes = []
    for theme_number in range(1, PLACEHOLDER_THEMES + 1):
        themes.append({
            "topic": "Тема %s" % theme_number,
            "stage": "Заглушка",
            "questions": [
                {
                    "value": value,
                    "text": "Вопрос %s" % question_number,
                    "answer": "",
                    "form": "",
                    "source": "",
                }
                for question_number, value in enumerate(PLACEHOLDER_VALUES, start=1)
            ],
        })
    return themes


def room_packages(conn, roomid):
    try:
        rows = conn.execute(
            "SELECT packageid, name FROM packages WHERE roomid = ? ORDER BY created_at DESC",
            (roomid,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(row) for row in rows]


@app.route('/plan/<int:roomid>/questions', subdomain="svoyak")
@room_access_required
def PlanQuestionsPage(roomid):
    """Админ раскладывает темы пакета по боям."""
    conn = get_db_connection()
    try:
        overview = room_overview(conn, roomid)
        if overview is None:
            return redirect(f"/init/{roomid}", code=302)
        plan = build_room_plan(conn, roomid)
        packages = room_packages(conn, roomid)
        assigned = {}
        for game in (plan.games if plan is not None else []):
            payload = room_questions(conn, roomid, game.gameindex)
            if payload:
                assigned[game.gameindex] = {
                    "packageid": payload.get("packageid"),
                    "topics": [theme.get("topic") for theme in payload.get("themes") or []],
                }
        started = {}
        try:
            for row in conn.execute(
                "SELECT gameindex, updated_at FROM game_protocols WHERE roomid = ?", (roomid,)
            ):
                started[row["gameindex"]] = row["updated_at"]
        except sqlite3.OperationalError:
            started = {}
    finally:
        conn.close()
    if plan is None:
        return redirect(f"/room/{roomid}", code=302)
    return render_template(
        "plan_questions.html", roomid=roomid, overview=overview, plan=plan,
        packages=packages, assigned=assigned, started=started,
        title=f"Вопросы турнира {roomid}",
    )


@app.route('/api/plan/<int:roomid>/questions', subdomain="svoyak", methods=["POST"])
@api_login_required
def PlanQuestionsSave(roomid):
    """Привязка тем пакета к бою. Пустой список тем снимает привязку."""
    if not can_manage_room(roomid):
        return json.dumps(
            {"status": "error", "message": "Нет прав на эту комнату"}, ensure_ascii=False
        ), 403

    ensure_tournament_tables()
    data = request.get_json(silent=True) or {}
    try:
        gameindex = int(data.get("gameindex"))
    except (TypeError, ValueError):
        return json.dumps(
            {"status": "error", "message": "Не указан номер боя"}, ensure_ascii=False
        ), 400

    theme_indexes = data.get("themes") or []
    if not isinstance(theme_indexes, list):
        return json.dumps(
            {"status": "error", "message": "themes должен быть списком"}, ensure_ascii=False
        ), 400

    force = bool(data.get("force"))
    user = current_user()

    conn = get_tx_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Начатый бой уже идёт по своему протоколу. Менять ему темы можно
        # только с явного согласия: протокол при этом стирается.
        has_protocol = conn.execute(
            "SELECT 1 FROM game_protocols WHERE roomid = ? AND gameindex = ?",
            (roomid, gameindex),
        ).fetchone() is not None
        if has_protocol and not force:
            conn.execute("ROLLBACK")
            return json.dumps({
                "status": "conflict",
                "message": "К бою уже есть протокол. Перепривязка тем его сотрёт — "
                           "подтвердите, если бой нужно начать заново.",
            }, ensure_ascii=False), 409
        if has_protocol:
            conn.execute(
                "DELETE FROM game_protocols WHERE roomid = ? AND gameindex = ?",
                (roomid, gameindex),
            )
            log_game_events(
                roomid, "protocol cleared",
                {"gameindex": gameindex, "reason": "rebind",
                 "by": user["name"] if user else None},
                conn=conn,
            )

        if not theme_indexes:
            conn.execute(
                "DELETE FROM game_questions WHERE roomid = ? AND gameindex = ?",
                (roomid, gameindex),
            )
            log_game_events(roomid, "questions cleared", {"gameindex": gameindex}, conn=conn)
            conn.execute("COMMIT")
            return json.dumps({"status": "ok", "gameindex": gameindex, "themes": []},
                              ensure_ascii=False)

        try:
            packageid = int(data.get("packageid"))
        except (TypeError, ValueError):
            conn.execute("ROLLBACK")
            return json.dumps(
                {"status": "error", "message": "Не выбран пакет"}, ensure_ascii=False
            ), 400

        row = conn.execute(
            "SELECT name, data FROM packages WHERE packageid = ? AND roomid = ?",
            (packageid, roomid),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return json.dumps(
                {"status": "error", "message": "Пакет не найден"}, ensure_ascii=False
            ), 404

        try:
            package_data = json.loads(row["data"])
        except (TypeError, ValueError):
            package_data = {}
        topics = package_data.get("topics") if isinstance(package_data, dict) else None
        if not isinstance(topics, list):
            conn.execute("ROLLBACK")
            return json.dumps(
                {"status": "error", "message": "В пакете нет тем"}, ensure_ascii=False
            ), 400

        chosen = []
        for index in theme_indexes:
            try:
                index = int(index)
            except (TypeError, ValueError):
                continue
            if 0 <= index < len(topics):
                chosen.append(topics[index])
        if not chosen:
            conn.execute("ROLLBACK")
            return json.dumps(
                {"status": "error", "message": "Ни одной темы не выбрано"}, ensure_ascii=False
            ), 400

        payload = {
            "themes": chosen,
            "package": row["name"],
            "source_file": package_data.get("sourceFileName") if isinstance(package_data, dict) else None,
        }
        conn.execute(
            "INSERT OR REPLACE INTO game_questions (roomid, gameindex, packageid, payload, updated_at) "
            "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)",
            (roomid, gameindex, packageid, json.dumps(payload, ensure_ascii=False)),
        )
        log_game_events(
            roomid, "questions assigned",
            {"gameindex": gameindex, "packageid": packageid,
             "themes": [theme.get("topic") for theme in chosen]},
            conn=conn,
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

    return json.dumps({
        "status": "ok",
        "gameindex": gameindex,
        "themes": [theme.get("topic") for theme in chosen],
    }, ensure_ascii=False)


def room_protocol(conn, roomid, gameindex):
    """Текущее состояние протокола боя. None, если протокол ещё не сохраняли."""
    try:
        row = conn.execute(
            "SELECT payload, updated_at, updated_by FROM game_protocols "
            "WHERE roomid = ? AND gameindex = ?",
            (roomid, gameindex),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None:
        return None
    try:
        protocol = json.loads(row["payload"])
    except (TypeError, ValueError):
        return None
    if not isinstance(protocol, dict):
        return None
    protocol["updated_at"] = row["updated_at"]
    protocol["updated_by"] = row["updated_by"]
    return protocol


@app.route('/api/plan/<int:roomid>/game/<int:gameindex>/protocol', subdomain="svoyak",
           methods=["GET", "POST", "DELETE"])
@api_room_access_required
def PlanGameProtocol(roomid, gameindex):
    """Протокол боя: чтение, автосохранение по ходу игры и явный сброс."""
    ensure_tournament_tables()
    user = current_user()

    if request.method == "GET":
        conn = get_db_connection()
        try:
            protocol = room_protocol(conn, roomid, gameindex)
        finally:
            conn.close()
        return json.dumps({"status": "ok", "protocol": protocol}, ensure_ascii=False)

    if request.method == "DELETE":
        conn = get_tx_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existed = conn.execute(
                "SELECT 1 FROM game_protocols WHERE roomid = ? AND gameindex = ?",
                (roomid, gameindex),
            ).fetchone() is not None
            conn.execute(
                "DELETE FROM game_protocols WHERE roomid = ? AND gameindex = ?",
                (roomid, gameindex),
            )
            if existed:
                log_game_events(
                    roomid, "protocol cleared",
                    {"gameindex": gameindex, "by": user["name"] if user else None},
                    conn=conn,
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
        return json.dumps({"status": "ok", "protocol": None}, ensure_ascii=False)

    data = request.get_json(silent=True) or {}
    themes = data.get("themes")
    marks = data.get("marks")
    # Бой на заглушке: настоящих тем нет, но отметки сохранять уже можно.
    placeholder = bool(data.get("placeholder"))
    if not isinstance(marks, dict):
        return json.dumps(
            {"status": "error", "message": "Нужны marks"}, ensure_ascii=False
        ), 400
    if themes is not None and not isinstance(themes, list):
        return json.dumps(
            {"status": "error", "message": "themes должен быть списком"}, ensure_ascii=False
        ), 400

    conn = get_tx_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Темы шлём один раз: дальше меняются только отметки, и гонять
        # вместе с ними весь текст вопросов незачем.
        if themes is None:
            existing = conn.execute(
                "SELECT payload FROM game_protocols WHERE roomid = ? AND gameindex = ?",
                (roomid, gameindex),
            ).fetchone()
            if existing is None:
                if not placeholder:
                    conn.execute("ROLLBACK")
                    return json.dumps(
                        {"status": "error", "message": "Нужны themes"}, ensure_ascii=False
                    ), 400
                themes = []
            try:
                themes = (json.loads(existing["payload"]) or {}).get("themes") or []
            except (TypeError, ValueError):
                themes = []

        payload = {
            "themes": themes,
            "marks": marks,
            "pointer": int(data.get("pointer") or 0),
        }
        if placeholder:
            payload["placeholder"] = True
        raw = json.dumps(payload, ensure_ascii=False)
        if len(raw) > 4 * 1024 * 1024:
            conn.execute("ROLLBACK")
            return json.dumps(
                {"status": "error", "message": "Протокол слишком большой"}, ensure_ascii=False
            ), 413

        conn.execute(
            "INSERT OR REPLACE INTO game_protocols (roomid, gameindex, payload, updated_at, updated_by) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?)",
            (roomid, gameindex, raw, user["name"] if user else None),
        )
        conn.execute("COMMIT")
        saved_at = conn.execute(
            "SELECT updated_at FROM game_protocols WHERE roomid = ? AND gameindex = ?",
            (roomid, gameindex),
        ).fetchone()["updated_at"]
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()

    return json.dumps({"status": "ok", "updated_at": saved_at}, ensure_ascii=False)


@app.route('/api/plan/<int:roomid>/game/<int:gameindex>', subdomain="svoyak", methods=["POST"])
@api_login_required
def PlanSaveGame(roomid, gameindex):
    """Сохраняет бой. При расхождении состава спрашивает подтверждение."""
    data = request.get_json(silent=True) or {}
    override = bool(data.get("override"))
    if not can_manage_room(roomid):
        return json.dumps(
            {"status": "error", "message": "Нет прав на эту комнату"}, ensure_ascii=False
        ), 403

    parsed = []
    for item in data.get("scores") or []:
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
        parsed.append({"name": name, "score": score, "playerid": item.get("playerid")})
    if not parsed:
        return json.dumps(
            {"status": "error", "message": "Нет игроков для сохранения"}, ensure_ascii=False
        ), 400

    probe = get_db_connection()
    try:
        plan = build_room_plan(probe, roomid)
        known = {row["name"]: row["playerid"] for row in probe.execute("SELECT playerid, name FROM players")}
    finally:
        probe.close()

    if plan is None:
        return json.dumps(
            {"status": "error", "message": "У комнаты нет схемы правил"}, ensure_ascii=False
        ), 400
    game = plan.game(gameindex)
    if game is None:
        return json.dumps(
            {"status": "error", "message": f"Боя {gameindex} нет в схеме"}, ensure_ascii=False
        ), 404

    expected = [player for player in game.expected if player is not None]
    expected_keys = {player.key for player in expected}

    actual_keys = set()
    for item in parsed:
        playerid = item["playerid"]
        if playerid in (None, ""):
            playerid = known.get(item["name"])
        try:
            actual_keys.add(("id", int(playerid)))
        except (TypeError, ValueError):
            actual_keys.add(("name", item["name"]))

    mismatch = expected_keys != actual_keys or len(expected) != len(parsed)

    if mismatch and not override:
        return json.dumps({
            "status": "warning",
            "warning": game.warning or "roster_mismatch",
            "gameindex": gameindex,
            "message": "Состав не совпадает с тем, что следует из правил",
            "expected": [{"playerid": p.playerid, "name": p.name} for p in expected],
            "actual": [player["name"] for player in parsed],
        }, ensure_ascii=False)

    conn = get_tx_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = _score_rows(conn, parsed)
        _write_game_results(conn, roomid, gameindex, rows)
        recompute_gamenumbers(conn, roomid)
        log_game_events(
            roomid, "save game result",
            {"roomid": roomid, "gameindex": gameindex, "scores": parsed}, conn=conn,
        )
        if mismatch:
            payload = {
                "expected": [{"playerid": p.playerid, "name": p.name} for p in expected],
                "actual": [player["name"] for player in parsed],
                "warning": game.warning,
            }
            conn.execute(
                "INSERT OR REPLACE INTO game_overrides (roomid, gameindex, kind, payload) "
                "VALUES (?, ?, ?, ?)",
                (roomid, gameindex, "manual_roster", json.dumps(payload, ensure_ascii=False)),
            )
            log_game_events(roomid, "manual roster", payload, conn=conn)
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
    return json.dumps({
        "status": "ok",
        "gameindex": gameindex,
        "warning": "roster_mismatch" if mismatch else None,
        "scores": conn_rows(roomid, gameindex),
    }, ensure_ascii=False)


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
@room_access_required
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
@room_access_required(allow_finished=True)
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
@room_access_required
def SvoyakNewMainPage2(roomid):
    # Отдельного шаблона FullRoom2.html нет — ведём в обычную комнату,
    # иначе маршрут падал с TemplateNotFound.
    return redirect(f"/room/{roomid}", code=302)


@app.route('/view/<int:roomid>', subdomain = "svoyak")
def SvoyakViewPage(roomid):
    
    rules = get_room_rules(roomid)

    if "brackets" in rules:
        if not rules["brackets"] is None:
            return PublicPlanPage(roomid)


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
@api_room_access_required
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
@api_room_access_required
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
@api_room_access_required
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
@login_required
def InitRoom(roomid):
    conn = get_db_connection()
    venues = conn.execute(f"SELECT * FROM venues").fetchall()
    # print(venues)
    rules = []
    for row in conn.execute("SELECT * FROM basic_rules").fetchall():
        item = dict(row)
        item["max_tables"] = 1
        if item.get("brackets"):
            try:
                schema = tournament_plan.normalize_schema(item["brackets"])
                item["max_tables"] = tournament_plan.tables_capacity(schema)
            except tournament_plan.PlanError:
                item["max_tables"] = 1
        rules.append(item)
    return render_template("InitRoom.html", roomid = roomid, venues = venues,
                           rules = rules, title = "Инициализация комнаты")


@app.route('/admin/<int:roomid>', subdomain = "svoyak")
@room_access_required
def AdminLogView(roomid):
    conn = get_db_connection()
    log_info = conn.execute(f"SELECT rowid as id, date, roomid as room, event as name, data as json FROM log WHERE roomid = {roomid}").fetchall()
    venues = conn.execute(f"SELECT * FROM venues").fetchall()
    # print(venues)
    return render_template("RoomAdmin.html", roomid = roomid, gamelog = log_info,
                           venues = venues, title = f"Лог комнаты {roomid}")


@app.route('/removegamelog/<int:roomid>', subdomain = "svoyak", methods = ["POST"])
@api_room_access_required
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


def _score_rows(conn, scores):
    """Приводит имена к каноническим из players и склеивает строки боя."""
    rows = {}
    for item in scores:
        playerid = item["playerid"]
        playerid = int(playerid) if playerid not in (None, "") else get_id(item["name"], conn=conn)
        known = conn.execute("SELECT name FROM players WHERE playerid = ?", (playerid,)).fetchone()
        if known is None:
            playerid = get_id(item["name"], conn=conn)
            known = conn.execute("SELECT name FROM players WHERE playerid = ?", (playerid,)).fetchone()
        rows[playerid] = {"playerid": playerid, "name": known["name"], "score": item["score"]}
    return list(rows.values())


def _write_game_results(conn, roomid, gameindex, rows):
    """Пишет результаты боя: повторное сохранение заменяет прошлое."""
    positions = _average_positions([row["score"] for row in rows])
    conn.execute("DELETE FROM results WHERE roomid = ? AND gameindex = ?", (roomid, gameindex))
    for row, position in zip(rows, positions):
        points = round(5 - position + row["score"] / 1000.0, 3)
        conn.execute(
            "INSERT INTO results (roomid, gameindex, playerid, name, score, position, points, gamenumber) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
            (roomid, gameindex, row["playerid"], row["name"], row["score"], position, points),
        )


@app.route('/gameresult/<int:roomid>', subdomain="svoyak", methods=["POST"])
@api_room_access_required
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
        rows = _score_rows(conn, scores)
        _write_game_results(conn, roomid, gameindex, rows)
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
@api_room_access_required
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
@api_room_access_required
def EstimateWeigth(roomid):
    data = request.get_json(silent=True) or []
    choused = []
    for pl in data:
        if isinstance(pl, dict) and pl.get("name"):
            choused.append(pl["name"])
    all_players = rate_all(roomid, choused = choused)

    return json.dumps( [{"name":ix[0], "weight":ix[1], "skiped":ix[2], "played":ix[3]} for ix in all_players])



@app.route('/fillpositions/<int:roomid>', subdomain = "svoyak", methods = ["POST"])
@api_room_access_required
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
@api_room_access_required
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


def ensure_tournament_tables():
    """Таблицы турнирного плана: жеребьёвка и отклонения от правил."""
    conn = get_db_connection()
    try:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS tournament_seed (
                roomid     INTEGER NOT NULL,
                place      INTEGER NOT NULL,
                playerid   INTEGER,
                name       TEXT NOT NULL,
                seed_value INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (roomid, place)
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS game_overrides (
                roomid     INTEGER NOT NULL,
                gameindex  INTEGER NOT NULL,
                kind       TEXT NOT NULL,
                payload    TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (roomid, gameindex, kind)
            )
        ''')
        # Вопросы, привязанные к бою: снимок тем из пакета, который делает админ.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS game_questions (
                roomid     INTEGER NOT NULL,
                gameindex  INTEGER NOT NULL,
                packageid  INTEGER,
                payload    TEXT NOT NULL,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (roomid, gameindex)
            )
        ''')
        # Протокол боя: текущее состояние отметок. Лог остаётся историей,
        # здесь — то, что ведущий видит при открытии боя.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS game_protocols (
                roomid     INTEGER NOT NULL,
                gameindex  INTEGER NOT NULL,
                payload    TEXT NOT NULL,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_by TEXT,
                PRIMARY KEY (roomid, gameindex)
            )
        ''')
        conn.commit()
    finally:
        conn.close()


@app.route('/api/addroom', subdomain = "svoyak", methods = ["POST"])
@api_login_required
def CreateRoomApi():
    ensure_auth_tables()
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

    # Сколько столов в турнире. По умолчанию один: так играли и раньше,
    # а ведущий поднимает число, если схема допускает параллельные столы.
    try:
        tables = int(data.get("tables", 1))
    except (TypeError, ValueError):
        tables = 1
    tables = max(1, min(tables, 8))

    if rules_brackets:
        try:
            base_params = json.loads(rules_param) if rules_param else {}
        except (TypeError, ValueError):
            base_params = {}
        if not isinstance(base_params, dict):
            base_params = {}
        # Половинные минусы — настройка турнира, её задаёт организатор при создании.
        base_params["half_minus"] = bool(data.get("half_minus"))
        try:
            schema, merged_params = tournament_plan.room_rules(rules_brackets, tables, base_params)
        except tournament_plan.PlanError as error:
            conn.close()
            return json.dumps(
                {"Status": "Схема правил не разобрана: %s" % error, "RoomId": 0},
                ensure_ascii=False,
            )
        rules_brackets = json.dumps(schema, ensure_ascii=False)
        rules_param = json.dumps(merged_params, ensure_ascii=False)

    ensure_tournament_tables()


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
        check_room_id = conn.execute('SELECT roomid, ownerid FROM activerooms WHERE roomid == '+str(new_room_id)).fetchall()
        if len(check_room_id) > 0:
            if force_room_overwrite:
                # Чужую комнату перезаписывать нельзя, даже зная её номер.
                owner = current_user()
                existing_owner = check_room_id[0]["ownerid"]
                if existing_owner not in (None, owner["userid"]) and not is_admin(owner):
                    conn.close()
                    return json.dumps(
                        {"Status": "Комната принадлежит другому ведущему", "RoomId": new_room_id},
                        ensure_ascii=False,
                    ), 403
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
    owner = current_user()
    conn.execute(
        "INSERT INTO activerooms (roomid, date, venueid, finished, ownerid) "
        "VALUES (?, ?, ?, 0, ?)",
        (new_room_id, roomdate, venueid, owner["userid"] if owner else None),
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
@api_room_access_required
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
@api_room_access_required
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
@api_room_access_required
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
@api_room_access_required
def delete_package(roomid, packageid):
    ensure_packages_table()
    conn = get_db_connection()
    conn.execute('DELETE FROM packages WHERE packageid = ? AND roomid = ?', (packageid, roomid))
    conn.commit()
    return json.dumps({"status": "ok", "packageid": packageid})


@app.route('/api/packages/<int:roomid>/sync', subdomain="svoyak", methods=["POST"])
@api_room_access_required
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


@app.route('/static/<path:filename>', subdomain="svoyak")
def svoyak_static(filename):
    """Статика на поддомене: встроенный /static живёт только на голом хосте."""
    return send_from_directory(app.static_folder, filename)
# Шаблоны перечитываются с диска: иначе правка вёрстки не видна до перезапуска.
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True


@app.after_request
def _no_store_html(response):
    """HTML не кэшируем: правки должны быть видны сразу после перезагрузки."""
    if response.mimetype == 'text/html':
        response.headers['Cache-Control'] = 'no-store, max-age=0'
    return response
app.jinja_env.filters['zip'] = zip

if __name__ == "__main__":
    ensure_schema()
    ensure_auth_tables()
    debug = os.environ.get("SVOYAK_DEBUG", "").lower() in ("1", "true", "yes")
    print(f"svoyak on {app.config['SERVER_NAME']}:{port}, debug={debug}, db={DB_PATH}")
    app.run(debug=debug, port=port, threaded=True)