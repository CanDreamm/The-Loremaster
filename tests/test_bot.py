import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from publikot.providers import AmbiguousPublish
from publikot.oauth import OAuthManager
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

    async def click(self, label, user=42):
        for message in reversed(self.client.sent):
            if message['peer'] != user:
                continue
            for row in (message.get('keyboard') or {}).get('buttons', []):
                for item in row:
                    if item['action'].get('label') == label:
                        return await self.send(user=user, payload=json.loads(item['action']['payload']))
        self.fail(f'Button not found: {label}')

    async def test_menu_add_select_compose_and_revise_without_commands(self):
        await self.send('/start')
        await self.click('Добавить сообщество')
        await self.send('Моя кофейня')
        await self.send('https://vk.ru/club123')
        project = self.store.project(42)
        self.assertEqual(project['vk_group'], 123)
        self.assertIn('500', project['text_prompt'])
        self.assertEqual(len(self.store.projects(42)), 1)
        self.app.text_ai.generate = AsyncMock(return_value='Короткий текст о кофе')
        await self.click('Написать текст')
        await self.send('Расскажи о новом сорте кофе')
        job = self.store.job(42)
        self.assertEqual(job['status'], 'review')
        self.app.text_ai.generate.assert_awaited_once()
        self.assertIn('500', self.app.text_ai.generate.call_args.args[0])
        await self.click('Изменить текст')
        self.app.text_ai.generate.return_value = 'Ещё короче'
        await self.send('Сократи до одного предложения')
        self.assertEqual(self.store.job(42)['revision'], 2)
        self.assertEqual(self.store.revision(42, job['id'])['body'], 'Ещё короче')
        await self.click('Назад')
        await self.click('Другие сообщества')
        await self.click(f"Моя кофейня · №{project['id']}")
        self.assertEqual(self.store.project(42)['id'], project['id'])
        self.publish.assert_not_awaited()

    async def test_add_wizard_invalid_group_can_be_retried_without_empty_projects(self):
        await self.send('/start')
        await self.click('Добавить сообщество')
        await self.send('Кофейня')
        await self.send('https://other.example.com/club123')
        self.assertEqual(self.store.projects(42), [])
        self.assertEqual(self.store.dialog(42)['kind'], 'add_group')
        await self.send('123')
        self.assertEqual(self.store.project(42)['vk_group'], 123)

    async def test_add_wizard_resolves_short_name_and_rejects_personal_profile(self):
        self.client.api = AsyncMock(return_value={'type': 'user', 'object_id': 99})
        await self.send('/start')
        await self.click('Добавить сообщество')
        await self.send('Кофейня')
        await self.send('https://vk.com/mycoffee')
        self.assertEqual(self.store.projects(42), [])
        self.client.api.return_value = {'type': 'group', 'object_id': 123}
        await self.send('https://vk.com/mycoffee')
        self.client.api.assert_awaited_with('utils.resolveScreenName', screen_name='mycoffee')
        self.assertEqual(self.store.project(42)['vk_group'], 123)

    async def test_home_cancels_prompt_and_plain_text_does_not_start_generation(self):
        project = await self.setup_project()
        await self.send(payload={'action': 'menu', 'screen': 'compose', 'project': project})
        await self.send('/menu')
        await self.send('Это не задание')
        self.assertEqual(self.store.jobs(42), [])
        self.assertIsNone(self.store.dialog(42))

    async def test_prompt_survives_store_and_bot_restart(self):
        project = await self.setup_project()
        await self.send(payload={'action': 'menu', 'screen': 'compose', 'project': project})
        self.store.db.close()
        self.store = Store(self.settings.database)
        self.app = VkBot(self.settings, self.store, self.client)
        await self.send('Напиши про кофе')
        self.assertEqual(self.store.job(42)['brief'], 'Напиши про кофе')
        self.assertIsNone(self.store.dialog(42))

    async def test_revoked_project_access_blocks_pending_prompt(self):
        project = await self.setup_project()
        await self.send('/allow 99')
        await self.send(f'/assign {project} 99')
        await self.send(user=99, payload={'action': 'menu', 'screen': 'compose', 'project': project})
        await self.send(f'/unassign {project} 99')
        await self.send('Чужое задание', user=99)
        self.assertEqual(self.store.jobs(42), [])

    async def test_stale_revision_prompt_cannot_revise_new_version(self):
        await self.setup_project()
        await self.send('/post Кофе')
        job = self.store.job(42)
        await self.click('Изменить текст')
        self.store.revise(42)
        self.store.save_version(42, job['id'], 2, 'Другая версия', True)
        await self.send('Замечание к старой версии')
        self.assertEqual(self.store.job(42)['revision'], 2)
        self.assertEqual(self.store.revision(42, job['id'])['body'], 'Другая версия')

    async def test_old_edit_button_keeps_job_and_project_bound(self):
        first = await self.setup_project()
        await self.send('/post Первый текст')
        job = self.store.job(42)
        await self.send('/new Второй')
        await self.send('/vk 456')
        await self.send(payload={'action': 'edittext', 'job': job['id'], 'revision': 1})
        await self.send('Короче')
        self.assertEqual(self.store.project(42)['id'], first)
        self.assertEqual(self.store.job(42)['id'], job['id'])
        self.assertEqual(self.store.job(42)['revision'], 2)

    async def test_menu_photo_replacement_preserves_text(self):
        await self.setup_project()
        await self.send('/post Кофе')
        job = self.store.job(42)
        previous = self.store.revision(42, job['id'])['body']
        await self.click('Добавить фото')
        await self.send('Без фото')
        self.assertEqual(self.store.job(42)['revision'], 1)
        await self.send(photo=True)
        self.assertEqual(self.store.job(42)['revision'], 2)
        self.assertEqual(self.store.revision(42, job['id'])['body'], previous)
        self.assertTrue(self.store.revision(42, job['id'])['image_path'])

    async def test_community_pages_fit_vk_inline_limits(self):
        for index in range(10):
            self.store.create_project(42, f'Сообщество {index}')
        await self.send('/projects')
        first_page = self.client.sent[-1]['keyboard']
        await self.click('Далее')
        second_page = self.client.sent[-1]['keyboard']
        for keys in (first_page, second_page):
            self.assertLessEqual(len(keys['buttons']), 6)
            self.assertLessEqual(sum(map(len, keys['buttons'])), 10)
        self.assertNotEqual(first_page['buttons'][0], second_page['buttons'][0])

    async def test_member_cannot_forge_admin_or_add_menus(self):
        await self.send('/allow 99')
        for screen in ('admin', 'admin_allow', 'add'):
            await self.send(user=99, payload={'action': 'menu', 'screen': screen})
        self.assertEqual(self.store.projects(99), [])
        self.assertIsNone(self.store.dialog(99))
        self.assertFalse(self.store.is_allowed(88, 42))

    async def test_admin_access_menu_prompts_for_id(self):
        await self.send('/start')
        await self.click('Управление доступом')
        await self.click('Разрешить доступ')
        await self.send('99')
        self.assertTrue(self.store.is_allowed(99, 42))

    async def test_text_settings_menu_saves_plain_input(self):
        project = await self.setup_project()
        await self.send(f'/select {project}')
        await self.click('Настройки текста')
        await self.click('Тон общения')
        await self.send('Дружелюбный')
        self.assertEqual(self.store.project(42)['tone'], 'Дружелюбный')
        self.assertIsNone(self.store.dialog(42))

    async def test_back_from_revision_restores_preview_without_changing_text(self):
        await self.setup_project()
        await self.send('/post Кофе')
        job = self.store.job(42)
        await self.click('Изменить текст')
        await self.click('Назад к тексту')
        self.assertIsNone(self.store.dialog(42))
        self.assertEqual(self.store.job(42)['revision'], 1)
        self.assertEqual(self.store.job(42)['status'], 'review')

    async def test_forged_menu_cannot_select_unassigned_project(self):
        project = await self.setup_project()
        await self.send('/allow 99')
        await self.send(user=99, payload={'action': 'menu', 'screen': 'workspace', 'project': project})
        await self.send(user=99, payload={'action': 'menu', 'screen': 'compose', 'project': project})
        self.assertIsNone(self.store.dialog(99))
        self.assertEqual(self.store.projects(99), [])

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

    async def test_connect_command_returns_official_link_not_token(self):
        await self.setup_project()
        settings = replace(self.settings, oauth_client_id='12345', oauth_public_url='https://bot.example.com')
        self.app.oauth = OAuthManager(settings, self.store, None)
        await self.send('/connect')
        response = self.client.sent[-1]
        link = response['keyboard']['buttons'][0][0]['action']['link']
        self.assertTrue(link.startswith('https://id.vk.com/authorize?'))
        self.assertNotIn('access_token', link)
        self.assertIn('10 минут', response['text'])

    async def test_publisher_uses_encrypted_connection_without_env_key(self):
        project = await self.setup_project()
        settings = replace(self.settings, oauth_client_id='12345', oauth_public_url='https://bot.example.com')
        provider = type('Provider', (), {})()
        provider.exchange = AsyncMock(return_value={'access_token': 'oauth-private-test-token'})
        provider.validate = AsyncMock()
        manager = OAuthManager(settings, self.store, provider)
        from urllib.parse import urlsplit, parse_qs
        state = parse_qs(urlsplit(manager.begin(42, project)).query)['state'][0]
        await manager.complete(state, 'one-use-code', 'device')
        self.app.oauth = manager
        keys = []
        def publisher(token):
            keys.append(token)
            return type('Publisher', (), {'publish': self.publish})()
        self.app.publisher_factory = publisher
        ident, _ = self.store.create_job(42, 'Тема')
        self.store.save_version(42, ident, 1, 'Текст', False)
        with patch.dict('os.environ', {}, clear=True):
            await self.send(payload={'action': 'publish', 'job': ident, 'revision': 1})
        self.assertEqual(keys, ['oauth-private-test-token'])
        self.assertNotIn('oauth-private-test-token', str(self.client.sent))
        self.assertEqual(self.store.job(42, ident)['status'], 'published')
