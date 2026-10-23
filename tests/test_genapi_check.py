import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from publikot.genapi_check import check, payload_summary, shape
from publikot.settings import Settings
from publikot.store import Store


class DiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_validation_diagnostic_uses_saved_metadata_without_any_network(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = Settings('', 1, 42, Path(temp), demo=False)
            store = Store(settings.database)
            store.create_project(42, 'Проект')
            ident, _ = store.create_job(42, 'Тема')
            store.db.execute("INSERT INTO ai_requests(job_id,revision,kind,slot,model,status,result,request_meta) VALUES (?,1,'image',0,'seedream-v5-lite','rejected',?,?)",
                (ident, json.dumps({'validation_error': 'width: минимум 1024'}), json.dumps({'width': 512, 'prompt_chars': 100})))
            try:
                output = io.StringIO()
                with patch('publikot.genapi_check.aiohttp.ClientSession') as factory, contextlib.redirect_stdout(output):
                    await check(settings, ident, 'image', 0)
                factory.assert_not_called()
                report = json.loads(output.getvalue())
                self.assertEqual(report['submitted']['width'], 512)
                self.assertEqual(report['rejection'], 'width: минимум 1024')
                self.assertEqual(report['kind'], 'image')
            finally:
                store.db.close()

    async def test_diagnostic_reads_real_request_and_does_not_write_or_reveal_contents(self):
        with tempfile.TemporaryDirectory() as temp:
            settings = Settings('', 1, 42, Path(temp), demo=False, genapi_key='PRIVATE-API-KEY')
            store = Store(settings.database)
            project = store.create_project(42, 'Проект')
            store.configure(42, 'concept', 'PRIVATE-CONCEPT')
            image = Path(temp) / 'reference.png'
            image.write_bytes(b'\x89PNG\r\n\x1a\nimage')
            context = {'project': dict(store.require(42, project)), 'brief': 'PRIVATE-BRIEF',
                'previous': '', 'feedback': '', 'reference': str(image), 'materials': []}
            ident, _ = store.create_job(42, 'PRIVATE-BRIEF')
            store.db.execute('UPDATE jobs SET generation_context=? WHERE id=?', (json.dumps(context), ident))
            store.db.execute("INSERT INTO ai_requests(job_id,revision,kind,slot,model,status,request_id) "
                "VALUES (?,1,'text',0,'gpt-6-luna','processing','123')", (ident,))
            response = MagicMock(status=200)
            response.json = AsyncMock(return_value={'status': 'success', 'result': [],
                'full_response': [{'choices': [{'message': {'content': 'PRIVATE-RESPONSE'}}]}],
                'parameters': {'api_key': 'PRIVATE-API-KEY'}})
            session = MagicMock()
            session.request.return_value.__aenter__ = AsyncMock(return_value=response)
            factory = MagicMock()
            factory.return_value.__aenter__ = AsyncMock(return_value=session)
            output = io.StringIO()
            try:
                with patch('publikot.genapi_check.aiohttp.ClientSession', factory), contextlib.redirect_stdout(output):
                    await check(settings)
                self.assertEqual(session.request.call_count, 1)
                self.assertEqual(session.request.call_args.args, ('GET', 'https://api.gen-api.ru/api/v1/request/get/123'))
                report = output.getvalue()
                self.assertIn('"extracted_text_chars": 16', report)
                self.assertIn('"base64_matches_file": true', report)
                for secret in ('PRIVATE-CONCEPT', 'PRIVATE-BRIEF', 'PRIVATE-API-KEY', 'PRIVATE-RESPONSE', str(image)):
                    self.assertNotIn(secret, report)
                self.assertEqual(store.job(42, ident)['status'], 'generating')
                self.assertEqual(store.db.execute('SELECT status FROM ai_requests').fetchone()[0], 'processing')
            finally:
                store.db.close()

    def test_shape_never_prints_dictionary_values_or_unknown_keys(self):
        report = json.dumps(shape({'unknown-secret-key': 'secret', 'parameters': {'token': 'secret'},
            'result': [{'content': 'PRIVATE-TEXT', 'type': 'PRIVATE-TYPE'}]}))
        for text in ('unknown-secret-key', 'secret', 'PRIVATE-TEXT', 'PRIVATE-TYPE'):
            self.assertNotIn(text, report)
