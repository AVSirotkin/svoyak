"""Точка входа для боевого WSGI-сервера (gunicorn, waitress, uwsgi).

Смысл файла в одном: при запуске через `python svoyak_site_app.py` подготовка
базы (индексы и таблицы) делается в блоке `__main__`, а WSGI-сервер этот блок
не исполняет. Здесь та же подготовка выполняется один раз при импорте.

    gunicorn --workers 2 --threads 4 --bind 127.0.0.1:8001 wsgi:app      # Linux
    waitress-serve --listen=127.0.0.1:8001 wsgi:app                      # Windows
"""

from svoyak_site_app import app, ensure_auth_tables, ensure_schema

ensure_schema()
ensure_auth_tables()

__all__ = ["app"]
