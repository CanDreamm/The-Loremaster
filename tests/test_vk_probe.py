import unittest

from publikot.vk_probe import METHODS, probe, report


class Response:
    status = 200

    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self):
        return self.value


class Session:
    def __init__(self, values):
        self.values, self.calls = values, []

    def post(self, url, **kwargs):
        method = url.rsplit('/', 1)[1]
        self.calls.append((method, url, kwargs))
        return Response(self.values[method])


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_rights_do_not_imply_photo_upload_or_publish_success(self):
        session = Session({
            'groups.getTokenPermissions': {'response': {'mask': 8196}},
            'account.getAppPermissions': {'error': {'error_code': 27, 'request_params': [{'value': 'private-key'}]}},
            'photos.getWallUploadServer': {'error': {'error_code': 27}},
        })
        result = await probe('private-key', 123, session)
        text = report(result)
        self.assertIn('wall=да, photos=да', text)
        self.assertIn('API 27', text)
        self.assertIn('НЕ выполнялись', text)
        self.assertNotIn('private-key', text)
        self.assertEqual({call[0] for call in session.calls}, set(METHODS))
        for _, url, options in session.calls:
            self.assertNotIn('private-key', url)
            self.assertFalse(options['allow_redirects'])

    async def test_upload_url_is_never_printed_or_used_for_upload(self):
        session = Session({
            'groups.getTokenPermissions': {'error': {'error_code': 15}},
            'account.getAppPermissions': {'response': 8196},
            'photos.getWallUploadServer': {'response': {'upload_url': 'https://vk.com/upload?hash=private-upload-secret'}},
        })
        result = await probe('private-key', 123, session)
        self.assertTrue(result[2]['ok'])
        self.assertNotIn('private-upload-secret', str(result))
        self.assertNotIn('private-upload-secret', report(result))
        self.assertEqual(len(session.calls), 3)
