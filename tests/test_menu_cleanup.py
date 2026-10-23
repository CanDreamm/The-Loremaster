import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

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
        self.sent, self.deleted, self.answers = [], [], []
        self.next_id, self.event_id = 100, 1000
        self.reject_delete = False

        async def api(method, **params):
            if method == 'messages.sendMessageEventAnswer':
                self.answers.append(params)
                return 1
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

    async def click(self, payload, actor=42, peer=42, event_id=None):
        self.event_id += 1
        event = {'type': 'message_event', 'group_id': 123, 'object': {
            'event_id': event_id or str(self.event_id), 'user_id': actor, 'peer_id': peer,
            'conversation_message_id': 7, 'payload': payload}}
        await self.bot.handle(event)
        return event

    async def test_start_and_callback_navigation_without_incoming_text(self):
        await self.send('/start')
        source = self.sent[-1]['id']
        keys = json.loads(self.sent[-1]['keyboard'])
        first = keys['buttons'][0][0]['action']
        self.assertEqual(first['type'], 'callback')
        await self.click(json.loads(first['payload']))
        self.assertEqual(len(self.answers), 1)
        self.assertEqual(self.deleted[-1]['message_ids'], str(source))
        self.assertIn('проект', self.sent[-1]['message'].lower())
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM inbox').fetchone()[0], 2)

    async def test_callback_starts_input_and_regular_text_saves_setting(self):
        await self.click({'action': 'menu', 'screen': 'field_add', 'project': self.project, 'field': 'concept'})
        self.assertEqual(self.store.dialog(42)['kind'], 'field')
        await self.send('Дружелюбный')
        self.assertEqual(self.store.project(42)['concept'], 'Дружелюбный')
        self.assertIsNone(self.store.dialog(42))
        await self.click({'action': 'menu', 'screen': 'settings', 'project': self.project})
        self.assertIn('Настройки проекта', self.sent[-1]['message'])

    async def test_duplicate_callback_does_not_publish_twice(self):
        self.store.configure(42, 'vk_group', 456)
        ident, _ = self.store.create_job(42, 'Пост')
        self.store.save_version(42, ident, 1, 'Готовый текст', False)
        publish = AsyncMock(return_value='https://vk.com/wall-456_1')
        self.bot.publisher_factory = lambda token: SimpleNamespace(publish=publish)
        with patch.dict('os.environ', {f'VK_ACCESS_TOKEN_PROJECT_{self.project}': 'test-publishing-key'}):
            event = await self.click({'action': 'publish', 'job': ident, 'revision': 1})
            await self.bot.handle(event)
        publish.assert_awaited_once()
        self.assertEqual(len(self.answers), 2)
        self.assertEqual(self.store.job(42, ident)['status'], 'published')

    async def test_callback_acknowledgement_is_before_action(self):
        await self.click({'action': 'menu', 'screen': 'home'})
        self.assertEqual(self.client.api.await_args_list[0].args[0], 'messages.sendMessageEventAnswer')
        self.assertEqual(self.client.api.await_args_list[1].args[0], 'messages.send')

    async def test_callback_rechecks_access_and_rejects_group_chats(self):
        payload = {'action': 'menu', 'screen': 'compose', 'project': self.project}
        await self.click(payload, actor=99, peer=99)
        self.assertIn('Доступ к боту отозван', self.sent[-1]['message'])
        self.assertIsNone(self.store.dialog(99))
        count = len(self.sent)
        await self.click(payload, actor=42, peer=2000000001)
        self.assertEqual(len(self.sent), count)
        self.assertIsNone(self.store.dialog(42))
        self.store.set_access(42, 99, True, 42)
        await self.click(payload, actor=99, peer=99)
        self.assertIn('Нет доступа к проекту', self.sent[-1]['message'])

    async def test_callback_deduplication_survives_restart(self):
        event = await self.click({'action': 'menu', 'screen': 'home'})
        count = len(self.sent)
        self.store.db.close()
        self.store = Store(self.settings.database)
        self.bot = VkBot(self.settings, self.store, self.client)
        await self.bot.handle(event)
        self.assertEqual(len(self.sent), count)
        self.assertEqual(len(self.answers), 2)

    async def test_callback_ack_failure_does_not_block_navigation(self):
        original = self.client.api.side_effect
        async def api(method, **params):
            if method == 'messages.sendMessageEventAnswer':
                raise VkApiError(method, 100)
            return await original(method, **params)
        self.client.api.side_effect = api
        with contextlib.redirect_stdout(io.StringIO()):
            await self.click({'action': 'menu', 'screen': 'home'})
        self.assertIn('Публикот', self.sent[-1]['message'])

    async def test_malformed_callback_preserves_previous_screen(self):
        await self.send('/start')
        original = self.sent[-1]['id']
        await self.click(['not-an-action'])
        self.assertIn('Кнопка некорректна', self.sent[-1]['message'])
        self.assertEqual(self.deleted, [])
        self.assertIn(original, self.client.tracked_messages(42))

    async def test_button_deletes_previous_bot_messages_for_both_participants(self):
        await self.send('/menu')
        old = self.sent[-1]['id']
        await self.send(screen='communities')
        self.assertEqual(self.deleted, [{'peer_id': 42, 'message_ids': str(old), 'delete_for_all': 1}])
        self.assertEqual(self.client.tracked_messages(42), [self.sent[-1]['id']])
        self.assertNotIn(str(self.event_id), self.deleted[0]['message_ids'].split(','))
        self.assertEqual(self.client.api.await_args_list[-2].args[0], 'messages.send')
        self.assertEqual(self.client.api.await_args_list[-1].args[0], 'messages.delete')

    async def test_material_photo_and_all_text_chunks_are_removed_on_next_button(self):
        self.store.set_material(42, self.project, 'character', '/character.jpg', 'Рыжий кот')
        await self.send(screen='characters', project=self.project)
        old = self.client.tracked_messages(42)
        self.assertEqual(len(old), 2)
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
