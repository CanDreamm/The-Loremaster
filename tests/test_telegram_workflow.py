import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from publikot.providers import ProviderError
from publikot.settings import Settings
from publikot.store import Store
from publikot.telegram_bot import TelegramBot
from publikot.telegram_client import TelegramSettings, TelegramWorkflowMessages, TelegramApiError
from publikot.telegram_runtime import TelegramVkOAuthProvider
from publikot.telegram_webhook import TelegramInbox


class Response:
    status = 200

    def __init__(self, result):
        self.result = result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self):
        return {'ok': True, 'result': self.result}


class Session:
    def __init__(self):
        self.calls = []

    def post(self, url, **options):
        self.calls.append((url.rsplit('/', 1)[-1], options))
        method = url.rsplit('/', 1)[-1]
        return Response(True if method == 'deleteMessage' else {'message_id': len(self.calls)})


class Request:
    def __init__(self, body):
        self.body = body

    async def json(self):
        return self.body


class TelegramWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings('', 1, 42, Path(self.tmp.name), transport='telegram')
        self.store = Store(self.settings.database)
        self.session = Session()
        self.client = TelegramWorkflowMessages(TelegramSettings('secret', '691ae7a7-6786-4d3d-9a35-7a3689263984'), self.session)
        self.bot = TelegramBot(self.settings, self.store, self.client)
        self.number = 0

    async def asyncTearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def update(self, text, actor=42, chat_type='private'):
        self.number += 1
        return {'update_id': self.number, 'message': {'message_id': self.number,
                'from': {'id': actor}, 'chat': {'id': actor, 'type': chat_type}, 'text': text}}

    async def test_projects_generation_callbacks_and_duplicate_updates(self):
        await self.bot.handle_update(self.update('/new Бренд'))
        event = self.update('/post Новый продукт')
        await self.bot.handle_update(event)
        project = self.store.project(42)
        job = self.store.db.execute('SELECT * FROM jobs').fetchone()
        self.assertEqual(job['project_id'], project['id'])
        self.assertEqual(job['status'], 'review')
        count = len(self.session.calls)
        await self.bot.handle_update(event)
        self.assertEqual(len(self.session.calls), count)
        markup = next(options['json']['reply_markup'] for method, options in reversed(self.session.calls) if method == 'sendMessage')
        token = markup['inline_keyboard'][0][0]['callback_data']
        self.assertLessEqual(len(token.encode()), 64)
        self.assertEqual(self.client.callback_payload(99, token), {})
        self.assertTrue(self.client.callback_payload(42, token))
        callback = {'update_id': 100, 'callback_query': {'id': 'callback-1', 'from': {'id': 42},
                    'message': {'message_id': 1, 'chat': {'id': 42, 'type': 'private'}}, 'data': token}}
        await self.bot.handle_update(callback)
        self.assertEqual(self.store.dialog(42)['kind'], 'revise')
        previous = len(self.session.calls)
        await self.bot.handle_update(callback)
        self.assertEqual(len(self.session.calls), previous + 1)  # acknowledge only
        self.assertEqual(self.session.calls[-1][0], 'answerCallbackQuery')

    async def test_private_only_and_access_control(self):
        await self.bot.handle_update(self.update('/new Secret', chat_type='group'))
        await self.bot.handle_update(self.update('/new Secret', actor=99))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM projects').fetchone()[0], 0)
        await self.bot.handle_update(self.update('/id', actor=99))
        self.assertIn('Telegram: 99', self.session.calls[-1][1]['json']['text'])

    async def test_photo_caption_uses_workflow_photo_input(self):
        await self.bot.handle_update(self.update('/new Бренд'))
        async def download(file_id, target):
            target.write_bytes(b'photo')
        self.client.download_photo = AsyncMock(side_effect=download)
        event = self.update('/post Фото товара')
        event['message']['caption'] = event['message'].pop('text')
        event['message']['photo'] = [{'file_id': 'small', 'width': 10, 'height': 10},
                                     {'file_id': 'large', 'width': 100, 'height': 100}]
        await self.bot.handle_update(event)
        self.assertEqual(self.client.download_photo.call_args.args[0], 'large')
        job = self.store.db.execute('SELECT * FROM jobs').fetchone()
        self.assertTrue(Path(job['reference_path']).is_file())

    async def test_durable_inbox_deduplicates_and_resumes_pending_only(self):
        bot = AsyncMock()
        inbox = TelegramInbox(self.store, bot)
        event = self.update('/menu')
        for _ in range(2):
            response = await inbox.receive(Request(event))
            self.assertEqual(response.status, 200)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM telegram_updates').fetchone()[0], 1)
        task = asyncio.create_task(inbox.worker())
        try:
            for _ in range(30):
                await asyncio.sleep(0)
                if bot.handle_update.await_count:
                    break
            bot.handle_update.assert_awaited_once_with(event)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        row = self.store.db.execute('SELECT * FROM telegram_updates').fetchone()
        self.assertEqual((row['status'], row['body']), ('done', ''))
        self.store.db.execute("INSERT INTO telegram_updates VALUES (999,'{}','processing')")
        TelegramInbox(self.store, bot)
        self.assertEqual(self.store.db.execute('SELECT status FROM telegram_updates WHERE update_id=999').fetchone()[0], 'failed')

    async def test_vk_identity_is_separate_but_permissions_still_required(self):
        provider = TelegramVkOAuthProvider(self.settings, None)
        provider.settings = Settings('', 1, 42, Path(self.tmp.name), oauth_provider='vk_api', transport='telegram')
        provider.api = AsyncMock(side_effect=[{'id': 999}, {'id': 999}, 8196])
        await provider.validate_identity('token', 42)
        provider.api = AsyncMock(side_effect=[{'id': 999}, {'id': 999}, 0])
        with self.assertRaises(ProviderError):
            await provider.validate_identity('token', 42)

    async def test_standard_relative_file_path_never_calls_telegram_directly(self):
        self.client._call = AsyncMock(return_value={'file_path': 'photos/file_1.jpg'})
        with self.assertRaises(ProviderError):
            await self.client.download_photo('photo', Path(self.tmp.name) / 'photo.jpg')
        self.assertEqual(self.session.calls, [])

    def test_telegram_settings_do_not_use_vk_owner_or_project_credentials(self):
        with patch.dict(os.environ, {'BOT_TRANSPORT': 'telegram', 'OWNER_TELEGRAM_ID': '99',
                                    'OWNER_VK_ID': '42', 'VK_ACCESS_TOKEN_PROJECT_1': 'vk-only'}, clear=True), patch('publikot.settings.load_dotenv'):
            settings = Settings.load()
            self.assertEqual(settings.owner_id, 99)
            self.assertEqual(settings.database.name, 'publikot-telegram.sqlite3')
            self.assertEqual(settings.vk_token_for(99, 1), '')

    async def test_webhook_secret_validation_and_http_delivery(self):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        from publikot.telegram_webhook import add_webhook
        app = web.Application(client_max_size=1024*1024)
        inbox = TelegramInbox(self.store, self.bot)
        add_webhook(app, inbox, 'a' * 43)
        async with TestClient(TestServer(app)) as client:
            event = self.update('/new HTTP')
            response = await client.post('/telegram/webhook/wrong', json=event)
            self.assertEqual(response.status, 404)
            self.assertEqual(self.store.db.execute('SELECT count(*) FROM telegram_updates').fetchone()[0], 0)
            response = await client.post('/telegram/webhook/' + 'a'*43, json=event)
            self.assertEqual(response.status, 200)
            response = await client.post('/telegram/webhook/' + 'a'*43, json=event)
            self.assertEqual(response.status, 200)
            response = await client.post('/telegram/webhook/' + 'a'*43, json={'update_id': True})
            self.assertEqual(response.status, 400)
            for _ in range(100):
                await asyncio.sleep(0)
                if self.store.db.execute('SELECT count(*) FROM projects').fetchone()[0]:
                    break
            self.assertEqual(self.store.db.execute('SELECT count(*) FROM projects').fetchone()[0], 1)

    async def test_publication_requires_confirmation_and_callback_replay_does_not_publish_twice(self):
        from dataclasses import replace
        settings = replace(self.settings, demo=False, vk_token='publication-token')
        publisher = AsyncMock()
        publisher.publish.return_value = 'https://vk.com/wall-123_1'
        factory = lambda token: publisher
        text_ai = AsyncMock()
        text_ai.generate.return_value = 'Готовый пост'
        self.bot = TelegramBot(settings, self.store, self.client, text_ai=text_ai, publisher_factory=factory)
        for text in ('/new Бренд', '/vk 123', '/post Тема'):
            await self.bot.handle_update(self.update(text))
        publisher.publish.assert_not_awaited()
        rows = next(options['json']['reply_markup'] for method, options in reversed(self.session.calls) if method == 'sendMessage')['inline_keyboard']
        token = next(item['callback_data'] for row in rows for item in row if item['text'] == 'Опубликовать')
        callback = {'update_id': 100, 'callback_query': {'id': 'publish-1', 'from': {'id': 42},
                    'message': {'message_id': 1, 'chat': {'id': 42, 'type': 'private'}}, 'data': token}}
        await self.bot.handle_update(callback)
        await self.bot.handle_update(callback)
        publisher.publish.assert_awaited_once()
        self.assertTrue(publisher.publish.call_args.args[-1].startswith('telegram-'))
        self.assertEqual(self.store.db.execute('SELECT status FROM jobs').fetchone()[0], 'published')

    async def test_commands_addressed_to_current_bot(self):
        await self.bot.handle_update(self.update('/new@publikot_bot Старый'))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM projects').fetchone()[0], 0)
        await self.bot.handle_update(self.update('/new@PubliKotBot Новый'))
        self.assertEqual(self.store.project(42)['name'], 'Новый')

    async def test_button_replaces_previous_screen_and_keeps_new_messages(self):
        await self.bot.handle_update(self.update('/menu'))
        previous = self.client.tracked_messages(42)
        await self.client.send(99, 'Другой чат')
        markup = next(options['json']['reply_markup'] for method, options in reversed(self.session.calls)
                      if method == 'sendMessage' and 'reply_markup' in options['json'])
        token = markup['inline_keyboard'][0][0]['callback_data']
        callback = {'update_id': 200, 'callback_query': {'id': 'cleanup', 'from': {'id': 42},
                    'message': {'message_id': previous[-1], 'chat': {'id': 42, 'type': 'private'}}, 'data': token}}
        start = len(self.session.calls)
        await self.bot.handle_update(callback)
        calls = self.session.calls[start:]
        self.assertEqual([method for method, _ in calls], ['answerCallbackQuery', 'sendMessage', 'deleteMessage'])
        self.assertEqual(calls[-1][1]['json'], {'chat_id': 42, 'message_id': previous[0]})
        self.assertTrue(self.client.tracked_messages(42))
        self.assertTrue(set(previous).isdisjoint(self.client.tracked_messages(42)))
        self.assertTrue(self.client.tracked_messages(99))
        await self.bot.handle_update(callback)
        self.assertEqual(sum(method == 'deleteMessage' for method, _ in self.session.calls), 1)

    async def test_failed_deletion_keeps_new_screen_and_does_not_repeat_action(self):
        await self.bot.handle_update(self.update('/menu'))
        token = self.session.calls[-1][1]['json']['reply_markup']['inline_keyboard'][0][0]['callback_data']
        original = self.client._call
        async def call(method, payload, request_id=None):
            if method == 'deleteMessage':
                raise ProviderError('Сервис Telegram: deleteMessage вернул HTTP 403.')
            return await original(method, payload, request_id)
        self.client._call = AsyncMock(side_effect=call)
        callback = {'update_id': 200, 'callback_query': {'id': 'cleanup-failed', 'from': {'id': 42},
                    'message': {'message_id': 1, 'chat': {'id': 42, 'type': 'private'}}, 'data': token}}
        with patch('builtins.print') as log:
            await self.bot.handle_update(callback)
            await self.bot.handle_update(callback)
        self.assertEqual(sum(method == 'sendMessage' for method, _ in self.session.calls), 2)
        self.assertEqual(sum(c.args[0] == 'deleteMessage' for c in self.client._call.call_args_list), 1)
        self.assertIn('не удалены', log.call_args.args[0])

    async def test_tracking_survives_client_restart_and_records_chunks_and_photos(self):
        await self.client.send(42, 'x' * 4000)
        photo = Path(self.tmp.name) / 'photo.jpg'
        photo.write_bytes(b'photo')
        photo_id = await self.client.send_photo(42, photo)
        ids = self.client.tracked_messages(42)
        self.assertEqual(len(ids), 3)
        self.assertIn(photo_id, ids)
        other = TelegramWorkflowMessages(self.client.settings, self.session)
        other.bind_store(self.store)
        self.assertEqual(other.tracked_messages(42), ids)
        await other.delete_messages(99, ids)
        self.assertFalse(any(method == 'deleteMessage' for method, _ in self.session.calls))
        await other.delete_messages(42, ids + ids + [9999])
        self.assertEqual(other.tracked_messages(42), [])
        self.assertEqual(sum(method == 'deleteMessage' for method, _ in self.session.calls), 3)

    async def test_successful_deletion_is_forgotten_even_when_next_deletion_fails(self):
        self.client.remember_message(42, 10)
        self.client.remember_message(42, 11)
        self.client._call = AsyncMock(side_effect=[True, ProviderError('HTTP 403')])
        with self.assertRaises(ProviderError):
            await self.client.delete_messages(42, [10, 11])
        self.assertEqual(self.client.tracked_messages(42), [10])

    async def test_existing_clicked_bot_menu_is_tracked_for_cleanup(self):
        token = self.client.markup(42, {'buttons': [[{'action': {'type': 'callback', 'label': 'Главная',
            'payload': json.dumps({'action': 'menu', 'screen': 'home'})}}]]})['inline_keyboard'][0][0]['callback_data']
        callback = {'update_id': 200, 'callback_query': {'id': 'old-menu', 'from': {'id': 42},
                    'message': {'message_id': 9001, 'from': {'is_bot': True, 'username': 'PubliKotBot'},
                                'chat': {'id': 42, 'type': 'private'}}, 'data': token}}
        await self.bot.handle_update(callback)
        self.assertEqual(self.session.calls[-1][0], 'deleteMessage')
        self.assertEqual(self.session.calls[-1][1]['json']['message_id'], 9001)

    async def test_one_permanently_undeletable_message_does_not_block_other_messages(self):
        for ident in (10, 11, 12):
            self.client.remember_message(42, ident)
        self.client._call = AsyncMock(side_effect=[True,
            TelegramApiError('deleteMessage', 400, {'description': "message can't be deleted"}), True])
        with patch('builtins.print'):
            await self.client.delete_messages(42, [10, 11, 12])
        self.assertEqual([c.args[1]['message_id'] for c in self.client._call.call_args_list], [12, 11, 10])
        self.assertEqual(self.client.tracked_messages(42), [])

    async def test_unknown_400_is_retained_but_other_screens_are_still_deleted(self):
        for ident in (10, 11, 12):
            self.client.remember_message(42, ident)
        self.client._call = AsyncMock(side_effect=[TelegramApiError('deleteMessage', 400, {}), True, True])
        with self.assertRaises(TelegramApiError):
            await self.client.delete_messages(42, [10, 11, 12])
        self.assertEqual(self.client.tracked_messages(42), [12])
        self.assertEqual(self.client._call.await_count, 3)

    async def test_403_does_not_discard_messages_or_attempt_every_record(self):
        for ident in (10, 11):
            self.client.remember_message(42, ident)
        self.client._call = AsyncMock(side_effect=TelegramApiError('deleteMessage', 403))
        with self.assertRaises(TelegramApiError):
            await self.client.delete_messages(42, [10, 11])
        self.assertEqual(self.client.tracked_messages(42), [10, 11])
        self.assertEqual(self.client._call.await_count, 1)
