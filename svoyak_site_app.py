import os
import sqlite3
import functools
from flask import Flask, render_template, request, redirect, abort, session, g, send_from_directory, send_file
import datetime
import time
import json
import random
from shuffle import * 
from player_state import player_state
import tournament_plan
import telegram_link
import site_admin
import package_library
import secrets
import hmac
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
# Полоса вверху страниц: «это тестовая база». Пусто — полосы нет.
BANNER = os.environ.get("SVOYAK_BANNER", "").strip()

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
        telegram_link.ensure_tables(conn)
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
    return {"current_user": current_user(), "banner": BANNER}


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


def api_login_or_open_room(view):
    """JSON-ручка комнаты: гостю можно, если у комнаты нет владельца
    или он пришёл по ссылке управления."""

    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        roomid = kwargs.get("roomid")
        if current_user() is None and not room_is_open(roomid) \
                and not control_session_for_room(roomid) \
                and not control_key_for_room(roomid, request.args.get("key")):
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


def room_is_open(roomid):
    """Комната без владельца: пульт открыт любому, кто знает адрес.

    Такие комнаты можно создавать без регистрации, и это осознанный режим:
    ведущий предупреждён, что управлять сможет любой. Кнопка «Закрепить за
    собой» в пульте привязывает комнату к аккаунту, после чего нужен вход.
    """
    if roomid is None:
        return False
    try:
        conn = get_db_connection()
        try:
            row = conn.execute(
                "SELECT ownerid, finished FROM activerooms WHERE roomid = ?", (roomid,)
            ).fetchone()
        except sqlite3.OperationalError:
            # Колонки ownerid ещё нет: база старая, все комнаты без владельца.
            return True
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return False
    return row is not None and row["ownerid"] is None and not row["finished"]


def room_access_required(view=None, allow_finished=False):
    """К пульту комнаты: владелец, администратор, комната без владельца или
    владелец ссылки управления."""

    def decorator(function):
        @functools.wraps(function)
        def wrapper(*args, **kwargs):
            user = current_user()
            roomid = kwargs.get("roomid")
            opened = take_control_key(roomid, CONTROL_AREA_ALL)
            if opened is not None:
                return opened
            if user is None:
                # Старый консольный адрес завершённого турнира уводит на витрину.
                if allow_finished and roomid is not None and room_is_finished(roomid):
                    return function(*args, **kwargs)
                if room_is_open(roomid) or control_session_for_room(roomid):
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
        roomid = kwargs.get("roomid")
        if user is None:
            if room_is_open(roomid) or control_session_for_room(roomid) \
                    or control_key_for_room(roomid, request.args.get("key")):
                return view(*args, **kwargs)
            return json.dumps(
                {"status": "error", "needs_login": True, "message": "Нужно войти в систему"},
                ensure_ascii=False,
            ), 401
        if not can_manage_room(roomid, user) and not control_session_for_room(roomid):
            return json.dumps(
                {"status": "error", "message": "Нет прав на эту комнату"}, ensure_ascii=False
            ), 403
        return view(*args, **kwargs)

    return wrapper


def admin_required(view):
    """Панель администратора сайта: только роль «администратор»."""

    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        user = current_user()
        if user is None:
            return redirect("/login?next=" + request.path)
        if not is_admin(user):
            abort(403)
        return view(*args, **kwargs)

    return wrapper


def api_admin_required(view):
    """То же для JSON-ручек: 401 гостю и 403 не администратору."""

    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        user = current_user()
        if user is None:
            return json.dumps(
                {"status": "error", "needs_login": True, "message": "Нужно войти в систему"},
                ensure_ascii=False,
            ), 401
        if not is_admin(user):
            return json.dumps(
                {"status": "error", "message": "Только администратор сайта"}, ensure_ascii=False
            ), 403
        return view(*args, **kwargs)

    return wrapper


def can_manage_room(roomid, user=None):
    user = user if user is not None else current_user()
    if user is None:
        # Гость управляет только комнатой без владельца: такие создают без входа.
        # Либо если он открыл ссылку управления этой комнатой.
        return room_is_open(roomid) or control_session_granted(roomid, CONTROL_AREA_ALL)
    if user["role"] == ROLE_ADMIN:
        return True
    if control_session_granted(roomid, CONTROL_AREA_ALL):
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


# ---------------------------------------------------------------
# Площадки: кто какую ведёт
# ---------------------------------------------------------------
def room_area_hosts(roomid):
    """Ведущие площадок комнаты: {номер площадки: {"userid", "name"}}.

    Площадка — это то, что в схеме турнира называется столом (поле table).
    Внутри данных имя осталось прежним: оно записано в JSON правил и в
    параметрах комнаты, менять его — ломать старые турниры.
    """
    ensure_tournament_tables()
    ensure_auth_tables()
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT a.area, a.userid, u.name FROM area_hosts a "
            "LEFT JOIN users u ON u.userid = a.userid WHERE a.roomid = ?",
            (roomid,),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()
    return {
        int(row["area"]): {"userid": row["userid"], "name": row["name"]}
        for row in rows
    }


