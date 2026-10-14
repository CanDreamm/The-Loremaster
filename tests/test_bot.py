import tempfile
import unittest
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery
from aiogram.types import Chat, Message, Update

from publikot.__main__ import build_router
from publikot.settings import Settings
from publikot.store import Store


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.sent.append(method)
        if isinstance(method, AnswerCallbackQuery):
            return True
        return Message(message_id=len(self.sent), date=datetime.now(timezone.utc),
                       chat=Chat(id=int(method.chat_id), type='private'), text=getattr(method, 'text', None))

    async def stream_content(self, *args, **kwargs):
        yield b''


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        directory = Path(self.tmp.name)
        self.store = Store(directory / 'test.db')
        self.settings = Settings('123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi', 42, directory, None, True, '', '', '')
        self.session = FakeSession()
        self.bot = Bot(self.settings.token, session=self.session)
        self.dp = Dispatcher()
        self.dp.include_router(build_router(self.settings, self.store))
        self.update = 0

    async def asyncTearDown(self):
        await self.bot.session.close()
        self.store.db.close()
        self.tmp.cleanup()

    async def send(self, text, owner=42, chat_type='private'):
        self.update += 1
        update = Update.model_validate({'update_id': self.update, 'message': {
            'message_id': self.update, 'date': 1700000000, 'text': text,
            'from': {'id': owner, 'is_bot': False, 'first_name': 'Test'},
            'chat': {'id': owner, 'type': chat_type}}}, context={'bot': self.bot})
        await self.dp.feed_update(self.bot, update)

    async def test_owner_can_create_review_and_revise(self):
        await self.send('/new Бренд')
        await self.send('/concept Кофейня, спокойный тон')
        await self.send('/vk 123')
        await self.send('/post Новое сезонное меню')
        job = self.store.job(42)
        self.assertEqual(job['status'], 'review')
        self.assertEqual(job['target_group'], 123)
        self.assertTrue(any(getattr(item, 'reply_markup', None) for item in self.session.sent))
        await self.send('/revise Сделай короче')
        self.assertEqual(self.store.job(42)['revision'], 2)
        self.assertIn('Сделай короче', self.store.revision(42, job['id'])['body'])

    async def test_unauthorized_and_group_messages_do_nothing(self):
        await self.send('/new Чужой', owner=99)
        await self.send('/new Группа', chat_type='group')
        self.assertEqual(self.store.projects(99), [])
        self.assertEqual(self.store.projects(42), [])
        self.assertEqual(self.session.sent, [])

    async def test_admin_grants_bot_and_project_access_separately(self):
        await self.send('/new Общий бренд')
        project_id = self.store.project(42)['id']
        await self.send('/allow 99')
        await self.send('/projects', owner=99)
        self.assertEqual(self.store.projects(99), [])
        await self.send(f'/assign {project_id} 99')
        await self.send(f'/select {project_id}', owner=99)
        await self.send('/concept Общая концепция', owner=99)
        await self.send('/textprompt Пиши коротко', owner=99)
        await self.send('/post Тема', owner=99)
        job = self.store.job(99)
        self.assertEqual(job['status'], 'review')
        await self.send(f"/job {job['id']}", owner=42)
        await self.send('/revise Добавь деталей', owner=42)
        self.assertEqual(self.store.job(99)['revision'], 2)
        self.assertEqual(self.store.project(42)['concept'], 'Общая концепция')

    async def test_members_cannot_manage_access_or_create_projects(self):
        await self.send('/allow 99')
        await self.send('/allow 88', owner=99)
        await self.send('/new Чужой', owner=99)
        self.assertFalse(self.store.is_allowed(88, 42))
        self.assertEqual(self.store.projects(99), [])

    async def test_revocation_blocks_messages_and_callbacks(self):
        await self.send('/new Общий')
        project_id = self.store.project(42)['id']
        await self.send('/allow 99')
        await self.send(f'/assign {project_id} 99')
        await self.send(f'/select {project_id}', owner=99)
        await self.send('/post Черновик', owner=99)
        job = self.store.job(99)
        await self.send('/deny 99')
        await self.send('/concept Нельзя', owner=99)
        self.assertNotEqual(self.store.project(42)['concept'], 'Нельзя')
        update = Update.model_validate({'update_id': 200, 'callback_query': {
            'id': 'click', 'from': {'id': 99, 'is_bot': False, 'first_name': 'Test'},
            'chat_instance': 'private', 'data': f"cancel:{job['id']}:1",
            'message': {'message_id': 100, 'date': 1700000000,
                        'chat': {'id': 99, 'type': 'private'}}}}, context={'bot': self.bot})
        await self.dp.feed_update(self.bot, update)
        self.assertEqual(self.store.job(42, job['id'])['status'], 'review')

    async def test_id_is_available_without_project_or_bot_access(self):
        await self.send('/id', owner=99)
        self.assertIn('99', self.session.sent[-1].text)

    async def test_admin_key_is_not_shared_with_members(self):
        settings = replace(self.settings, vk_token='admin-only-test-key')
        with patch.dict('os.environ', {}, clear=True):
            self.assertEqual(settings.vk_token_for(42, 1), 'admin-only-test-key')
            self.assertEqual(settings.vk_token_for(99, 1), '')
        with patch.dict('os.environ', {'VK_ACCESS_TOKEN_PROJECT_1': 'project-key'}, clear=True):
            self.assertEqual(self.settings.vk_token_for(99, 1), 'project-key')
            self.assertEqual(self.settings.vk_token_for(99, 2), '')

    async def test_member_can_confirm_shared_project_post_once(self):
        await self.send('/new Общий')
        project_id = self.store.project(42)['id']
        await self.send('/vk 123')
        await self.send('/allow 99')
        await self.send(f'/assign {project_id} 99')
        ident, _ = self.store.create_job(42, 'Тема')
        self.store.save_version(42, ident, 1, 'Согласованный текст', False)
        update = Update.model_validate({'update_id': 300, 'callback_query': {
            'id': 'approve', 'from': {'id': 99, 'is_bot': False, 'first_name': 'Test'},
            'chat_instance': 'private', 'data': f'publish:{ident}:1',
            'message': {'message_id': 100, 'date': 1700000000,
                        'chat': {'id': 99, 'type': 'private'}}}}, context={'bot': self.bot})
        with patch.dict('os.environ', {f'VK_ACCESS_TOKEN_PROJECT_{project_id}': 'test-project-key'}):
            with patch('publikot.__main__.VkPublisher.publish', new_callable=AsyncMock, return_value='https://vk.com/wall-123_1') as publish:
                await self.dp.feed_update(self.bot, update)
                await self.dp.feed_update(self.bot, update)
                publish.assert_awaited_once()
        self.assertEqual(self.store.job(99, ident)['status'], 'published')
        approval = self.store.db.execute('SELECT owner FROM approvals WHERE job_id=?', (ident,)).fetchone()
        self.assertEqual(approval['owner'], 99)
