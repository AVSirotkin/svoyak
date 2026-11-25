# -*- coding: utf-8 -*-
"""Права на пакеты: правообладатель, организатор, ведущий, открытый пакет."""

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

WORK_DIR = tempfile.mkdtemp(prefix='svoyak-rights-')
WORK_DB = os.path.join(WORK_DIR, 'rights.db')
shutil.copyfile(os.path.join(ROOT, 'svoyak-test.db'), WORK_DB)

os.environ['SVOYAK_DB'] = WORK_DB
os.environ['SVOYAK_SERVER_NAME'] = 'localhost'
os.environ.pop('SVOYAK_TELEGRAM_TOKEN', None)

import svoyak_site_app as site  # noqa: E402
import package_library  # noqa: E402

site.app.subdomain_matching = True
site.DB_PATH = WORK_DB
BASE = 'http://svoyak.localhost'


def db():
    conn = sqlite3.connect(WORK_DB)
    conn.row_factory = sqlite3.Row
    return conn


def package_data(topics, suffix=''):
    return {
        "topics": [
            {"topic": topic, "questions": [
                {"value": 10, "text": "вопрос %s %s" % (topic, suffix), "answer": "ответ"}]}
            for topic in topics
        ],
        "sourceFileName": "rights.json",
    }


class RightsBase(unittest.TestCase):
    """Общая обвязка: пользователи, комнаты и загруженный пакет."""

    def setUp(self):
        package_library.reset_caches()
        site.DB_PATH = WORK_DB
        self.suffix = uuid.uuid4().hex[:6]
        self.holder = self.registered('Правообладатель')
        self.other = self.registered('Посторонний')
        self.admin = self.registered('Админ', role='администратор')
        self.room1 = self.new_room(self.holder.userid)
        self.room2 = self.new_room(self.other.userid)
        self.libraryid = self.upload(self.holder, 'Пакет прав', ['Тема А', 'Тема Б'])
        self.hex = self.suffix

    # --- обвязка ---------------------------------------------------------

    def registered(self, name, role='ведущий'):
        client = site.app.test_client()
        login = 'pr_' + uuid.uuid4().hex[:8]
        response = client.post(BASE + '/register', data={
            'login': login, 'name': name + ' ' + login,
            'password': 'secret123', 'password2': 'secret123'})
        self.assertEqual(response.status_code, 302, response.get_data(as_text=True))
        conn = db()
        userid = conn.execute("SELECT userid FROM users WHERE login = ?",
                              (login,)).fetchone()[0]
        conn.execute("UPDATE users SET role = ? WHERE userid = ?", (role, userid))
        conn.commit()
        conn.close()
        client.userid = userid
        client.login = login
        return client

    def new_room(self, ownerid):
        roomid = 9500 + int(uuid.uuid4().hex[:3], 16)
        conn = db()
        conn.execute(
            "INSERT OR REPLACE INTO activerooms (roomid, date, venueid, finished, ownerid) "
            "VALUES (?, '2026-11-20', 0, 0, ?)", (roomid, ownerid))
        conn.commit()
        conn.close()
        return roomid

    def upload(self, client, name, topics):
        payload = {
            "data": json.dumps(package_data(topics, self.suffix), ensure_ascii=False),
            "name": name + ' ' + self.suffix,
            "tournament": 'Турнир ' + self.suffix,
        }
        response = client.post(BASE + '/api/packages/library', json=payload)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        return json.loads(response.get_data(as_text=True))['libraryid']

    def grant(self, client, userid, role, roomid=None):
        body = {'userid': userid, 'role': role}
        if roomid is not None:
            body['roomid'] = roomid
        return client.post(BASE + '/api/packages/library/%d/rights' % self.libraryid,
                           json=body)

    def revoke(self, client, userid, role, roomid=0):
        return client.delete(BASE + '/api/packages/library/%d/rights' % self.libraryid,
                             json={'userid': userid, 'role': role, 'roomid': roomid})

    def rights(self):
        conn = db()
        rows = conn.execute(
            "SELECT userid, role, roomid FROM package_rights WHERE libraryid = ? "
            "ORDER BY role, userid", (self.libraryid,)).fetchall()
        conn.close()
        return [(row[0], row[1], row[2]) for row in rows]

    def access(self, client, roomid=None):
        conn = db()
        try:
            user = conn.execute("SELECT * FROM users WHERE userid = ?",
                                (client.userid,)).fetchone()
            return package_library.package_access(
                conn, self.libraryid, user, roomid,
                manages=lambda room: site.can_manage_room(room, user))
        finally:
            conn.close()

    def page(self, client, path=''):
        return client.get(BASE + '/packages/%d%s' % (self.libraryid, path))