def set_area_host(roomid, area, userid, actor=None):
    """Назначает ведущего площадки. userid=None снимает назначение."""
    ensure_tournament_tables()
    conn = get_tx_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if userid is None:
            conn.execute(
                "DELETE FROM area_hosts WHERE roomid = ? AND area = ?", (roomid, area)
            )
        else:
            conn.execute(
                "INSERT INTO area_hosts (roomid, area, userid, assigned_at) "
                "VALUES (?, ?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(roomid, area) DO UPDATE SET "
                "userid = excluded.userid, assigned_at = CURRENT_TIMESTAMP",
                (roomid, area, userid),
            )
        log_game_events(
            roomid,
            "area host",
            {"area": area, "userid": userid, "by": actor["name"] if actor else None},
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
    notify_area_assigned(roomid, area, userid)


# ---------------------------------------------------------------
# Ссылка управления: единственный пульт на бой
# ---------------------------------------------------------------
# Площадка с назначенным ведущим уже закрыта для остальных. Свободная посадка
# так закрыться не может: у неё нет площадок. Для неё и для площади, которую
# делят ведущий с ассистентом, есть ссылка с ключом: кто открыл её, тот и
# правит бой. Ключ живёт в сессии браузера, после первого открытия адрес
# работает без него.
CONTROL_AREA_ALL = 0   # ключ на комнату целиком (свободная посадка)
CONTROL_SESSION_LIMIT = 12   # сколько открытых ссылок помнит сессия


def control_link(roomid, area):
    """Действующая ссылка управления: {token, created_at, created_by}."""
    ensure_auth_tables()
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT token, created_at, created_by FROM control_links "
            "WHERE roomid = ? AND area = ?",
            (roomid, int(area)),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    return dict(row) if row is not None else None


def control_room_links(roomid):
    """Ссылки управления комнаты: {номер площадки: token}."""
    ensure_auth_tables()
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT area, token FROM control_links WHERE roomid = ?", (roomid,)
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()
    return {int(row["area"]): row["token"] for row in rows}


def control_link_path(roomid, area):
    if int(area) == CONTROL_AREA_ALL:
        return f"/room/{roomid}"
    return f"/room/{roomid}/{int(area)}"


def control_link_url(roomid, area, token):
    return f"{control_link_path(roomid, area)}?key={token}"


def issue_control_link(roomid, area, actor=None):
    """Создаёт или перевыпускает ключ площадки, возвращает токен."""
    token = secrets.token_urlsafe(18)
    conn = get_tx_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO control_links (roomid, area, token, created_by) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(roomid, area) DO UPDATE SET token = excluded.token, "
            "created_by = excluded.created_by, created_at = CURRENT_TIMESTAMP",
            (roomid, int(area), token, actor["userid"] if actor else None),
        )
        log_game_events(
            roomid,
            "control link",
            {"area": int(area), "action": "issued",
             "by": actor["name"] if actor else None},
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
    return token


def revoke_control_link(roomid, area, actor=None):
    """Отключает ключ площадки: старые ссылки перестают работать."""
    ensure_auth_tables()
    conn = get_tx_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "DELETE FROM control_links WHERE roomid = ? AND area = ?",
            (roomid, int(area)),
        )
        log_game_events(
            roomid,
            "control link",
            {"area": int(area), "action": "revoked",
             "by": actor["name"] if actor else None},
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


def control_key_matches(roomid, area, key):
    """Подходит ли ключ к этой площадке."""
    if not key:
        return False
    link = control_link(roomid, area)
    if link is None:
        return False
    return hmac.compare_digest(str(key), str(link["token"]))


def control_key_grants(roomid, area, key):
    """Ключ площадки или общий ключ комнаты."""
    return (control_key_matches(roomid, area, key)
            or control_key_matches(roomid, CONTROL_AREA_ALL, key))


def control_key_for_room(roomid, key):
    """Ключ подходит хоть к одной площадке комнаты."""
    if not key:
        return False
    return any(hmac.compare_digest(str(key), str(token))
               for token in control_room_links(roomid).values())


def _control_links_cached(roomid):
    """Ссылки комнаты на время запроса: проверки прав идут часто."""
    try:
        store = getattr(g, "_control_links", None)
        if store is None:
            store = {}
            g._control_links = store
        if roomid not in store:
            store[roomid] = control_room_links(roomid)
        return store[roomid]
    except RuntimeError:   # вне контекста приложения
        return control_room_links(roomid)


def control_session_granted(roomid, area):
    """Ссылка открыта в этом браузере и ещё действует.

    В сессии лежит сам ключ: перевыпуск ссылки сразу закрывает доступ у всех,
    кому досталась старая.
    """
    try:
        granted = session.get("control_tokens") or {}
    except RuntimeError:   # вне запроса
        return False
    for candidate in (f"{int(roomid)}:{int(area)}",
                      f"{int(roomid)}:{CONTROL_AREA_ALL}"):
        token = granted.get(candidate)
        if not token:
            continue
        current = _control_links_cached(roomid).get(int(candidate.split(":")[1]))
        if current and hmac.compare_digest(str(token), str(current)):
            return True
    return False


def control_session_for_room(roomid):
    """В сессии есть действующий ключ хоть одной площадки комнаты."""
    try:
        granted = session.get("control_tokens") or {}
    except RuntimeError:
        return False
    prefix = f"{int(roomid)}:"
    current = _control_links_cached(roomid)
    for key, token in granted.items():
        if not str(key).startswith(prefix):
            continue
        try:
            area = int(str(key).split(":")[1])
        except (IndexError, ValueError):
            continue
        if current.get(area) and hmac.compare_digest(str(token), str(current[area])):
            return True
    return False


def grant_control_session(roomid, area, token):
    """Запоминает открытую ссылку: дальше адрес работает без ключа."""
    granted = dict(session.get("control_tokens") or {})
    granted.pop(f"{int(roomid)}:{int(area)}", None)
    granted[f"{int(roomid)}:{int(area)}"] = token
    # Старые площадки не копим: в сессии живут последние открытые ссылки.
    while len(granted) > CONTROL_SESSION_LIMIT:
        granted.pop(next(iter(granted)))
    session["control_tokens"] = granted
    try:
        g._control_links = {int(roomid): control_room_links(roomid)}
    except RuntimeError:
        pass


def take_control_key(roomid, area):
    """Открывает ссылку управления и уводит на тот же адрес без ключа.

    Возвращает редирект, если ключ подошёл, иначе None — пусть решает
    обычная проверка прав.
    """
    key = request.args.get("key")
    if not key:
        return None
    granted_area = int(area)
    token = key
    if not control_key_matches(roomid, area, key):
        if not control_key_matches(roomid, CONTROL_AREA_ALL, key):
            return None
        granted_area = CONTROL_AREA_ALL
        token = key
    grant_control_session(roomid, granted_area, token)
    app.logger.info("ссылка управления: комната %s, площадка %s открыта",
                    roomid, granted_area)
    return redirect(control_link_path(roomid, area), code=302)


# ---------------------------------------------------------------
# Уведомления в Telegram
# ---------------------------------------------------------------
def notify_user(userid, text):
    """Шлёт уведомление, если аккаунт привязан к Telegram.

    Сеть и база не должны ломать сохранение боя, поэтому всё глушится.
    """
    if not userid:
        return None
    try:
        conn = get_db_connection()
        try:
            return telegram_link.notify(conn, userid, text)
        finally:
            conn.close()
    except Exception as exc:
        print("WARNING: telegram notify failed:", exc)
        return False


def room_label(roomid):
    """«Турнир #8801 (Azart-12, 2026-11-08)» — начало текста уведомления."""
    parts = ["Турнир #%s" % roomid]
    try:
        conn = get_db_connection()
        try:
            row = conn.execute(
                "SELECT r.date, v.venuename FROM activerooms r "
                "LEFT JOIN venues v ON v.venueid = r.venueid WHERE r.roomid = ?",
                (roomid,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        row = None
    if row is not None:
        extras = [str(value) for value in (row["venuename"], row["date"]) if value]
        if extras:
            parts.append("(%s)" % ", ".join(extras))
    return " ".join(parts)


def telegram_footer():
    """Хвост уведомления со ссылкой на сайт, если она задана в окружении."""
    site = telegram_link.config()["site"]
    return "\n\n" + site if site else ""


def notify_area_assigned(roomid, area, userid):
    """Ведущему: его назначили на площадку."""
    if not userid:
        return
    notify_user(
        userid,
        "🎯 %s: вы ведущий площадки %s. Ваши бои — в разделе «Мои площадки».%s"
        % (room_label(roomid), area, telegram_footer()),
    )


def notify_game_saved(roomid, gameindex, area, plan):
    """Организатору и ведущему площадки: бой сохранён и что дальше на площадке."""
    actor = (current_user() or {}).get("userid")
    recipients = []
    owner = None
    try:
        conn = get_db_connection()
        try:
            row = conn.execute(
                "SELECT ownerid FROM activerooms WHERE roomid = ?", (roomid,)
            ).fetchone()
        finally:
            conn.close()
        owner = row["ownerid"] if row is not None else None
    except sqlite3.OperationalError:
        owner = None
    if owner:
        recipients.append(owner)
    host = room_area_hosts(roomid).get(area, {}).get("userid")
    if host:
        recipients.append(host)

    following = None
    if plan is not None:
        queue = list(plan.queue(area))
        try:
            following = queue[queue.index(gameindex) + 1]
        except (ValueError, IndexError):
            following = None
    tail = "Следующий бой: %s." % following if following else "Площадка закрыта."
    text = "✅ %s, площадка %s: бой %s сохранён. %s%s" % (
        room_label(roomid), area, gameindex, tail, telegram_footer()
    )
    for recipient in dict.fromkeys(recipients):
        if recipient and recipient != actor:
            notify_user(recipient, text)


def user_areas(roomid, user=None):
    """Площадки комнаты, которые назначены этому пользователю."""
    user = user if user is not None else current_user()
    if user is None:
        return []
    hosts = room_area_hosts(roomid)
    return sorted(area for area, host in hosts.items() if host["userid"] == user["userid"])


def can_manage_area(roomid, area, user=None):
    """Пульт площадки: администратор, владелец комнаты, её ведущий или
    владелец ссылки управления этой площадкой.

    Комната без владельца остаётся открытой всем вошедшим — так работали
    старые турниры и так они продолжают работать.
    """
    user = user if user is not None else current_user()
    if area is not None and control_session_granted(roomid, area):
        return True
    if user is None:
        # Гость работает только с комнатой без владельца.
        return room_is_open(roomid)
    if can_manage_room(roomid, user):
        return True
    if area is None:
        return False
    return int(area) in user_areas(roomid, user)


def room_exists(roomid):
    """Есть ли такая комната. Без неё писать протокол некуда.

    У свободной посадки схемы нет, но сама комната быть обязана: иначе
    опечатка в адресе тихо создавала бы протоколы для несуществующего боя.
    """
    try:
        conn = get_db_connection()
        try:
            row = conn.execute(
                "SELECT 1 FROM activerooms WHERE roomid = ?", (roomid,)
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        # База старой версии: мешать работе не будем.
        return True
    return row is not None


def room_plan_area(roomid, gameindex):
    """Площадка боя и признак того, что у комнаты вообще есть схема.

    Свободная посадка схемы не создаёт: бои нумерует сам пульт, площадок
    нет. Второе значение и отличает такую комнату от комнаты, где бой
    просто не из этой схемы. Пустая схема без единого боя — та же свободная
    посадка: сетку ещё не составили.
    """
    conn = get_db_connection()
    try:
        plan = build_room_plan(conn, roomid)
    finally:
        conn.close()
    if plan is None or not plan.games:
        return None, False
    game = plan.game(gameindex)
    return (None if game is None else game.table), True


def game_area(roomid, gameindex):
    """Номер площадки, к которой относится бой. None, если боя нет."""
    return room_plan_area(roomid, gameindex)[0]


def area_access_required(view):
    """Пульт площадки: организатор, назначенный ведущий этой площадки
    или владелец её ссылки управления."""

    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        user = current_user()
        roomid = kwargs.get("roomid")
        area = kwargs.get("area")
        opened = take_control_key(roomid, area)
        if opened is not None:
            return opened
        if can_manage_area(roomid, area, user):
            return view(*args, **kwargs)
        if user is None:
            front = f"/login?next={request.path}"
            if request.query_string:
                front += "%3F" + request.query_string.decode("utf-8")
            return redirect(front)
        own = user_areas(roomid, user)
        if own:
            # Ведущий открыл чужую площадку — возвращаем на свою.
            return redirect(f"/room/{roomid}/{own[0]}", code=302)
        abort(403)

    return wrapper


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


@app.route('/profile', subdomain="svoyak")
@login_required
def ProfilePage():
    """Профиль: аккаунт, привязка Telegram и журнал уведомлений."""
    ensure_auth_tables()
    user = current_user()
    cfg = telegram_link.config()
    link_token = request.args.get("link_token") or ""
    conn = get_db_connection()
    try:
        account = telegram_link.account(conn, user["userid"])
        messages = telegram_link.recent_messages(conn, user["userid"])
    finally:
        conn.close()
    return render_template(
        "profile.html",
        account=account,
        messages=messages,
        configured=telegram_link.is_configured(cfg),
        bot=cfg["bot"],
        start_link=telegram_link.start_url(cfg, link_token) if link_token else None,
        ok=request.args.get("ok"),
        error=request.args.get("error"),
        title="Профиль",
    )


@app.route('/telegram/link', subdomain="svoyak", methods=["POST"])
@login_required
def TelegramLinkStart():
    """Выдаёт одноразовую ссылку привязки и показывает её в профиле."""
    ensure_auth_tables()
    user = current_user()
    cfg = telegram_link.config()
    if not telegram_link.is_configured(cfg):
        return redirect("/profile?error=config")

    conn = get_tx_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        token = telegram_link.issue_token(conn, user["userid"], "link")
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
    return redirect("/profile?link_token=" + token)


@app.route('/telegram/unlink', subdomain="svoyak", methods=["POST"])
@login_required
def TelegramUnlink():
    """Снимает привязку. Telegram — ключ к аккаунту, поэтому нужен пароль."""
    ensure_auth_tables()
    user = current_user()
    password = str(request.form.get("password") or "")

    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT password_hash FROM users WHERE userid = ?", (user["userid"],)
        ).fetchone()
    finally:
        conn.close()
    if row is None or not check_password(row["password_hash"], password):
        return redirect("/profile?error=password")

    conn = get_db_connection()
    try:
        telegram_link.unlink_account(conn, user["userid"])
        conn.commit()
    finally:
        conn.close()
    log_game_events(0, "telegram unlink", {"userid": user["userid"], "by": user["name"]})
    return redirect("/profile?ok=unlinked")


@app.route('/telegram/webhook/<secret>', subdomain="svoyak", methods=["POST"])
def TelegramWebhook(secret):
    """Вебхук бота: сюда Telegram присылает /start link_<токен>.

    Секрет в пути и заголовке — единственная защита этого адреса:
    авторизации тут нет и быть не может.
    """
    cfg = telegram_link.config()
    if not cfg["secret"] or not secrets.compare_digest(secret, cfg["secret"]):
        abort(404)
    header = request.headers.get("X-Telegram-Bot-Api-Secret-Token") or ""
    if header and not secrets.compare_digest(header, cfg["secret"]):
        abort(403)

    payload = request.get_json(silent=True) or {}
    message = payload.get("message") or {}
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    chat_id = chat.get("id")
    text = message.get("text") or ""
    if not chat_id:
        return json.dumps({"ok": True})

    def reply(answer):
        telegram_link.send_message(cfg, chat_id, answer)
        return json.dumps({"ok": True})

    token = telegram_link.parse_start(text)
    if token is None:
        return json.dumps({"ok": True})
    if chat.get("type") not in (None, "private"):
        return reply("Привязка работает только в личном чате с ботом.")
    if not token:
        return reply(
            "Чтобы привязать аккаунт: откройте на сайте раздел «Профиль» "
            "и нажмите «Привязать Telegram»."
        )

    conn = get_tx_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = telegram_link.find_token(conn, token, "link")
        if row is None:
            conn.execute("ROLLBACK")
            return reply(
                "Ссылка привязки устарела или уже использована. "
                "Получите новую в «Профиле» на сайте."
            )
        status, login = telegram_link.link_account(
            conn, row, sender.get("id") or chat_id, sender.get("username"), chat_id
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

    if status == "busy":
        return reply(
            "Этот Telegram уже привязан к аккаунту %s. Сначала отвяжите его там." % login
        )
    if status == "gone":
        return reply("Аккаунт не найден — возможно, его удалили.")
    log_game_events(0, "telegram link", {"login": login, "chat": chat_id})
    return reply(
        "Готово: аккаунт %s привязан. Сюда будут приходить уведомления "
        "о ваших площадках." % login
    )


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


FINAL_PARTICIPANTS = {
    "best_active": "лучшие по текущему зачёту",
}


@app.template_filter('plural')
def plural_filter(number, one, few, many):
    """Русское склонение по числу: 1 игра, 2 игры, 5 игр."""
    try:
        value = abs(int(number))
    except (TypeError, ValueError):
        return many
    if value % 10 == 1 and value % 100 != 11:
        return one
    if 2 <= value % 10 <= 4 and not 12 <= value % 100 <= 14:
        return few
    return many


def free_seating_view(rules, params, played):
    """Правила комнаты без сетки: отбор, финал, сколько уже сыграно.

    Свободная посадка — это тоже правила, просто без расписанных боёв:
    ведущему полезно видеть, сколько игр в отборе и есть ли финал.
    """
    params = params if isinstance(params, dict) else {}

    def number(key):
        try:
            return int(params.get(key))
        except (TypeError, ValueError):
            return None

    participants = params.get("final_participants")
    return {
        "name": (rules or {}).get("name"),
        "qualifying": number("basic_game_number"),
        "final_players": number("final_players"),
        "final_note": FINAL_PARTICIPANTS.get(participants, participants),
        "played": played,
    }


def log_text(event, payload):
    """Событие журнала комнаты человеческим языком."""
    data = payload if isinstance(payload, dict) else {}
    name = data.get("name")
    index = data.get("gameindex")
    if event == "add active player":
        return f"добавлен игрок {name}" if name else "добавлен игрок"
    if event == "remove active player":
        return f"убран игрок {name}" if name else "убран игрок"
    if event == "save game result":
        return f"сохранён бой {index}" if index is not None else "сохранён бой"
    if event == "area host":
        area = data.get("area")
        who = data.get("by") or "гость"
        return f"ведущий площадки {area} — {who}" if area is not None else f"ведущий — {who}"
    if event == "control link":
        area = data.get("area")
        who = data.get("by") or "гость"
        where = "комнаты" if area in (0, None) else f"площадки {area}"
        if data.get("action") == "revoked":
            return f"ссылка управления {where} отключена — {who}"
        return f"выдана ссылка управления {where} — {who}"
    if event == "draw":
        places = data.get("places") or []
        return f"жеребьёвка, мест: {len(places)}" if places else "жеребьёвка"
    if event == "plan order rule":
        return ("порядок боёв можно менять" if data.get("allow_any_order")
                else "порядок боёв закреплён")
    if event == "questions assigned":
        themes = data.get("themes") or []
        if index is None:
            return "вопросы назначены"
        return f"бою {index} назначено тем: {len(themes)}"
    if event == "questions cleared":
        return f"у боя {index} сняты вопросы" if index is not None else "вопросы сняты"
    if event == "room settings":
        parts = []
        if data.get("date"):
            parts.append(f"дата {data['date']}")
        if data.get("venue"):
            parts.append(f"площадка «{data['venue']}»")
        by = data.get("by") or "гость"
        return "настройки комнаты: " + (", ".join(parts) or "без изменений") + f" — {by}"
    return event


def log_date(value):
    """Время события: в базе ctime(), в журнале — 13.11.2026 23:06."""
    try:
        return time.strftime("%d.%m.%Y %H:%M", time.strptime(str(value), "%a %b %d %H:%M:%S %Y"))
    except (TypeError, ValueError):
        return value


def room_log_view(rows):
    """Журнал комнаты для пульта турнира: свежие события сверху."""
    items = []
    for row in rows:
        try:
            payload = json.loads(row["data"]) if row["data"] else {}
        except (TypeError, ValueError):
            payload = {}
        items.append({
            "id": row["id"],
            "date": log_date(row["date"]),
            "event": row["event"],
            "text": log_text(row["event"], payload),
        })
    return items


@app.route('/plan/<int:roomid>', subdomain="svoyak")
@room_access_required
def PlanPage(roomid):
    ensure_auth_tables()
    conn = get_db_connection()
    try:
        overview = room_overview(conn, roomid)
        if overview is None:
            return redirect(f"/init/{roomid}", code=302)
        plan = build_room_plan(conn, roomid)
        seed = room_seed(conn, roomid)
        activity = room_activity(conn, roomid, plan)
        free = None
        if plan is None:
            played = conn.execute(
                "SELECT COUNT(DISTINCT gameindex) AS games FROM results WHERE roomid = ?",
                (roomid,),
            ).fetchone()["games"]
            free = free_seating_view(overview["rules"], overview["params"], played)
        hosts = conn.execute(
            "SELECT userid, name, role FROM users ORDER BY name"
        ).fetchall()
        venues = conn.execute(
            "SELECT venueid, venuename FROM venues ORDER BY venuename"
        ).fetchall()
        manage = can_manage_room(roomid)
        room_log = []
        room_log_total = 0
        if manage:
            room_log_total = conn.execute(
                "SELECT COUNT(*) AS total FROM log WHERE roomid = ?", (roomid,)
            ).fetchone()["total"]
            room_log = room_log_view(conn.execute(
                "SELECT rowid AS id, date, event, data FROM log "
                "WHERE roomid = ? ORDER BY rowid DESC LIMIT 50", (roomid,)
            ).fetchall())
    finally:
        conn.close()
    # Ссылки управления: организатору показываем действующие адреса, чтобы
    # можно было передать пульт ассистенту или закрыть свободную площадку.
    control_links = {}
    if manage:
        control_links = {
            area: control_link_url(roomid, area, token)
            for area, token in control_room_links(roomid).items()
        }
    return render_template(
        "plan.html", roomid=roomid, overview=overview, plan=plan, seed=seed,
        stages=plan_view(plan) if plan is not None else [],
        queues=[{"table": table, "games": plan.queue(table)} for table in sorted(plan.queues)]
        if plan is not None else [],
        hosts=hosts,
        area_hosts=room_area_hosts(roomid),
        control_links=control_links,
        activity=activity,
        free=free,
        manage=manage,
        room_log=room_log,
        room_log_total=room_log_total,
        venues=venues,
        title=f"Турнир {roomid}",
    )


@app.route('/my', subdomain="svoyak")
@login_required
def MyAreasPage():
    """Свои площадки: сюда ведущий приходит за пультом."""
    user = current_user()
    ensure_auth_tables()
    ensure_tournament_tables()
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT a.roomid, a.area, r.date, r.finished, r.ownerid, v.venuename "
            "FROM area_hosts a "
            "LEFT JOIN activerooms r ON r.roomid = a.roomid "
            "LEFT JOIN venues v ON v.venueid = r.venueid "
            "WHERE a.userid = ? ORDER BY a.roomid DESC, a.area",
            (user["userid"],),
        ).fetchall()
        owned = conn.execute(
            "SELECT r.roomid, r.date, r.finished, v.venuename FROM activerooms r "
            "LEFT JOIN venues v ON v.venueid = r.venueid "
            "WHERE r.ownerid = ? ORDER BY r.roomid DESC",
            (user["userid"],),
        ).fetchall()
    finally:
        conn.close()

    rooms = {}

    def entry(roomid):
        if roomid not in rooms:
            rooms[roomid] = {"roomid": roomid, "date": None, "venue": None, "areas": [], "owner": False}
        return rooms[roomid]

    for row in owned:
        item = entry(row["roomid"])
        item["date"] = row["date"]
        item["venue"] = row["venuename"]
        item["owner"] = True
    for row in rows:
        item = entry(row["roomid"])
        item["date"] = item["date"] or row["date"]
        item["venue"] = item["venue"] or row["venuename"]
        item["areas"].append(row["area"])

    ordered = sorted(rooms.values(), key=lambda item: item["roomid"], reverse=True)
    conn = get_db_connection()
    try:
        telegram = telegram_link.account(conn, user["userid"])
    finally:
        conn.close()
    return render_template(
        "my_areas.html", rooms=ordered,
        telegram=telegram, telegram_configured=telegram_link.is_configured(),
        title="Мои площадки",
    )


@app.route('/room/<int:roomid>/<int:area>', subdomain="svoyak")
@area_access_required
def PlanTablePage(roomid, area):
    table = area
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
        can_all_areas=can_manage_room(roomid),
        area_host=room_area_hosts(roomid).get(table),
        placeholder=placeholder,
        title=f"Площадка {table} — турнир {roomid}",
    )


@app.route('/api/plan/<int:roomid>/areas/<int:area>', subdomain="svoyak", methods=["POST"])
@api_login_or_open_room
def PlanAreaHost(roomid, area):
    """Организатор назначает ведущего площадки или снимает назначение."""
    if not can_manage_room(roomid):
        return json.dumps(
            {"status": "error", "message": "Ведущих площадок назначает организатор турнира"},
            ensure_ascii=False,
        ), 403

    data = request.get_json(silent=True) or {}
    raw = data.get("userid")
    userid = None
    if raw not in (None, "", 0, "0"):
        try:
            userid = int(raw)
        except (TypeError, ValueError):
            return json.dumps(
                {"status": "error", "message": "Некорректный пользователь"}, ensure_ascii=False
            ), 400
        ensure_auth_tables()
        conn = get_db_connection()
        try:
            row = conn.execute(
                "SELECT userid, name FROM users WHERE userid = ?", (userid,)
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return json.dumps(
                {"status": "error", "message": "Такого пользователя нет"}, ensure_ascii=False
            ), 404

    set_area_host(roomid, area, userid, current_user())
    return json.dumps(
        {
            "status": "ok",
            "area": area,
            "host": room_area_hosts(roomid).get(area),
        },
        ensure_ascii=False,
    )


@app.route('/api/plan/<int:roomid>/control-link', subdomain="svoyak", methods=["POST", "DELETE"])
@api_login_or_open_room
def PlanControlLink(roomid):
    """Ссылка управления площадкой: выдать, перевыпустить или отключить.

    Выдаёт организатор турнира (для любой площадки) или ведущий этой
    площадки — например, чтобы позвать ассистента. Ссылка одноразовая в том
    смысле, что старый ключ перестаёт работать после перевыпуска.
    """
    data = request.get_json(silent=True) or {}
    if "area" not in data:
        return json.dumps(
            {"status": "error", "message": "Не указана площадка"}, ensure_ascii=False
        ), 400
    try:
        area = int(data.get("area") or 0)
    except (TypeError, ValueError):
        return json.dumps(
            {"status": "error", "message": "Некорректная площадка"}, ensure_ascii=False
        ), 400
    if area < 0:
        return json.dumps(
            {"status": "error", "message": "Некорректная площадка"}, ensure_ascii=False
        ), 400
    if area != CONTROL_AREA_ALL and not can_manage_area(roomid, area):
        return json.dumps(
            {"status": "error", "message": "Нет прав на эту площадку"}, ensure_ascii=False
        ), 403
    if area == CONTROL_AREA_ALL and not can_manage_room(roomid):
        return json.dumps(
            {"status": "error", "message": "Нет прав на эту комнату"}, ensure_ascii=False
        ), 403

    actor = current_user()
    if request.method == "DELETE":
        revoke_control_link(roomid, area, actor)
        return json.dumps({"status": "ok", "area": area, "url": None}, ensure_ascii=False)

    token = issue_control_link(roomid, area, actor)
    url = control_link_url(roomid, area, token)
    return json.dumps(
        {"status": "ok", "area": area, "url": url, "token": token}, ensure_ascii=False
    )


@app.route('/api/plan/<int:roomid>/games', subdomain="svoyak")
@api_login_or_open_room
def PlanGames(roomid):
    """Бои схемы комнаты: что пульт забирает перед автономной работой.

    Свободная посадка схемы не имеет — тогда список пуст, и пульт живёт
    своим номером боя.
    """
    ensure_auth_tables()
    conn = get_db_connection()
    try:
        plan = build_room_plan(conn, roomid)
    finally:
        conn.close()
    games = []
    if plan is not None:
        games = [
            {"gameindex": game.gameindex, "table": game.table,
             "stage": game.stage, "status": game.status}
            for game in plan.games
        ]
    return json.dumps({"status": "ok", "games": games}, ensure_ascii=False)


@app.route('/api/plan/<int:roomid>/order', subdomain="svoyak", methods=["POST"])
@api_login_or_open_room
def PlanOrderRule(roomid):
    """Админ турнира решает, можно ли играть бои площадки не по порядку."""
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
@api_login_or_open_room
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


@app.route('/api/plan/<int:roomid>/settings', subdomain="svoyak", methods=["POST"])
@api_room_access_required
def PlanRoomSettings(roomid):
    """Дата и площадка комнаты.

    Раньше их задавали только при создании, а /init занятую комнату не
    настраивает — он её перезаписывает. Здесь меняем ровно эти поля.
    """
    data = request.get_json(silent=True) or {}
    updates = {}
    if "date" in data:
        try:
            updates["date"] = datetime.datetime.strptime(
                str(data["date"]).strip(), "%Y-%m-%d"
            ).strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            return json.dumps(
                {"status": "error", "message": "Дата нужна в виде ГГГГ-ММ-ДД"},
                ensure_ascii=False,
            ), 400
    venue_name = None
    if "venueid" in data:
        try:
            venueid = int(data["venueid"])
        except (TypeError, ValueError):
            return json.dumps(
                {"status": "error", "message": "Площадка должна быть числом"},
                ensure_ascii=False,
            ), 400
        conn = get_db_connection()
        try:
            venue = conn.execute(
                "SELECT venuename FROM venues WHERE venueid = ?", (venueid,)
            ).fetchone() if venueid else None
        finally:
            conn.close()
        if venueid and venue is None:
            return json.dumps(
                {"status": "error", "message": f"Площадки {venueid} нет"}, ensure_ascii=False
            ), 400
        updates["venueid"] = venueid
        venue_name = venue["venuename"] if venue is not None else "без площадки"

    if not updates:
        return json.dumps(
            {"status": "error", "message": "Нечего менять"}, ensure_ascii=False
        ), 400

    user = current_user()
    by = user["name"] if user else "гость"
    conn = get_tx_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        room = conn.execute(
            "SELECT roomid FROM activerooms WHERE roomid = ?", (roomid,)
        ).fetchone()
        if room is None:
            conn.execute("ROLLBACK")
            return json.dumps(
                {"status": "error", "message": f"Комнаты {roomid} нет"}, ensure_ascii=False
            ), 404
        assignments = ", ".join(f"{key} = ?" for key in updates)
        conn.execute(
            f"UPDATE activerooms SET {assignments} WHERE roomid = ?",
            (*updates.values(), roomid),
        )
        if "date" in updates:
            # Архив комнаты хранит свою копию даты — держим их вместе.
            conn.execute(
                "UPDATE roomhistory SET date = ? WHERE roomid = ?",
                (updates["date"], roomid),
            )
        log_game_events(
            roomid, "room settings",
            {"date": updates.get("date"), "venue": venue_name, "by": by},
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
    return json.dumps({"status": "ok"}, ensure_ascii=False)


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
            "SELECT packageid, name, libraryid FROM packages WHERE roomid = ? "
            "ORDER BY created_at DESC",
            (roomid,),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(row) for row in rows]


def free_seating_games(conn, roomid):
    """Бои комнаты со свободной посадкой: их видно по результатам и протоколам."""
    indexes = set()
    for table in ("results", "game_protocols", "game_questions"):
        try:
            rows = conn.execute(
                "SELECT DISTINCT gameindex FROM %s WHERE roomid = ?" % table, (roomid,)
            ).fetchall()
        except sqlite3.OperationalError:
            continue
        for row in rows:
            try:
                index = int(row[0])
            except (TypeError, ValueError):
                continue
            if index > 0:
                indexes.add(index)
    last = max(indexes) if indexes else 0
    # Последний бой ещё не сыгран — его тоже надо показать, чтобы было куда
    # разложить темы заранее.
    indexes.add(last + 1)
    return [{"gameindex": index, "name": "Бой %s" % index, "stage": "—",
             "table": "—", "order": index} for index in sorted(indexes)]


def room_package_for_themes(conn, packages, themes):
    """Какой пакет комнаты содержит эти темы (по вопросам, а не по названию)."""
    wanted = {package_library.theme_signature(theme) for theme in themes}
    wanted.discard(None)
    if not wanted:
        return None
    for package in packages:
        row = conn.execute(
            "SELECT data FROM packages WHERE packageid = ?", (package["packageid"],)
        ).fetchone()
        if row is None:
            continue
        signatures = {package_library.theme_signature(theme)
                      for theme in package_library.package_topics(row["data"])}
        if wanted & signatures:
            return package["packageid"]
    return None


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
        free_seating = plan is None
        if free_seating:
            # Свободная посадка: сетки нет, бои нумеруются по порядку игры.
            games = free_seating_games(conn, roomid)
        else:
            games = [{"gameindex": game.gameindex, "name": game.name,
                      "stage": game.stage, "table": game.table, "order": game.order}
                     for game in plan.games]
        packages = room_packages(conn, roomid)
        # Раскладки пакетов: готовый набор тем на бой, чтобы не отмечать шесть
        # тем из тридцати шести руками. Пакет без раскладок сюда не попадает.
        package_layouts = {}
        for package in packages:
            libraryid = package.get("libraryid")
            if not libraryid:
                continue
            try:
                rows = package_library.package_layouts(conn, libraryid)
            except sqlite3.Error:
                rows = []
            if not rows:
                continue
            data = package_library.current_package_data(conn, libraryid)
            topics = package_library.package_topics(data) if data else []
            package_layouts[str(package["packageid"])] = [
                dict(row, view=package_library.layout_parts_view(row, topics))
                for row in rows]
        layouts = {}
        try:
            for layout in package_library.theme_layouts(conn):
                if layout["roomid"] == roomid:
                    layouts[layout["gameindex"]] = layout
        except sqlite3.Error:
            layouts = {}
        assigned = {}
        for game in games:
            payload = room_questions(conn, roomid, game["gameindex"])
            if payload:
                assigned[game["gameindex"]] = {
                    "packageid": payload.get("packageid"),
                    "topics": [theme.get("topic") for theme in payload.get("themes") or []],
                    "indexes": payload.get("indexes") or [],
                    "layoutid": payload.get("layoutid"),
                    "part": payload.get("part"),
                    "source": "plan",
                }
                continue
            # Темы могли остаться только в протоколе боя: ведущий раскладывал их
            # в пульте, и в план они не попадали.
            layout = layouts.get(game["gameindex"])
            if layout is None:
                continue
            assigned[game["gameindex"]] = {
                "packageid": layout["packageid"] or room_package_for_themes(
                    conn, packages, layout["themes"]),
                "topics": [package_library.theme_title(theme) for theme in layout["themes"]],
                "source": "protocol",
            }
        started = {}
        try:
            for row in conn.execute(
                "SELECT gameindex, updated_at FROM game_protocols WHERE roomid = ?", (roomid,)
            ):
                started[row["gameindex"]] = row["updated_at"]
        except sqlite3.OperationalError:
            started = {}
        # Что из этих пакетов уже игралось где-то: админ видит пометку у темы.
        # Тема опознаётся по своим вопросам, поэтому пометка — по номеру темы.
        theme_use = {}
        try:
            package_library.ensure_library(conn)
            conn.commit()
            played = package_library.played_themes(conn)
            for package in packages:
                if not package.get("libraryid"):
                    continue
                row = conn.execute(
                    "SELECT data FROM packages WHERE packageid = ?",
                    (package["packageid"],),
                ).fetchone()
                if row is None:
                    continue
                theme_use[str(package["packageid"])] = package_library.theme_use_by_index(
                    conn, row["data"], played=played)
        except sqlite3.Error:
            theme_use = {}
    finally:
        conn.close()
    return render_template(
        "plan_questions.html", roomid=roomid, overview=overview, games=games,
        free_seating=free_seating, packages=packages, assigned=assigned, started=started,
        package_layouts=package_layouts,
        theme_use=theme_use, title=f"Вопросы турнира {roomid}",
    )


def battle_themes_for(conn, roomid, gameindex):
    """Темы боя, как они записаны: сначала план, потом раскладка из пульта."""
    payload = room_questions(conn, roomid, gameindex)
    if payload and payload.get("themes"):
        return list(payload["themes"])
    try:
        for layout in package_library.theme_layouts(conn):
            if layout["roomid"] == roomid and layout["gameindex"] == gameindex:
                return list(layout["themes"])
    except sqlite3.Error:
        return []
    return []


@app.route('/api/plan/<int:roomid>/heard', subdomain="svoyak", methods=["GET"])
@api_room_access_required
def PlanHeard(roomid):
    """Что известно о темах боя: где они уже игрались и кто их слышал.

    Пульт спрашивает это перед боем: при свободной посадке темы выбираются
    прямо перед игрой и тогда же сажают игроков, поэтому ведущему нужно сразу
    видеть и засвеченные темы, и посаженных, которые их уже слышали.
    Темы задаются номерами в пакете, а если бой разложен не из пульта —
    номером боя (gameindex). Игроки — посаженные в бой; кроме них проверяются
    все, кто уже добавлен в комнату.
    """
    packageid = request.args.get("packageid", type=int)
    gameindex = request.args.get("gameindex", type=int)
    indices = [int(part) for part in (request.args.get("themes") or "").split(",")
               if part.strip().isdigit()]
    seated = [name.strip() for name in (request.args.get("players") or "").split(",")
              if name.strip()]
    conn = get_db_connection()
    try:
        usage = []
        chosen = []
        if packageid is not None:
            row = conn.execute("SELECT data FROM packages WHERE packageid = ?",
                               (packageid,)).fetchone()
            if row is None:
                return json.dumps({"status": "error", "message": "Пакет не найден"},
                                  ensure_ascii=False), 404
            usage = package_library.theme_use_by_index(conn, row["data"])
            themes = list(package_library.package_topics(row["data"]))
            chosen = [themes[index] for index in indices if 0 <= index < len(themes)]
        elif gameindex is not None:
            chosen = battle_themes_for(conn, roomid, gameindex)
        names = [item[0] for item in conn.execute(
            "SELECT name FROM activeplayers WHERE roomid = ?", (roomid,))]
        for name in seated:
            if name not in names:
                names.append(name)
        heard = package_library.heard_by_themes(conn, chosen, names) if chosen else {}
    finally:
        conn.close()
    return json.dumps({
        # Пометки по всем темам пакета: ведущий видит их и в списке тем, и в бою.
        "themes": {str(index): value for index, value in enumerate(usage)},
        "players": heard,
    }, ensure_ascii=False)


@app.route('/api/plan/<int:roomid>/questions', subdomain="svoyak", methods=["POST"])
@api_login_or_open_room
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
        # Пакет и выбранные темы — только чтение, до каких-либо изменений.
        chosen, chosen_indexes, packageid, package_name, source_file = [], [], None, "", None
        if theme_indexes:
            try:
                packageid = int(data.get("packageid"))
            except (TypeError, ValueError):
                return json.dumps(
                    {"status": "error", "message": "Не выбран пакет"}, ensure_ascii=False
                ), 400

            row = conn.execute(
                "SELECT name, data FROM packages WHERE packageid = ? AND roomid = ?",
                (packageid, roomid),
            ).fetchone()
            if row is None:
                return json.dumps(
                    {"status": "error", "message": "Пакет не найден"}, ensure_ascii=False
                ), 404

            try:
                package_data = json.loads(row["data"])
            except (TypeError, ValueError):
                package_data = {}
            topics = package_data.get("topics") if isinstance(package_data, dict) else None
            if not isinstance(topics, list):
                return json.dumps(
                    {"status": "error", "message": "В пакете нет тем"}, ensure_ascii=False
                ), 400

            for index in theme_indexes:
                try:
                    index = int(index)
                except (TypeError, ValueError):
                    continue
                if 0 <= index < len(topics):
                    chosen.append(topics[index])
                    chosen_indexes.append(index)
            if not chosen:
                return json.dumps(
                    {"status": "error", "message": "Ни одной темы не выбрано"},
                    ensure_ascii=False
                ), 400
            package_name = row["name"]
            if isinstance(package_data, dict):
                source_file = package_data.get("sourceFileName")

        conn.execute("BEGIN IMMEDIATE")
        protocol_row = conn.execute(
            "SELECT payload FROM game_protocols WHERE roomid = ? AND gameindex = ?",
            (roomid, gameindex),
        ).fetchone()
        protocol_themes = []
        if protocol_row is not None:
            try:
                protocol_payload = json.loads(protocol_row["payload"] or "{}")
            except (TypeError, ValueError):
                protocol_payload = {}
            protocol_themes = [theme for theme in (protocol_payload.get("themes") or [])
                               if isinstance(theme, dict)]

        # Раскладка уже лежит в протоколе боя и совпадает с нашей: это не
        # перепривязка, а перенос той же раскладки в план — протокол не трогаем.
        same_layout = bool(chosen) and len(protocol_themes) == len(chosen) and (
            {package_library.theme_signature(theme) for theme in protocol_themes} ==
            {package_library.theme_signature(theme) for theme in chosen})

        # Начатый бой уже идёт по своему протоколу. Менять ему темы можно
        # только с явного согласия: протокол при этом стирается.
        has_protocol = protocol_row is not None
        if has_protocol and not force and not same_layout:
            conn.execute("ROLLBACK")
            return json.dumps({
                "status": "conflict",
                "message": "К бою уже есть протокол. Перепривязка тем его сотрёт — "
                           "подтвердите, если бой нужно начать заново.",
            }, ensure_ascii=False), 409

        if not theme_indexes:
            conn.execute(
                "DELETE FROM game_questions WHERE roomid = ? AND gameindex = ?",
                (roomid, gameindex),
            )
            log_game_events(roomid, "questions cleared", {"gameindex": gameindex}, conn=conn)
            conn.execute("COMMIT")
            return json.dumps({"status": "ok", "gameindex": gameindex, "themes": []},
                              ensure_ascii=False)

        # Кто-то из сидящих в комнате уже слышал эти темы — говорим об этом
        # ведущему, но решает он: повтор разрешён по его подтверждению.
        if not data.get("ack_heard"):
            try:
                player_rows = conn.execute(
                    "SELECT name FROM activeplayers WHERE roomid = ?", (roomid,)
                ).fetchall()
                heard = package_library.heard_by_themes(
                    conn, chosen, [row["name"] for row in player_rows])
            except sqlite3.Error:
                heard = {}
            warnings = []
            for name, items in sorted(heard.items()):
                warnings.append({
                    "name": name,
                    "places": sorted({item["place"] for item in items}),
                    "themes": sorted({item["topic"] for item in items}),
                    # В старых боях от темы осталось одно название.
                    "by_title": sorted({item["topic"] for item in items
                                        if item.get("by_title")}),
                })
            if warnings:
                conn.execute("ROLLBACK")
                return json.dumps({
                    "status": "heard",
                    "message": "Эти темы уже слышали игроки в комнате: " + "; ".join(
                        "%s (%s%s)" % (item["name"], ", ".join(item["themes"]),
                                       ", часть — по названию" if item["by_title"] else "")
                        for item in warnings) + ". Поставить всё равно?",
                    "players": warnings,
                }, ensure_ascii=False), 409

        if has_protocol and not same_layout:
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

        payload = {
            "themes": chosen,
            "package": package_name,
            "source_file": source_file,
            # Номера тем в пакете: по ним видно, какая это часть раскладки,
            # и не приходится опознавать темы по названиям.
            "indexes": chosen_indexes,
            "layoutid": data.get("layoutid"),
            "part": data.get("part"),
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


def protocol_state(payload):
    """Содержание протокола без служебных пометок о том, кто его писал.

    По нему видно, спорят ли две версии по существу или это один и тот же
    бой, сохранённый дважды (второй пульт ничего не менял).
    """
    if not isinstance(payload, dict):
        return {}
    return {
        key: value for key, value in payload.items()
        if key not in ("client", "tab", "base_revision", "v",
                       "updated_at", "updated_by", "revision", "mode")
    }


def room_protocol(conn, roomid, gameindex):
    """Текущее состояние протокола боя. None, если протокол ещё не сохраняли."""
    try:
        row = conn.execute(
            "SELECT payload, updated_at, updated_by, revision, mode FROM game_protocols "
            "WHERE roomid = ? AND gameindex = ?",
            (roomid, gameindex),
        ).fetchone()
    except sqlite3.OperationalError:
        # База старой версии: колонок ревизии и режима ещё нет.
        try:
            row = conn.execute(
                "SELECT payload, updated_at, updated_by FROM game_protocols "
                "WHERE roomid = ? AND gameindex = ?",
                (roomid, gameindex),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        if row is not None:
            row = dict(row, revision=0, mode=None)
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
    protocol["revision"] = row["revision"] or 0
    protocol["mode"] = row["mode"]
    return protocol


def room_activity(conn, roomid, plan):
    """Последняя синхронизация площадки: кто, когда и в каком режиме.

    Отдельного механизма присутствия нет: сигналом служит сам протокол,
    который площадка записывает с той частотой, что задаёт режим. Молчащая
    площадка видна организатору по старому времени синхронизации.
    """
    activity = {}
    if plan is None:
        return activity
    for table in sorted(plan.queues):
        current = plan.current_game(table)
        if current is None:
            continue
        protocol = room_protocol(conn, roomid, current.gameindex)
        if protocol is None:
            continue
        moment = None
        stamp = protocol.get("updated_at")
        if stamp:
            try:
                moment = datetime.datetime.strptime(
                    str(stamp)[:19], "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=datetime.timezone.utc).astimezone()
            except ValueError:
                moment = None
        activity[table] = {
            "gameindex": current.gameindex,
            "at": moment.isoformat() if moment else None,
            "time": moment.strftime("%H:%M") if moment else "",
            "by": protocol.get("updated_by"),
            "mode": protocol.get("mode") or "",
            "marks": len(protocol.get("marks") or {}),
            "conflicts": _open_conflicts(conn, roomid, current.gameindex),
        }
    return activity


def _open_conflicts(conn, roomid, gameindex):
    """Сколько спорных версий протокола ждут решения организатора."""
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS total FROM game_protocol_conflicts "
            "WHERE roomid = ? AND gameindex = ? AND status = 'open'",
            (roomid, gameindex),
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row["total"] or 0) if row is not None else 0


def _protocol_access(roomid, gameindex):
    """Общая проверка прав для ручек протокола. None — можно работать."""
    if not room_exists(roomid):
        app.logger.info("протокол: комнаты %s нет", roomid)
        return json.dumps(
            {"status": "error", "message": f"Комнаты {roomid} нет"}, ensure_ascii=False
        ), 404
    area, has_plan = room_plan_area(roomid, gameindex)
    if has_plan and area is None:
        # Причину отказа пишем в лог: по одному коду 404 её не разглядеть.
        app.logger.info("протокол: боя %s нет в схеме комнаты %s", gameindex, roomid)
        return json.dumps(
            {"status": "error", "message": f"Боя {gameindex} нет в схеме"}, ensure_ascii=False
        ), 404
    if has_plan:
        allowed = can_manage_area(roomid, area)
        message = "Нет прав на эту площадку"
    else:
        # Свободная посадка: площадок нет, бой ведёт тот, кому доступна комната.
        allowed = can_manage_room(roomid)
        message = "Нет прав на этот бой"
    if not allowed:
        app.logger.info("протокол: нет прав на бой %s комнаты %s", gameindex, roomid)
        return json.dumps(
            {"status": "error", "message": message}, ensure_ascii=False
        ), 403
    return None


@app.route('/api/plan/<int:roomid>/game/<int:gameindex>/protocol/history',
           subdomain="svoyak", methods=["GET"])
@api_login_or_open_room
def PlanProtocolHistory(roomid, gameindex):
    """Снимки протокола, которые были затёрты: чтобы вернуть расплюсовку."""
    ensure_tournament_tables()
    denied = _protocol_access(roomid, gameindex)
    if denied is not None:
        return denied
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT id, payload, saved_at, saved_by FROM game_protocol_history "
            "WHERE roomid = ? AND gameindex = ? ORDER BY id DESC LIMIT 20",
            (roomid, gameindex),
        ).fetchall()
    finally:
        conn.close()
    items = []
    for row in rows:
        try:
            payload = json.loads(row["payload"]) or {}
        except (TypeError, ValueError):
            payload = {}
        marks = payload.get("marks") or {}
        items.append({
            "id": row["id"],
            "saved_at": row["saved_at"],
            "saved_by": row["saved_by"],
            "marks": len(marks),
            "plus": sum(1 for value in marks.values() if value == "plus"),
            "pointer": payload.get("pointer"),
        })
    return json.dumps({"status": "ok", "history": items}, ensure_ascii=False)


@app.route('/api/plan/<int:roomid>/game/<int:gameindex>/protocol/restore',
           subdomain="svoyak", methods=["POST"])
@api_login_or_open_room
def PlanProtocolRestore(roomid, gameindex):
    """Возвращает протокол из истории (снимок текущего тоже сохраняется)."""
    ensure_tournament_tables()
    denied = _protocol_access(roomid, gameindex)
    if denied is not None:
        return denied
    data = request.get_json(silent=True) or {}
    try:
        snapshot_id = int(data.get("id"))
    except (TypeError, ValueError):
        return json.dumps(
            {"status": "error", "message": "Нужен id снимка"}, ensure_ascii=False
        ), 400
    user = current_user()
    conn = get_tx_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        snapshot = conn.execute(
            "SELECT payload FROM game_protocol_history WHERE id = ? AND roomid = ? AND gameindex = ?",
            (snapshot_id, roomid, gameindex),
        ).fetchone()
        if snapshot is None:
            conn.execute("ROLLBACK")
            return json.dumps(
                {"status": "error", "message": "Снимок не найден"}, ensure_ascii=False
            ), 404
        current = conn.execute(
            "SELECT payload, revision FROM game_protocols WHERE roomid = ? AND gameindex = ?",
            (roomid, gameindex),
        ).fetchone()
        if current is not None and current["payload"] != snapshot["payload"]:
            conn.execute(
                "INSERT INTO game_protocol_history (roomid, gameindex, payload, saved_by) "
                "VALUES (?, ?, ?, ?)",
                (roomid, gameindex, current["payload"], user["name"] if user else None),
            )
        if current is None:
            conn.execute(
                "INSERT INTO game_protocols "
                "(roomid, gameindex, payload, updated_at, updated_by, revision) "
                "VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?, 1)",
                (roomid, gameindex, snapshot["payload"], user["name"] if user else None),
            )
        else:
            conn.execute(
                "UPDATE game_protocols SET payload = ?, updated_at = CURRENT_TIMESTAMP, "
                "updated_by = ?, revision = ? WHERE roomid = ? AND gameindex = ?",
                (snapshot["payload"], user["name"] if user else None,
                 (current["revision"] or 0) + 1, roomid, gameindex),
            )
        log_game_events(
            roomid, "protocol restored",
            {"gameindex": gameindex, "snapshot": snapshot_id,
             "by": user["name"] if user else None},
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
    return json.dumps({"status": "ok"}, ensure_ascii=False)


def _protocol_summary(payload):
    """Короткая сводка версии протокола: по ней организатор сравнивает спорные."""
    payload = payload or {}
    marks = payload.get("marks") or {}
    return {
        "marks": len(marks),
        "plus": sum(1 for value in marks.values() if value == "plus"),
        "pointer": payload.get("pointer"),
        "scores": payload.get("scores") or {},
        "scoresSource": payload.get("scoresSource"),
        "finished": bool(payload.get("finished")),
        "players": [
            item.get("name") for item in (payload.get("players") or [])
            if isinstance(item, dict)
        ],
    }


@app.route('/api/plan/<int:roomid>/game/<int:gameindex>/protocol/conflicts',
           subdomain="svoyak", methods=["GET"])
@api_login_required
def PlanProtocolConflicts(roomid, gameindex):
    """Спорные версии протокола: обе сохранены, выбирает организатор."""
    ensure_tournament_tables()
    area, has_plan = room_plan_area(roomid, gameindex)
    if has_plan and area is None:
        return json.dumps(
            {"status": "error", "message": f"Боя {gameindex} нет в схеме"}, ensure_ascii=False
        ), 404
    if not can_manage_room(roomid):
        return json.dumps(
            {"status": "error", "message": "Спорные версии разбирает организатор"},
            ensure_ascii=False,
        ), 403
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT id, payload, revision, author, client, created_at, status "
            "FROM game_protocol_conflicts WHERE roomid = ? AND gameindex = ? "
            "ORDER BY id DESC LIMIT 20",
            (roomid, gameindex),
        ).fetchall()
        current = room_protocol(conn, roomid, gameindex)
    finally:
        conn.close()
    conflicts = []
    for row in rows:
        try:
            payload = json.loads(row["payload"]) or {}
        except (TypeError, ValueError):
            payload = {}
        item = _protocol_summary(payload)
        item.update({
            "id": row["id"],
            "created_at": row["created_at"],
            "author": row["author"],
            "client": row["client"],
            "revision": row["revision"],
            "status": row["status"],
        })
        conflicts.append(item)
    return json.dumps({
        "status": "ok",
        "conflicts": conflicts,
        "current": _protocol_summary(current) if current else None,
    }, ensure_ascii=False)


@app.route('/api/plan/<int:roomid>/game/<int:gameindex>/protocol/conflicts/<int:conflict_id>',
           subdomain="svoyak", methods=["POST"])
@api_login_required
def PlanProtocolConflictChoose(roomid, gameindex, conflict_id):
    """Организатор выбирает, какая версия протокола верна. Вторая остаётся в истории."""
    ensure_tournament_tables()
    area, has_plan = room_plan_area(roomid, gameindex)
    if has_plan and area is None:
        return json.dumps(
            {"status": "error", "message": f"Боя {gameindex} нет в схеме"}, ensure_ascii=False
        ), 404
    if not can_manage_room(roomid):
        return json.dumps(
            {"status": "error", "message": "Спорные версии разбирает организатор"},
            ensure_ascii=False,
        ), 403
    data = request.get_json(silent=True) or {}
    action = data.get("action") or "choose"
    if action not in ("choose", "reject"):
        return json.dumps(
            {"status": "error", "message": "action: choose или reject"}, ensure_ascii=False
        ), 400
    user = current_user()
    author = user["name"] if user else None
    conn = get_tx_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT payload, status FROM game_protocol_conflicts "
            "WHERE id = ? AND roomid = ? AND gameindex = ?",
            (conflict_id, roomid, gameindex),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return json.dumps(
                {"status": "error", "message": "Спорная версия не найдена"}, ensure_ascii=False
            ), 404
        if row["status"] != "open":
            conn.execute("ROLLBACK")
            return json.dumps(
                {"status": "error", "message": "Эта версия уже разобрана"}, ensure_ascii=False
            ), 409
        if action == "choose":
            current = conn.execute(
                "SELECT payload, revision FROM game_protocols "
                "WHERE roomid = ? AND gameindex = ?",
                (roomid, gameindex),
            ).fetchone()
            if current is not None and current["payload"] != row["payload"]:
                conn.execute(
                    "INSERT INTO game_protocol_history (roomid, gameindex, payload, saved_by) "
                    "VALUES (?, ?, ?, ?)",
                    (roomid, gameindex, current["payload"], author),
                )
            if current is None:
                conn.execute(
                    "INSERT INTO game_protocols "
                    "(roomid, gameindex, payload, updated_at, updated_by, revision) "
                    "VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?, 1)",
                    (roomid, gameindex, row["payload"], author),
                )
            else:
                conn.execute(
                    "UPDATE game_protocols SET payload = ?, updated_at = CURRENT_TIMESTAMP, "
                    "updated_by = ?, revision = ? WHERE roomid = ? AND gameindex = ?",
                    (row["payload"], author, (current["revision"] or 0) + 1,
                     roomid, gameindex),
                )
        conn.execute(
            "UPDATE game_protocol_conflicts SET status = ?, resolved_at = CURRENT_TIMESTAMP, "
            "resolved_by = ? WHERE id = ?",
            ("chosen" if action == "choose" else "rejected", author, conflict_id),
        )
        if action == "choose":
            # Остальные открытые версии этого боя считаем отклонёнными: выбор сделан.
            conn.execute(
                "UPDATE game_protocol_conflicts SET status = 'rejected', "
                "resolved_at = CURRENT_TIMESTAMP, resolved_by = ? "
                "WHERE roomid = ? AND gameindex = ? AND status = 'open'",
                (author, roomid, gameindex),
            )
        log_game_events(
            roomid, "protocol conflict resolved",
            {"gameindex": gameindex, "conflict": conflict_id, "action": action,
             "by": author},
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
    return json.dumps({"status": "ok"}, ensure_ascii=False)


@app.route('/api/plan/<int:roomid>/game/<int:gameindex>/protocol', subdomain="svoyak",
           methods=["GET", "POST", "DELETE"])
@api_login_or_open_room
def PlanGameProtocol(roomid, gameindex):
    """Протокол боя: чтение, автосохранение по ходу игры и явный сброс."""
    ensure_tournament_tables()
    user = current_user()

    if not room_exists(roomid):
        app.logger.info("протокол: комнаты %s нет", roomid)
        return json.dumps(
            {"status": "error", "message": f"Комнаты {roomid} нет"}, ensure_ascii=False
        ), 404
    area, has_plan = room_plan_area(roomid, gameindex)
    if has_plan and area is None:
        app.logger.info("протокол: боя %s нет в схеме комнаты %s", gameindex, roomid)
        return json.dumps(
            {"status": "error", "message": f"Боя {gameindex} нет в схеме"}, ensure_ascii=False
        ), 404
    allowed = can_manage_area(roomid, area, user) if has_plan else can_manage_room(roomid, user)
    if not allowed:
        app.logger.info("протокол: нет прав на бой %s комнаты %s", gameindex, roomid)
        return json.dumps(
            {"status": "error", "message": "Нет прав на этот бой"}, ensure_ascii=False
        ), 403

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
    # Режим синхронизации площадки: по нему организатор видит, как часто
    # ведущий выходит на связь. Неизвестное значение просто не записываем.
    mode = data.get("mode")
    if mode not in ("sync", "limited", "autonomous"):
        mode = None
    client = str(data.get("client") or "")[:64] or None
    try:
        base_revision = int(data.get("base_revision"))
    except (TypeError, ValueError):
        base_revision = None
    author = user["name"] if user else None

    # Указатель двигает чтец: ревизию не трогаем, чтобы не мешать тому, кто
    # ведёт счёт. Это единственная запись протокола, не создающая конфликтов.
    if data.get("pointer_only"):
        conn = get_tx_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT payload, revision FROM game_protocols "
                "WHERE roomid = ? AND gameindex = ?",
                (roomid, gameindex),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return json.dumps(
                    {"status": "error", "message": "Протокол ещё не начат"},
                    ensure_ascii=False,
                ), 409
            try:
                payload = json.loads(row["payload"]) or {}
            except (TypeError, ValueError):
                payload = {}
            payload["pointer"] = int(data.get("pointer") or 0)
            conn.execute(
                "UPDATE game_protocols SET payload = ?, updated_at = CURRENT_TIMESTAMP, "
                "updated_by = ?, mode = COALESCE(?, mode) "
                "WHERE roomid = ? AND gameindex = ?",
                (json.dumps(payload, ensure_ascii=False), author, mode, roomid, gameindex),
            )
            conn.execute("COMMIT")
            revision = row["revision"] or 0
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()
        return json.dumps({"status": "ok", "revision": revision}, ensure_ascii=False)

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
        existing = conn.execute(
            "SELECT payload, revision FROM game_protocols "
            "WHERE roomid = ? AND gameindex = ?",
            (roomid, gameindex),
        ).fetchone()
        previous_payload = {}
        if existing is not None:
            try:
                previous_payload = json.loads(existing["payload"]) or {}
            except (TypeError, ValueError):
                previous_payload = {}
        current_revision = (existing["revision"] or 0) if existing is not None else 0

        # Темы шлём один раз: дальше меняются только отметки, и гонять
        # вместе с ними весь текст вопросов незачем.
        if themes is None:
            if existing is None and not placeholder:
                conn.execute("ROLLBACK")
                return json.dumps(
                    {"status": "error", "message": "Нужны themes"}, ensure_ascii=False
                ), 400
            themes = previous_payload.get("themes") or []

        payload = {
            "v": 2,
            "themes": themes,
            "marks": marks,
            "pointer": int(data.get("pointer") or 0),
        }
        # Поля, которых может не быть у старого клиента, берём из прежней
        # версии: иначе автосохранение из старого пульта стёрло бы состав.
        for key, default in (
            ("players", []), ("scores", {}), ("scoresSource", "marks"),
            ("halfMinus", False), ("finished", False),
            # Кто писал протокол: браузер и окно. Без этого не разобраться,
            # откуда взялась чужая ревизия.
            ("client", None), ("tab", None),
        ):
            if key in data:
                payload[key] = data[key]
            elif key in previous_payload:
                payload[key] = previous_payload[key]
            else:
                payload[key] = default
        if placeholder or previous_payload.get("placeholder"):
            payload["placeholder"] = True
        # Заготовка следующего боя (пустые места, те же темы) ничего не описывает:
        # не заводим бой, которого не было. Уже существующий бой так не чистим —
        # обнулить сыгранный бой ведущий вправе.
        if existing is None and not placeholder and package_library.protocol_is_empty(payload):
            has_results = conn.execute(
                "SELECT 1 FROM results WHERE roomid = ? AND gameindex = ? LIMIT 1",
                (roomid, gameindex),
            ).fetchone()
            if has_results is None:
                conn.execute("COMMIT")
                return json.dumps(
                    {"status": "ok", "skipped": "empty", "revision": 0},
                    ensure_ascii=False)
        raw = json.dumps(payload, ensure_ascii=False)
        if len(raw) > 4 * 1024 * 1024:
            conn.execute("ROLLBACK")
            return json.dumps(
                {"status": "error", "message": "Протокол слишком большой"}, ensure_ascii=False
            ), 413

        # Версии разошлись: присланную сохраняем отдельно, текущую не трогаем.
        # Организатор увидит обе и выберет, какая верна.
        if (base_revision is not None and existing is not None
                and base_revision != current_revision):
            # Спорить не о чем, если прислали ровно то, что уже лежит: так
            # выглядит второй пульт, который ничего не менял.
            if protocol_state(payload) == protocol_state(previous_payload):
                conn.execute("COMMIT")
                current = room_protocol(conn, roomid, gameindex)
                return json.dumps({
                    "status": "ok",
                    "message": "Версия уже такая же",
                    "revision": current_revision,
                    "protocol": current,
                }, ensure_ascii=False)
            cursor = conn.execute(
                "INSERT INTO game_protocol_conflicts "
                "(roomid, gameindex, payload, revision, author, client) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (roomid, gameindex, raw, current_revision, author, client),
            )
            conflict_id = cursor.lastrowid
            log_game_events(
                roomid, "protocol conflict",
                {"gameindex": gameindex, "conflict": conflict_id,
                 "base_revision": base_revision, "revision": current_revision,
                 "by": author},
                conn=conn,
            )
            conn.execute("COMMIT")
            current = room_protocol(conn, roomid, gameindex)
            return json.dumps({
                "status": "conflict",
                "message": "Версия разошлась с сервером: сохранена отдельно, "
                           "ждёт решения организатора",
                "conflict_id": conflict_id,
                "revision": current_revision,
                "protocol": current,
            }, ensure_ascii=False), 409

        if existing is not None and existing["payload"] != raw:
            previous_marks = previous_payload.get("marks") or {}
            # Снимок делаем, когда отметок стало меньше: именно так выглядит
            # случайный сброс. Обычные правки историю не засоряют.
            if len(marks) < len(previous_marks):
                conn.execute(
                    "INSERT INTO game_protocol_history (roomid, gameindex, payload, saved_by) "
                    "VALUES (?, ?, ?, ?)",
                    (roomid, gameindex, existing["payload"], author),
                )
                conn.execute(
                    "DELETE FROM game_protocol_history WHERE roomid = ? AND gameindex = ? "
                    "AND id NOT IN (SELECT id FROM game_protocol_history "
                    "WHERE roomid = ? AND gameindex = ? ORDER BY id DESC LIMIT 20)",
                    (roomid, gameindex, roomid, gameindex),
                )
        if existing is None:
            new_revision = 1
            conn.execute(
                "INSERT INTO game_protocols "
                "(roomid, gameindex, payload, updated_at, updated_by, revision, mode) "
                "VALUES (?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?)",
                (roomid, gameindex, raw, author, new_revision, mode),
            )
        else:
            new_revision = current_revision + 1
            conn.execute(
                "UPDATE game_protocols SET payload = ?, updated_at = CURRENT_TIMESTAMP, "
                "updated_by = ?, revision = ?, mode = COALESCE(?, mode) "
                "WHERE roomid = ? AND gameindex = ?",
                (raw, author, new_revision, mode, roomid, gameindex),
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

    return json.dumps(
        {"status": "ok", "updated_at": saved_at, "revision": new_revision},
        ensure_ascii=False,
    )


@app.route('/api/plan/<int:roomid>/game/<int:gameindex>', subdomain="svoyak", methods=["POST"])
@api_login_or_open_room
def PlanSaveGame(roomid, gameindex):
    """Сохраняет бой. При расхождении состава спрашивает подтверждение."""
    data = request.get_json(silent=True) or {}
    override = bool(data.get("override"))

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
    if not can_manage_area(roomid, game.table):
        return json.dumps(
            {"status": "error", "message": "Нет прав на эту площадку"}, ensure_ascii=False
        ), 403

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
    notify_game_saved(roomid, gameindex, game.table, plan)
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

    # Темы боя по раскладке организатора: план турнира уже знает, что играют в
    # этом бою, и пульту не надо собирать шесть тем из тридцати шести руками.
    plan_battle = None
    try:
        payload = room_questions(conn, roomid, gameindex)
    except sqlite3.Error:
        payload = None
    if payload and payload.get("themes"):
        package_name = ""
        if payload.get("packageid"):
            row = conn.execute("SELECT name FROM packages WHERE packageid = ?",
                               (payload["packageid"],)).fetchone()
            package_name = row["name"] if row is not None else ""
        plan_battle = {
            "key": "%s:%s" % (roomid, gameindex),
            "gameindex": gameindex,
            "packageid": payload.get("packageid"),
            "name": "План: " + (package_name or "темы боя"),
            "layoutid": payload.get("layoutid"),
            "part": payload.get("part"),
            "topics": payload["themes"],
        }
    conn.close()

    rules = get_room_rules(roomid)
    # with open("12-2-azart.json", "rt", encoding="UTF8") as fo:
    #     rules["brackets"] = json.load(fo)    

    return render_template("FullRoom.html", roomid = roomid, Players = all_players,
                           gameindex = gameindex, rules = rules, plan_battle = plan_battle)

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
    return json.dumps(_active_players(roomid), ensure_ascii=False)


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


def _active_players(roomid, with_heard=True):
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT playerid, name FROM activeplayers WHERE roomid = ? ORDER BY name", (roomid,)
        ).fetchall()
        players = [dict(r) for r in rows]
        if with_heard and players:
            # Кто из сидящих в комнате уже слышал разложенные здесь темы.
            try:
                heard = package_library.heard_before(
                    conn, roomid, [player["name"] for player in players])
            except sqlite3.Error:
                heard = {}
            for player in players:
                player["heard"] = heard.get(player["name"]) or []
        return players
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
def InitRoom(roomid):
    # Без @login_required: комнату можно создать, не регистрируясь. Она будет
    # без владельца, и об этом предупреждает шаблон.
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


def _admin_error(message, code=400):
    return json.dumps({"status": "error", "message": message}, ensure_ascii=False), code


def _admin_count(conn):
    row = conn.execute(
        "SELECT COUNT(*) FROM users WHERE role = ?", (ROLE_ADMIN,)
    ).fetchone()
    return int(row[0])


@app.route('/admin', subdomain="svoyak")
@admin_required
def AdminPage():
    """Панель администратора сайта: турниры, пользователи, резервные копии."""
    ensure_auth_tables()
    ensure_tournament_tables()
    query = (request.args.get("q") or "").strip()
    state = request.args.get("state") or "all"
    if state not in ("all", "active", "finished"):
        state = "all"
    conn = get_db_connection()
    try:
        sql = (
            "SELECT r.roomid, r.date, r.finished, r.ownerid, u.name AS owner, "
            "u.login AS owner_login, v.venuename, "
            "(SELECT name FROM rules WHERE roomid = r.roomid) AS rule_name, "
            "(SELECT COUNT(*) FROM activeplayers WHERE roomid = r.roomid "
            " AND trim(name) <> '') AS players, "
            "(SELECT COUNT(DISTINCT gameindex) FROM results WHERE roomid = r.roomid) AS games "
            "FROM activerooms r "
            "LEFT JOIN users u ON u.userid = r.ownerid "
            "LEFT JOIN venues v ON v.venueid = r.venueid "
        )
        where, params = [], []
        if state == "active":
            where.append("r.finished = 0")
        elif state == "finished":
            where.append("r.finished = 1")
        if query:
            where.append("(CAST(r.roomid AS TEXT) LIKE ? OR u.name LIKE ? "
                         "OR u.login LIKE ? OR v.venuename LIKE ?)")
            like = "%" + query + "%"
            params.extend([like, like, like, like])
        if where:
            sql += "WHERE " + " AND ".join(where) + " "
        sql += "ORDER BY r.roomid DESC LIMIT 300"
        rooms = conn.execute(sql, params).fetchall()
        users = conn.execute(
            "SELECT u.userid, u.login, u.name, u.role, u.created_at, u.telegram_username, "
            "(SELECT COUNT(*) FROM activerooms WHERE ownerid = u.userid) AS rooms, "
            "(SELECT COUNT(*) FROM area_hosts WHERE userid = u.userid) AS areas "
            "FROM users u ORDER BY u.userid"
        ).fetchall()
    finally:
        conn.close()
    return render_template(
        "admin.html",
        rooms=rooms,
        users=users,
        backups=site_admin.list_backups(DB_PATH),
        query=query,
        state=state,
        me=current_user(),
        next_roomid_template=None,
        title="Администратор сайта",
    )


@app.route('/api/admin/rooms/<int:roomid>/delete', subdomain="svoyak", methods=["POST"])
@api_admin_required
def AdminDeleteRoom(roomid):
    """Удаляет комнату, но сначала кладёт её полную копию рядом с базой."""
    data = request.get_json(silent=True) or {}
    if str(data.get("confirm") or "").strip() != str(roomid):
        return _admin_error("Для удаления наберите номер комнаты")
    user = current_user()
    author = f"{user['name']} ({user['login']})"
    conn = get_tx_connection()
    dump = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        dump = site_admin.room_dump(conn, roomid)
        # Копию пишем до удаления: база и диск не переживут падение посередине.
        path = site_admin.save_dump(DB_PATH, dump, author)
        removed = site_admin.delete_room(conn, roomid)
        conn.execute("COMMIT")
    except site_admin.AdminError as error:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        return _admin_error(str(error), 404)
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
    # Кэш весов старой комнаты больше не нужен.
    game_states.pop(roomid, None)
    return json.dumps({
        "status": "ok",
        "backup": os.path.basename(path),
        "removed": removed,
    }, ensure_ascii=False)


@app.route('/api/admin/rooms/<int:roomid>/renumber', subdomain="svoyak", methods=["POST"])
@api_admin_required
def AdminRenumberRoom(roomid):
    """Переносит комнату на другой номер вместе со всеми её записями."""
    data = request.get_json(silent=True) or {}
    try:
        new_roomid = int(data.get("roomid"))
    except (TypeError, ValueError):
        return _admin_error("Нужен новый номер комнаты")
    if new_roomid <= 0:
        return _admin_error("Номер должен быть положительным")
    user = current_user()
    by = f"{user['name']} ({user['login']})"
    conn = get_tx_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        moved = site_admin.renumber_room(conn, roomid, new_roomid)
        log_game_events(
            new_roomid, "room renumbered", {"from": roomid, "to": new_roomid, "by": by},
            conn=conn,
        )
        conn.execute("COMMIT")
    except site_admin.AdminError as error:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        return _admin_error(str(error))
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
    game_states.pop(roomid, None)
    game_states.pop(new_roomid, None)
    return json.dumps({"status": "ok", "roomid": new_roomid, "moved": moved}, ensure_ascii=False)


@app.route('/api/admin/rooms/<int:roomid>/owner', subdomain="svoyak", methods=["POST"])
@api_admin_required
def AdminRoomOwner(roomid):
    """Передаёт комнату другому ведущему или снимает владельца."""
    data = request.get_json(silent=True) or {}
    userid = data.get("userid")
    if userid in (None, "", 0, "0"):
        ownerid = None
    else:
        try:
            ownerid = int(userid)
        except (TypeError, ValueError):
            return _admin_error("Владелец должен быть числом")
    conn = get_db_connection()
    try:
        room = conn.execute(
            "SELECT roomid FROM activerooms WHERE roomid = ?", (roomid,)
        ).fetchone()
        if room is None:
            return _admin_error(f"Комнаты {roomid} нет", 404)
        if ownerid is not None:
            person = conn.execute(
                "SELECT userid FROM users WHERE userid = ?", (ownerid,)
            ).fetchone()
            if person is None:
                return _admin_error(f"Пользователя {ownerid} нет")
        conn.execute(
            "UPDATE activerooms SET ownerid = ? WHERE roomid = ?", (ownerid, roomid)
        )
        conn.commit()
    finally:
        conn.close()
    return json.dumps({"status": "ok", "ownerid": ownerid}, ensure_ascii=False)


@app.route('/admin/backups/<path:name>', subdomain="svoyak")
@admin_required
def AdminBackupFile(name):
    """Скачать резервную копию удалённой комнаты."""
    try:
        path = site_admin.backup_path(DB_PATH, name)
    except site_admin.AdminError as error:
        abort(404, description=str(error))
    return send_file(path, as_attachment=True, download_name=os.path.basename(path))


@app.route('/api/admin/rooms/restore', subdomain="svoyak", methods=["POST"])
@api_admin_required
def AdminRestoreRoom():
    """Возвращает комнату из резервной копии — под своим или новым номером."""
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()
    if not name:
        return _admin_error("Не выбрана резервная копия")
    try:
        dump = site_admin.load_dump(site_admin.backup_path(DB_PATH, name))
    except site_admin.AdminError as error:
        return _admin_error(str(error), 404)
    roomid = data.get("roomid")
    if roomid in (None, ""):
        target = None
    else:
        try:
            target = int(roomid)
        except (TypeError, ValueError):
            return _admin_error("Номер комнаты должен быть числом")
        if target <= 0:
            return _admin_error("Номер должен быть положительным")
    conn = get_tx_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        restored = site_admin.restore_room(conn, dump, target)
        conn.execute("COMMIT")
    except site_admin.AdminError as error:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        return _admin_error(str(error))
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
    game_states.pop(restored, None)
    return json.dumps({"status": "ok", "roomid": restored}, ensure_ascii=False)


@app.route('/api/admin/users/<int:userid>/role', subdomain="svoyak", methods=["POST"])
@api_admin_required
def AdminUserRole(userid):
    """Меняет роль: ведущий или администратор."""
    data = request.get_json(silent=True) or {}
    role = str(data.get("role") or "").strip()
    if role not in (ROLE_ADMIN, ROLE_HOST):
        return _admin_error("Неизвестная роль")
    me = current_user()
    conn = get_db_connection()
    try:
        person = conn.execute(
            "SELECT userid, role FROM users WHERE userid = ?", (userid,)
        ).fetchone()
        if person is None:
            return _admin_error(f"Пользователя {userid} нет", 404)
        if userid == me["userid"] and role != ROLE_ADMIN:
            return _admin_error("Себя разжаловать нельзя")
        if person["role"] == ROLE_ADMIN and role != ROLE_ADMIN and _admin_count(conn) <= 1:
            return _admin_error("Это последний администратор")
        conn.execute("UPDATE users SET role = ? WHERE userid = ?", (role, userid))
        conn.commit()
    finally:
        conn.close()
    return json.dumps({"status": "ok", "role": role}, ensure_ascii=False)


@app.route('/api/admin/users/<int:userid>/delete', subdomain="svoyak", methods=["POST"])
@api_admin_required
def AdminDeleteUser(userid):
    """Удаляет пользователя: его комнаты остаются, но становятся без владельца."""
    me = current_user()
    if userid == me["userid"]:
        return _admin_error("Себя удалить нельзя")
    conn = get_tx_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        person = conn.execute(
            "SELECT userid, role, login FROM users WHERE userid = ?", (userid,)
        ).fetchone()
        if person is None:
            conn.execute("ROLLBACK")
            return _admin_error(f"Пользователя {userid} нет", 404)
        if person["role"] == ROLE_ADMIN and _admin_count(conn) <= 1:
            conn.execute("ROLLBACK")
            return _admin_error("Это последний администратор")
        freed = conn.execute(
            "UPDATE activerooms SET ownerid = NULL WHERE ownerid = ?", (userid,)
        ).rowcount
        conn.execute("DELETE FROM area_hosts WHERE userid = ?", (userid,))
        for table in ("auth_tokens", "telegram_messages"):
            if site_admin.has_table(conn, table):
                conn.execute("DELETE FROM %s WHERE userid = ?" % table, (userid,))
        conn.execute("DELETE FROM users WHERE userid = ?", (userid,))
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
    return json.dumps(
        {"status": "ok", "freed_rooms": freed, "login": person["login"]}, ensure_ascii=False
    )


@app.route('/admin/<int:roomid>', subdomain = "svoyak")
@room_access_required
def AdminLogView(roomid):
    """Журнал комнаты переехал в пульт турнира; старые ссылки ведут туда."""
    return redirect(f"/plan/{roomid}#log", code=302)


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


def _ensure_column(conn, table, column, ddl):
    """Добавляет колонку, если её ещё нет: база на сервере живёт своей жизнью."""
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


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
        # Ревизия и режим синхронизации: по ревизии видно, что два устройства
        # правят один протокол, по режиму — как часто площадка выходит на связь.
        _ensure_column(conn, "game_protocols", "revision",
                       "revision INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "game_protocols", "mode", "mode TEXT")
        # Спорные версии протокола: обе сохраняются, выбирает организатор.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS game_protocol_conflicts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                roomid      INTEGER NOT NULL,
                gameindex   INTEGER NOT NULL,
                payload     TEXT NOT NULL,
                revision    INTEGER NOT NULL DEFAULT 0,
                author      TEXT,
                client      TEXT,
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
                status      TEXT NOT NULL DEFAULT 'open',
                resolved_at TEXT,
                resolved_by TEXT
            )
        ''')
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_protocol_conflicts "
            "ON game_protocol_conflicts(roomid, gameindex, status)"
        )
        # История протокола: снимок, который затирается следующей записью.
        # Нужна, чтобы случайный сброс отметок можно было откатить.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS game_protocol_history (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                roomid    INTEGER NOT NULL,
                gameindex INTEGER NOT NULL,
                payload   TEXT NOT NULL,
                saved_at  TEXT DEFAULT CURRENT_TIMESTAMP,
                saved_by  TEXT
            )
        ''')
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_protocol_history "
            "ON game_protocol_history(roomid, gameindex, id)"
        )
        # Ведущий площадки: кому из пользователей открыт пульт этой площадки.
        # Площадка — это бывший «стол»: номер из схемы турнира.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS area_hosts (
                roomid      INTEGER NOT NULL,
                area        INTEGER NOT NULL,
                userid      INTEGER NOT NULL,
                assigned_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (roomid, area)
            )
        ''')
        # Ссылка управления площадкой: ключ в адресе, который открывает пульт
        # тому, кому его передали. area = 0 — ключ на комнату целиком.
        conn.execute('''
            CREATE TABLE IF NOT EXISTS control_links (
                roomid     INTEGER NOT NULL,
                area       INTEGER NOT NULL,
                token      TEXT NOT NULL,
                created_by INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (roomid, area)
            )
        ''')
        conn.commit()
    finally:
        conn.close()


@app.route('/api/room/<int:roomid>/claim', subdomain = "svoyak", methods = ["POST"])
@api_login_required
def ClaimRoomApi(roomid):
    """Закрепляет комнату без владельца за вошедшим пользователем."""
    ensure_auth_tables()
    user = current_user()
    conn = get_tx_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT ownerid FROM activerooms WHERE roomid = ?", (roomid,)
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return json.dumps(
                {"status": "error", "message": "Такой комнаты нет"}, ensure_ascii=False
            ), 404
        if row["ownerid"] is not None:
            conn.execute("ROLLBACK")
            return json.dumps(
                {"status": "error", "message": "У комнаты уже есть владелец"},
                ensure_ascii=False,
            ), 409
        conn.execute(
            "UPDATE activerooms SET ownerid = ? WHERE roomid = ?", (user["userid"], roomid)
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return json.dumps({"status": "ok", "owner": user["name"]}, ensure_ascii=False)


@app.route('/api/addroom', subdomain = "svoyak", methods = ["POST"])
def CreateRoomApi():
    # Без @api_login_required: гость создаёт комнату без владельца.
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
                actor_id = owner["userid"] if owner else None
                existing_owner = check_room_id[0]["ownerid"]
                if existing_owner not in (None, actor_id) and not is_admin(owner):
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
    play_hist = conn.execute("SELECT data FROM log WHERE roomid = ? and event = 'save game result'",
                             (roomid,)).fetchall()
    res = None
    for rs in play_hist:
        try:
            rj = json.loads(rs["data"])
        except (TypeError, ValueError):
            continue
        if rj.get("gameindex") != gameindex:
            continue
        if "detailed" in rj:
            # Бой могли сохранить повторно (поправили счёт или имена) —
            # отчёт показываем по последнему сохранению.
            res = rj["detailed"]
    conn.close()
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
        try:
            package_library.ensure_library(conn)
            package_library.link_package(conn, cur.lastrowid)
        except sqlite3.Error:
            # Библиотека — надстройка: без неё пакет всё равно сохранится,
            # а библиотечный номер ему выдадут при первом открытии страницы.
            pass
        results.append({"localId": local_id, "serverId": cur.lastrowid, "status": "created"})

    conn.commit()
    conn.close()
    return json.dumps({"status": "ok", "results": results}, ensure_ascii=False)


def package_library_access(conn, libraryid, user, roomid=None, manages=None):
    """Права на пакет: словарь из package_library.package_access (см. там)."""
    return package_library.package_access(conn, libraryid, user, roomid, manages)


@app.route('/packages', subdomain="svoyak")
@login_required
def PackagesPage():
    """Библиотека пакетов: где игрались и кто мог их слышать."""
    user = current_user()
    query = (request.args.get("q") or "").strip()
    mine_only = request.args.get("mine") == "1"
    conn = get_db_connection()
    try:
        package_library.ensure_library(conn)
        conn.commit()
        items = package_library.library_rows(
            conn, user, query or None,
            manages=lambda roomid: can_manage_room(roomid, user))
        pending = package_library.pending_links(conn) if is_admin(user) else 0
    finally:
        conn.close()
    if mine_only:
        items = [item for item in items if item["mine"]]
    return render_template(
        "packages.html", items=items, query=query, pending=pending, me=user,
        mine_only=mine_only, roles=package_library.ROLE_TITLES,
        title="Библиотека пакетов",
    )


@app.route('/packages/<int:libraryid>', subdomain="svoyak")
@login_required
def PackagePage(libraryid):
    """Пакет и вся его история: копии по комнатам, бои, темы, слышавшие."""
    user = current_user()
    mode = request.args.get("mode") or package_library.EXPOSURE_HALL
    if mode not in package_library.EXPOSURE_MODES:
        mode = package_library.EXPOSURE_HALL
    conn = get_db_connection()
    try:
        package_library.ensure_library(conn)
        conn.commit()
        context_room = request.args.get("room")
        try:
            context_room = int(context_room) if context_room not in (None, "") else None
        except (TypeError, ValueError):
            context_room = None
        access = package_library_access(
            conn, libraryid, user, context_room,
            manages=lambda roomid: can_manage_room(roomid, user))
        if access is None or not access["card"]:
            abort(404)
        entry = package_library.library_entry(conn, libraryid)
        copies = package_library.library_copies(conn, libraryid) if access["content"] else []
        usage = package_library.library_usage(conn, libraryid, mode) if access["content"] else []
        rejected = package_library.rejected_battles(conn, libraryid) if access["content"] else []
        listeners = package_library.library_listeners(conn, libraryid, mode) if access["content"] else []
        themes = package_library.theme_history(conn, libraryid, mode) if access["content"] else []
        similar = package_library.similar_libraries(conn, libraryid) if access["admin"] else []
        # Ревизии и их содержимое: показываем только тем, кому виден пакет.
        revisions = package_library.package_revisions(conn, libraryid) if access["content"] else []
        rights = (package_library.package_rights_view(conn, libraryid)
                  if access["content"] or access["can_grant_host"] else [])
        holders = package_library.package_holders(conn, libraryid)
        theme_names = (package_library.package_theme_names(conn, libraryid)
                       if access["content"] or access["preview"] == "themes" else [])
        # Куда можно положить копию: комнаты, которыми человек управляет.
        if is_admin(user):
            rooms = [dict(row) for row in conn.execute(
                "SELECT roomid, date FROM activerooms ORDER BY roomid DESC LIMIT 200")]
            grant_rooms = rooms
        else:
            rooms = [dict(row) for row in conn.execute(
                "SELECT roomid, date FROM activerooms WHERE ownerid = ? "
                "ORDER BY roomid DESC LIMIT 200", (user["userid"],))]
            grant_rooms = rooms
            if access["can_grant"]:
                # Правообладатель назначает ведущих в любой турнир, не только свой.
                grant_rooms = [dict(row) for row in conn.execute(
                    "SELECT roomid, date FROM activerooms ORDER BY roomid DESC LIMIT 200")]
        # Пометки боя ставит тот, кто ведёт эту комнату: список без ограничения длины.
        if is_admin(user):
            managed = {row[0] for row in conn.execute("SELECT roomid FROM activerooms")}
        else:
            managed = {row[0] for row in conn.execute(
                "SELECT roomid FROM activerooms WHERE ownerid = ?", (user["userid"],))}
        grantable = []
        if access["can_grant_host"]:
            grantable = [dict(row) for row in conn.execute(
                "SELECT userid, name, login FROM users ORDER BY name")]
        # Раскладки на бои: пресеты считаем по действующей ревизии, сохранённые
        # раскладки показываем с названиями тем.
        presets, layouts_view = [], []
        if access["content"]:
            data_json = package_library.current_package_data(conn, libraryid)
            topics = package_library.package_topics(data_json) if data_json else []
            presets = package_library.layout_presets(topics, 6)
            presets = [dict(preset,
                            view=package_library.layout_parts_view(
                                {"parts": preset["parts"]}, topics))
                       for preset in presets]
            layouts_view = [
                dict(layout, view=package_library.layout_parts_view(layout, topics))
                for layout in package_library.package_layouts(conn, libraryid)]
    finally:
        conn.close()
    return render_template(
        "package.html", entry=entry, copies=copies, usage=usage, listeners=listeners,
        themes=themes, similar=similar, mode=mode, access=access, rooms=rooms,
        rejected=rejected, managed=managed, revisions=revisions, rights=rights,
        holders=holders, theme_names=theme_names, grantable=grantable,
        grant_rooms=grant_rooms, presets=presets, layouts=layouts_view,
        context_room=context_room, roles=package_library.ROLE_TITLES,
        me=user, title="Пакет «%s»" % (entry["name"] if entry else ""),
    )


@app.route('/api/packages/library/<int:libraryid>/take', subdomain="svoyak", methods=["POST"])
@login_required
def PackageTakeToRoom(libraryid):
    """Кладёт копию пакета в комнату — дальше он виден в пульте этой комнаты."""
    user = current_user()
    data = request.get_json(silent=True) or {}
    try:
        roomid = int(data.get("roomid"))
    except (TypeError, ValueError):
        return json.dumps({"status": "error", "message": "Не указана комната"},
                          ensure_ascii=False), 400
    if not can_manage_room(roomid, user):
        return json.dumps({"status": "error", "message": "Нет прав на эту комнату"},
                          ensure_ascii=False), 403
    conn = get_tx_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        package_library.ensure_library(conn)
        access = package_library.package_access(
            conn, libraryid, user, roomid,
            manages=lambda room: can_manage_room(room, user))
        if access is None:
            conn.execute("ROLLBACK")
            return json.dumps({"status": "error", "message": "Пакета нет"},
                              ensure_ascii=False), 404
        if not access["can_take"]:
            conn.execute("ROLLBACK")
            return json.dumps(
                {"status": "error",
                 "message": "Пакет доступен только в своём турнире или по правам"},
                ensure_ascii=False), 403
        source = conn.execute(
            "SELECT data FROM package_revisions WHERE libraryid = ? "
            "ORDER BY revision DESC LIMIT 1", (libraryid,),
        ).fetchone()
        if source is None:
            source = conn.execute(
                "SELECT data FROM packages WHERE libraryid = ? "
                "ORDER BY packageid DESC LIMIT 1", (libraryid,),
            ).fetchone()
        if source is None:
            conn.execute("ROLLBACK")
            return json.dumps({"status": "error", "message": "Пакета нет"},
                              ensure_ascii=False), 404
        card = package_library.library_entry(conn, libraryid) or {}
        revision = int(card.get("revision") or 0)
        cursor = conn.execute(
            "INSERT INTO packages (roomid, name, data, libraryid, revision) "
            "VALUES (?, ?, ?, ?, ?)",
            (roomid, card.get("name") or "Без названия", source[0], libraryid, revision),
        )
        packageid = cursor.lastrowid
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
    return json.dumps({"status": "ok", "packageid": packageid, "roomid": roomid},
                      ensure_ascii=False)


@app.route('/api/packages/library/<int:libraryid>/rights', subdomain="svoyak",
           methods=["POST", "DELETE"])
@login_required
def PackageRights(libraryid):
    """Выдача и отзыв прав на пакет.

    Правообладатель распоряжается любыми ролями, организатор — только ведущими
    и только с указанием турнира. Себе роль не выдаёт никто: правообладателей
    назначает действующий правообладатель или администратор.
    """
    user = current_user()
    data = request.get_json(silent=True) or {}
    role = data.get("role")
    if role not in package_library.ROLES:
        return json.dumps({"status": "error", "message": "Неизвестная роль"},
                          ensure_ascii=False), 400
    try:
        userid = int(data.get("userid"))
    except (TypeError, ValueError):
        return json.dumps({"status": "error", "message": "Не указан пользователь"},
                          ensure_ascii=False), 400
    roomid = package_library.SCOPE_ALL
    if role == package_library.ROLE_HOST:
        try:
            roomid = int(data.get("roomid"))
        except (TypeError, ValueError):
            return json.dumps(
                {"status": "error", "message": "Ведущему нужно указать турнир"},
                ensure_ascii=False), 400
    conn = get_tx_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        package_library.ensure_library(conn)
        access = package_library.package_access(conn, libraryid, user)
        if access is None:
            conn.execute("ROLLBACK")
            return json.dumps({"status": "error", "message": "Пакета нет"},
                              ensure_ascii=False), 404
        if not package_library.can_grant_role(access, role):
            conn.execute("ROLLBACK")
            message = ("Ведущих назначает организатор или правообладатель"
                       if role == package_library.ROLE_HOST
                       else "Права передаёт только правообладатель")
            return json.dumps({"status": "error", "message": message},
                              ensure_ascii=False), 403
        person = conn.execute(
            "SELECT userid, name FROM users WHERE userid = ?", (userid,)
        ).fetchone()
        if person is None:
            conn.execute("ROLLBACK")
            return json.dumps({"status": "error", "message": "Такого пользователя нет"},
                              ensure_ascii=False), 404
        if (role == package_library.ROLE_HOST and not access["can_grant"]
                and not can_manage_room(roomid, user)):
            conn.execute("ROLLBACK")
            return json.dumps({"status": "error", "message": "Нет прав на эту комнату"},
                              ensure_ascii=False), 403
        if request.method == "DELETE":
            package_library.revoke_package_right(conn, libraryid, userid, role, roomid)
        else:
            package_library.grant_package_right(
                conn, libraryid, userid, role, roomid, user["userid"])
        conn.execute("COMMIT")
    except package_library.LibraryError as error:
        conn.execute("ROLLBACK")
        return json.dumps({"status": "error", "message": str(error)},
                          ensure_ascii=False), 400
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
    return json.dumps({"status": "ok"}, ensure_ascii=False)


@app.route('/api/packages/library/<int:libraryid>/meta', subdomain="svoyak",
           methods=["POST"])
@login_required
def PackageMeta(libraryid):
    """Карточка пакета и режим показа: правообладатель решает, что видно без права."""
    user = current_user()
    data = request.get_json(silent=True) or {}
    fields = {}
    for key, column in (("name", "name"), ("author", "author_name"),
                        ("tournament", "tournament"), ("origin_date", "origin_date"),
                        ("description", "description")):
        if key in data:
            fields[column] = (str(data.get(key) or "")).strip() or None
    preview = data.get("preview")
    if preview is not None and preview not in package_library.PREVIEWS:
        return json.dumps({"status": "error", "message": "Неизвестный режим показа"},
                          ensure_ascii=False), 400
    access_mode = data.get("access")
    if access_mode is not None and access_mode not in package_library.ACCESS_MODES:
        return json.dumps({"status": "error", "message": "Неизвестный статус"},
                          ensure_ascii=False), 400
    conn = get_tx_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        package_library.ensure_library(conn)
        access = package_library.package_access(conn, libraryid, user)
        if access is None:
            conn.execute("ROLLBACK")
            return json.dumps({"status": "error", "message": "Пакета нет"},
                              ensure_ascii=False), 404
        if not access["can_edit"]:
            conn.execute("ROLLBACK")
            return json.dumps({"status": "error", "message": "Нет прав на правку"},
                              ensure_ascii=False), 403
        # Статус «открытый» и режим показа меняет только правообладатель.
        if (preview is not None or access_mode is not None) and not access["can_grant"]:
            conn.execute("ROLLBACK")
            return json.dumps(
                {"status": "error", "message": "Это решает правообладатель"},
                ensure_ascii=False), 403
        sets, params = [], []
        for column, value in fields.items():
            sets.append("%s = ?" % column)
            params.append(value)
        if preview is not None:
            sets.append("preview = ?")
            params.append(preview)
        if access_mode is not None:
            sets.append("access = ?")
            params.append(access_mode)
        if sets:
            params.append(libraryid)
            conn.execute(
                "UPDATE package_library SET %s WHERE libraryid = ?" % ", ".join(sets), params
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
    return json.dumps({"status": "ok"}, ensure_ascii=False)


@app.route('/api/packages/library/<int:libraryid>/revision', subdomain="svoyak",
           methods=["POST"])
@login_required
def PackageRevision(libraryid):
    """Новая ревизия из JSON: правка канона не меняет копию в идущем бою."""
    user = current_user()
    data = request.get_json(silent=True) or {}
    payload = data.get("data")
    if isinstance(payload, dict):
        payload = json.dumps(payload, ensure_ascii=False)
    if not payload or not str(payload).strip():
        return json.dumps({"status": "error", "message": "Пустой пакет"},
                          ensure_ascii=False), 400
    try:
        parsed = json.loads(payload)
    except (TypeError, ValueError):
        return json.dumps({"status": "error", "message": "Это не JSON"},
                          ensure_ascii=False), 400
    if not isinstance(parsed, dict) or not isinstance(parsed.get("topics"), list):
        return json.dumps(
            {"status": "error", "message": "В пакете нет списка тем (topics)"},
            ensure_ascii=False), 400
    conn = get_tx_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        package_library.ensure_library(conn)
        access = package_library.package_access(conn, libraryid, user)
        if access is None:
            conn.execute("ROLLBACK")
            return json.dumps({"status": "error", "message": "Пакета нет"},
                              ensure_ascii=False), 404
        if not access["can_edit"]:
            conn.execute("ROLLBACK")
            return json.dumps({"status": "error", "message": "Нет прав на правку"},
                              ensure_ascii=False), 403
        revision = package_library.add_revision(
            conn, libraryid, payload, note=(data.get("note") or "").strip() or None,
            created_by=user["userid"], name=(str(data.get("name") or "").strip() or None))
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
    return json.dumps({"status": "ok", "revision": revision}, ensure_ascii=False)


@app.route('/api/packages/library/<int:libraryid>/layouts', subdomain="svoyak",
           methods=["POST"])
@login_required
def PackageLayoutSave(libraryid):
    """Раскладка пакета по боям: из готового пресета или своим списком тем.

    Раскладка — это бои пакета по порядку: [номера тем первого боя, второго, …].
    Пакет ЭК на 36 тем с пресетом «как есть» даёт шесть боёв по шесть тем.
    """
    user = current_user()
    data = request.get_json(silent=True) or {}
    preset_key = str(data.get("preset") or "").strip()
    name = str(data.get("name") or "").strip()
    parts = data.get("parts")
    conn = get_tx_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        package_library.ensure_library(conn)
        access = package_library.package_access(conn, libraryid, user)
        if access is None:
            conn.execute("ROLLBACK")
            return json.dumps({"status": "error", "message": "Пакета нет"},
                              ensure_ascii=False), 404
        if not access["can_edit"]:
            conn.execute("ROLLBACK")
            return json.dumps({"status": "error", "message": "Нет прав на правку"},
                              ensure_ascii=False), 403
        if preset_key:
            data_json = package_library.current_package_data(conn, libraryid)
            topics = package_library.package_topics(data_json) if data_json else []
            presets = {item["key"]: item for item in
                       package_library.layout_presets(topics, 6)}
            preset = presets.get(preset_key)
            if preset is None:
                conn.execute("ROLLBACK")
                return json.dumps({"status": "error", "message": "Такого пресета нет"},
                                  ensure_ascii=False), 400
            parts = preset["parts"]
            name = name or preset["name"]
        try:
            layoutid = package_library.save_layout(
                conn, libraryid, name, parts, user=user, preset=preset_key or None)
        except ValueError as error:
            conn.execute("ROLLBACK")
            return json.dumps({"status": "error", "message": str(error)},
                              ensure_ascii=False), 400
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
    return json.dumps({"status": "ok", "layoutid": layoutid}, ensure_ascii=False)


@app.route('/api/packages/library/<int:libraryid>/layouts/<int:layoutid>/delete',
           subdomain="svoyak", methods=["POST"])
@login_required
def PackageLayoutDelete(libraryid, layoutid):
    """Раскладку убираем мягко: бои, разложенные ею, не трогаем."""
    user = current_user()
    conn = get_tx_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        package_library.ensure_library(conn)
        access = package_library.package_access(conn, libraryid, user)
        if access is None:
            conn.execute("ROLLBACK")
            return json.dumps({"status": "error", "message": "Пакета нет"},
                              ensure_ascii=False), 404
        if not access["can_edit"]:
            conn.execute("ROLLBACK")
            return json.dumps({"status": "error", "message": "Нет прав на правку"},
                              ensure_ascii=False), 403
        row = conn.execute(
            "SELECT libraryid FROM package_layouts WHERE layoutid = ? AND deleted_at IS NULL",
            (layoutid,)).fetchone()
        if row is None or int(row["libraryid"]) != libraryid:
            conn.execute("ROLLBACK")
            return json.dumps({"status": "error", "message": "Раскладки нет"},
                              ensure_ascii=False), 404
        package_library.delete_layout(conn, layoutid)
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
    return json.dumps({"status": "ok"}, ensure_ascii=False)


@app.route('/api/packages/library/<int:libraryid>/delete', subdomain="svoyak",
           methods=["POST"])
@login_required
def PackageDelete(libraryid):
    """Мягкое удаление: пакет исчезает у всех, кроме администратора."""
    user = current_user()
    conn = get_tx_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        package_library.ensure_library(conn)
        access = package_library.package_access(conn, libraryid, user)
        if access is None:
            conn.execute("ROLLBACK")
            return json.dumps({"status": "error", "message": "Пакета нет"},
                              ensure_ascii=False), 404
        if not access["can_delete"]:
            conn.execute("ROLLBACK")
            return json.dumps({"status": "error", "message": "Удаляет правообладатель"},
                              ensure_ascii=False), 403
        conn.execute(
            "UPDATE package_library SET deleted_at = CURRENT_TIMESTAMP WHERE libraryid = ?",
            (libraryid,),
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
    return json.dumps({"status": "ok"}, ensure_ascii=False)


@app.route('/api/packages/library', subdomain="svoyak", methods=["POST"])
@login_required
def PackageUpload():
    """Загрузка нового пакета JSON: карточка + ревизия 1, загрузивший — правообладатель."""
    user = current_user()
    data = request.get_json(silent=True) or {}
    payload = data.get("data")
    if isinstance(payload, dict):
        payload = json.dumps(payload, ensure_ascii=False)
    try:
        parsed = json.loads(payload or "")
    except (TypeError, ValueError):
        return json.dumps({"status": "error", "message": "Это не JSON"},
                          ensure_ascii=False), 400
    if not isinstance(parsed, dict) or not isinstance(parsed.get("topics"), list):
        return json.dumps(
            {"status": "error", "message": "В пакете нет списка тем (topics)"},
            ensure_ascii=False), 400
    name = (str(data.get("name") or "").strip()
            or str(parsed.get("name") or "").strip() or "Без названия")
    meta = {
        "author_name": (str(data.get("author") or "").strip() or None),
        "tournament": (str(data.get("tournament") or "").strip() or None),
        "origin_date": (str(data.get("origin_date") or "").strip() or None),
        "description": (str(data.get("description") or "").strip() or None),
    }
    conn = get_tx_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        package_library.ensure_library(conn)
        digest = package_library.content_hash(payload)
        # Такой же пакет уже загружали? Если он наш — это новая ревизия, а не
        # второй пакет. Чужой трогать нельзя: его заведёт администратор связкой.
        own = conn.execute(
            "SELECT l.libraryid FROM package_library l "
            "JOIN package_rights r ON r.libraryid = l.libraryid "
            "WHERE l.hash = ? AND l.deleted_at IS NULL AND r.userid = ? AND r.role = ? "
            "ORDER BY l.libraryid LIMIT 1",
            (digest, user["userid"], package_library.ROLE_HOLDER),
        ).fetchone()
        if own is not None:
            libraryid = own[0]
            revision = package_library.add_revision(
                conn, libraryid, payload,
                note=(str(data.get("note") or "").strip() or "повторная загрузка"),
                created_by=user["userid"], name=name, **meta)
            twins = []
        else:
            twins = [row[0] for row in conn.execute(
                "SELECT libraryid FROM package_library WHERE hash = ? AND deleted_at IS NULL",
                (digest,))]
            libraryid = package_library.create_package(
                conn, name, payload, user["userid"],
                author_name=meta["author_name"], tournament=meta["tournament"],
                origin_date=meta["origin_date"], description=meta["description"],
                note=(str(data.get("note") or "").strip() or None))
            revision = 1
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
    return json.dumps({"status": "ok", "libraryid": libraryid, "revision": revision,
                       "twins": twins}, ensure_ascii=False)


@app.route('/api/packages/library/<int:libraryid>/merge', subdomain="svoyak", methods=["POST"])
@api_admin_required
def PackageMerge(libraryid):
    """Администратор подтверждает: это тот же пакет."""
    data = request.get_json(silent=True) or {}
    try:
        other = int(data.get("other"))
    except (TypeError, ValueError):
        return json.dumps({"status": "error", "message": "Не указан пакет"},
                          ensure_ascii=False), 400
    user = current_user()
    conn = get_tx_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        package_library.ensure_library(conn)
        moved = package_library.merge_libraries(
            conn, libraryid, other, "%s (%s)" % (user["name"], user["login"]))
        conn.execute("COMMIT")
    except package_library.LibraryError as error:
        conn.execute("ROLLBACK")
        return json.dumps({"status": "error", "message": str(error)},
                          ensure_ascii=False), 400
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()
    return json.dumps({"status": "ok", "moved": moved}, ensure_ascii=False)


@app.route('/api/packages/library/<int:libraryid>/reject', subdomain="svoyak", methods=["POST"])
@api_admin_required
def PackageReject(libraryid):
    """Администратор говорит: нет, это разные пакеты."""
    data = request.get_json(silent=True) or {}
    try:
        other = int(data.get("other"))
    except (TypeError, ValueError):
        return json.dumps({"status": "error", "message": "Не указан пакет"},
                          ensure_ascii=False), 400
    user = current_user()
    conn = get_db_connection()
    try:
        package_library.ensure_library(conn)
        package_library.reject_link(
            conn, libraryid, other, "%s (%s)" % (user["name"], user["login"]))
        conn.commit()
    finally:
        conn.close()
    return json.dumps({"status": "ok"}, ensure_ascii=False)


@app.route('/api/packages/library/<int:libraryid>/battle', subdomain="svoyak", methods=["POST"])
@login_required
def PackageBattleMark(libraryid):
    """Ведущий поправляет опознание боя: «наш бой» или «не наш».

    Автоматика привязывает старый бой к пакету, если в пакете нашлись почти все
    названия тем боя. Ошибки исправляются пометкой: status "foreign" убирает бой
    из пакета, "own" привязывает его вручную, null — снимает пометку.
    """
    user = current_user()
    data = request.get_json(silent=True) or {}
    try:
        roomid = int(data.get("roomid"))
        gameindex = int(data.get("gameindex"))
    except (TypeError, ValueError):
        return json.dumps({"status": "error", "message": "Не указан бой"},
                          ensure_ascii=False), 400
    status = data.get("status")
    if status not in ("own", "foreign", None):
        return json.dumps({"status": "error", "message": "Неизвестная пометка"},
                          ensure_ascii=False), 400
    conn = get_db_connection()
    try:
        package_library.ensure_library(conn)
        access = package_library_access(
            conn, libraryid, user, roomid,
            manages=lambda room: can_manage_room(room, user))
        if access is None or not access["content"]:
            return json.dumps({"status": "error", "message": "Нет доступа к пакету"},
                              ensure_ascii=False), 403
        if status is not None and not can_manage_room(roomid, user):
            return json.dumps({"status": "error", "message": "Нет прав на эту комнату"},
                              ensure_ascii=False), 403
        package_library.set_battle_mark(
            conn, roomid, gameindex, libraryid, status or None,
            "%s (%s)" % (user["name"], user["login"]))
        conn.commit()
    except sqlite3.OperationalError as error:
        return json.dumps({"status": "error", "message": str(error)},
                          ensure_ascii=False), 400
    finally:
        conn.close()
    return json.dumps({"status": "ok"}, ensure_ascii=False)


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
# Все маршруты объявлены с subdomain="svoyak", а Flask по умолчанию поддомены не
# разбирает: без этой строки он подставляет пустой поддомен и отдаёт 404 на всё.
# SERVER_NAME — родительский домен, поэтому на нестандартном порту его нужно
# указывать вместе с портом: SVOYAK_SERVER_NAME=localhost:8099.
app.subdomain_matching = True


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
    elif request.path.startswith('/static/') and request.path.endswith(('.js', '.css')):
        # Скрипты и стили пультов обновляются вместе с сайтом: браузер должен
        # переспросить сервер, иначе после выкладки останется старая версия.
        response.headers['Cache-Control'] = 'no-cache'
    return response
app.jinja_env.filters['zip'] = zip

if __name__ == "__main__":
    ensure_schema()
    ensure_auth_tables()
    debug = os.environ.get("SVOYAK_DEBUG", "").lower() in ("1", "true", "yes")
    try:
        port = int(os.environ.get("SVOYAK_PORT") or port)
    except ValueError:
        print("WARNING: SVOYAK_PORT не число, беру порт из site.cfg")
    print(f"svoyak on {app.config['SERVER_NAME']}:{port}, debug={debug}, db={DB_PATH}")
    if BANNER:
        print(f"banner: {BANNER}")
    if (app.config['SERVER_NAME'] or '').split('.')[0] == 'svoyak':
        print("WARNING: SERVER_NAME должен быть родительским доменом (chgk.fun), "
              "а не адресом сайта (svoyak.chgk.fun): иначе поддомен не разберётся")
    app.run(debug=debug, port=port, threaded=True)