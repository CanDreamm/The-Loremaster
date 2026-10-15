"""Exercise bot -> implicit callback -> VK validation -> wall.post over local HTTP."""
import json
import re
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from publikot.oauth import OAuthManager, VkOAuthProvider, create_web_app
from publikot.providers import VkPublisher
from publikot.settings import Settings
from publikot.store import Store
from publikot.vk_bot import VkBot


class Messages:
    def __init__(self):
        self.sent = []

    async def send(self, peer, text, keyboard=None):
        self.sent.append({'text': text, 'keyboard': keyboard})


class LocalVkSession:
    """Redirect only the VK API transport to the test server."""
    def __init__(self, server, **kwargs):
        self.server = server
        self.session = aiohttp.ClientSession(**kwargs)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.session.close()

    def post(self, url, **kwargs):
        assert url.startswith('https://api.vk.com/method/')
        method = url.removeprefix('https://api.vk.com/method/')
        return self.session.post(self.server.make_url('/method/' + method), **kwargs)


class PublishingFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings('messages-test-key', 242305804, 42, Path(self.temp.name), demo=False,
                                 oauth_client_id='54825414', oauth_public_url='https://bot.example.com',
                                 oauth_provider='vk_implicit')
        self.store = Store(self.settings.database)
        self.api_calls = []
        self.reject_wall = False

        async def vk_api(request):
            method = request.match_info['method']
            data = dict(await request.post())
            self.assertEqual(data['access_token'], 'publishing-flow-test-key')
            self.api_calls.append((method, data))
            if method == 'wall.post' and self.reject_wall:
                return web.json_response({'error': {'error_code': 15, 'error_msg': 'publishing-flow-test-key'}})
            results = {'account.getProfileInfo': {'id': 42}, 'account.getAppPermissions': 8196,
                       'groups.get': {'count': 1, 'items': [{'id': 123, 'name': 'Кофейня', 'is_admin': 1, 'admin_level': 3}]},
                       'groups.getById': {'groups': [{'id': 123, 'is_admin': 1, 'admin_level': 3}]},
                       'wall.post': {'post_id': 77}}
            self.assertIn(method, results)
            return web.json_response({'response': results[method]})

        api = web.Application()
        api.router.add_post('/method/{method}', vk_api)
        self.vk_server = TestServer(api)
        await self.vk_server.start_server()
        self.transport = LocalVkSession(self.vk_server)
        self.messages = Messages()
        provider = VkOAuthProvider(self.settings, self.transport)
        self.manager = OAuthManager(self.settings, self.store, provider)
        text_ai = type('TextAI', (), {'generate': AsyncMock(side_effect=['Первый текст.', 'Короткий текст.'])})()
        self.bot = VkBot(self.settings, self.store, self.messages, text_ai=text_ai, oauth=self.manager,
                         publisher_factory=lambda token: VkPublisher(token,
                             session_factory=lambda **kwargs: LocalVkSession(self.vk_server, **kwargs)))
        self.callback = TestClient(TestServer(create_web_app(self.manager, self.messages.send)))
        await self.callback.start_server()
        self.number = 0

    async def asyncTearDown(self):
        await self.callback.close()
        await self.transport.session.close()
        await self.vk_server.close()
        self.store.db.close()
        self.temp.cleanup()

    async def send(self, text='', payload=None):
        self.number += 1
        message = {'id': self.number, 'from_id': 42, 'peer_id': 42, 'text': text}
        if payload is not None:
            message['payload'] = json.dumps(payload)
        await self.bot.handle({'type': 'message_new', 'group_id': self.settings.group_id,
                               'event_id': str(self.number), 'object': {'message': message}})

    async def click(self, label):
        for message in reversed(self.messages.sent):
            for row in (message['keyboard'] or {}).get('buttons', []):
                for item in row:
                    if item['action']['label'] == label:
                        return await self.send(payload=json.loads(item['action']['payload']))
        self.fail('Missing button: ' + label)

    async def connect_and_compose(self):
        await self.send('/start')
        await self.click('Добавить сообщество')
        keys = self.messages.sent[-1]['keyboard']
        url = keys['buttons'][0][0]['action']['link']
        params = parse_qs(urlsplit(url).query)
        self.assertEqual(urlsplit(url).hostname, 'oauth.vk.com')
        self.assertEqual(params['response_type'], ['token'])
        response = await self.callback.post('/oauth/vk/callback', json={
            'state': params['state'][0], 'access_token': 'publishing-flow-test-key', 'expires_in': '3600',
        }, headers={'Origin': self.settings.oauth_public_url})
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(self.store.projects(42), [])
        selection_url = (await response.json())['selection_url']
        cookie = {'Cookie': 'vk_import=' + response.cookies['vk_import'].value}
        picker = await self.callback.get(selection_url, headers=cookie)
        self.assertEqual(picker.status, 200)
        body = await picker.text()
        self.assertIn('Кофейня', body)
        csrf = re.search(r'name="csrf" value="([^"]+)"', body).group(1)
        response = await self.callback.post('/oauth/vk/select', data={
            'state': params['state'][0], 'csrf': csrf, 'group': '123'}, headers=cookie)
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual([method for method, _ in self.api_calls],
                         ['account.getProfileInfo', 'account.getAppPermissions', 'groups.get',
                          'account.getProfileInfo', 'account.getAppPermissions', 'groups.getById'])
        await self.click('Мои сообщества')
        await self.click(f"Кофейня · №{self.store.project(42)['id']}")
        await self.click('Написать текст')
        await self.send('Напиши короткий пост про кофе')
        self.assertFalse(any(method == 'wall.post' for method, _ in self.api_calls))
        return self.store.job(42)

    async def test_connect_revise_confirm_publish_and_reject_duplicate(self):
        job = await self.connect_and_compose()
        await self.click('Изменить текст')
        await self.send('Сократи')
        await self.send(payload={'action': 'publish', 'job': job['id'], 'revision': 1})
        self.assertFalse(any(method == 'wall.post' for method, _ in self.api_calls))
        await self.click('Опубликовать')
        await self.send(payload={'action': 'publish', 'job': job['id'], 'revision': 2})
        posts = [data for method, data in self.api_calls if method == 'wall.post']
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]['owner_id'], '-123')
        self.assertEqual(posts[0]['from_group'], '1')
        self.assertEqual(posts[0]['message'], 'Короткий текст.')
        self.assertEqual(posts[0]['guid'], str(job['id']))
        final = self.store.job(42)
        self.assertEqual(final['status'], 'published')
        self.assertEqual(final['remote_url'], 'https://vk.com/wall-123_77')
        self.assertNotIn('publishing-flow-test-key', json.dumps(self.messages.sent))
        encrypted = self.store.db.execute('SELECT token FROM vk_connections').fetchone()['token']
        self.assertNotIn('publishing-flow-test-key', encrypted)

    async def test_explicit_vk_rejection_is_failed_and_never_reported_as_published(self):
        await self.connect_and_compose()
        self.reject_wall = True
        await self.click('Опубликовать')
        self.assertEqual(self.store.job(42)['status'], 'failed')
        self.assertIsNone(self.store.job(42)['remote_url'])
        self.assertIn('wall.post', self.messages.sent[-1]['text'])
        self.assertIn('код API 15', self.messages.sent[-1]['text'])
        self.assertNotIn('publishing-flow-test-key', json.dumps(self.messages.sent))