class UploadTests(RightsBase):
    """Загрузка: загрузивший — правообладатель, содержимое сохранено ревизией."""

    def test_uploader_becomes_holder(self):
        self.assertEqual(self.rights(), [(self.holder.userid, 'holder', 0)])

    def test_upload_creates_first_revision(self):
        conn = db()
        rows = conn.execute(
            "SELECT revision FROM package_revisions WHERE libraryid = ?",
            (self.libraryid,)).fetchall()
        entry = conn.execute("SELECT revision FROM package_library WHERE libraryid = ?",
                             (self.libraryid,)).fetchone()
        conn.close()
        self.assertEqual([row[0] for row in rows], [1])
        self.assertEqual(entry[0], 1)

    def test_broken_json_is_rejected(self):
        response = self.holder.post(BASE + '/api/packages/library',
                                    json={'data': 'не json', 'name': 'Плохой'})
        self.assertEqual(response.status_code, 400)

    def test_json_without_topics_is_rejected(self):
        response = self.holder.post(BASE + '/api/packages/library',
                                    json={'data': '{"questions": []}', 'name': 'Плохой'})
        self.assertEqual(response.status_code, 400)

    def test_repeat_upload_of_own_package_adds_revision(self):
        # Тот же файл второй раз — это новая ревизия, а не второй пакет.
        response = self.holder.post(BASE + '/api/packages/library', json={
            'data': json.dumps(package_data(['Тема А', 'Тема Б'], self.suffix),
                               ensure_ascii=False),
            'name': 'Пакет прав ' + self.suffix})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = json.loads(response.get_data(as_text=True))
        self.assertEqual(data['libraryid'], self.libraryid)
        self.assertEqual(data['revision'], 2)

    def test_same_file_from_another_user_is_a_twin(self):
        # Чужой пакет не сливаем молча: заводим отдельный канон и подсказываем админу.
        response = self.other.post(BASE + '/api/packages/library', json={
            'data': json.dumps(package_data(['Тема А', 'Тема Б'], self.suffix),
                               ensure_ascii=False),
            'name': 'Копия пакета прав ' + self.suffix})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        data = json.loads(response.get_data(as_text=True))
        self.assertNotEqual(data['libraryid'], self.libraryid)
        self.assertIn(self.libraryid, data['twins'])


