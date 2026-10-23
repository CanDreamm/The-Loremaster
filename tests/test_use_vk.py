import os
import json
import runpy
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from dotenv import dotenv_values

from publikot.oauth import Vault
from publikot.settings import Settings
from publikot.store import Store
from publikot.use_vk import import_projects, switch


class ReturnToVKTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.settings = Settings('vk-key', 123, 500, self.root, demo=False, genapi_key='private-genapi-key')
        self.source = Store(self.root / 'publikot-telegram.sqlite3')
        self.target = Store(self.settings.database)
        self.vault = Vault(self.root / 'platforms.key', may_create=True)
        self.project = self.source.create_project(42, 'Бренд')
        self.source.configure(42, 'concept', 'Истории персонажа')
        self.source.configure(42, 'image_count', 3)
        self.source.configure(42, 'image_format', 'landscape')
        self.source.put_credential(42, 'postmypost', self.vault.encrypt('private-owner-pmp'))
        self.source.attach_destination(42, self.project, 'postmypost', 10, 20, 'vkontakte', 'Бренд ВК')
        reference = self.root / 'character.png'
        reference.write_bytes(b'\x89PNG\r\n\x1a\nphoto')
        self.source.set_material(42, self.project, 'character', str(reference), 'Описание персонажа')
        self.source.set_access(42, 500, True, 42)  # Same numeric ID as VK owner must NOT migrate as that user.
        self.source.set_member(42, self.project, 500, True, 42)
        self.other = self.source.create_project(500, 'Чужой проект')
        self.job, _ = self.source.create_job(42, 'Старый пост', project_id=self.project)
        self.source.save_version(42, self.job, 1, 'Текст', False)
        self.target.create_project(500, 'Бренд')  # Existing VK data must stay intact.

    def tearDown(self):
        self.source.db.close()
        self.target.db.close()
        self.tmp.cleanup()

    def import_owner(self):
        return import_projects(self.root / 'publikot-telegram.sqlite3', self.target, 42, 500)

    def test_owner_projects_materials_and_credentials_move_to_vk_without_identity_collision(self):
        report = self.import_owner()
        self.assertEqual(report['projects'], 1)
        self.assertEqual(report['destinations'], 1)
        self.assertEqual(report['members_to_assign'], 1)
        projects = self.target.projects(500)
        self.assertEqual(len(projects), 2)
        imported = next(dict(p) for p in projects if p['concept'])
        self.assertNotEqual(imported['name'], 'Бренд')
        self.assertEqual((imported['image_count'], imported['image_format']), (3, 'landscape'))
        self.assertEqual(self.target.db.execute('SELECT count(*) FROM project_members WHERE project_id=?', (imported['id'],)).fetchone()[0], 1)
        self.assertEqual(self.target.require(500, imported['id'])['owner'], 500)
        self.assertTrue(Path(self.target.material(500, imported['id'], 'character')['path']).is_file())
        target = self.target.destination_rows(500, imported['id'])[0]
        self.assertEqual(target['credential_owner'], 500)
        self.assertEqual(self.vault.decrypt(self.target.credential_record(500, 'postmypost')['ciphertext']), 'private-owner-pmp')
        self.assertEqual(self.target.db.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)
        self.assertEqual(self.source.job(42, self.job)['status'], 'review')
        self.assertEqual(self.import_owner()['already_imported'], 1)
        self.assertEqual(len(self.target.projects(500)), 2)

    def test_conflicting_vk_token_is_preserved_and_destination_requires_reconnection(self):
        self.target.put_credential(500, 'postmypost', self.vault.encrypt('private-existing-vk-pmp'))
        report = self.import_owner()
        self.assertEqual(report['connections_to_reconnect'], 1)
        self.assertEqual(report['destinations'], 0)
        self.assertEqual(self.vault.decrypt(self.target.credential_record(500, 'postmypost')['ciphertext']), 'private-existing-vk-pmp')

    def test_same_token_with_different_ciphertext_reuses_connection(self):
        self.target.put_credential(500, 'postmypost', self.vault.encrypt('private-owner-pmp'))
        self.assertEqual(self.import_owner()['destinations'], 1)

    def test_other_users_destinations_are_not_transferred_to_administrator(self):
        self.source.put_credential(500, 'postmypost', self.vault.encrypt('private-other-user'))
        self.source.attach_destination(500, self.project, 'postmypost', 10, 21, 'vkontakte', 'Чужое подключение')
        report = self.import_owner()
        self.assertEqual(report['destinations'], 1)
        self.assertEqual(report['connections_to_reconnect'], 1)

    def test_switch_persists_vk_and_preserves_all_ai_settings_and_source_database(self):
        config = self.root / '.env.python'
        config.write_text('BOT_TRANSPORT=telegram\nGENAPI_API_KEY=private-genapi-key\nDEMO_MODE=false\n')
        source_count = self.source.db.execute('SELECT count(*) FROM projects').fetchone()[0]
        self.target.db.close()
        try:
            report, backup = switch(config, self.settings, 42)
            self.assertEqual(report['projects'], 1)
            self.assertTrue(backup.is_file())
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
            values = dotenv_values(config)
            self.assertEqual(values['BOT_TRANSPORT'], 'vk')
            self.assertEqual(values['GENAPI_API_KEY'], 'private-genapi-key')
            self.assertEqual(values['DEMO_MODE'], 'false')
            self.assertEqual(self.source.db.execute('SELECT count(*) FROM projects').fetchone()[0], source_count)
        finally:
            self.target = Store(self.settings.database)

    def test_explicit_vk_settings_ignore_previous_telegram_selection(self):
        with patch.dict(os.environ, {'BOT_TRANSPORT': 'telegram', 'VK_BOT_TOKEN': 'vk-key',
            'VK_BOT_GROUP_ID': '123', 'OWNER_VK_ID': '500', 'OWNER_TELEGRAM_ID': '42'}, clear=True), \
             patch('publikot.settings.load_dotenv'):
            settings = Settings.load(transport='vk')
        self.assertEqual((settings.transport, settings.owner_id), ('vk', 500))

    def test_dzen_session_is_mapped_to_vk_owner_with_channel_unchanged(self):
        key = 'dzen:publisher123'
        auth = json.dumps({'publisher_id': 'publisher123', 'csrf_token': 'private-csrf', 'cookies': {'session': 'private'}})
        self.source.put_credential(42, key, self.vault.encrypt(auth))
        self.source.attach_destination(42, self.project, 'dzen', '', 'publisher123', 'dzen', 'Дзен', credential_key=key)
        self.assertEqual(self.import_owner()['destinations'], 2)
        imported = next(p for p in self.target.projects(500) if p['concept'])
        target = next(t for t in self.target.destination_rows(500, imported['id']) if t['provider'] == 'dzen')
        self.assertEqual((target['credential_owner'], target['remote_account']), (500, 'publisher123'))
        self.assertEqual(self.vault.decrypt(self.target.credential_record(500, key)['ciphertext']), auth)

    def test_invalid_source_settings_roll_back_project_and_credential_import(self):
        self.source.db.execute('UPDATE projects SET image_count=99 WHERE id=?', (self.project,))
        from publikot.store import Conflict
        with self.assertRaises(Conflict):
            self.import_owner()
        self.assertEqual(len(self.target.projects(500)), 1)
        self.assertIsNone(self.target.credential_record(500, 'postmypost'))

    def test_setup_script_really_switches_transport_and_keeps_secret_values(self):
        scripts = self.root / 'scripts'
        scripts.mkdir()
        script = scripts / 'setup-python.py'
        script.write_text((Path(__file__).resolve().parent.parent / 'scripts' / 'setup-python.py').read_text())
        config = self.root / '.env.python'
        config.write_text('BOT_TRANSPORT=telegram\nVK_BOT_TOKEN=private-vk-key\nOWNER_VK_ID=500\nGENAPI_API_KEY=private-genapi-key\n')
        with patch('getpass.getpass', return_value=''), patch('builtins.input', side_effect=['', '']), patch('builtins.print'):
            runpy.run_path(str(script), run_name='__main__')
        values = dotenv_values(config)
        self.assertEqual(values['BOT_TRANSPORT'], 'vk')
        self.assertEqual(values['VK_BOT_TOKEN'], 'private-vk-key')
        self.assertEqual(values['GENAPI_API_KEY'], 'private-genapi-key')
