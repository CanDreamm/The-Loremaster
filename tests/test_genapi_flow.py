"""Full bot -> GenAPI queue/media -> approval -> PMP upload and direct Dzen, over local HTTP."""
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from publikot.genapi import GenAPI
from publikot.media import version_images
from publikot.providers import ProviderError
from publikot.settings import Settings
from publikot.store import Store, Conflict
from publikot.telegram_bot import TelegramBot
from publikot.vk_bot import VkBot

PNG = b'\x89PNG\r\n\x1a\n' + b'image-bytes'
ARTICLE = ('Как кот открыл кофейню\n\n' + ('Я расскажу историю от первого лица. ' * 180)).strip()


class Messages:
    def __init__(self):
        self.sent, self.photos = [], []
    async def send(self, peer, text, keyboard=None):
        self.sent.append((peer, text, keyboard))
    async def send_photo(self, peer, path):
        self.photos.append((peer, path))


class GenAPIWholeStack(unittest.IsolatedAsyncioTestCase):
    async def test_project_members_settings_generation_three_images_and_two_destinations(self):
        for transport in ('vk', 'telegram'):
            with self.subTest(transport=transport), tempfile.TemporaryDirectory() as temp:
                settings = Settings('', 1, 42, Path(temp), demo=False, transport=transport, genapi_key='genapi-private-key')
                store = Store(settings.database)
                messages = Messages()
                tasks, gen_payloads, published, uploaded = {}, [], [], []
                image_number = 0
                fail_next_poll = False

                async def service(request):
                    nonlocal image_number, fail_next_poll
                    route = request.path
                    if route.startswith('/gen/'):
                        self.assertEqual(request.headers.get('Authorization'), 'Bearer genapi-private-key')
                        if route.startswith('/gen/networks/'):
                            data = await request.json()
                            gen_payloads.append((route, data))
                            ident = len(tasks) + 1
                            if route.endswith('/gpt-6-luna'):
                                self.assertIn('Характер кота', data['messages'][0]['content'])
                                self.assertIn('Статья 6000 знаков', data['messages'][0]['content'])
                                self.assertEqual(len([c for c in data['messages'][1]['content'] if c['type'] == 'image_url']), 2)
                                tasks[ident] = [ARTICLE]
                            else:
                                self.assertIn('Палитра: оранжевая', data['prompt'])
                                self.assertIn('не рисуй надписей', data['prompt'])
                                self.assertIn(ARTICLE, data['prompt'])
                                self.assertEqual(len(data['image_urls']), 3 if 'Замечания' in data['prompt'] else 2)
                                tasks[ident] = [f'https://cdn.example/{ident}.png']
                            return web.json_response({'request_id': ident, 'status': 'processing'})
                        ident = int(route.rsplit('/', 1)[-1])
                        if fail_next_poll and ident > 1:
                            fail_next_poll = False
                            return web.json_response({}, status=503)
                        return web.json_response({'status': 'success', 'result': tasks[ident], 'cost': 1})
                    if route.startswith('/media/'):
                        self.assertNotIn('Authorization', request.headers)
                        return web.Response(body=PNG, content_type='image/png')
                    if route == '/storage':
                        self.assertNotIn('Authorization', request.headers)
                        form = await request.post()
                        uploaded.append(form['file'].file.read())
                        return web.Response(status=204)
                    if route.startswith('/pmp/'):
                        self.assertEqual(request.headers.get('Authorization'), 'Bearer owner-personal-token')
                        if route == '/pmp/projects':
                            return web.json_response({'data': [{'id': 10, 'name': 'Бренд'}]})
                        if route == '/pmp/accounts':
                            return web.json_response({'data': [{'id': 20, 'connection_status': 1}]})
                        if route == '/pmp/upload/init':
                            image_number += 1
                            return web.json_response({'id': image_number, 'action': 'https://test.s3.amazonaws.com/', 'fields': []})
                        if route == '/pmp/upload/complete':
                            return web.json_response({'status': 1})
                        if route == '/pmp/upload/status':
                            return web.json_response({'status': 1, 'file_id': int(request.query['id']) + 800})
                        if route == '/pmp/publications':
                            data = await request.json()
                            published.append(data)
                            return web.json_response({'id': 90, 'publication_status': 5})
                        if route == '/pmp/publications/90':
                            return web.json_response({'id': 90, 'publication_status': 1})
                    if route.startswith('/dzen/'):
                        if route.endswith('/add-publication'):
                            return web.json_response({'id': 'article12345'})
                        if route.endswith('/add-image'):
                            form = await request.post()
                            self.assertEqual(form['image'].file.read(), PNG)
                            image_number += 1
                            return web.json_response({'id': 'image' + str(image_number)})
                        if route.endswith('/update-publication-content-and-publish'):
                            published.append(await request.json())
                            return web.json_response({'publicUrl': 'https://dzen.ru/a/article12345'})
                        return web.json_response({'ok': True})
                    self.fail('Unexpected HTTP request ' + route)

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
                        def request(self, *args, **kwargs):
                            return self.session.request(*args, **kwargs)
                        def post(self, url, **kwargs):
                            if url == 'https://test.s3.amazonaws.com/':
                                url = server.make_url('/storage')
                            return self.session.post(url, **kwargs)
                        def get(self, url, **kwargs):
                            assert url.startswith('https://cdn.example/')
                            return self.session.get(server.make_url('/media/' + url.rsplit('/', 1)[-1]), **kwargs)
                    with patch('publikot.genapi.BASE', str(server.make_url('/gen'))), \
                         patch('publikot.postmypost.BASE', str(server.make_url('/pmp'))), \
                         patch('publikot.dzen_api.BASE', str(server.make_url('/dzen/'))), \
                         patch('publikot.platforms.aiohttp.ClientSession', LocalSession):
                        ai = GenAPI(settings, store, session_factory=LocalSession, poll_interval=0)
                        cls = TelegramBot if transport == 'telegram' else VkBot
                        bot = cls(settings, store, messages, text_ai=ai, image_ai=ai)
                        await bot.command(42, 42, '/new', 'Кот и кофе', {})
                        project = store.project(42)['id']
                        await bot.command(42, 42, '/allow', '99', {})
                        await bot.command(42, 42, '/assign', f'{project} 99', {})
                        store.configure(42, 'concept', 'Характер кота: добрый рассказчик')
                        store.configure(42, 'text_prompt', 'Статья 6000 знаков, от первого лица')
                        store.configure(42, 'image_prompt', 'Палитра: оранжевая; не рисуй надписей')
                        await bot.menu(42, 42, {'screen': 'image_count', 'project': project, 'count': 3})
                        reference = Path(temp) / 'reference.png'
                        reference.write_bytes(PNG)
                        for kind in ('style', 'character'):
                            store.set_material(42, project, kind, str(reference), 'Материал ' + kind)
                        await bot.platforms.save_token(42, 'owner-personal-token')
                        store.attach_destination(42, project, 'postmypost', 10, 20, 'vk', 'Кофейня ВК')
                        auth = {'publisher_id': 'publisher12345', 'csrf_token': 'private-csrf', 'cookies': {'Session_id': 'private-session'}}
                        key = bot.platforms.import_dzen(42, auth)
                        store.attach_destination(42, project, 'dzen', '', auth['publisher_id'], 'dzen', 'Кофейня Дзен', credential_key=key)
                        await bot.command(99, 99, '/select', str(project), {})
                        await bot.command(99, 99, '/post', 'Расскажи, как открылась кофейня', {})
                        job = store.job(99)
                        self.assertEqual(store.revision(99, job['id'])['body'], ARTICLE)
                        self.assertEqual(len(gen_payloads), 1)
                        self.assertEqual(published, [])
                        with self.assertRaises(Conflict):
                            await bot.action(99, 99, {'action': 'publish', 'job': job['id'], 'revision': 1})
                        # Simulate a lost poll: request ID survives and the next click resumes it.
                        fail_next_poll = True
                        with self.assertRaises(ProviderError):
                            await bot.generate_images(99, 99, job['id'], 1)
                        self.assertEqual(store.job(99)['status'], 'review')
                        await bot.generate_images(99, 99, job['id'], 1)
                        self.assertEqual(len(gen_payloads), 4)  # One text + exactly three paid image requests.
                        job = store.job(99)
                        images = version_images(store.revision(99, job['id']))
                        self.assertEqual(len(images), 3)
                        self.assertTrue(all(Path(p).read_bytes() == PNG for p in images))
                        # Changing one picture retains the other two and the entire article.
                        await bot.generate_images(99, 99, job['id'], job['revision'], 1, 'Сделай светлее')
                        new_images = version_images(store.revision(99, job['id']))
                        self.assertEqual((new_images[0], new_images[2]), (images[0], images[2]))
                        self.assertNotEqual(new_images[1], images[1])
                        self.assertEqual(len(gen_payloads), 5)
                        job = store.job(99)
                        confirmation = {'action': 'publish', 'job': job['id'], 'revision': job['revision']}
                        await bot.action(99, 99, confirmation)
                        self.assertEqual(store.job(99)['status'], 'published')
                        self.assertEqual(len(published), 2)
                        self.assertEqual(published[0]['details'][0]['content'], ARTICLE)
                        self.assertEqual(len(published[0]['details'][0]['file_ids']), 3)
                        self.assertEqual(uploaded, [PNG, PNG, PNG])
                        blocks = json.loads(published[1]['articleContent']['contentState'])['blocks']
                        self.assertEqual(sum(b['type'] == 'atomic:image' for b in blocks), 3)
                        self.assertEqual(''.join(b['text'] for b in blocks if b['type'] == 'unstyled'), ARTICLE)
                        with self.assertRaises(Conflict):
                            await bot.action(99, 99, confirmation)
                        self.assertEqual(len(published), 2)
                        with self.assertRaises(Conflict):
                            store.job(777, job['id'])
                        self.assertIsNone(store.credential_record(99, 'postmypost'))
                store.db.close()
