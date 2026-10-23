"""Actual VK message events, photo downloads/uploads, and GenAPI requests over local HTTP."""
import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from publikot.genapi import GenAPI
from publikot.media import version_images
from publikot.settings import Settings
from publikot.store import Store
from publikot.vk_bot import VkBot
from publikot.vk_client import VkMessages

PNG = b'\x89PNG\r\n\x1a\nphoto'


class VkPhotoWorkflow(unittest.IsolatedAsyncioTestCase):
    async def test_vk_uploads_project_references_and_generates_three_images(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = Settings('vk-message-key', 123, 42, Path(temp), demo=False, genapi_key='genapi-private')
            store = Store(settings.database)
            calls, payloads = [], []
            sent = 0

            async def service(request):
                nonlocal sent
                if request.path.startswith('/vk/'):
                    method = request.path.removeprefix('/vk/')
                    fields = await request.post()
                    self.assertEqual(fields['access_token'], 'vk-message-key')
                    calls.append(method)
                    if method == 'messages.send':
                        if 'keyboard' in fields:
                            keys = json.loads(fields['keyboard'])
                            self.assertLessEqual(len(keys['buttons']), 6)
                            self.assertLessEqual(sum(map(len, keys['buttons'])), 10)
                            self.assertTrue(all(1 <= len(row) <= 5 for row in keys['buttons']))
                        sent += 1
                        return web.json_response({'response': sent})
                    if method == 'messages.sendMessageEventAnswer':
                        return web.json_response({'response': 1})
                    if method == 'messages.delete':
                        return web.json_response({'response': {ident: 1 for ident in fields['message_ids'].split(',')}})
                    if method == 'photos.getMessagesUploadServer':
                        return web.json_response({'response': {'upload_url': 'https://sun1.userapi.com/messages-upload'}})
                    if method == 'photos.saveMessagesPhoto':
                        return web.json_response({'response': [{'owner_id': -123, 'id': 100}]})
                    self.fail(method)
                if request.path == '/messages-upload':
                    self.assertNotIn('Authorization', request.headers)
                    fields = await request.post()
                    self.assertEqual(fields['photo'].file.read(), PNG)
                    return web.json_response({'server': 1, 'photo': 'uploaded', 'hash': 'hash'})
                if request.path.startswith('/photo/'):
                    self.assertNotIn('Authorization', request.headers)
                    return web.Response(body=PNG, content_type='image/png')
                if request.path.startswith('/gen/networks/'):
                    self.assertEqual(request.headers['Authorization'], 'Bearer genapi-private')
                    payload = await request.json()
                    payloads.append(payload)
                    if 'messages' in payload:
                        self.assertIn('Добрый персонаж', payload['messages'][0]['content'])
                        images = [p['image_url']['url'] for p in payload['messages'][1]['content'] if p['type'] == 'image_url']
                    else:
                        images = payload['image_urls']
                    self.assertEqual(len(images), 3)
                    for uri in images:
                        self.assertEqual(base64.b64decode(uri.split(',', 1)[1]), PNG)
                    return web.json_response({'request_id': len(payloads)})
                if request.path.startswith('/gen/request/get/'):
                    ident = int(request.path.rsplit('/', 1)[-1])
                    result = ['Готовая история от лица персонажа'] if ident == 1 else [f'https://cdn.example/{ident}.png']
                    return web.json_response({'status': 'success', 'result': result})
                self.fail(request.path)

            app = web.Application()
            app.router.add_route('*', '/{path:.*}', service)
            async with TestServer(app) as server:
                real_session = aiohttp.ClientSession
                class LocalSession:
                    def __init__(self, **kwargs):
                        self.session = real_session(trust_env=False, connector=kwargs.get('connector'))
                    async def __aenter__(self):
                        return self
                    async def __aexit__(self, *args):
                        await self.session.close()
                    def post(self, url, **kwargs):
                        if url.startswith('https://api.vk.com/method/'):
                            url = server.make_url('/vk/' + url.rsplit('/', 1)[-1])
                        elif url == 'https://sun1.userapi.com/messages-upload':
                            url = server.make_url('/messages-upload')
                        else:
                            raise AssertionError(url)
                        return self.session.post(url, **kwargs)
                    def get(self, url, **kwargs):
                        assert url.startswith(('https://sun1.userapi.com/', 'https://cdn.example/'))
                        return self.session.get(server.make_url('/photo/' + url.rsplit('/', 1)[-1]), **kwargs)
                    def request(self, *args, **kwargs):
                        return self.session.request(*args, **kwargs)
                async with LocalSession() as session:
                    client = VkMessages(settings.bot_token, settings.group_id, session)
                    ai = GenAPI(settings, store, session_factory=LocalSession, poll_interval=0)
                    bot = VkBot(settings, store, client, text_ai=ai, image_ai=ai)
                    event_number = 0
                    async def send(text, photo=False):
                        nonlocal event_number
                        event_number += 1
                        message = {'from_id': 42, 'peer_id': 42, 'id': event_number, 'text': text}
                        if photo:
                            message['attachments'] = [{'type': 'photo', 'photo': {'sizes': [
                                {'width': 100, 'height': 100, 'url': 'https://sun1.userapi.com/large.png'},
                                {'width': 10, 'height': 10, 'url': 'https://sun1.userapi.com/small.png'}]}}]
                        await bot.handle({'group_id': 123, 'type': 'message_new', 'event_id': str(event_number),
                                          'object': {'message': message}})
                    with patch('publikot.genapi.BASE', str(server.make_url('/gen'))):
                        await send('/new Бренд')
                        await bot.handle({'group_id': 123, 'type': 'message_event', 'object': {
                            'user_id': 42, 'peer_id': 42, 'event_id': 'settings-callback',
                            'payload': {'action': 'menu', 'screen': 'settings', 'project': store.project(42)['id']}}})
                        await send('/concept Добрый персонаж')
                        await send('/style Мягкий свет', photo=True)
                        await send('/character Узнаваемая внешность', photo=True)
                        project = store.project(42)
                        for kind in ('style', 'character'):
                            material = store.material(42, project['id'], kind)
                            self.assertEqual(Path(material['path']).read_bytes(), PNG)
                        store.configure(42, 'image_count', 3)
                        await send('/post Расскажи историю с этим предметом', photo=True)
                        job = store.job(42)
                        self.assertEqual(store.revision(42, job['id'])['body'], 'Готовая история от лица персонажа')
                        await bot.handle({'group_id': 123, 'type': 'message_event', 'object': {
                            'user_id': 42, 'peer_id': 42, 'event_id': 'images-callback',
                            'payload': {'action': 'generateimages', 'job': job['id'], 'revision': 1}}})
                        self.assertEqual(len(version_images(store.revision(42, job['id']))), 3)
                        self.assertEqual(len(payloads), 4)
                        self.assertIn('messages.sendMessageEventAnswer', calls)
                        self.assertIn('photos.getMessagesUploadServer', calls)
                        self.assertNotIn('wall.post', calls)
            store.db.close()
