import unittest

from publikot.__main__ import advance_server
from publikot.providers import ProviderError
from publikot.vk_client import VkMessages, trusted_vk_url


class Response:
    status = 200

    def __init__(self, data):
        self.data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self):
        return self.data


class Session:
    def __init__(self, result):
        self.result, self.calls = result, []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return Response(self.result)


class VkClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_key_is_sent_in_body_not_url(self):
        session = Session({'response': 123})
        client = VkMessages('private-key', 242305804, session)
        await client.send(42, 'Сообщение')
        url, options = session.calls[0]
        self.assertNotIn('private-key', url)
        self.assertEqual(options['data']['access_token'], 'private-key')
        self.assertGreater(options['data']['random_id'], 0)
        self.assertFalse(options['allow_redirects'])

    async def test_error_response_does_not_expose_token(self):
        session = Session({'error': {'error_code': 5, 'request_params': [{'key': 'access_token', 'value': 'private-key'}]}})
        with self.assertRaises(ProviderError) as error:
            await VkMessages('private-key', 242305804, session).api('messages.send')
        self.assertIn('5', str(error.exception))
        self.assertNotIn('private-key', str(error.exception))

    async def test_long_poll_url_must_belong_to_vk(self):
        session = Session({'response': {'server': 'https://attacker.invalid/poll', 'key': 'secret', 'ts': '1'}})
        with self.assertRaises(ProviderError):
            await VkMessages('key', 242305804, session).long_poll_server()


class LongPollTests(unittest.TestCase):
    def test_cursor_advance_and_expiry(self):
        server = {'server': 'https://lp.vk.com', 'key': 'secret', 'ts': '1'}
        self.assertEqual(advance_server(server, {'ts': '2', 'updates': []})['ts'], '2')
        self.assertEqual(advance_server(server, {'failed': 1, 'ts': '3'})['ts'], '3')
        self.assertIsNone(advance_server(server, {'failed': 2}))
        self.assertIsNone(advance_server(server, {'failed': 3}))

    def test_external_urls_and_credential_urls_are_rejected(self):
        self.assertTrue(trusted_vk_url('https://lp.vk.com/wh/123'))
        self.assertTrue(trusted_vk_url('https://sun.userapi.com/photo.jpg'))
        for url in ('https://vk.com.attacker.invalid/x', 'http://lp.vk.com', 'https://127.0.0.1/x', 'https://user:password@vk.com/x', 'https://vk.com:8080/x'):
            self.assertFalse(trusted_vk_url(url), url)
