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

    def test_second_store_cannot_interrupt_generation_or_publication(self):
        from publikot.settings import ConfigError
        job, _ = self.store.create_job(42, 'Live generation')
        self.store.begin_event('live-event')
        with self.assertRaisesRegex(ConfigError, 'База Публикота уже используется'):
            Store(self.path)
        self.assertEqual(self.store.job(42, job)['status'], 'generating')
        self.assertEqual(self.store.db.execute("SELECT status FROM inbox WHERE event_id='live-event'").fetchone()[0], 'processing')
        self.store.save_version(42, job, 1, 'Completed text', False)
        self.store.claim(42, job, 1)
        with self.assertRaises(ConfigError):
            Store(self.path)
        self.assertEqual(self.store.job(42, job)['status'], 'publishing')
        self.store.db.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.job(42, job)['status'], 'unknown')

    def test_separate_process_cannot_run_startup_recovery_on_live_database(self):
        import subprocess
        import sys
        job, _ = self.store.create_job(42, 'Active text request')
        program = (
            'import sys\nfrom pathlib import Path\n'
            'from publikot.store import Store\n'
            'from publikot.settings import ConfigError\n'
            'try:\n    Store(Path(sys.argv[1]))\n'
            'except ConfigError:\n    sys.exit(42)\n'
        )
        result = subprocess.run([sys.executable, '-c', program, str(self.path)],
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 42, result.stderr.decode())
        self.assertEqual(self.store.job(42, job)['status'], 'generating')
        self.store.save_version(42, job, 1, 'Paid result preserved', False)
        self.assertEqual(self.store.revision(42, job)['body'], 'Paid result preserved')

    def test_default_templates_update_but_custom_templates_and_image_tasks_survive_restart(self):
        from publikot.prompts import (TEXT_TEMPLATE, IMAGE_TEMPLATE,
                                      LEGACY_TEXT_TEMPLATE, LEGACY_IMAGE_TEMPLATE)
        custom = self.store.create_project(42, 'Custom')
        self.store.db.execute('UPDATE projects SET text_template=?, image_template=? WHERE id=?',
                             ('Custom text', 'Custom image', custom))
        self.store.db.execute('UPDATE projects SET text_template=?, image_template=? WHERE id=?',
                             (LEGACY_TEXT_TEMPLATE, LEGACY_IMAGE_TEMPLATE, self.project))
        self.store.select(42, self.project)
        job = self.draft()
        self.store.db.execute('UPDATE jobs SET image_brief=?, image_reference_path=? WHERE id=?',
                             ('Separate image task', '/image-ref.png', job))
        self.store.set_dialog(42, {'kind': 'image_task', 'project': self.project, 'job': job, 'revision': 1})
        self.store.db.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.require(42, self.project)['text_template'], TEXT_TEMPLATE)
        self.assertEqual(self.store.require(42, self.project)['image_template'], IMAGE_TEMPLATE)
        self.assertEqual(self.store.require(42, custom)['text_template'], 'Custom text')
        self.assertEqual(self.store.require(42, custom)['image_template'], 'Custom image')
        self.assertEqual(self.store.job(42, job)['image_brief'], 'Separate image task')
        self.assertEqual(self.store.job(42, job)['image_reference_path'], '/image-ref.png')
        self.assertEqual(self.store.dialog(42)['kind'], 'image_task')

    def test_legacy_tone_and_style_migrate_to_concept_once_without_losing_files_or_drafts(self):
        self.store.configure(42, 'concept', 'Истории рыжего кота')
        self.store.configure(42, 'tone', 'Разговорная речь')
        self.store.configure(42, 'visual_style', 'Мягкий свет')
        self.store.set_material(42, self.project, 'style', '/archive/style.png', 'Тёплая палитра')
        # Old databases may have both the textual field and a reference caption.
        self.store.db.execute('UPDATE projects SET visual_style=? WHERE id=?', ('Мягкий свет', self.project))
        job = self.draft()
        self.store.db.execute("DELETE FROM schema_meta WHERE name='concept_consolidated'")
        self.store.db.close()
        self.store = Store(self.path)
        project = self.store.project(42)
        for value in ('Истории рыжего кота', 'Разговорная речь', 'Мягкий свет', 'Тёплая палитра'):
            self.assertIn(value, project['concept'])
        self.assertEqual((project['tone'], project['visual_style']), ('', ''))
        self.assertEqual(self.store.material(42, self.project, 'style')['path'], '/archive/style.png')
        self.assertEqual(self.store.revision(42, job)['body'], 'Текст')
        self.store.configure(42, 'concept', 'Новая концепция без прежнего оформления')
        self.store.db.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.project(42)['concept'], 'Новая концепция без прежнего оформления')

    def test_concept_consolidation_does_not_duplicate_existing_text(self):
        self.store.configure(42, 'concept', 'Дружелюбный кот. Тёплые цвета.')
        self.store.configure(42, 'tone', 'Дружелюбный кот.')
        self.store.set_material(42, self.project, 'style', '/archive.png', 'Тёплые цвета.')
        with self.store.transaction():
            self.store.consolidate_project_concept(self.project)
        self.assertEqual(self.store.project(42)['concept'], 'Дружелюбный кот. Тёплые цвета.')

    def test_delete_project_cleans_dependencies_and_preserves_other_projects(self):
        from publikot.oauth import OAuthManager
        from publikot.settings import Settings
        settings = Settings('test-key', 123, 42, Path(self.tmp.name))
        manager = OAuthManager(settings, self.store, None)
        self.store.set_access(42, 99, True, 42)
        self.store.set_member(42, self.project, 99, True, 42)
        self.store.select(99, self.project)
        self.store.set_dialog(99, {'kind': 'field', 'project': self.project, 'field': 'concept'})
        self.store.set_material(42, self.project, 'style', '/style.jpg')
        self.store.reference(42, '/old-reference.jpg', 'Архив')
        character = self.store.db.execute('INSERT INTO characters(project_id,name) VALUES (?,?)',
                                          (self.project, 'Архив')).lastrowid
        self.store.db.execute('INSERT INTO character_images(character_id,path) VALUES (?,?)', (character, '/old.jpg'))
        ident = self.draft()
        self.store.claim(42, ident, 1)
        self.store.finish(42, ident, 'published', 'https://vk.ru/wall-123_1')
        self.store.db.execute("INSERT INTO vk_connections VALUES (?,?,?,?,0,'active')",
                              (self.project, 123, 42, manager.vault.encrypt('private-token')))
        self.store.db.execute("INSERT INTO oauth_states(state_hash,actor,project_id,group_id,expires_at,verifier) VALUES ('state',42,?,123,1,'encrypted')", (self.project,))
        other = self.store.create_project(42, 'Другой')
        other_job = self.draft()
        with self.assertRaises(Conflict):
            self.store.delete_project(99, self.project, 42)
        self.store.delete_project(42, self.project, 42)
        self.assertEqual([p['id'] for p in self.store.projects(42)], [other])
        self.assertEqual(self.store.job(42, other_job)['project_id'], other)
        self.assertIsNone(self.store.dialog(99))
        self.assertIsNone(self.store.db.execute('SELECT project_id FROM sessions WHERE owner=99').fetchone()[0])
        for table in ('approvals', 'characters', 'character_images', 'project_references',
                      'project_materials', 'oauth_states', 'vk_connections'):
            self.assertEqual(self.store.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 0)
        self.assertEqual(self.store.db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_active_jobs_block_deletion_and_ids_are_not_reused_after_restart(self):
        ident, _ = self.store.create_job(42, 'Задание')
        with self.assertRaises(Conflict):
            self.store.delete_project(42, self.project, 42)
        self.store.save_version(42, ident, 1, 'Текст', False)
        self.store.claim(42, ident, 1)
        with self.assertRaises(Conflict):
            self.store.delete_project(42, self.project, 42)
        self.store.finish(42, ident, 'published')
        self.store.delete_project(42, self.project, 42)
        self.store.db.close()
        self.store = Store(self.path)
        new = self.store.create_project(42, 'Новый')
        new_job, _ = self.store.create_job(42, 'Задание')
        self.assertGreater(new, self.project)
        self.assertGreater(new_job, ident)

    def test_admin_can_delete_project_created_by_another_user(self):
        self.store.set_access(42, 99, True, 42)
        project = self.store.create_project(99, 'Проект участника')
        self.store.delete_project(42, project, 42)
        self.assertEqual(self.store.projects(99), [])

    def test_project_data_survives_restart(self):
        self.store.db.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.project(42)['name'], 'Первый')

    def test_project_prompt_defaults_and_custom_values_survive_restart(self):
        from publikot.prompts import TEXT_TEMPLATE, IMAGE_TEMPLATE
        self.assertEqual(self.store.project(42)['text_template'], TEXT_TEMPLATE)
        self.assertEqual(self.store.project(42)['image_template'], IMAGE_TEMPLATE)
        self.store.configure(42, 'text_template', 'Свой текстовый промпт')
        self.store.configure(42, 'image_template', 'Свой промпт картинки')
        self.store.db.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.project(42)['text_template'], 'Свой текстовый промпт')
        self.assertEqual(self.store.project(42)['image_template'], 'Свой промпт картинки')
        self.store.create_project(42, 'Новый проект')
        self.assertEqual(self.store.project(42)['text_template'], TEXT_TEMPLATE)
        self.assertEqual(self.store.project(42)['image_template'], IMAGE_TEMPLATE)

    def test_existing_projects_receive_both_base_prompts_on_upgrade(self):
        from publikot.prompts import TEXT_TEMPLATE, IMAGE_TEMPLATE
        self.store.db.execute('ALTER TABLE projects DROP COLUMN text_template')
        self.store.db.execute('ALTER TABLE projects DROP COLUMN image_template')
        self.store.db.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.project(42)['text_template'], TEXT_TEMPLATE)
        self.assertEqual(self.store.project(42)['image_template'], IMAGE_TEMPLATE)

    def test_legacy_character_photos_migrate_once_without_deleting_archive(self):
        self.store.db.execute("DELETE FROM schema_meta WHERE name='single_project_materials'")
        character = self.store.db.execute('INSERT INTO characters(project_id,name,description) VALUES (?,?,?)',
                                          (self.project, 'Кот', 'Рыжий')).lastrowid
        for path in ('/old.jpg', '/latest.jpg'):
            self.store.db.execute('INSERT INTO character_images(character_id,path) VALUES (?,?)', (character, path))
        self.store.reference(42, '/legacy-reference.jpg', 'Архив')
        self.store.db.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.material(42, self.project, 'character')['path'], '/latest.jpg')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM character_images').fetchone()[0], 2)
        self.assertEqual(len(self.store.references(42)), 1)
        self.store.set_material(42, self.project, 'character', '/replacement.jpg', 'Новый')
        self.store.set_material(42, self.project, 'style', '/style.jpg', 'Пастель')
        self.store.db.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.material(42, self.project, 'character')['path'], '/replacement.jpg')
        self.assertEqual(self.store.material(42, self.project, 'style')['path'], '/style.jpg')

    def test_materials_reject_foreign_project_and_keep_one_per_section(self):
        for index in range(3):
            self.store.set_material(42, self.project, 'style', f'/style-{index}.jpg', 'Описание')
        self.assertEqual(self.store.material(42, self.project, 'style')['path'], '/style-2.jpg')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM project_materials').fetchone()[0], 1)
        with self.assertRaises(Conflict):
            self.store.set_material(99, self.project, 'style', '/foreign.jpg')
        with self.assertRaises(Conflict):
            self.store.material(99, self.project, 'style')

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
        self.assertEqual(character['images'], 1)
        self.assertEqual(self.store.material(42, self.project, 'character')['path'], '/test/side.jpg')
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
