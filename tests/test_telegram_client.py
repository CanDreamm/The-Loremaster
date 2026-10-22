import unittest
from uuid import uuid4

from publikot.providers import ProviderError
from publikot.telegram_client import TelegramMessages, TelegramSettings, TelegramApiError


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
        self.status = 200

    def post(self, url, **options):
        self.calls.append((url, options))
        response = Response(self.data)
        response.status = self.status
        return response


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
        client, _ = self.client({'ok': True, 'result': {'username': 'PubliKotBot'}})
        self.assertEqual((await client.get_me())['username'], 'PubliKotBot')

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

    async def test_http_400_explains_known_delete_error_without_exposing_response(self):
        client, session = self.client({'ok': False, 'error_code': 400,
                                      'error': {'description': 'Bad Request: message to delete not found'},
                                      'debug': 'secret-key'})
        session.status = 400
        with self.assertRaises(TelegramApiError) as error:
            await client._call('deleteMessage', {'chat_id': '42', 'message_id': 1})
        self.assertEqual(error.exception.reason, 'not_found')
        self.assertEqual(error.exception.code, 400)
        self.assertIn('не найдено', str(error.exception))
        self.assertNotIn('secret-key', str(error.exception))
        self.assertNotIn('API-ключ', str(error.exception))

    async def test_unknown_bad_request_is_not_misreported_as_credentials_error(self):
        client, session = self.client({'description': 'secret-key some private request data'})
        session.status = 400
        with self.assertRaises(TelegramApiError) as error:
            await client._call('deleteMessage', {})
        self.assertEqual(error.exception.reason, 'unknown')
        self.assertNotIn('secret-key', str(error.exception))
        self.assertIn('причина не распознана', str(error.exception))

    async def test_service_error_envelope_on_http_200_is_preserved(self):
        client, _ = self.client({'ok': False, 'error_code': 400, 'description': "Bad Request: message can't be deleted"})
        with self.assertRaises(TelegramApiError) as error:
            await client._call('deleteMessage', {})
        self.assertEqual(error.exception.code, 400)
        self.assertEqual(error.exception.reason, 'undeletable')
