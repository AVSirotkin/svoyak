"""Проверки модуля привязки Telegram: разбор /start, токены, привязка."""

import datetime
import os
import sqlite3
import unittest
from unittest import mock

import telegram_link


def memory_db():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE users (userid INTEGER PRIMARY KEY AUTOINCREMENT, login TEXT,"
        " name TEXT, password_hash TEXT, role TEXT)"
    )
    conn.execute("INSERT INTO users (login, name) VALUES ('one', 'Первый')")
    conn.execute("INSERT INTO users (login, name) VALUES ('two', 'Второй')")
    conn.commit()
    return conn


class ParseStartTest(unittest.TestCase):
    def test_link_payload(self):
        self.assertEqual(telegram_link.parse_start('/start link_abc123'), 'abc123')

    def test_bot_suffix_and_spaces(self):
        self.assertEqual(telegram_link.parse_start('  /start@svoyak_bot link_x  '), 'x')

    def test_plain_start(self):
        self.assertEqual(telegram_link.parse_start('/start'), '')

    def test_foreign_payload(self):
        self.assertEqual(telegram_link.parse_start('/start anything'), '')

    def test_not_a_command(self):
        self.assertIsNone(telegram_link.parse_start('привет'))
        self.assertIsNone(telegram_link.parse_start('/help'))
        self.assertIsNone(telegram_link.parse_start(''))


class ConfigTest(unittest.TestCase):
    def test_configured_flag(self):
        cfg = {'token': 't', 'bot': 'b', 'secret': '', 'api': '', 'site': ''}
        self.assertTrue(telegram_link.is_configured(cfg))
        self.assertFalse(telegram_link.is_configured({'token': '', 'bot': 'b'}))
        self.assertFalse(telegram_link.is_configured({'token': 't', 'bot': ''}))

    def test_start_url(self):
        cfg = {'bot': 'svoyak_bot'}
        self.assertEqual(
            telegram_link.start_url(cfg, 'tok'),
            'https://t.me/svoyak_bot?start=link_tok',
        )

    def test_env_config(self):
        with mock.patch.dict(os.environ, {
            'SVOYAK_TELEGRAM_TOKEN': ' token ',
            'SVOYAK_TELEGRAM_BOT': '@bot_name',
            'SVOYAK_TELEGRAM_API': 'http://127.0.0.1:9/',
            'SVOYAK_TELEGRAM_SECRET': ' s ',
        }):
            cfg = telegram_link.config()
        self.assertEqual(cfg['token'], 'token')
        self.assertEqual(cfg['bot'], 'bot_name')
        self.assertEqual(cfg['api'], 'http://127.0.0.1:9')
        self.assertEqual(cfg['secret'], 's')


class SendMessageTest(unittest.TestCase):
    def test_without_token(self):
        ok, error = telegram_link.send_message({'token': ''}, 1, 'текст')
        self.assertFalse(ok)
        self.assertIn('токен', error)

    def test_without_chat(self):
        ok, error = telegram_link.send_message({'token': 't'}, None, 'текст')
        self.assertFalse(ok)
        self.assertIn('chat_id', error)


class StorageTest(unittest.TestCase):
    def setUp(self):
        self.conn = memory_db()
        telegram_link.ensure_tables(self.conn)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_tables_are_idempotent(self):
        telegram_link.ensure_tables(self.conn)
        columns = {row[1] for row in self.conn.execute('PRAGMA table_info(users)')}
        self.assertIn('telegram_chat_id', columns)
        self.assertIn('telegram_linked_at', columns)

    def test_token_lifecycle(self):
        token = telegram_link.issue_token(self.conn, 1)
        self.conn.commit()
        row = telegram_link.find_token(self.conn, token)
        self.assertEqual(row['userid'], 1)
        self.assertIsNone(telegram_link.find_token(self.conn, 'нет-такого'))

    def test_expired_token_is_not_found(self):
        token = telegram_link.issue_token(self.conn, 1, ttl_minutes=-1)
        self.conn.commit()
        self.assertIsNone(telegram_link.find_token(self.conn, token))

    def test_token_is_single_use(self):
        token = telegram_link.issue_token(self.conn, 1)
        row = telegram_link.find_token(self.conn, token)
        status, login = telegram_link.link_account(self.conn, row, 111, 'nick', 111)
        self.conn.commit()
        self.assertEqual((status, login), ('ok', 'one'))
        self.assertIsNone(telegram_link.find_token(self.conn, token))

    def test_one_telegram_for_one_account(self):
        first = telegram_link.issue_token(self.conn, 1)
        telegram_link.link_account(self.conn, telegram_link.find_token(self.conn, first), 111)
        second = telegram_link.issue_token(self.conn, 2)
        status, login = telegram_link.link_account(
            self.conn, telegram_link.find_token(self.conn, second), 111)
        self.conn.commit()
        self.assertEqual(status, 'busy')
        self.assertEqual(login, 'one')

    def test_unlink_clears_account(self):
        token = telegram_link.issue_token(self.conn, 1)
        telegram_link.link_account(self.conn, telegram_link.find_token(self.conn, token), 111, 'nick', 111)
        self.conn.commit()
        self.assertTrue(telegram_link.account(self.conn, 1)['linked'])
        telegram_link.unlink_account(self.conn, 1)
        self.conn.commit()
        account = telegram_link.account(self.conn, 1)
        self.assertFalse(account['linked'])
        self.assertIsNone(account['chat_id'])

    def test_notify_without_link_is_silent(self):
        self.assertIsNone(telegram_link.notify(self.conn, 1, 'привет', cfg={'token': 't'}))
        self.assertEqual(telegram_link.recent_messages(self.conn, 1), [])

    def test_notify_records_failure(self):
        token = telegram_link.issue_token(self.conn, 1)
        telegram_link.link_account(self.conn, telegram_link.find_token(self.conn, token), 111, 'nick', 111)
        self.conn.commit()
        with mock.patch.object(telegram_link, 'send_message', return_value=(False, 'нет сети')):
            ok = telegram_link.notify(self.conn, 1, 'привет')
        self.assertFalse(ok)
        messages = telegram_link.recent_messages(self.conn, 1)
        self.assertEqual(messages[0]['status'], 'error')
        self.assertEqual(messages[0]['error'], 'нет сети')

    def test_notify_marks_sent(self):
        token = telegram_link.issue_token(self.conn, 1)
        telegram_link.link_account(self.conn, telegram_link.find_token(self.conn, token), 111, 'nick', 111)
        self.conn.commit()
        with mock.patch.object(telegram_link, 'send_message', return_value=(True, None)):
            self.assertTrue(telegram_link.notify(self.conn, 1, 'привет'))
        message = telegram_link.recent_messages(self.conn, 1)[0]
        self.assertEqual(message['status'], 'sent')
        self.assertIsNotNone(message['sent_at'])


if __name__ == '__main__':
    unittest.main()
