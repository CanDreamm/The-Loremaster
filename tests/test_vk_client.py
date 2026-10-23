import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

from publikot.__main__ import advance_server, event_failure
from publikot.providers import ProviderError
from publikot.vk_client import VkApiError, VkMessages, trusted_vk_url, vk_keyboard
from publikot.store import Store


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
    async def test_inline_keyboard_is_compacted_before_sending_without_losing_callbacks(self):
        buttons = [{'action': {'type': 'callback', 'label': str(i), 'payload': json.dumps({'id': i})},
                    'color': 'secondary'} for i in range(10)]
        original = {'inline': True, 'buttons': [buttons[:2], buttons[2:4], *[[b] for b in buttons[4:]]]}
        before = json.dumps(original)
        session = Session({'response': 100})
        await VkMessages('private-key', 123, session).send(42, 'Настройки', keyboard=original)
        sent = json.loads(session.calls[0][1]['data']['keyboard'])
        self.assertLessEqual(len(sent['buttons']), 6)
        self.assertTrue(all(1 <= len(row) <= 5 for row in sent['buttons']))
        self.assertEqual([b for row in sent['buttons'] for b in row], buttons)
        self.assertEqual(json.dumps(original), before)

    async def test_oversized_keyboard_does_not_silently_discard_buttons(self):
        session = Session({'response': 100})
        keys = {'inline': True, 'buttons': [[{'action': {'type': 'callback', 'label': str(i)}}] for i in range(11)]}
        with self.assertRaises(ProviderError):
            await VkMessages('private-key', 123, session).send(42, 'Меню', keyboard=keys)
        self.assertEqual(session.calls, [])

    async def test_callback_events_are_enabled_without_disabling_other_subscriptions(self):
        client = VkMessages('private-key', 123, None)
        client.api = AsyncMock(side_effect=[{'enabled': 1, 'api_version': '5.199',
            'events': {'message_new': 1, 'message_event': 0, 'group_join': 1, 'message_reply': 0}}, 1])
        await client.enable_callback_events()
        client.api.assert_awaited_with('groups.setLongPollSettings', group_id=123, enabled=1,
            api_version='5.199', message_new=1, message_event=1, group_join=1, message_reply=0)

    async def test_enabled_callback_events_do_not_reconfigure_long_poll(self):
        client = VkMessages('private-key', 123, None)
        client.api = AsyncMock(return_value={'enabled': 1, 'api_version': '5.199',
            'events': {'message_new': 1, 'message_event': 1}})
        await client.enable_callback_events()
        client.api.assert_awaited_once_with('groups.getLongPollSettings', group_id=123)

    async def test_callback_events_upgrade_old_long_poll_version(self):
        client = VkMessages('private-key', 123, None)
        client.api = AsyncMock(side_effect=[{'enabled': 0, 'api_version': '5.103',
            'events': {'message_new': 0, 'message_event': 0}}, 1])
        await client.enable_callback_events()
        client.api.assert_awaited_with('groups.setLongPollSettings', group_id=123, enabled=1,
            api_version='5.199', message_new=1, message_event=1)

    async def test_callback_answer_uses_event_identity(self):
        client = VkMessages('private-key', 123, None)
        client.api = AsyncMock(return_value=1)
        await client.answer_event(42, 42, 'callback-id')
        client.api.assert_awaited_once_with('messages.sendMessageEventAnswer', event_id='callback-id', user_id=42, peer_id=42)

    async def test_successful_chunks_are_tracked_even_if_later_send_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'test.sqlite3')
            try:
                client = VkMessages('private-key', 123, None)
                client.bind_store(store)
                client.api = AsyncMock(side_effect=[11, ProviderError('Нет связи')])
                with self.assertRaises(ProviderError):
                    await client.send(42, 'x' * 4000)
                self.assertEqual(store.bot_message_ids(123, 42), [11])
            finally:
                store.db.close()

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
        self.assertIn('messages.send', event_failure(error.exception))

    async def test_disabled_bot_keyboard_gets_plain_explanation(self):
        class Responses(Session):
            def post(self, url, **kwargs):
                self.calls.append((url, kwargs))
                return Response({'error': {'error_code': 912, 'error_msg': 'private-key'}}
                                if len(self.calls) == 1 else {'response': 123})
        session = Responses(None)
        keys = {'inline': True, 'buttons': []}
        await VkMessages('private-key', 242305804, session).send(42, 'Меню', keyboard=keys)
        self.assertEqual(len(session.calls), 2)
        first, second = [options['data'] for _, options in session.calls]
        self.assertIn('keyboard', first)
        self.assertNotIn('keyboard', second)
        self.assertIn('Возможности ботов', second['message'])
        self.assertNotIn('private-key', second['message'])
        self.assertEqual(first['random_id'], second['random_id'])

    async def test_other_vk_errors_do_not_retry_message(self):
        for code in (5, 100, 901, 902):
            session = Session({'error': {'error_code': code}})
            with self.assertRaises(VkApiError):
                await VkMessages('private-key', 242305804, session).send(42, 'Меню', keyboard={'inline': True})
            self.assertEqual(len(session.calls), 1)

    async def test_non_numeric_error_code_is_not_printed(self):
        session = Session({'error': {'error_code': 'private-key', 'error_msg': 'private-key'}})
        with self.assertRaises(VkApiError) as error:
            await VkMessages('private-key', 242305804, session).api('messages.send')
        self.assertNotIn('private-key', event_failure(error.exception))

    async def test_long_poll_url_must_belong_to_vk(self):
        session = Session({'response': {'server': 'https://attacker.invalid/poll', 'key': 'secret', 'ts': '1'}})
        with self.assertRaises(ProviderError):
            await VkMessages('key', 242305804, session).long_poll_server()


class LongPollTests(unittest.TestCase):
    def test_unexpected_errors_do_not_expose_details_in_log(self):
        self.assertNotIn('private-key', event_failure(RuntimeError('private-key')))
        diagnostic = event_failure(VkApiError('messages.send', 912))
        self.assertIn('messages.send', diagnostic)
        self.assertIn('912', diagnostic)
        self.assertIn('Возможности ботов', diagnostic)

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
