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
from publikot.telegram_client import TelegramSettings, TelegramWorkflowMessages
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
        return Response({'message_id': len(self.calls)})


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
        markup = self.session.calls[-1][1]['json']['reply_markup']
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
        rows = self.session.calls[-1][1]['json']['reply_markup']['inline_keyboard']
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
