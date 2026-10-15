import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from publikot.settings import Settings
from publikot.store import Store
from publikot.vk_bot import VkBot
from publikot.vk_client import VkApiError, VkMessages


class MenuCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings('test-key', 123, 42, Path(self.temp.name))
        self.store = Store(self.settings.database)
        self.project = self.store.create_project(42, 'Кофейня')
        self.client = VkMessages('test-key', 123, None)
        self.sent, self.deleted = [], []
        self.next_id, self.event_id = 100, 1000
        self.reject_delete = False

        async def api(method, **params):
            if method == 'messages.send':
                self.next_id += 1
                self.sent.append({**params, 'id': self.next_id})
                return self.next_id
            self.assertEqual(method, 'messages.delete')
            self.deleted.append(params)
            if self.reject_delete:
                raise VkApiError(method, 924)
            return {value: 1 for value in params['message_ids'].split(',')}

        self.client.api = AsyncMock(side_effect=api)

        async def photo(peer, path, caption=''):
            return await self.client.send(peer, caption or 'Фото', attachment=path)

        self.client.send_photo = AsyncMock(side_effect=photo)
        self.bot = VkBot(self.settings, self.store, self.client)

    async def asyncTearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    async def send(self, text='', screen=None, **params):
        self.event_id += 1
        message = {'id': self.event_id, 'from_id': 42, 'peer_id': 42, 'text': text}
        if screen:
            message['payload'] = json.dumps({'action': 'menu', 'screen': screen, **params})
        event = {'type': 'message_new', 'group_id': 123, 'event_id': str(self.event_id),
                 'object': {'message': message}}
        await self.bot.handle(event)
        return event

    async def test_button_deletes_previous_bot_messages_for_both_participants(self):
        await self.send('/menu')
        old = self.sent[-1]['id']
        await self.send(screen='communities')
        self.assertEqual(self.deleted, [{'peer_id': 42, 'message_ids': str(old), 'delete_for_all': 1}])
        self.assertEqual(self.client.tracked_messages(42), [self.sent[-1]['id']])
        self.assertNotIn(str(self.event_id), self.deleted[0]['message_ids'].split(','))
        self.assertEqual(self.client.api.await_args_list[-2].args[0], 'messages.send')
        self.assertEqual(self.client.api.await_args_list[-1].args[0], 'messages.delete')

    async def test_image_gallery_and_all_text_chunks_are_removed_on_next_button(self):
        for index in range(4):
            self.store.reference(42, f'/image-{index}.jpg', str(index))
        await self.send(screen='references', project=self.project)
        old = self.client.tracked_messages(42)
        self.assertEqual(len(old), 5)
        await self.send(screen='settings', project=self.project)
        self.assertEqual(self.deleted[-1]['message_ids'], ','.join(map(str, old)))
        self.store.configure(42, 'concept', 'Концепция' * 1000)
        await self.send(screen='field', project=self.project, field='concept')
        chunks = self.client.tracked_messages(42)
        self.assertGreater(len(chunks), 1)
        await self.send(screen='settings', project=self.project)
        self.assertEqual(self.deleted[-1]['message_ids'], ','.join(map(str, chunks)))

    async def test_tracking_survives_restart_and_is_scoped_to_peer_and_bot(self):
        await self.send('/menu')
        old = self.sent[-1]['id']
        await self.client.send(99, 'Другой пользователь')
        other_peer = self.sent[-1]['id']
        self.store.remember_bot_message(456, 42, 999)
        self.store.db.close()
        self.store = Store(self.settings.database)
        self.bot = VkBot(self.settings, self.store, self.client)
        await self.send(screen='communities')
        self.assertEqual(self.deleted[-1]['message_ids'], str(old))
        self.assertEqual(self.store.bot_message_ids(123, 99), [other_peer])
        self.assertEqual(self.store.bot_message_ids(456, 42), [999])

    async def test_failed_navigation_preserves_previous_screen(self):
        await self.send('/menu')
        old = self.sent[-1]['id']
        await self.send(screen='settings', project=999)
        self.assertEqual(self.deleted, [])
        self.assertIn(old, self.client.tracked_messages(42))

    async def test_delete_failure_does_not_repeat_successful_action(self):
        await self.send('/menu')
        self.reject_delete = True
        with contextlib.redirect_stdout(io.StringIO()) as output:
            event = await self.send(screen='compose', project=self.project)
        self.assertIn('924', output.getvalue())
        self.assertEqual(self.store.dialog(42)['kind'], 'post')
        self.assertEqual(self.store.db.execute('SELECT status FROM inbox WHERE event_id=?',
            ('123:' + event['event_id'],)).fetchone()['status'], 'done')
        count = len(self.sent)
        await self.bot.handle(event)
        self.assertEqual(len(self.sent), count)
        self.assertEqual(len(self.deleted), 1)

    async def test_untracked_ids_cannot_be_deleted(self):
        await self.client.send(99, 'Другое сообщение')
        await self.client.delete_messages(42, [self.sent[-1]['id'], self.event_id])
        self.assertEqual(self.deleted, [])

    async def test_structured_delete_response_and_partial_failure(self):
        await self.send('/menu')
        await self.client.send(42, 'Ещё сообщение')
        first, second = self.client.tracked_messages(42)
        self.client.api = AsyncMock(return_value=[
            {'peer_id': 42, 'message_id': first, 'response': 1},
            {'peer_id': 42, 'message_id': second, 'response': 0, 'error': {'code': 924}}])
        from publikot.providers import ProviderError
        with self.assertRaises(ProviderError):
            await self.client.delete_messages(42, [first, second])
        self.assertEqual(self.client.tracked_messages(42), [second])

    async def test_partial_rejection_does_not_block_later_batches(self):
        for ident in range(1, 206):
            self.store.remember_bot_message(123, 42, ident)
        self.client.api = AsyncMock(side_effect=lambda method, **params: {
            value: 0 if value == '1' else 1 for value in params['message_ids'].split(',')})
        from publikot.providers import ProviderError
        with self.assertRaises(ProviderError):
            await self.client.delete_messages(42, list(range(1, 206)))
        self.assertEqual(self.client.api.await_count, 3)
        self.assertEqual(self.client.tracked_messages(42), [1])