class HolderTests(RightsBase):
    """Правообладатель распоряжается пакетом."""

    def test_holder_gives_any_role(self):
        for role in ('organizer', 'host', 'holder'):
            roomid = self.room1 if role == 'host' else None
            response = self.grant(self.holder, self.other.userid, role, roomid)
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        roles = {row[1] for row in self.rights()}
        self.assertEqual(roles, {'holder', 'organizer', 'host'})

    def test_host_needs_tournament(self):
        response = self.grant(self.holder, self.other.userid, 'host')
        self.assertEqual(response.status_code, 400)

    def test_unknown_role_is_rejected(self):
        response = self.grant(self.holder, self.other.userid, 'viewer')
        self.assertEqual(response.status_code, 400)

    def test_last_holder_cannot_revoke_himself(self):
        response = self.revoke(self.holder, self.holder.userid, 'holder')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.rights(), [(self.holder.userid, 'holder', 0)])

    def test_holder_can_add_second_holder_and_leave(self):
        self.assertEqual(self.grant(self.holder, self.other.userid, 'holder').status_code, 200)
        self.assertEqual(self.revoke(self.holder, self.holder.userid, 'holder').status_code, 200)
        self.assertEqual(self.rights(), [(self.other.userid, 'holder', 0)])

    def test_stranger_cannot_grant(self):
        response = self.grant(self.other, self.other.userid, 'organizer')
        self.assertEqual(response.status_code, 403)

    def test_stranger_does_not_see_content(self):
        self.assertFalse(self.access(self.other)['content'])
        self.assertNotIn('Тема А', self.page(self.other).get_data(as_text=True))

    def test_stranger_sees_card_and_holder_name(self):
        text = self.page(self.other).get_data(as_text=True)
        self.assertIn('Пакет прав', text)
        self.assertIn('Правообладатель', text)

    def test_stranger_cannot_take(self):
        response = self.other.post(
            BASE + '/api/packages/library/%d/take' % self.libraryid,
            json={'roomid': self.room2})
        self.assertEqual(response.status_code, 403)

    def test_delete_hides_from_stranger_but_not_admin(self):
        response = self.holder.post(
            BASE + '/api/packages/library/%d/delete' % self.libraryid, json={})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(self.page(self.other).status_code, 404)
        self.assertEqual(self.page(self.admin).status_code, 200)
        self.assertIn('удалён', self.page(self.admin).get_data(as_text=True))

    def test_organizer_cannot_delete(self):
        self.grant(self.holder, self.other.userid, 'organizer')
        response = self.other.post(
            BASE + '/api/packages/library/%d/delete' % self.libraryid, json={})
        self.assertEqual(response.status_code, 403)


class OrganizerTests(RightsBase):
    """Организатор работает с пакетом, но передаёт только ведущих."""

    def setUp(self):
        super().setUp()
        response = self.grant(self.holder, self.other.userid, 'organizer')
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

    def test_organizer_sees_content(self):
        self.assertTrue(self.access(self.other)['content'])
        self.assertIn('Тема А', self.page(self.other).get_data(as_text=True))

    def test_organizer_sees_rights_panel(self):
        text = self.page(self.other).get_data(as_text=True)
        self.assertIn('Доступ к пакету', text)

    def test_organizer_grants_host_in_own_room(self):
        response = self.grant(self.other, self.holder.userid, 'host', self.room2)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

    def test_organizer_cannot_grant_organizer_or_holder(self):
        for role in ('organizer', 'holder'):
            response = self.grant(self.other, self.holder.userid, role)
            self.assertEqual(response.status_code, 403, role)

    def test_organizer_cannot_grant_host_in_foreign_room(self):
        response = self.grant(self.other, self.holder.userid, 'host', self.room1)
        self.assertEqual(response.status_code, 403)

    def test_organizer_edits_canon(self):
        response = self.other.post(
            BASE + '/api/packages/library/%d/revision' % self.libraryid,
            json={'data': json.dumps(package_data(['Тема Новая'], self.suffix),
                                     ensure_ascii=False),
                  'note': 'правка организатора'})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(json.loads(response.get_data(as_text=True))['revision'], 2)


