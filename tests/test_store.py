import tempfile
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


if __name__ == '__main__':
    unittest.main()
