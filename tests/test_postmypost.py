import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from publikot.postmypost import Postmypost
from publikot.providers import AmbiguousPublish, ProviderError


def response(data=None, status=200, error=None):
    item = MagicMock()
    item.status = status
    item.json = AsyncMock(return_value=data, side_effect=error)
    manager = MagicMock()
    manager.__aenter__ = AsyncMock(return_value=item)
    manager.__aexit__ = AsyncMock(return_value=False)
    return manager


class PostmypostClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session = MagicMock()
        self.client = Postmypost('private-pmp-token', self.session)
        self.target = {'remote_project': '10', 'remote_account': '20'}

    async def test_pagination_is_complete_and_uses_bearer(self):
        self.session.request.side_effect = [
            response({'data': [{'id': 1}], 'pages': {'total_pages': 2}}),
            response({'data': [{'id': 2}], 'pages': {'total_pages': 2}})]
        self.assertEqual(await self.client.projects(), [{'id': 1}, {'id': 2}])
        calls = self.session.request.call_args_list
        self.assertEqual(calls[1].kwargs['params']['page'], 2)
        self.assertEqual(calls[0].kwargs['headers']['Authorization'], 'Bearer private-pmp-token')
        self.assertFalse(calls[0].kwargs['allow_redirects'])

    async def test_create_is_pending_publication_never_draft(self):
        self.client.upload = AsyncMock(return_value=800)
        self.session.request.return_value = response({'id': 90, 'publication_status': 5})
        result = await self.client.create(self.target, 'Согласованный текст', 'photo.jpg')
        payload = self.session.request.call_args.kwargs['json']
        self.assertEqual(payload['publication_status'], 5)
        self.assertEqual(payload['account_ids'], [20])
        self.assertEqual(payload['details'][0]['file_ids'], [800])
        self.assertEqual(payload['details'][0]['content'], 'Согласованный текст')
        self.assertEqual(result['id'], 90)

    async def test_lost_create_response_and_server_error_are_unknown_and_never_retried(self):
        for data in [response(status=503), response(error=ValueError()), response(error=TimeoutError())]:
            self.session.request.reset_mock()
            self.session.request.return_value = data
            with self.assertRaises(AmbiguousPublish):
                await self.client.create(self.target, 'Текст', None)
            self.assertEqual(self.session.request.call_count, 1)

    async def test_rejections_do_not_echo_remote_error_or_secret(self):
        self.session.request.return_value = response({'error': 'private-pmp-token'}, status=403)
        with self.assertRaises(ProviderError) as error:
            await self.client.projects()
        self.assertNotIn('private-pmp-token', str(error.exception))
        self.assertIn('403', str(error.exception))

    async def test_direct_upload_has_no_api_authorization_and_waits_for_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / 'photo.jpg'
            image.write_bytes(b'photo-bytes')
            self.session.request.side_effect = [response({
                'id': 7, 'action': 'https://bucket.s3.amazonaws.com/',
                'fields': [{'name': 'key', 'value': 'signed-key'}]}),
                response({'id': 7, 'status': 3}),
                response({'id': 7, 'status': 3}),
                response({'id': 7, 'file_id': 100, 'status': 1})]
            self.session.post.return_value = response(status=204)
            with patch('publikot.postmypost.asyncio.sleep', AsyncMock()):
                self.assertEqual(await self.client.upload(10, str(image)), 100)
            self.assertNotIn('headers', self.session.post.call_args.kwargs)
            self.assertFalse(self.session.post.call_args.kwargs['allow_redirects'])
            calls = self.session.request.call_args_list
            self.assertTrue(calls[1].args[1].endswith('/upload/complete'))
            self.assertEqual(calls[1].kwargs['params'], {'id': 7})

    async def test_upload_cannot_send_to_arbitrary_url_or_redirect(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / 'photo.jpg'
            image.write_bytes(b'photo-bytes')
            self.session.request.return_value = response({'id': 1, 'action': 'http://127.0.0.1/upload', 'fields': []})
            with self.assertRaises(ProviderError):
                await self.client.upload(10, str(image))
            self.session.post.assert_not_called()

    async def test_photo_error_never_creates_text_only_publication(self):
        self.client.upload = AsyncMock(side_effect=ProviderError('Фото не готово'))
        with self.assertRaises(ProviderError):
            await self.client.create(self.target, 'Текст', 'missing.jpg')
        self.session.request.assert_not_called()

    async def test_account_requires_reauthorization(self):
        self.client.accounts = AsyncMock(return_value=[{'id': 20, 'connection_status': 2}])
        with self.assertRaises(ProviderError):
            await self.client.check_account(self.target)
