import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

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
