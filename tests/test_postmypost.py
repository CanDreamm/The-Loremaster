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
        self.storage_factory = MagicMock()
        self.storage_factory.return_value.__aenter__ = AsyncMock(return_value=self.session)
        self.storage_factory.return_value.__aexit__ = AsyncMock(return_value=False)
        self.client = Postmypost('private-pmp-token', self.session, upload_session_factory=self.storage_factory)
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
        self.assertEqual(payload['details'][0]['publication_type'], 1)
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
                'fields': [{'key': 'key', 'value': 'signed-key'}]}),
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

    async def test_upload_accepts_api_storage_hosts_without_cloud_vendor_pinning(self):
        for host in ('storage.yandexcloud.net', 'postmypost.io', 'files.postmypost.ru', 'uploads.cdn.example'):
            with self.subTest(host=host), tempfile.TemporaryDirectory() as tmp:
                from publikot.postmypost import RequestBudget
                self.client.budget = RequestBudget()
                image = Path(tmp) / 'photo.png'
                image.write_bytes(b'photo-bytes')
                action = f'https://{host}/bucket?signature=private-signed-value'
                self.session.request.side_effect = [response({'id': 7, 'action': action,
                    'fields': [{'key': 'key', 'value': 'signed-key'}]}), response({'status': 1}),
                    response({'status': 1, 'file_id': 100})]
                self.session.post.return_value = response(status=204)
                self.assertEqual(await self.client.upload(10, str(image)), 100)
                self.assertEqual(self.session.post.call_args.args[0], action)
                self.assertNotIn('headers', self.session.post.call_args.kwargs)
                self.assertFalse(self.session.post.call_args.kwargs['allow_redirects'])
                self.assertTrue(self.storage_factory.call_args.kwargs['trust_env'])
                self.assertNotIn('headers', self.storage_factory.call_args.kwargs)

    def test_storage_url_validation_keeps_https_and_private_network_guards(self):
        from publikot.http_media import public_https_url
        for value in (None, 1, [], '', 'http://storage.example/x', 'https://localhost/x',
                      'https://127.0.0.1/x', 'https://169.254.169.254/x', 'https://[::1]/x',
                      'https://[fc00::1]/x', 'https://host.local/x', 'https://host.internal/x',
                      'https://user:secret@storage.example/x', 'https://storage.example:8443/x',
                      'https://storage.example/x#secret', 'https://storage.example/\nsecret'):
            with self.subTest(value=value):
                self.assertFalse(public_https_url(value))

    async def test_public_storage_name_cannot_resolve_to_private_address(self):
        from publikot.http_media import PublicResolver
        resolver = PublicResolver()
        try:
            with patch('publikot.http_media.DefaultResolver.resolve', AsyncMock(return_value=[{'host': '127.0.0.1'}])):
                with self.assertRaises(OSError):
                    await resolver.resolve('storage.example', 443)
        finally:
            await resolver.close()

    async def test_invalid_upload_fields_stop_before_file_or_publication_is_sent(self):
        for fields in (None, {}, [{'name': 'key', 'value': 'legacy-wrong-field'}],
                       [{'key': None, 'value': 'value'}], [{'key': 'key', 'value': None}],
                       [{'key': 'bad\r\nname', 'value': 'value'}]):
            with self.subTest(fields=fields), tempfile.TemporaryDirectory() as tmp:
                image = Path(tmp) / 'photo.jpg'
                image.write_bytes(b'photo')
                self.session.request.reset_mock(side_effect=True)
                self.session.post.reset_mock()
                self.session.request.return_value = response({'id': 7,
                    'action': 'https://storage.yandexcloud.net/bucket', 'fields': fields})
                with self.assertRaisesRegex(ProviderError, 'неверные параметры загрузки. Файл не отправлен.'):
                    await self.client.upload(10, str(image))
                self.session.post.assert_not_called()
                self.assertEqual(self.session.request.call_count, 1)

    async def test_422_identifies_request_stage_and_fields_without_echoing_response(self):
        self.session.request.return_value = response({'message': 'details.0.publication_type invalid private-pmp-token',
            'errors': {'post_at': ['private URL https://secret.example/signed?signature=private']}}, status=422)
        with self.assertRaises(ProviderError) as caught:
            await self.client.create(self.target, 'Текст', None)
        text = str(caught.exception)
        self.assertIn('POST /publications', text)
        self.assertIn('publication_type', text)
        self.assertIn('post_at', text)
        self.assertNotIn('private', text)
        self.assertNotIn('https://', text)
        self.assertEqual(self.session.request.call_count, 1)

    async def test_photo_error_never_creates_text_only_publication(self):
        self.client.upload = AsyncMock(side_effect=ProviderError('Фото не готово'))
        with self.assertRaises(ProviderError):
            await self.client.create(self.target, 'Текст', 'missing.jpg')
        self.session.request.assert_not_called()

    async def test_account_requires_reauthorization(self):
        self.client.accounts = AsyncMock(return_value=[{'id': 20, 'connection_status': 2}])
        with self.assertRaises(ProviderError):
            await self.client.check_account(self.target)
