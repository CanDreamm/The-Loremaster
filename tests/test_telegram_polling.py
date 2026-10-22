import asyncio
import os
import runpy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from dotenv import dotenv_values

from publikot.providers import ProviderError
from publikot.settings import Settings
from publikot.store import Store
from publikot.telegram_client import TelegramMessages, TelegramSettings
from publikot.telegram_polling import TelegramPolling
from publikot.telegram_runtime import run_telegram
from publikot.telegram_webhook import TelegramInbox


class TelegramPollingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / 'telegram.sqlite3')
        self.client = AsyncMock()
        self.inbox = TelegramInbox(self.store, AsyncMock())
        self.poller = TelegramPolling(self.inbox, self.client)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_offset_and_batch_persist_together_and_deduplicate_webhook_history(self):
        self.store.db.execute("INSERT INTO telegram_updates VALUES (10,'','done')")
        self.poller.save([{'update_id': 12}, {'update_id': 10}, {'update_id': 11}])
        self.assertEqual(self.poller.offset, 13)
        rows = self.store.db.execute('SELECT update_id,status FROM telegram_updates ORDER BY update_id').fetchall()
        self.assertEqual([(r['update_id'], r['status']) for r in rows], [(10, 'done'), (11, 'pending'), (12, 'pending')])
        self.assertTrue(self.inbox.wake.is_set())
        self.store.db.close()
        self.store = Store(Path(self.tmp.name) / 'telegram.sqlite3')
        inbox = TelegramInbox(self.store, AsyncMock())
        restarted = TelegramPolling(inbox, self.client)
        self.assertEqual(restarted.offset, 13)
        restarted.save([{'update_id': 11}])
        self.assertEqual(restarted.offset, 13)

    def test_failed_batch_rolls_back_events_and_offset(self):
        self.poller.save([{'update_id': 2}])
        with self.assertRaises(TypeError):
            self.poller.save([{'update_id': 3}, {'update_id': 4, 'invalid': object()}])
        self.assertEqual(self.poller.offset, 3)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM telegram_updates').fetchone()[0], 1)

    async def test_next_request_acknowledges_only_saved_batch(self):
        self.client.get_updates.side_effect = [[{'update_id': 7}], asyncio.CancelledError()]
        with self.assertRaises(asyncio.CancelledError):
            await self.poller.run()
        self.assertEqual([c.args for c in self.client.get_updates.call_args_list], [(None,), (8,)])
        self.assertEqual(self.store.db.execute('SELECT status FROM telegram_updates').fetchone()[0], 'pending')

    async def test_connection_failure_retries_without_advancing_offset(self):
        self.poller.save([{'update_id': 4}])
        self.client.get_updates.side_effect = [ProviderError('Нет связи'), asyncio.CancelledError()]
        with patch('publikot.telegram_polling.asyncio.sleep', new_callable=AsyncMock) as sleep, patch('builtins.print'):
            with self.assertRaises(asyncio.CancelledError):
                await self.poller.run()
        sleep.assert_awaited_once_with(2)
        self.assertEqual([c.args for c in self.client.get_updates.call_args_list], [(5,), (5,)])

    async def test_api_parameters_and_malformed_batches(self):
        client = TelegramMessages(TelegramSettings('key', '691ae7a7-6786-4d3d-9a35-7a3689263984'), None)
        client._call = AsyncMock(return_value=[{'update_id': 10}])
        self.assertEqual(await client.get_updates(8), [{'update_id': 10}])
        client._call.assert_awaited_once_with('getUpdates', {'offset': 8, 'timeout': 25, 'limit': 100,
                                                          'allowed_updates': ['message', 'callback_query']})
        for result in ({}, [{'update_id': True}], [{'update_id': -1}], [{'update_id': 2**63-1}]):
            client._call.return_value = result
            with self.assertRaises(ProviderError):
                await client.get_updates()

    async def test_disabling_webhook_preserves_pending_updates(self):
        client = TelegramMessages(None, None)
        client._call = AsyncMock(return_value=True)
        await client.delete_webhook()
        client._call.assert_awaited_once_with('deleteWebhook', {'drop_pending_updates': False})
        client._call.return_value = False
        with self.assertRaises(ProviderError):
            await client.delete_webhook()

    async def test_runtime_polling_without_secret_disables_webhook_and_starts_poller(self):
        settings = Settings('', 1, 42, Path(self.tmp.name), transport='telegram', oauth_port=0)
        config = TelegramSettings('key', '691ae7a7-6786-4d3d-9a35-7a3689263984')
        with patch.dict(os.environ, {'TELEGRAM_UPDATES_MODE': 'polling'}, clear=True), \
                patch('publikot.telegram_runtime.TelegramSettings.load', return_value=config), \
                patch('publikot.telegram_runtime.TelegramWorkflowMessages.get_me', new_callable=AsyncMock), \
                patch('publikot.telegram_runtime.TelegramWorkflowMessages.delete_webhook', new_callable=AsyncMock) as disable, \
                patch('publikot.telegram_runtime.TelegramPolling.run', new_callable=AsyncMock, side_effect=asyncio.CancelledError()) as poll, \
                patch('publikot.telegram_runtime.add_webhook') as webhook, patch('builtins.print'):
            with self.assertRaises(asyncio.CancelledError):
                await run_telegram(settings, SimpleNamespace(check=False, identify=False))
            disable.assert_awaited_once()
            poll.assert_awaited_once()
            webhook.assert_not_called()

    def test_mode_switch_preserves_server_key_and_owner_without_prompting(self):
        original = Path('scripts/configure-telegram.py').read_text()
        root = Path(self.tmp.name)
        (root / 'scripts').mkdir()
        script = root / 'scripts' / 'configure-telegram.py'
        script.write_text(original)
        config = root / '.env.python'
        config.write_text('CHATBOT_API_KEY=private\nOWNER_TELEGRAM_ID=42\nTELEGRAM_UPDATES_MODE=webhook\nOTHER=keep\n')
        with patch('sys.argv', [str(script), '--updates-mode', 'polling']), patch('builtins.input') as input_, \
                patch('getpass.getpass') as password, patch('builtins.print'):
            runpy.run_path(str(script), run_name='__main__')
        values = dotenv_values(config)
        self.assertEqual(values['TELEGRAM_UPDATES_MODE'], 'polling')
        self.assertEqual(values['OWNER_TELEGRAM_ID'], '42')
        self.assertEqual(values['CHATBOT_API_KEY'], 'private')
        self.assertEqual(values['OTHER'], 'keep')
        input_.assert_not_called()
        password.assert_not_called()