class HostTests(RightsBase):
    """Ведущий видит пакет только в своём турнире."""

    def setUp(self):
        super().setUp()
        self.host = self.other
        self.third_room = self.new_room(self.other.userid)
        response = self.grant(self.holder, self.host.userid, 'host', self.third_room)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

    def test_host_sees_card_without_room_context(self):
        text = self.page(self.host).get_data(as_text=True)
        self.assertIn('Пакет прав', text)
        self.assertNotIn('Тема А', text)
        self.assertIn('как ведущему в турнирах', text)

    def test_host_sees_content_in_his_tournament(self):
        text = self.page(self.host, '?room=%d' % self.third_room).get_data(as_text=True)
        self.assertIn('Тема А', text)

    def test_host_does_not_see_content_in_foreign_room(self):
        text = self.page(self.host, '?room=%d' % self.room2).get_data(as_text=True)
        self.assertNotIn('Тема А', text)

    def test_host_cannot_grant(self):
        response = self.grant(self.host, self.holder.userid, 'host', self.third_room)
        self.assertEqual(response.status_code, 403)

    def test_host_cannot_take_into_another_room(self):
        response = self.host.post(
            BASE + '/api/packages/library/%d/take' % self.libraryid,
            json={'roomid': self.third_room})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        response = self.host.post(
            BASE + '/api/packages/library/%d/take' % self.libraryid,
            json={'roomid': self.room2})
        self.assertEqual(response.status_code, 403)

    def test_host_cannot_edit(self):
        response = self.host.post(
            BASE + '/api/packages/library/%d/revision' % self.libraryid,
            json={'data': json.dumps(package_data(['Другая'], self.suffix),
                                     ensure_ascii=False)})
        self.assertEqual(response.status_code, 403)


class RevisionTests(RightsBase):
    """Ревизия меняет канон, но не копию в идущем бою."""

    def test_revision_does_not_touch_room_copy(self):
        take = self.holder.post(
            BASE + '/api/packages/library/%d/take' % self.libraryid,
            json={'roomid': self.room1})
        self.assertEqual(take.status_code, 200, take.get_data(as_text=True))
        packageid = json.loads(take.get_data(as_text=True))['packageid']

        payload = json.dumps(package_data(['Тема А', 'Тема Б', 'Тема В'], self.suffix),
                             ensure_ascii=False)
        response = self.holder.post(
            BASE + '/api/packages/library/%d/revision' % self.libraryid,
            json={'data': payload, 'note': 'добавили тему'})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(json.loads(response.get_data(as_text=True))['revision'], 2)

        conn = db()
        copy = conn.execute("SELECT data, revision FROM packages WHERE packageid = ?",
                            (packageid,)).fetchone()
        entry = conn.execute(
            "SELECT revision, hash FROM package_library WHERE libraryid = ?",
            (self.libraryid,)).fetchone()
        conn.close()
        self.assertEqual(copy['revision'], 1)
        self.assertEqual(entry['revision'], 2)
        stored = json.loads(copy['data'])
        self.assertEqual(len(stored['topics']), 2)
        self.assertEqual(entry['hash'], package_library.content_hash(payload))

    def test_revision_is_added_without_copy(self):
        # Пакет без копий в комнатах: ревизия всё равно сохраняется.
        payload = json.dumps(package_data(['Только тема'], self.suffix), ensure_ascii=False)
        response = self.holder.post(
            BASE + '/api/packages/library/%d/revision' % self.libraryid,
            json={'data': payload})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))


class CommunityTests(RightsBase):
    """Открытый пакет доступен любому вошедшему для любых целей."""

    def become_community(self):
        response = self.holder.post(
            BASE + '/api/packages/library/%d/meta' % self.libraryid,
            json={'access': 'community'})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

    def test_community_is_visible_to_stranger(self):
        self.become_community()
        text = self.page(self.other).get_data(as_text=True)
        self.assertIn('Тема А', text)
        self.assertIn('открытый', text)

    def test_community_can_be_taken_by_anyone(self):
        self.become_community()
        response = self.other.post(
            BASE + '/api/packages/library/%d/take' % self.libraryid,
            json={'roomid': self.room2})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

    def test_community_is_not_editable_by_stranger(self):
        self.become_community()
        response = self.other.post(
            BASE + '/api/packages/library/%d/revision' % self.libraryid,
            json={'data': json.dumps(package_data(['Своя'], self.suffix), ensure_ascii=False)})
        self.assertEqual(response.status_code, 403)

    def test_only_holder_changes_status(self):
        self.grant(self.holder, self.other.userid, 'organizer')
        response = self.other.post(
            BASE + '/api/packages/library/%d/meta' % self.libraryid,
            json={'access': 'community'})
        self.assertEqual(response.status_code, 403)


