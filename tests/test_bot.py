import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from publikot.providers import AmbiguousPublish
from publikot.settings import Settings
from publikot.store import Store
from publikot.vk_bot import VkBot


class FakeMessages:
    def __init__(self):
        self.sent = []
        self.photos = []

    async def send(self, peer, text, keyboard=None, attachment=None):
        self.sent.append({'peer': peer, 'text': text, 'keyboard': keyboard})

    async def send_photo(self, peer, path, caption=''):
        self.photos.append((peer, path))

    async def download(self, url, path):
        path.write_bytes(b'test-photo')


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        directory = Path(self.tmp.name)
        self.settings = Settings('test-bot-key', 242305804, 42, directory)
        self.store = Store(self.settings.database)
        self.client = FakeMessages()
        self.publish = AsyncMock(return_value='https://vk.com/wall-123_1')
        class Publisher:
            publish = self.publish
        self.app = VkBot(self.settings, self.store, self.client, publisher_factory=lambda token: Publisher())
        self.number = 0

    async def asyncTearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def event(self, text='', user=42, payload=None, photo=False):
        self.number += 1
        message = {'id': self.number, 'conversation_message_id': self.number, 'from_id': user, 'peer_id': user, 'text': text}
        if payload is not None:
            message['payload'] = json.dumps(payload)
        if photo:
            message['attachments'] = [{'type': 'photo', 'photo': {'sizes': [{'width': 100, 'height': 100, 'url': 'https://sun.userapi.com/test.jpg'}]}}]
        return {'type': 'message_new', 'group_id': self.settings.group_id, 'event_id': str(self.number), 'object': {'message': message}}

    async def send(self, text='', user=42, payload=None, photo=False):
        await self.app.handle(self.event(text, user, payload, photo))

    async def setup_project(self):
        await self.send('/new Бренд')
        await self.send('/vk 123')
        return self.store.project(42)['id']

    async def test_create_review_and_revise(self):
        await self.setup_project()
        await self.send('/concept Кофейня')
        await self.send('/post Новое меню')
        job = self.store.job(42)
        self.assertEqual(job['status'], 'review')
        self.assertTrue(any(item['keyboard'] for item in self.client.sent))
        await self.send('/revise Короче')
        self.assertEqual(self.store.job(42)['revision'], 2)
        self.assertIn('Короче', self.store.revision(42, job['id'])['body'])

    async def test_admin_assigns_projects_separately_from_bot_access(self):
        project = await self.setup_project()
        await self.send('/allow 99')
        self.assertEqual(self.store.projects(99), [])
        await self.send(f'/assign {project} 99')
        await self.send(f'/select {project}', user=99)
        await self.send('/tone Дружелюбный', user=99)
        await self.send('/post Тема', user=99)
        self.assertEqual(self.store.project(42)['tone'], 'Дружелюбный')
        self.assertEqual(self.store.job(99)['status'], 'review')

    async def test_members_cannot_manage_access(self):
        await self.send('/allow 99')
        await self.send('/allow 88', user=99)
        await self.send('/new Чужой', user=99)
        self.assertFalse(self.store.is_allowed(88, 42))
        self.assertEqual(self.store.projects(99), [])

    async def test_id_is_public_but_project_commands_are_not(self):
        await self.send('/new Закрытый', user=99)
        self.assertEqual(self.client.sent, [])
        await self.send('/id', user=99)
        self.assertIn('99', self.client.sent[-1]['text'])

    async def test_wrong_community_and_group_chats_are_ignored(self):
        event = self.event('/new Чужой')
        event['group_id'] = 1
        await self.app.handle(event)
        event = self.event('/new Группа')
        event['object']['message']['peer_id'] = 2000000001
        await self.app.handle(event)
        self.assertEqual(self.store.projects(42), [])
        self.assertEqual(self.client.sent, [])

    async def test_duplicate_long_poll_event_does_not_repeat_generation(self):
        await self.setup_project()
        self.app.text_ai.generate = AsyncMock(return_value='Текст')
        event = self.event('/post Тема')
        await self.app.handle(event)
        await self.app.handle(event)
        self.app.text_ai.generate.assert_awaited_once()
        self.assertEqual(len(self.store.jobs(42)), 1)

    async def test_revoked_bot_access_blocks_buttons(self):
        project = await self.setup_project()
        await self.send('/allow 99')
        await self.send(f'/assign {project} 99')
        await self.send('/post Тема')
        job = self.store.job(42)
        await self.send('/deny 99')
        await self.send(user=99, payload={'action': 'cancel', 'job': job['id'], 'revision': 1})
        self.assertEqual(self.store.job(42)['status'], 'review')

    async def test_member_can_publish_shared_project_only_once(self):
        project = await self.setup_project()
        await self.send('/allow 99')
        await self.send(f'/assign {project} 99')
        ident, _ = self.store.create_job(42, 'Тема')
        self.store.save_version(42, ident, 1, 'Текст', False)
        with patch.dict('os.environ', {f'VK_ACCESS_TOKEN_PROJECT_{project}': 'project-key'}):
            payload = {'action': 'publish', 'job': ident, 'revision': 1}
            await self.send(user=99, payload=payload)
            await self.send(user=99, payload=payload)
        self.publish.assert_awaited_once()
        self.assertEqual(self.store.job(99, ident)['status'], 'published')

    async def test_bot_messages_key_is_never_used_to_publish(self):
        await self.setup_project()
        ident, _ = self.store.create_job(42, 'Тема')
        self.store.save_version(42, ident, 1, 'Текст', False)
        with patch.dict('os.environ', {}, clear=True):
            await self.send(payload={'action': 'publish', 'job': ident, 'revision': 1})
        self.publish.assert_not_awaited()
        self.assertEqual(self.store.job(42, ident)['status'], 'review')

    async def test_new_image_preserves_text_and_invalidates_old_buttons(self):
        await self.setup_project()
        await self.send('/post Тема', photo=True)
        job = self.store.job(42)
        previous = dict(self.store.revision(42, job['id']))
        await self.send(f"/image {job['id']}", photo=True)
        current = self.store.revision(42, job['id'])
        self.assertEqual(current['body'], previous['body'])
        self.assertNotEqual(current['image_path'], previous['image_path'])
        old = self.store.db.execute('SELECT image_path FROM versions WHERE job_id=? AND revision=1', (job['id'],)).fetchone()
        self.assertEqual(old['image_path'], previous['image_path'])
        await self.send(payload={'action': 'cancel', 'job': job['id'], 'revision': 1})
        self.assertEqual(self.store.job(42, job['id'])['status'], 'review')

    async def test_ambiguous_publish_is_not_retried(self):
        project = await self.setup_project()
        ident, _ = self.store.create_job(42, 'Тема')
        self.store.save_version(42, ident, 1, 'Текст', False)
        self.publish.side_effect = AmbiguousPublish('Проверьте стену')
        with patch.dict('os.environ', {f'VK_ACCESS_TOKEN_PROJECT_{project}': 'project-key'}):
            payload = {'action': 'publish', 'job': ident, 'revision': 1}
            await self.send(payload=payload)
            await self.send(payload=payload)
        self.publish.assert_awaited_once()
        self.assertEqual(self.store.job(42, ident)['status'], 'unknown')

    async def test_unassigned_user_cannot_forge_project_button(self):
        await self.setup_project()
        await self.send('/post Тема')
        job = self.store.job(42)
        await self.send('/allow 99')
        await self.send(user=99, payload={'action': 'cancel', 'job': job['id'], 'revision': 1})
        self.assertEqual(self.store.job(42, job['id'])['status'], 'review')
