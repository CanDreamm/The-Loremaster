import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from publikot.genapi import GenAPI, image_uri
from publikot.media import version_images
from publikot.providers import ProviderError
from publikot.settings import Settings
from publikot.store import Store
from publikot.vk_bot import VkBot

PNG = b'\x89PNG\r\n\x1a\n' + b'test-image'


def response(body=None, status=200, data=PNG):
    result = MagicMock(status=status)
    result.json = AsyncMock(return_value=body)
    async def chunks(size):
        yield data
    result.content.iter_chunked = chunks
    manager = MagicMock()
    manager.__aenter__ = AsyncMock(return_value=result)
    manager.__aexit__ = AsyncMock(return_value=False)
    return manager


class GenAPITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings('', 1, 42, Path(self.tmp.name), demo=False, genapi_key='private-key')
        self.store = Store(self.settings.database)
        self.project = self.store.create_project(42, 'Персонаж')
        self.job, _ = self.store.create_job(42, 'История')
        self.session = MagicMock()
        self.factory = MagicMock()
        self.factory.return_value.__aenter__ = AsyncMock(return_value=self.session)
        self.factory.return_value.__aexit__ = AsyncMock(return_value=False)
        self.ai = GenAPI(self.settings, self.store, session_factory=self.factory, poll_interval=0, poll_attempts=2)
        self.ref = Path(self.tmp.name) / 'style.jpg'
        self.ref.write_bytes(PNG)

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    async def text(self):
        return await self.ai.generate('', '', prompt='Пиши от лица кота. Статья 5000 знаков.',
            materials=[{'role': 'character', 'path': str(self.ref)}], request_key=(42, self.job, 1))

    async def test_native_async_request_forwards_prompt_and_actual_image_bytes(self):
        self.session.request.side_effect = [response({'request_id': 100}),
            response({'status': 'processing'}), response({'status': 'success', 'result': ['История кота'], 'cost': .3})]
        self.assertEqual(await self.text(), 'История кота')
        calls = self.session.request.call_args_list
        self.assertEqual(calls[0].args, ('POST', 'https://api.gen-api.ru/api/v1/networks/gpt-6-luna'))
        payload = calls[0].kwargs['json']
        self.assertFalse(payload['is_sync'])
        self.assertEqual(payload['reasoning_effort'], 'low')
        self.assertEqual(payload['messages'][0]['content'], 'Пиши от лица кота. Статья 5000 знаков.')
        self.assertTrue(payload['messages'][1]['content'][2]['image_url']['url'].startswith('data:image/png;base64,'))
        self.assertNotIn(str(self.ref), json.dumps(payload))
        self.assertFalse(calls[0].kwargs['allow_redirects'])
        self.assertEqual(await self.text(), 'История кота')
        self.assertEqual(self.session.request.call_count, 3)  # Cached completed request, no second charge.

    async def test_poll_timeout_and_restart_resume_by_id_without_second_post(self):
        self.session.request.side_effect = [response({'request_id': 101}),
            response({'status': 'processing'}), response({'status': 'processing'})]
        with self.assertRaises(ProviderError):
            await self.text()
        self.store.db.close()
        self.store = Store(self.settings.database)
        self.ai.store = self.store
        self.session.request.side_effect = [response({'status': 'success', 'result': ['Готово']})]
        self.assertEqual(await self.text(), 'Готово')
        self.assertEqual(sum(c.args[0] == 'POST' for c in self.session.request.call_args_list), 1)
        self.assertTrue(self.session.request.call_args.args[1].endswith('/request/get/101'))

    async def test_lost_submit_response_and_restart_never_repeat_paid_post(self):
        self.session.request.side_effect = TimeoutError('private-key')
        for _ in range(2):
            with self.assertRaises(ProviderError) as caught:
                await self.text()
            self.assertNotIn('private-key', str(caught.exception))
        self.assertEqual(self.session.request.call_count, 1)

    async def test_http_errors_are_redacted_and_no_automatic_fallback(self):
        for status in (401, 402, 422, 429, 503):
            ident, _ = self.store.create_job(42, 'Ещё')
            self.session.request.side_effect = None
            self.session.request.return_value = response({'error': 'private-key'}, status)
            before = self.session.request.call_count
            with self.assertRaises(ProviderError) as caught:
                await self.ai.run(42, ident, 1, 'text', 0, 'gpt-6-luna', {})
            self.assertNotIn('private-key', str(caught.exception))
            self.assertEqual(self.session.request.call_count, before + 1)

    async def test_image_payload_limits_to_one_output_and_saves_download_locally(self):
        self.session.request.side_effect = [response({'request_id': 102}),
            response({'status': 'success', 'result': ['https://cdn.example/picture.png']})]
        self.session.get.return_value = response()
        result = await self.ai.generate_image(42, self.job, 1, 0, 'Сцена',
            [{'role': 'style', 'path': str(self.ref)}], 'portrait')
        self.assertEqual(Path(result).read_bytes(), PNG)
        payload = self.session.request.call_args_list[0].kwargs['json']
        self.assertEqual((payload['width'], payload['height']), (1024, 1536))
        self.assertEqual((payload['num_images'], payload['max_images']), (1, 1))
        self.assertFalse(payload['translate_input'])
        self.assertEqual(payload['image_urls'], [image_uri(self.ref)])
        self.assertNotIn('headers', self.session.get.call_args.kwargs)
        self.assertFalse(self.session.get.call_args.kwargs['allow_redirects'])
        self.assertEqual(await self.ai.generate_image(42, self.job, 1, 0, 'Сцена', [], 'portrait'), result)
        self.assertEqual(self.session.request.call_count, 2)
        self.assertEqual(self.session.get.call_count, 1)

    async def test_download_failure_reuses_completed_generation(self):
        self.session.request.side_effect = [response({'request_id': 103}),
            response({'status': 'success', 'result': ['https://cdn.example/picture.png']})]
        self.session.get.return_value = response(status=503)
        with self.assertRaises(ProviderError):
            await self.ai.generate_image(42, self.job, 1, 0, 'Сцена', [], 'square')
        self.session.get.return_value = response()
        result = await self.ai.generate_image(42, self.job, 1, 0, 'Сцена', [], 'square')
        self.assertTrue(Path(result).is_file())
        self.assertEqual(self.session.request.call_count, 2)

    async def test_nano_banana_lite_uses_its_own_parameters(self):
        self.ai.settings = replace(self.settings, genapi_image_model='nano-banana-lite')
        self.ai.run = AsyncMock(return_value={'result': ['https://cdn.example/picture.png']})
        self.ai.download = AsyncMock(return_value=str(self.ref))
        # The real run creates this persistent record; this test mocks only transport execution.
        self.store.db.execute("INSERT INTO ai_requests(job_id,revision,kind,slot,model,status) VALUES (?,1,'image',0,'nano-banana-lite','success')", (self.job,))
        await self.ai.generate_image(42, self.job, 1, 0, 'Сцена', [], 'landscape')
        payload = self.ai.run.call_args.args[-1]
        self.assertEqual(payload['aspect_ratio'], '3:2')
        self.assertNotIn('width', payload)
        self.assertNotIn('max_images', payload)

    async def test_missing_key_and_invalid_reference_never_submit(self):
        self.ai.settings = replace(self.settings, genapi_key='')
        with self.assertRaises(ProviderError):
            await self.text()
        self.session.request.assert_not_called()
        self.ai.settings = self.settings
        self.ref.write_bytes(b'not a photo')
        with self.assertRaises(ProviderError):
            await self.text()
        self.session.request.assert_not_called()

    async def test_private_and_redirect_media_destinations_are_rejected(self):
        for url in ('http://example.com/x', 'https://127.0.0.1/x', 'https://[::1]/x',
                    'https://[fc00::1]/x', 'https://localhost/x', 'https://user:secret@example.com/x'):
            with self.assertRaises(ProviderError):
                await self.ai.download(url)
        self.session.get.assert_not_called()
        self.session.get.return_value = response(status=302)
        with self.assertRaises(ProviderError):
            await self.ai.download('https://cdn.example/picture.png')

    async def test_image_restart_restores_review_without_repeating_text(self):
        self.store.save_version(42, self.job, 1, 'Согласованный текст', False)
        self.store.db.execute("UPDATE jobs SET status='generating' WHERE id=?", (self.job,))
        self.store.db.close()
        self.store = Store(self.settings.database)
        self.assertEqual(self.store.job(42, self.job)['status'], 'review')
        self.assertEqual(self.store.revision(42, self.job)['body'], 'Согласованный текст')

    async def test_bot_resume_uses_original_project_settings_and_never_resubmits(self):
        self.store.configure(42, 'concept', 'Исходный характер')
        self.store.configure(42, 'image_count', 0)
        client = SimpleNamespace(send=AsyncMock(), send_photo=AsyncMock())
        bot = VkBot(self.settings, self.store, client, text_ai=self.ai, image_ai=self.ai)
        self.session.request.side_effect = [response({'request_id': 104}),
            response({'status': 'processing'}), response({'status': 'processing'})]
        with self.assertRaises(ProviderError):
            await bot.generate(42, 42, '/post', 'История от лица кота', {})
        ident = self.store.job(42)['id']
        self.assertEqual(self.store.job(42)['status'], 'failed')
        self.store.configure(42, 'concept', 'Другой характер')
        self.store.configure(42, 'image_count', 3)
        self.session.request.side_effect = [response({'status': 'success', 'result': ['Готовая история']})]
        await bot.generate(42, 42, '/resume', '', {})
        version = self.store.revision(42, ident)
        self.assertEqual(self.store.job(42)['status'], 'review')
        self.assertIn('Исходный характер', version['text_prompt'])
        self.assertNotIn('Другой характер', version['text_prompt'])
        self.assertEqual(json.loads(self.store.job(42)['generation_context'])['project']['image_count'], 0)
        self.assertEqual(sum(c.args[0] == 'POST' for c in self.session.request.call_args_list), 1)

    async def test_truncated_text_is_not_accepted_for_publication(self):
        self.session.request.side_effect = [response({'request_id': 105}), response({
            'status': 'success', 'result': ['Незаконченная статья'],
            'full_response': [{'choices': [{'finish_reason': 'length'}]}]})]
        with self.assertRaisesRegex(ProviderError, 'обрезан'):
            await self.text()
        with self.assertRaises(ProviderError):
            await self.text()
        self.assertEqual(self.session.request.call_count, 2)

    async def test_revoked_user_does_not_start_a_paid_request(self):
        self.store.set_access(42, 99, True, 42)
        self.store.set_member(42, self.project, 99, True, 42)
        self.store.set_access(42, 99, False, 42)
        with self.assertRaises(ProviderError):
            await self.ai.run(99, self.job, 1, 'text', 0, 'gpt-6-luna', {})
        self.session.request.assert_not_called()

    async def test_multi_photo_versions_survive_text_revision_and_manual_replacement(self):
        self.store.save_version(42, self.job, 1, 'Первый текст', False)
        self.store.replace_images(42, self.job, 1, ['1.png', '2.png', '3.png'])
        job, old = self.store.revise(42)
        self.store.save_version(42, self.job, job['revision'], 'Новый текст', False)
        self.assertEqual(version_images(self.store.revision(42, self.job)), ['1.png', '2.png', '3.png'])
        self.store.replace_image(42, self.job, job['revision'], 'replacement.png', slot=1)
        self.assertEqual(version_images(self.store.revision(42, self.job)), ['1.png', 'replacement.png', '3.png'])