class PreviewTests(RightsBase):
    """Что видит посторонний: ничего, карточку или карточку с темами."""

    def set_preview(self, value):
        response = self.holder.post(
            BASE + '/api/packages/library/%d/meta' % self.libraryid,
            json={'preview': value})
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

    def test_card_preview_hides_themes(self):
        self.set_preview('card')
        text = self.page(self.other).get_data(as_text=True)
        self.assertIn('Пакет прав', text)
        self.assertNotIn('Тема А', text)

    def test_themes_preview_shows_theme_names(self):
        self.set_preview('themes')
        text = self.page(self.other).get_data(as_text=True)
        self.assertIn('Тема А', text)
        self.assertNotIn('вопрос Тема А', text)

    def test_none_preview_hides_package(self):
        self.set_preview('none')
        self.assertEqual(self.page(self.other).status_code, 404)
        listing = self.other.get(BASE + '/packages').get_data(as_text=True)
        self.assertNotIn('Пакет прав ' + self.suffix, listing)
        # Правообладатель свой пакет видит.
        self.assertEqual(self.page(self.holder).status_code, 200)

    def test_preview_does_not_leak_questions(self):
        self.set_preview('themes')
        text = self.page(self.other).get_data(as_text=True)
        self.assertNotIn('ответ', text)

    def test_only_holder_changes_preview(self):
        self.grant(self.holder, self.other.userid, 'organizer')
        response = self.other.post(
            BASE + '/api/packages/library/%d/meta' % self.libraryid,
            json={'preview': 'themes'})
        self.assertEqual(response.status_code, 403)


class MigrationTests(unittest.TestCase):
    """Старая база: права и ревизии появляются сами, без потери доступа."""

    def setUp(self):
        self.conn = db()
        self.conn.isolation_level = None
        self.conn.execute("BEGIN IMMEDIATE")
        self.addCleanup(self._rollback)

    def _rollback(self):
        try:
            self.conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def test_existing_package_gets_holder_and_revision(self):
        package_library.reset_caches()
        # Старая запись: библиотека без прав и без ревизий, копия без ревизии.
        cursor = self.conn.execute(
            "INSERT INTO packages (roomid, name, data) VALUES (?, ?, ?)",
            (9601, 'Старый пакет', json.dumps(package_data(['Старая тема']),
                                              ensure_ascii=False)))
        packageid = cursor.lastrowid
        self.conn.execute(
            "INSERT INTO package_library (name, hash, ownerid) VALUES (?, ?, ?)",
            ('Старый пакет', 'hash-old', 3))
        libraryid = self.conn.execute("SELECT MAX(libraryid) FROM package_library"
                                      ).fetchone()[0]
        self.conn.execute("UPDATE packages SET libraryid = ? WHERE packageid = ?",
                          (libraryid, packageid))

        package_library.ensure_library(self.conn)

        holder = self.conn.execute(
            "SELECT userid, role FROM package_rights WHERE libraryid = ?",
            (libraryid,)).fetchall()
        revision = self.conn.execute(
            "SELECT revision, data FROM package_revisions WHERE libraryid = ?",
            (libraryid,)).fetchone()
        copy = self.conn.execute("SELECT revision FROM packages WHERE packageid = ?",
                                 (packageid,)).fetchone()
        self.assertEqual([(row[0], row[1]) for row in holder], [(3, 'holder')])
        self.assertEqual(revision[0], 1)
        self.assertEqual(json.loads(revision[1])['topics'][0]['topic'], 'Старая тема')
        self.assertEqual(copy[0], 1)

    def test_migration_is_idempotent(self):
        package_library.ensure_library(self.conn)
        first = self.conn.execute("SELECT COUNT(*) FROM package_rights").fetchone()[0]
        revisions = self.conn.execute("SELECT COUNT(*) FROM package_revisions").fetchone()[0]
        package_library.ensure_library(self.conn)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM package_rights").fetchone()[0], first)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM package_revisions").fetchone()[0],
            revisions)


if __name__ == '__main__':
    unittest.main()
