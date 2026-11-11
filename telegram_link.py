"""Привязка аккаунтов к Telegram и уведомления.

Модуль знает только про сеть, токены и формат сообщений: соединение с базой
и настройки передаёт вызывающая сторона. Поэтому его легко проверить без
Flask и без настоящего бота.

Схема работы:

1. Пользователь нажимает «Привязать Telegram» — приложение выдаёт одноразовый
   токен и показывает ссылку ``https://t.me/<бот>?start=link_<токен>``.
2. Пользователь открывает ссылку и нажимает Start — Telegram присылает нашему
   вебхуку ``/start link_<токен>``.
3. Вебхук находит токен, записывает в аккаунт ``telegram_id`` и ``chat_id``
   (по chat_id и отправляются уведомления) и отвечает человеку в чат.

Токен одноразовый и живёт 15 минут: иначе ссылку можно передать другому
и привязать чужой Telegram к своему аккаунту.
"""

import json
import os
import re
import secrets
import urllib.error
import urllib.request

LINK_TTL_MINUTES = 15
REQUEST_TIMEOUT = 4
API_DEFAULT = "https://api.telegram.org"
START_PREFIX = "link_"

START_PATTERN = re.compile(r"^/start(?:@\w+)?(?:\s+(\S+))?\s*$")


def config():
    """Настройки из окружения. Пустые строки — «не настроено»."""
    return {
        "token": os.environ.get("SVOYAK_TELEGRAM_TOKEN", "").strip(),
        "bot": os.environ.get("SVOYAK_TELEGRAM_BOT", "").strip().lstrip("@"),
        "secret": os.environ.get("SVOYAK_TELEGRAM_SECRET", "").strip(),
        "api": (os.environ.get("SVOYAK_TELEGRAM_API", "").strip() or API_DEFAULT).rstrip("/"),
        "site": os.environ.get("SVOYAK_SITE_URL", "").strip().rstrip("/"),
    }


def is_configured(cfg=None):
    """Привязка возможна, только если заданы токен и имя бота."""
    cfg = cfg or config()
    return bool(cfg["token"] and cfg["bot"])


def start_url(cfg, token):
    """Ссылка, по которой пользователь открывает бота и жмёт Start."""
    return "https://t.me/%s?start=%s%s" % (cfg["bot"], START_PREFIX, token)


def parse_start(text):
    """Разбирает текст сообщения боту.

    None — это не /start; "" — /start без полезной нагрузки; иначе токен.
    """
    match = START_PATTERN.match((text or "").strip())
    if not match:
        return None
    payload = match.group(1) or ""
    if not payload.startswith(START_PREFIX):
        return ""
    return payload[len(START_PREFIX):]


