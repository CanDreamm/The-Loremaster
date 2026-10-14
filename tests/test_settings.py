import contextlib
import io
import os
import runpy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dotenv import dotenv_values

from publikot.settings import ConfigError, Settings


class SettingsTests(unittest.TestCase):
    def load(self, values, require_owner=True):
        with patch.dict(os.environ, values, clear=True), patch('publikot.settings.load_dotenv'):
            return Settings.load(require_owner=require_owner)

    def test_vk_uses_separate_identity_and_database(self):
        settings = self.load({'VK_BOT_TOKEN': 'test-bot', 'OWNER_VK_ID': '42'})
        self.assertEqual(settings.group_id, 242305804)
        self.assertEqual(settings.database.name, 'publikot-vk.sqlite3')
        self.assertEqual(settings.owner_id, 42)

    def test_telegram_identity_does_not_grant_vk_administration(self):
        with self.assertRaises(ConfigError):
            self.load({'VK_BOT_TOKEN': 'test-bot', 'OWNER_TELEGRAM_ID': '42'})

    def test_identify_does_not_require_admin_id(self):
        self.assertEqual(self.load({'VK_BOT_TOKEN': 'test-bot'}, require_owner=False).owner_id, 0)

    def test_bot_key_and_admin_key_are_not_shared_with_members(self):
        settings = self.load({'VK_BOT_TOKEN': 'bot-secret', 'OWNER_VK_ID': '42', 'VK_ACCESS_TOKEN': 'admin-secret'})
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(settings.vk_token_for(99, 1), '')
            self.assertEqual(settings.vk_token_for(42, 1), 'admin-secret')
        with patch.dict(os.environ, {'VK_ACCESS_TOKEN_PROJECT_1': 'project-secret'}, clear=True):
            self.assertEqual(settings.vk_token_for(99, 1), 'project-secret')
            self.assertEqual(settings.vk_token_for(99, 2), '')
        self.assertNotIn('bot-secret', repr(settings))
        self.assertNotIn('admin-secret', repr(settings))

    def test_setup_preserves_existing_values_and_unrelated_settings(self):
        source = Path(__file__).resolve().parent.parent / 'scripts' / 'setup-python.py'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'scripts').mkdir()
            script = root / 'scripts' / 'setup-python.py'
            script.write_text(source.read_text())
            config = root / '.env.python'
            config.write_text('VK_BOT_TOKEN=existing-test-key\nOWNER_VK_ID=42\nYANDEX_API_KEY=existing-ai-key\nTELEGRAM_PROXY=old-unused-value\n')
            output = io.StringIO()
            with patch('getpass.getpass', return_value=''), patch('builtins.input', side_effect=['', '']), contextlib.redirect_stdout(output):
                runpy.run_path(str(script), run_name='__main__')
            values = dotenv_values(config)
            self.assertEqual(values['VK_BOT_TOKEN'], 'existing-test-key')
            self.assertEqual(values['OWNER_VK_ID'], '42')
            self.assertEqual(values['YANDEX_API_KEY'], 'existing-ai-key')
            self.assertEqual(values['TELEGRAM_PROXY'], 'old-unused-value')
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            self.assertNotIn('existing-test-key', output.getvalue())
