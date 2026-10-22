import unittest
from uuid import uuid4

from publikot.providers import ProviderError
from publikot.telegram_client import TelegramMessages, TelegramSettings


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
    def __init__(self, data):
        self.data, self.calls = data, []

    def post(self, url, **options):
        self.calls.append((url, options))
        return Response(self.data)


class TelegramClientTests(unittest.IsolatedAsyncioTestCase):
    def client(self, data):
        session = Session(data)
        return TelegramMessages(TelegramSettings('secret-key', str(uuid4())), session), session

    async def test_service_auth_and_stable_idempotency(self):
        client, session = self.client({'ok': True, 'result': {'message_id': 12}})
        ident = str(uuid4())
        for _ in range(2):
            self.assertEqual(await client.send(123, 'Привет', request_id=ident), 12)
        for url, options in session.calls:
            self.assertTrue(url.startswith('https://chatbot.hopop.ru/api/v1/bots/'))
            self.assertNotIn('secret-key', url)
            self.assertEqual(options['headers']['Authorization'], 'Bearer secret-key')
            self.assertEqual(options['headers']['Idempotency-Key'], ident)
            self.assertFalse(options['allow_redirects'])

    async def test_wrong_bot_is_rejected(self):
        client, _ = self.client({'ok': True, 'result': {'username': 'Studio912'}})
        with self.assertRaises(ProviderError):
            await client.get_me()

    async def test_expected_bot(self):
        client, _ = self.client({'ok': True, 'result': {'username': 'publikot_bot'}})
        self.assertEqual((await client.get_me())['username'], 'publikot_bot')

    async def test_error_does_not_expose_response_or_retry(self):
        client, session = self.client({'ok': False, 'description': 'secret-key'})
        with self.assertRaises(ProviderError) as error:
            await client.send(123, 'Привет', request_id=uuid4())
        self.assertNotIn('secret-key', str(error.exception))
        self.assertEqual(len(session.calls), 1)

    async def test_invalid_request_does_not_send(self):
        client, session = self.client({})
        for text, ident in [('x', 'bad'), ('x' * 4097, uuid4())]:
            with self.assertRaises(ProviderError):
                await client.send(123, text, request_id=ident)
        self.assertEqual(session.calls, [])
