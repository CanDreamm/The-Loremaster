import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from publikot.genapi import GenAPI, GenAPIRejected, image_uri, extract_text, validation_details
from publikot.media import version_images
from publikot.providers import ProviderError
from publikot.settings import Settings
from publikot.store import Store, Conflict
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

    async def test_documented_validation_rejection_is_saved_and_explicit_retry_survives_restart(self):
        self.session.request.side_effect = [response({'error': True, 'errors_validation': {
            'width': ['The width field must be at least 1024. private-key'],
            'image_urls.0': ['The image_urls.0 must be a valid URL. https://secret.example/token'],
            'private-key': ['secret']}}, 422)]
        with self.assertRaises(GenAPIRejected) as caught:
            await self.ai.run(42, self.job, 1, 'image', 0, 'seedream-v5-lite',
                              {'prompt': 'PRIVATE-PROMPT', 'width': 512, 'image_urls': [image_uri(self.ref)]})
        message = str(caught.exception)
        self.assertIn('seedream-v5-lite', message)
        self.assertIn('width: минимум 1024', message)
        self.assertIn('image_urls.0', message)
        self.assertNotIn('private-key', message)
        self.assertNotIn('secret.example', message)
        self.assertEqual(self.session.request.call_count, 1)
        row = self.store.db.execute('SELECT * FROM ai_requests').fetchone()
        self.assertEqual(row['status'], 'rejected')
        metadata = json.loads(row['request_meta'])
        self.assertEqual(metadata['prompt_chars'], len('PRIVATE-PROMPT'))
        self.assertEqual(metadata['reference_count'], 1)
        for secret in ('PRIVATE-PROMPT', 'PRIVATE-REFERENCE', 'private-key'):
            self.assertNotIn(secret, row['request_meta'] + row['result'])
        self.store.db.close()
        self.store = Store(self.settings.database)
        self.ai.store = self.store
        self.session.request.side_effect = [response({'request_id': 700}),
            response({'status': 'success', 'result': ['https://cdn.example/img.png']})]
        await self.ai.run(42, self.job, 1, 'image', 0, 'seedream-v5-lite', {'width': 1024})
        row = self.store.db.execute('SELECT * FROM ai_requests').fetchone()
        self.assertEqual(row['status'], 'success')
        self.assertEqual(row['request_id'], '700')
        self.assertEqual(json.loads(row['request_meta'])['width'], 1024)

    async def test_undocumented_422_or_get_rejection_never_unlocks_paid_request(self):
        self.session.request.side_effect = [response({'error': True, 'errors': {'width': ['invalid']}}, 422)]
        with self.assertRaises(ProviderError):
            await self.ai.run(42, self.job, 1, 'image', 0, 'seedream-v5-lite', {})
        with self.assertRaises(ProviderError):
            await self.ai.run(42, self.job, 1, 'image', 0, 'seedream-v5-lite', {})
        self.assertEqual(self.session.request.call_count, 1)
        self.assertEqual(self.store.db.execute('SELECT status FROM ai_requests').fetchone()[0], 'submitting')
        self.session.request.side_effect = [response({'error': True, 'errors_validation': {'width': ['required']}}, 422)]
        with self.assertRaises(ProviderError) as caught:
            await self.ai.request(self.session, 'GET', '/request/get/123')
        self.assertNotIsInstance(caught.exception, GenAPIRejected)

    def test_validation_messages_only_extract_known_rules_and_fields(self):
        self.assertEqual(validation_details({'errors_validation': {'prompt': ['PRIVATE-TEXT'],
            'width': ['The width field must not be greater than 2048.'],
            'height': ['The height must be an integer.'], 'https://secret': ['required']}}),
            ['prompt: отклонено', 'width: максимум 2048', 'height: нужно целое число'])

    async def test_old_unknown_image_requires_explicit_new_attempt_and_keeps_known_ids(self):
        self.store.configure(42, 'image_count', 2)
        self.store.db.execute('UPDATE jobs SET generation_context=? WHERE id=?',
            (json.dumps({'project': dict(self.store.require(42, self.project))}), self.job))
        self.store.save_version(42, self.job, 1, 'Согласованный текст', False)
        self.store.db.execute("INSERT INTO ai_requests(job_id,revision,kind,slot,model,status) VALUES (?,1,'image',0,'seedream-v5-lite','submitting')", (self.job,))
        self.store.db.execute("INSERT INTO ai_requests(job_id,revision,kind,slot,model,status,request_id,result,local_path) VALUES (?,1,'image',1,'seedream-v5-lite','success','711',?,?)",
            (self.job, json.dumps({'result': ['https://cdn.example/saved.png']}), str(self.ref)))
        client = SimpleNamespace(send=AsyncMock(), send_photo=AsyncMock())
        bot = VkBot(self.settings, self.store, client, text_ai=self.ai, image_ai=self.ai)
        await bot.generate_images(42, 42, self.job, 1)
        self.session.request.assert_not_called()
        self.assertIn('новую оплату', client.send.call_args.args[1])
        self.session.request.side_effect = [response({'request_id': 712}),
            response({'status': 'success', 'result': ['https://cdn.example/new.png']})]
        self.session.get.return_value = response()
        payload = {'action': 'newimages', 'job': self.job, 'revision': 1}
        await bot.action(42, 42, payload)
        self.assertEqual(self.store.revision(42, self.job)['body'], 'Согласованный текст')
        self.assertEqual(len(version_images(self.store.revision(42, self.job))), 2)
        self.assertEqual(sum(c.args[0] == 'POST' for c in self.session.request.call_args_list), 1)
        self.assertEqual(self.store.db.execute("SELECT status FROM ai_requests WHERE revision=1 AND slot=0").fetchone()[0], 'submitting')
        before = self.session.request.call_count
        with self.assertRaises(Conflict):  # stale confirmation must not create another generation
            await bot.action(42, 42, payload)
        self.assertEqual(self.session.request.call_count, before)

    async def test_explicit_retry_also_replaces_an_existing_image_with_unknown_regeneration(self):
        self.store.db.execute('UPDATE jobs SET generation_context=? WHERE id=?',
            (json.dumps({'project': dict(self.store.require(42, self.project))}), self.job))
        self.store.save_version(42, self.job, 1, 'Текст', False)
        self.store.replace_images(42, self.job, 1, [str(self.ref)])
        self.store.db.execute("INSERT INTO ai_requests(job_id,revision,kind,slot,model,status) VALUES (?,2,'image',0,'seedream-v5-lite','submitting')", (self.job,))
        self.session.request.side_effect = [response({'request_id': 713}),
            response({'status': 'success', 'result': ['https://cdn.example/new.png']})]
        self.session.get.return_value = response()
        bot = VkBot(self.settings, self.store, SimpleNamespace(send=AsyncMock(), send_photo=AsyncMock()),
                    text_ai=self.ai, image_ai=self.ai)
        await bot.action(42, 42, {'action': 'newimages', 'job': self.job, 'revision': 2})
        images = version_images(self.store.revision(42, self.job))
        self.assertEqual(len(images), 1)
        self.assertNotEqual(images[0], str(self.ref))
        self.assertEqual(sum(c.args[0] == 'POST' for c in self.session.request.call_args_list), 1)

    async def test_optional_reference_field_is_omitted_without_materials(self):
        self.session.request.side_effect = [response({'request_id': 102}),
            response({'status': 'success', 'result': ['https://cdn.example/picture.png']})]
        self.session.get.return_value = response()
        await self.ai.generate_image(42, self.job, 1, 0, 'Сцена', [], 'square')
        self.assertNotIn('image_urls', self.session.request.call_args_list[0].kwargs['json'])

    async def test_long_prompt_is_sent_verbatim_directly_to_image_model_without_a_text_request(self):
        prompt = 'Задание: персонаж в кофейне. Концепция: ' + 'Важные детали концепции. ' * 600
        self.session.request.side_effect = [response({'request_id': 801}),
            response({'status': 'success', 'result': ['https://cdn.example/image.png']})]
        self.session.get.return_value = response()
        materials = []
        result = await self.ai.generate_image(42, self.job, 1, 0, prompt, materials, 'square')
        calls = self.session.request.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0].args[1].endswith('/seedream-v5-lite'))
        self.assertEqual(calls[0].kwargs['json']['prompt'], prompt)
        self.assertFalse(calls[0].kwargs['json']['translate_input'])
        self.assertIsNone(self.store.db.execute("SELECT 1 FROM ai_requests WHERE kind!='image'").fetchone())
        metadata = json.loads(self.store.db.execute("SELECT request_meta FROM ai_requests WHERE kind='image'").fetchone()[0])
        self.assertEqual(metadata['prompt_chars'], len(prompt))
        self.assertEqual(await self.ai.generate_image(42, self.job, 1, 0, prompt, materials, 'square'), result)
        self.assertEqual(self.session.request.call_count, 2)

    async def test_long_prompt_image_poll_resumes_by_id_without_any_text_request(self):
        prompt = 'Контекст: ' + 'История. ' * 600
        self.session.request.side_effect = [response({'request_id': 803}), response(status=503)]
        with self.assertRaises(ProviderError):
            await self.ai.generate_image(42, self.job, 1, 0, prompt, [], 'square')
        self.session.request.side_effect = [response({'status': 'success', 'result': ['https://cdn.example/image.png']})]
        self.session.get.return_value = response()
        await self.ai.generate_image(42, self.job, 1, 0, prompt, [], 'square')
        posts = [call for call in self.session.request.call_args_list if call.args[0] == 'POST']
        self.assertEqual(len(posts), 1)
        self.assertTrue(posts[0].args[1].endswith('/seedream-v5-lite'))

    async def test_empty_image_prompt_never_submits_a_paid_request(self):
        for prompt in ('', '   '):
            with self.assertRaisesRegex(ProviderError, 'пустой промпт'):
                await self.ai.generate_image(42, self.job, 1, 0, prompt, [], 'square')
        self.session.request.assert_not_called()

    async def test_known_image_request_resumes_without_starting_new_scene_preparation(self):
        self.store.db.execute("INSERT INTO ai_requests(job_id,revision,kind,slot,model,status,request_id) VALUES (?,1,'image',0,'seedream-v5-lite','processing','806')", (self.job,))
        self.session.request.side_effect = [response({'status': 'success', 'result': ['https://cdn.example/image.png']})]
        self.session.get.return_value = response()
        await self.ai.generate_image(42, self.job, 1, 0, 'Long context. ' * 500, [], 'square')
        self.assertEqual(self.session.request.call_count, 1)
        self.assertEqual(self.session.request.call_args.args[0], 'GET')

    async def test_legacy_draft_and_cached_article_scene_are_not_inputs_to_a_new_image_request(self):
        project = dict(self.store.require(42, self.project))
        project['concept'] = 'USER_CONCEPT ' * 300
        context = {'project': project, 'brief': 'ORIGINAL_USER_TASK',
                   'materials': [{'kind': 'character', 'path': str(self.ref), 'description': 'CHARACTER_DESCRIPTION'}],
                   'previous': 'PREVIOUS_ARTICLE_SECRET', 'feedback': 'TEXT_CORRECTION_SECRET'}
        self.store.db.execute('UPDATE jobs SET generation_context=? WHERE id=?', (json.dumps(context), self.job))
        self.store.save_version(42, self.job, 1, 'GENERATED_ARTICLE_SECRET', False)
        self.store.db.execute('UPDATE versions SET image_prompt=? WHERE job_id=?',
                             ('LEGACY_PROMPT_WITH_ARTICLE_SECRET', self.job))
        self.store.db.execute("INSERT INTO ai_requests(job_id,revision,kind,slot,model,status,result) VALUES (?,1,'image_prompt',0,'gpt-6-luna','success',?)",
            (self.job, json.dumps({'result': ['LEGACY_SCENE_WITH_ARTICLE_SECRET']})))
        self.store.db.execute("INSERT INTO ai_requests(job_id,revision,kind,slot,model,status,result) VALUES (?,1,'image_prompt_v2',0,'gpt-6-luna','success',?)",
            (self.job, json.dumps({'result': ['GENERATED_SCENE_SECRET']})))
        self.store.db.execute("INSERT INTO ai_requests(job_id,revision,kind,slot,model,status) VALUES (?,1,'image',0,'seedream-v5-lite','rejected')", (self.job,))
        self.session.request.side_effect = [response({'request_id': 900}),
            response({'status': 'success', 'result': ['https://cdn.example/new.png']})]
        self.session.get.return_value = response()
        bot = VkBot(self.settings, self.store, SimpleNamespace(send=AsyncMock(), send_photo=AsyncMock()),
                    text_ai=self.ai, image_ai=self.ai)
        await bot.generate_images(42, 42, self.job, 1)
        posted = self.session.request.call_args_list[0]
        self.assertTrue(posted.args[1].endswith('/seedream-v5-lite'))
        parts = posted.kwargs['data']._fields
        actual_prompt = next(value for options, headers, value in parts if options['name'] == 'prompt')
        self.assertIn('ORIGINAL_USER_TASK', actual_prompt)
        self.assertIn('USER_CONCEPT', actual_prompt)
        self.assertIn('CHARACTER_DESCRIPTION', actual_prompt)
        for forbidden in ('GENERATED_ARTICLE_SECRET', 'PREVIOUS_ARTICLE_SECRET', 'TEXT_CORRECTION_SECRET',
                          'LEGACY_PROMPT_WITH_ARTICLE_SECRET', 'LEGACY_SCENE_WITH_ARTICLE_SECRET', 'GENERATED_SCENE_SECRET'):
            self.assertNotIn(forbidden, actual_prompt)
        self.assertEqual(self.session.request.call_count, 2)
        self.assertIsNotNone(self.store.db.execute("SELECT 1 FROM ai_requests WHERE kind='image' AND request_id='900'").fetchone())
        self.assertEqual(self.store.revision(42, self.job)['body'], 'GENERATED_ARTICLE_SECRET')
        self.assertNotIn('ARTICLE_SECRET', self.store.revision(42, self.job)['image_prompt'])

    def test_validation_explanations_preserve_constraint_text_without_echoed_secrets(self):
        details = validation_details({'errors_validation': {
            'prompt': ['Длина поля prompt должна составлять от 1 до 2000 символов.'],
            'image_urls': ['The selected image_urls is invalid. "private-key" https://example.com/private'],
            'width': ['The width must be a multiple of 16.']}}, payload={'prompt': 'private prompt' * 20}, secret='private-key')
        text = '\n'.join(details)
        self.assertIn('от 1 до 2000 символов', text)
        self.assertIn('multiple of 16', text)
        self.assertNotIn('private-key', text)
        self.assertNotIn('https://', text)

    async def test_image_payload_limits_to_one_output_and_saves_download_locally(self):
        self.session.request.side_effect = [response({'request_id': 102}),
            response({'status': 'success', 'result': ['https://cdn.example/picture.png']})]
        self.session.get.return_value = response()
        result = await self.ai.generate_image(42, self.job, 1, 0, 'Сцена',
            [{'role': 'style', 'path': str(self.ref)}], 'portrait')
        self.assertEqual(Path(result).read_bytes(), PNG)
        self.assertTrue(self.session.request.call_args_list[0].kwargs['data'].is_multipart)
        payload = json.loads(self.store.db.execute('SELECT request_meta FROM ai_requests').fetchone()[0])
        self.assertEqual((payload['width'], payload['height']), (1024, 1536))
        self.assertEqual((payload['num_images'], payload['max_images']), (1, 1))
        self.assertFalse(payload['translate_input'])
        self.assertEqual(payload['reference_count'], 1)
        self.assertEqual(payload['transport'], 'multipart/form-data')
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

    async def test_nested_and_full_responses_produce_text(self):
        cases = [
            {'result': [{'choices': [{'message': {'role': 'assistant', 'content': 'Готовый текст'}}]}]},
            {'result': [{'message': {'content': [{'type': 'text', 'text': 'Готовый текст'}]}}]},
            {'result': [], 'full_response': [{'choices': [{'message': {'content': 'Готовый текст'}}]}]},
            {'result': None, 'full_response': {'output': [{'type': 'reasoning', 'text': 'Скрытые мысли'},
                {'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'Готовый текст'}]}]}},
            {'result': {'output_text': 'Готовый текст'}},
        ]
        for index, case in enumerate(cases):
            with self.subTest(index=index):
                self.session.request.side_effect = [response({'request_id': 200 + index}),
                    response({'status': 'success', **case})]
                result = await self.ai.generate('', '', request_key=(42, self.job, index + 2))
                self.assertEqual(result, 'Готовый текст')
                self.assertEqual(await self.ai.generate('', '', request_key=(42, self.job, index + 2)), result)

    async def test_old_empty_success_cache_is_refetched_by_get_only(self):
        self.store.db.execute('INSERT INTO ai_requests(job_id,revision,kind,slot,model,status,request_id,result) '
            "VALUES (?,1,'text',0,'gpt-6-luna','success','300',?)", (self.job, json.dumps({'result': [{}]})))
        self.session.request.return_value = response({'status': 'success', 'result': [],
            'full_response': [{'choices': [{'message': {'content': 'Сохранённая статья'}}]}]})
        self.assertEqual(await self.text(), 'Сохранённая статья')
        self.assertEqual(await self.text(), 'Сохранённая статья')
        self.assertEqual(self.session.request.call_count, 1)
        self.assertEqual(self.session.request.call_args.args,
                         ('GET', 'https://api.gen-api.ru/api/v1/request/get/300'))

    def test_parser_never_uses_reasoning_input_tools_or_alternative_choices(self):
        self.assertEqual(extract_text([{'role': 'user', 'content': 'Не публиковать'},
            {'type': 'reasoning', 'text': 'Не публиковать'},
            {'type': 'function_call', 'text': 'Не публиковать'},
            {'type': 'function_call_output', 'output': 'Не публиковать'},
            {'type': 'input_text', 'text': 'Не публиковать'},
            {'role': 'assistant', 'content': [{'type': 'text', 'text': 'Публикация'}]}]), 'Публикация')
        self.assertEqual(extract_text({'parameters': {'messages': [{'content': 'Секрет'}]},
            'usage': {'text': 'Секрет'}}), '')
        self.assertEqual(extract_text({'choices': [{'message': {'content': 'Первый'}},
            {'message': {'content': 'Другой вариант'}}]}), 'Первый')

    async def test_empty_and_oversized_text_have_distinct_errors(self):
        self.session.request.side_effect = [response({'request_id': 301}),
            response({'status': 'success', 'result': ['']})]
        with self.assertRaisesRegex(ProviderError, 'нет готового текста') as error:
            await self.text()
        self.assertNotIn('30000', str(error.exception))
        self.session.request.side_effect = [response({'request_id': 302}),
            response({'status': 'success', 'result': ['я' * 30001]})]
        with self.assertRaisesRegex(ProviderError, '30001 символов'):
            await self.ai.generate('', '', request_key=(42, self.job, 2))

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
