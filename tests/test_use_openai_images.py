import tempfile
import unittest
from pathlib import Path

from dotenv import dotenv_values

from publikot.use_openai_images import configure


class OpenAIImageSetupTests(unittest.TestCase):
    def test_selects_low_and_preserves_tokens_transport_and_other_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env.python'
            path.write_text('# Configuration\nBOT_TRANSPORT=vk\nGENAPI_API_KEY=private-key\n'
                'GENAPI_IMAGE_MODEL=seedream-v5-lite\nGENAPI_IMAGE_QUALITY=high\nDEMO_MODE=false\n')
            configure(path)
            configure(path)
            values = dotenv_values(path)
            self.assertEqual(values['GENAPI_IMAGE_MODEL'], 'gpt-image-2-5')
            self.assertEqual(values['GENAPI_IMAGE_QUALITY'], 'low')
            self.assertEqual(values['GENAPI_API_KEY'], 'private-key')
            self.assertEqual(values['BOT_TRANSPORT'], 'vk')
            self.assertEqual(values['DEMO_MODE'], 'false')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.read_text().count('GENAPI_IMAGE_MODEL='), 1)

    def test_missing_configuration_does_not_create_a_partial_env_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '.env.python'
            with self.assertRaises(ValueError):
                configure(path)
            self.assertFalse(path.exists())
