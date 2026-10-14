import tempfile
import sqlite3
import unittest
from pathlib import Path

from publikot.store import Conflict, Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'test.sqlite3'
        self.store = Store(self.path)
        self.project = self.store.create_project(42, 'Первый')
        self.store.configure(42, 'vk_group', 123)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def draft(self, demo=False):
        ident, _ = self.store.create_job(42, 'Тема')
        self.store.save_version(42, ident, 1, 'Текст', demo)
        return ident

    def test_project_data_survives_restart(self):
        self.store.db.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.project(42)['name'], 'Первый')

    def test_owner_isolation(self):
        with self.assertRaises(Conflict):
            self.store.select(99, self.project)
        ident = self.draft()
        with self.assertRaises(Conflict):
            self.store.claim(99, ident, 1)
        self.assertEqual(self.store.projects(99), [])

    def test_old_approval_cannot_publish_new_revision(self):
        ident = self.draft()
        job, _ = self.store.revise(42)
        self.store.save_version(42, ident, job['revision'], 'Исправлено', False)
        with self.assertRaises(Conflict):
            self.store.claim(42, ident, 1)
        self.store.claim(42, ident, 2)

    def test_double_click_is_rejected(self):
        ident = self.draft()
        self.store.claim(42, ident, 1)
        with self.assertRaises(Conflict):
            self.store.claim(42, ident, 1)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM approvals').fetchone()[0], 1)

    def test_demo_cannot_be_published(self):
        ident = self.draft(demo=True)
        with self.assertRaises(Conflict):
            self.store.claim(42, ident, 1)
        self.assertEqual(self.store.job(42, ident)['status'], 'review')

    def test_group_is_frozen_at_creation(self):
        ident = self.draft()
        self.store.configure(42, 'vk_group', 999)
        job, _ = self.store.claim(42, ident, 1)
        self.assertEqual(job['target_group'], 123)

    def test_cancel_disables_publication(self):
        ident = self.draft()
        self.store.cancel(42, ident, 1)
        with self.assertRaises(Conflict):
            self.store.claim(42, ident, 1)

    def test_crashed_publish_is_not_retried(self):
        ident = self.draft()
        self.store.claim(42, ident, 1)
        self.store.db.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.job(42, ident)['status'], 'unknown')
        with self.assertRaises(Conflict):
            self.store.claim(42, ident, 1)

    def test_switch_project_clears_active_job(self):
        self.draft()
        self.store.create_project(42, 'Второй')
        with self.assertRaises(Conflict):
            self.store.job(42)

    def test_no_target_blocks_publication(self):
        self.store.create_project(42, 'Без ВК')
        ident = self.draft()
        with self.assertRaises(Conflict):
            self.store.claim(42, ident, 1)

    def test_character_references_belong_to_project(self):
        self.store.character(42, 'Лис', 'Рыжий', '/test/front.jpg')
        self.store.character(42, 'Лис', '', '/test/side.jpg')
        character = self.store.characters(42)[0]
        self.assertEqual(character['images'], 2)
        self.assertEqual(character['description'], 'Рыжий')
        self.store.create_project(42, 'Другой бренд')
        self.assertEqual(self.store.characters(42), [])

    def test_assignment_does_not_grant_bot_access(self):
        with self.assertRaises(Conflict):
            self.store.set_member(42, self.project, 99, True, 42)
        self.store.set_access(42, 99, True, 42)
        self.assertEqual(self.store.projects(99), [])
        self.store.set_member(42, self.project, 99, True, 42)
        self.store.select(99, self.project)
        self.store.configure(99, 'text_prompt', 'Своя инструкция')
        self.assertEqual(self.store.project(42)['text_prompt'], 'Своя инструкция')

    def test_only_admin_can_assign_projects(self):
        self.store.set_access(42, 99, True, 42)
        self.store.set_member(42, self.project, 99, True, 42)
        with self.assertRaises(Conflict):
            self.store.set_member(99, self.project, 42, False, 42)

    def test_admin_can_be_unassigned_without_losing_administration(self):
        self.store.set_member(42, self.project, 42, False, 42)
        self.assertEqual(self.store.projects(42), [])
        self.assertEqual(len(self.store.all_projects(42, 42)), 1)
        self.store.db.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.projects(42), [])
        self.store.set_member(42, self.project, 42, True, 42)
        self.assertEqual(len(self.store.projects(42)), 1)

    def test_removed_member_cannot_use_old_job_button(self):
        self.store.set_access(42, 99, True, 42)
        self.store.set_member(42, self.project, 99, True, 42)
        ident = self.draft()
        self.store.set_member(42, self.project, 99, False, 42)
        with self.assertRaises(Conflict):
            self.store.claim(99, ident, 1)

    def test_project_settings_and_references_are_isolated(self):
        self.store.configure(42, 'tone', 'Дружелюбный')
        self.store.configure(42, 'image_prompt', 'Пастель')
        self.store.reference(42, '/test/style.jpg', 'Палитра')
        self.store.create_project(42, 'Второй')
        self.assertEqual(self.store.project(42)['tone'], '')
        self.assertEqual(self.store.references(42), [])
        self.store.select(42, self.project)
        self.assertEqual(self.store.references(42)[0]['description'], 'Палитра')

    def test_old_database_migrates_without_losing_project(self):
        legacy = Path(self.tmp.name) / 'legacy.sqlite3'
        with sqlite3.connect(legacy) as db:
            db.execute("CREATE TABLE projects(id INTEGER PRIMARY KEY,owner INTEGER NOT NULL,name TEXT NOT NULL,concept TEXT NOT NULL DEFAULT '',vk_group INTEGER,UNIQUE(owner,name))")
            db.execute("INSERT INTO projects(owner,name,concept) VALUES (42,'Старый проект','Концепция')")
        migrated = Store(legacy)
        try:
            self.assertEqual(migrated.projects(42)[0]['concept'], 'Концепция')
            migrated.select(42, 1)
            migrated.configure(42, 'tone', 'Новый тон')
            self.assertEqual(migrated.project(42)['tone'], 'Новый тон')
        finally:
            migrated.db.close()

    def test_project_does_not_change_during_attachment_download(self):
        second = self.store.create_project(42, 'Другой')
        self.store.reference(42, '/test/first.jpg', 'Первый', project_id=self.project)
        ident, project = self.store.create_job(42, 'Задание первого', project_id=self.project)
        self.assertEqual(project['id'], self.project)
        self.assertEqual(self.store.job(42, ident)['project_id'], self.project)
        self.assertEqual(self.store.project(42)['id'], second)
        self.assertEqual(self.store.references(42), [])

    def test_processed_events_remain_deduplicated_after_restart(self):
        self.assertTrue(self.store.begin_event('vk:1'))
        self.store.end_event('vk:1', True)
        self.store.cursor(242305804, '123')
        self.store.db.close()
        self.store = Store(self.path)
        self.assertFalse(self.store.begin_event('vk:1'))
        self.assertEqual(self.store.cursor(242305804), '123')

    def test_crashed_events_are_not_automatically_reexecuted(self):
        self.assertTrue(self.store.begin_event('vk:interrupted'))
        self.store.db.close()
        self.store = Store(self.path)
        self.assertFalse(self.store.begin_event('vk:interrupted'))
        self.assertEqual(self.store.db.execute("SELECT status FROM inbox WHERE event_id='vk:interrupted'").fetchone()['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
