import runpy
import tempfile
import unittest
from pathlib import Path

from dotenv import dotenv_values


class OAuthSetupTests(unittest.TestCase):
    def setUp(self):
        script = Path(__file__).resolve().parents[1] / 'scripts' / 'configure-oauth.py'
        self.configure = runpy.run_path(str(script))['configure']

    def test_configuration_preserves_existing_secrets_and_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / '.env.python'
            target.write_text('VK_BOT_TOKEN=test-bot\nVK_OAUTH_CLIENT_SECRET=test-secret\n'
                              'VK_OAUTH_PROVIDER=vk_api\nVK_OAUTH_CLIENT_ID=123\n'
                              'OAUTH_PUBLIC_URL=https://old.example.com\nOWNER_VK_ID=42\n')
            self.configure(target)
            self.configure(target)
            self.assertEqual(dotenv_values(target), {
                'VK_BOT_TOKEN': 'test-bot', 'VK_OAUTH_CLIENT_SECRET': 'test-secret',
                'VK_OAUTH_PROVIDER': 'vk_api', 'VK_OAUTH_CLIENT_ID': '54825414',
                'OAUTH_PUBLIC_URL': 'https://bot.itiscat.ru', 'OWNER_VK_ID': '42',
            })
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_missing_configuration_is_not_replaced_with_empty_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / '.env.python'
            with self.assertRaises(SystemExit):
                self.configure(target)
            self.assertFalse(target.exists())