def send_message(cfg, chat_id, text):
    """Отправляет сообщение. Возвращает (успех, текст ошибки)."""

    token = (cfg or {}).get("token") or ""
    if not token:
        return False, "не настроен токен бота"
    if not chat_id:
        return False, "у аккаунта нет chat_id"

    body = json.dumps(
        {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    ).encode("utf-8")
    request = urllib.request.Request(
        "%s/bot%s/sendMessage" % (cfg.get("api") or API_DEFAULT, token),
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        return False, "Telegram ответил HTTP %s: %s" % (exc.code, detail)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)

    if not payload.get("ok"):
        return False, str(payload.get("description") or "Telegram ответил отказом")
    return True, None


# ---------------------------------------------------------------
# Хранилище: токены привязки и журнал отправленных сообщений
# ---------------------------------------------------------------
def ensure_tables(conn):
    """Дополняет users и создаёт служебные таблицы. Звать можно сколько угодно."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    for name, kind in (
        ("telegram_id", "INTEGER"),
        ("telegram_username", "TEXT"),
        ("telegram_chat_id", "INTEGER"),
        ("telegram_linked_at", "TEXT"),
    ):
        if name not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN %s %s" % (name, kind))

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_users_telegram ON users(telegram_id)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS auth_tokens ("
        " token      TEXT PRIMARY KEY,"
        " userid     INTEGER NOT NULL,"
        " purpose    TEXT NOT NULL,"
        " created_at TEXT DEFAULT CURRENT_TIMESTAMP,"
        " expires_at TEXT NOT NULL,"
        " used_at    TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS telegram_messages ("
        " id         INTEGER PRIMARY KEY AUTOINCREMENT,"
        " userid     INTEGER,"
        " chat_id    INTEGER,"
        " text       TEXT NOT NULL,"
        " status     TEXT NOT NULL,"
        " error      TEXT,"
        " created_at TEXT DEFAULT CURRENT_TIMESTAMP,"
        " sent_at    TEXT)"
    )


def issue_token(conn, userid, purpose="link", ttl_minutes=LINK_TTL_MINUTES):
    """Выдаёт одноразовый токен привязки для пользователя."""
    conn.execute(
        "DELETE FROM auth_tokens WHERE expires_at <= datetime('now') OR used_at IS NOT NULL"
    )
    token = secrets.token_urlsafe(16)
    conn.execute(
        "INSERT INTO auth_tokens (token, userid, purpose, expires_at) "
        "VALUES (?, ?, ?, datetime('now', ?))",
        (token, userid, purpose, "%+d minutes" % ttl_minutes),
    )
    return token


def find_token(conn, token, purpose="link"):
    """Живой и неиспользованный токен. None — если такого нет."""
    if not token:
        return None
    return conn.execute(
        "SELECT token, userid, purpose FROM auth_tokens "
        "WHERE token = ? AND purpose = ? AND used_at IS NULL AND expires_at > datetime('now')",
        (token, purpose),
    ).fetchone()


def link_account(conn, token_row, telegram_id, username=None, chat_id=None):
    """Привязывает Telegram к аккаунту из токена.

    Возвращает ("ok", login) или ("busy", login другого аккаунта): один
    Telegram не может быть ключом к двум аккаунтам на сайте.
    """
    other = conn.execute(
        "SELECT login FROM users WHERE telegram_id = ? AND userid <> ?",
        (telegram_id, token_row["userid"]),
    ).fetchone()
    if other is not None:
        return "busy", other["login"]

    row = conn.execute(
        "SELECT login FROM users WHERE userid = ?", (token_row["userid"],)
    ).fetchone()
    if row is None:
        return "gone", None

    conn.execute(
        "UPDATE users SET telegram_id = ?, telegram_username = ?, telegram_chat_id = ?,"
        " telegram_linked_at = CURRENT_TIMESTAMP WHERE userid = ?",
        (telegram_id, username, chat_id, token_row["userid"]),
    )
    conn.execute(
        "UPDATE auth_tokens SET used_at = CURRENT_TIMESTAMP WHERE token = ?",
        (token_row["token"],),
    )
    return "ok", row["login"]


def unlink_account(conn, userid):
    """Снимает привязку. Пароль проверяет вызывающая сторона."""
    conn.execute(
        "UPDATE users SET telegram_id = NULL, telegram_username = NULL,"
        " telegram_chat_id = NULL, telegram_linked_at = NULL WHERE userid = ?",
        (userid,),
    )


def account(conn, userid):
    """Данные привязки пользователя: {"linked", "username", "chat_id", "linked_at"}."""
    row = conn.execute(
        "SELECT telegram_username, telegram_chat_id, telegram_linked_at"
        " FROM users WHERE userid = ?",
        (userid,),
    ).fetchone()
    if row is None:
        return {"linked": False, "username": None, "chat_id": None, "linked_at": None}
    return {
        "linked": row["telegram_chat_id"] is not None,
        "username": row["telegram_username"],
        "chat_id": row["telegram_chat_id"],
        "linked_at": row["telegram_linked_at"],
    }


def recent_messages(conn, userid, limit=8):
    """Последние уведомления: человек видит, дошло ли и что случилось."""
    return [
        dict(row)
        for row in conn.execute(
            "SELECT text, status, error, created_at, sent_at FROM telegram_messages"
            " WHERE userid = ? ORDER BY id DESC LIMIT ?",
            (userid, limit),
        )
    ]


def notify(conn, userid, text, cfg=None):
    """Пишет уведомление пользователю, если у него привязан Telegram.

    Звать только после COMMIT: функция сама коммитит журнал отправки.
    Возвращает True/False/None (None — привязки нет, писать некуда).
    """
    cfg = cfg or config()
    row = conn.execute(
        "SELECT telegram_chat_id FROM users WHERE userid = ?", (userid,)
    ).fetchone()
    chat_id = row["telegram_chat_id"] if row is not None else None
    if not chat_id:
        return None

    cursor = conn.execute(
        "INSERT INTO telegram_messages (userid, chat_id, text, status)"
        " VALUES (?, ?, ?, 'pending')",
        (userid, chat_id, text),
    )
    message_id = cursor.lastrowid
    conn.commit()

    ok, error = send_message(cfg, chat_id, text)
    if ok:
        conn.execute(
            "UPDATE telegram_messages SET status = 'sent', sent_at = CURRENT_TIMESTAMP"
            " WHERE id = ?",
            (message_id,),
        )
    else:
        conn.execute(
            "UPDATE telegram_messages SET status = 'error', error = ? WHERE id = ?",
            (error, message_id),
        )
    conn.commit()
    return ok
