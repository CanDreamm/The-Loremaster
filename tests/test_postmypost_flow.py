"""Exercise personal token -> projects/accounts -> direct file upload -> create -> status over HTTP."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from publikot.platforms import Platforms
from publikot.settings import Settings
from publikot.store import Store


class PostmypostHTTPFlow(unittest.IsolatedAsyncioTestCase):
    async def test_shared_project_full_image_delivery_and_duplicate_guard(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = Settings('messages-key', 123, 42, Path(temp), demo=False)
            store = Store(settings.database)
            project = store.create_project(42, 'Бренд')
            store.set_access(42, 99, True, 42)
            store.set_member(42, project, 99, True, 42)
            store.select(99, project)
            calls = []
            async def api(request):
                self.assertEqual(request.headers.get('Authorization'), 'Bearer owner-personal-token')
                calls.append(request.method + ' ' + request.path)
                if request.path == '/projects':
                    return web.json_response({'data': [{'id': 10, 'name': 'Бренд PMP'}], 'pages': {'total_pages': 1}})
                if request.path == '/accounts':
                    self.assertEqual(request.query['project_id'], '10')
                    return web.json_response({'data': [{'id': 20, 'chanel_id': 1, 'external_id': '123',
                        'name': 'Сообщество ВК', 'connection_status': 1}], 'pages': {'total_pages': 1}})
                if request.path == '/upload/init':
                    data = await request.json()
                    self.assertEqual(data['project_id'], 10)
                    self.assertEqual(data['size'], 11)
                    return web.json_response({'id': 7, 'action': 'https://storage.yandexcloud.net/pmp-bucket',
                                              'fields': [{'key': 'key', 'value': 'upload-key'},
                                                         {'key': 'policy', 'value': 'signed+/policy=='},
                                                         {'key': 'x-amz-signature', 'value': 'signed-signature'}]})
                if request.path == '/upload/complete':
                    self.assertEqual(request.query['id'], '7')
                    return web.json_response({'id': 7, 'status': 1})
                if request.path == '/upload/status':
                    return web.json_response({'id': 7, 'status': 1, 'file_id': 800})
                if request.path == '/publications':
                    data = await request.json()
                    self.assertEqual(data['publication_status'], 5)
                    self.assertEqual(data['details'][0]['content'], 'Согласованный текст')
                    self.assertEqual(data['details'][0]['file_ids'], [800])
                    self.assertEqual(data['account_ids'], [20])
                    return web.json_response({'id': 90, 'publication_status': 5})
                if request.path == '/publications/90':
                    return web.json_response({'id': 90, 'publication_status': 1})
                self.fail('Unexpected request ' + request.path)
            async def storage(request):
                self.assertNotIn('Authorization', request.headers)
                fields = await request.post()
                self.assertEqual(fields['key'], 'upload-key')
                self.assertEqual(fields['policy'], 'signed+/policy==')
                self.assertEqual(fields['x-amz-signature'], 'signed-signature')
                self.assertEqual(fields['file'].file.read(), b'photo-bytes')
                return web.Response(status=204)
            app = web.Application()
            app.router.add_post('/storage', storage)
            app.router.add_route('*', '/{path:.*}', api)
            server = TestServer(app)
            await server.start_server()
            real_session = aiohttp.ClientSession
            class LocalSession:
                def __init__(self, **kwargs):
                    self.session = real_session(trust_env=False)
                async def __aenter__(self):
                    return self
                async def __aexit__(self, *args):
                    await self.session.close()
                def request(self, *args, **kwargs):
                    return self.session.request(*args, **kwargs)
                def post(self, url, **kwargs):
                    assert url == 'https://storage.yandexcloud.net/pmp-bucket'
                    return self.session.post(server.make_url('/storage'), **kwargs)
            try:
                with patch('publikot.postmypost.BASE', str(server.make_url('')).rstrip('/')), \
                     patch('publikot.platforms.aiohttp.ClientSession', LocalSession):
                    manager = Platforms(settings, store)
                    await manager.save_token(42, 'owner-personal-token')
                    store.attach_destination(42, project, 'postmypost', 10, 20, 'vk', 'Сообщество ВК')
                    image = Path(temp) / 'approved.jpg'
                    image.write_bytes(b'photo-bytes')
                    job, _ = store.create_job(99, 'Тема', image_path=str(image))
                    store.save_version(99, job, 1, 'Согласованный текст', False)
                    await manager.publish(99, job, 1)
                    self.assertEqual(store.job(99, job)['status'], 'published')
                    from publikot.store import Conflict
                    with self.assertRaises(Conflict):
                        await manager.publish(42, job, 1)
                    self.assertEqual(calls.count('POST /publications'), 1)
                    self.assertEqual(store.delivery_rows(99, job)[0]['remote_id'], '90')
            finally:
                await server.close()
                store.db.close()
