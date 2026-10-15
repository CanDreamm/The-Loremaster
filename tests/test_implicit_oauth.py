import re
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock

from aiohttp.test_utils import TestClient, TestServer

from publikot.oauth import OAuthManager, VkOAuthProvider, create_web_app
from publikot.settings import Settings
from publikot.store import Store


class ImplicitOAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings('bot-test-key', 242305804, 42, Path(self.temp.name),
            oauth_client_id='54825414', oauth_public_url='https://bot.example.com', oauth_provider='vk_implicit')
        self.store = Store(self.settings.database)
        self.project = self.store.create_project(42, 'Кофейня')
        self.store.configure(42, 'vk_group', 123)
        self.clock = [1000]
        self.provider = VkOAuthProvider(self.settings, None)
        self.provider.exchange = AsyncMock()
        self.provider.api = AsyncMock(side_effect=[{'id': 42}, 8196,
            {'groups': [{'id': 123, 'is_admin': 1, 'admin_level': 2}]}])
        self.manager = OAuthManager(self.settings, self.store, self.provider, now=lambda: self.clock[0])
        self.notify = AsyncMock()
        self.client = TestClient(TestServer(create_web_app(self.manager, self.notify)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.store.db.close()
        self.temp.cleanup()

    def payload(self):
        state = parse_qs(urlsplit(self.manager.begin(42, self.project)).query)['state'][0]
        return {'state': state, 'access_token': 'private-implicit-user-key', 'expires_in': '3600'}

    async def post(self, payload):
        return await self.client.post('/oauth/vk/callback', json=payload,
                                      headers={'Origin': self.settings.oauth_public_url})

    async def test_authorize_uses_own_app_and_implicit_response_type(self):
        url = self.manager.begin(42, self.project)
        self.assertEqual(urlsplit(url).hostname, 'oauth.vk.com')
        params = parse_qs(urlsplit(url).query)
        self.assertEqual(params['client_id'], ['54825414'])
        self.assertEqual(params['response_type'], ['token'])
        self.assertEqual(params['redirect_uri'], [self.settings.oauth_redirect])
        self.assertEqual(set(params['scope'][0].split(',')), {'wall', 'photos', 'groups', 'offline'})
        self.assertNotIn('client_secret', params)
        self.assertNotIn('code_challenge', params)
        self.assertEqual(self.store.db.execute('SELECT verifier FROM oauth_states').fetchone()['verifier'], '')

    async def test_callback_html_has_nonce_policy_and_no_external_assets(self):
        response = await self.client.get('/oauth/vk/callback')
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers['X-Publikot-OAuth'], '1')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        body = await response.text()
        nonce = re.search(r'<script nonce="([^"]+)"', body).group(1)
        self.assertIn(f"script-src 'nonce-{nonce}'", response.headers['Content-Security-Policy'])
        self.assertIn("connect-src 'self'", response.headers['Content-Security-Policy'])
        self.assertNotIn('src="https://', body)
        self.assertNotIn('unsafe-inline', response.headers['Content-Security-Policy'])

    async def test_success_checks_identity_scopes_group_and_encrypts_token(self):
        payload = self.payload()
        response = await self.post(payload)
        self.assertEqual(response.status, 200)
        body = await response.text()
        self.assertNotIn(payload['access_token'], body)
        self.assertEqual(self.manager.token_for(42, self.project, 123), payload['access_token'])
        row = self.store.db.execute('SELECT * FROM vk_connections').fetchone()
        self.assertNotIn(payload['access_token'], row['token'])
        self.assertEqual(row['expires_at'], 4600)
        self.assertEqual([call.args[1] for call in self.provider.api.await_args_list],
                         ['account.getProfileInfo', 'account.getAppPermissions', 'groups.getById'])
        self.provider.exchange.assert_not_awaited()
        self.notify.assert_awaited_once()
        self.assertNotIn(payload['access_token'], str(self.notify.call_args))
        response = await self.post(payload)
        self.assertEqual(response.status, 400)
        self.assertEqual(self.provider.api.await_count, 3)

    async def test_wrong_user_missing_wall_and_moderator_are_rejected(self):
        for answers in ([{'id': 99}], [{'id': 42}, 4],
                        [{'id': 42}, 8196, {'groups': [{'id': 123, 'is_admin': 1, 'admin_level': 1}]}]):
            self.provider.api.side_effect = answers
            response = await self.post(self.payload())
            self.assertEqual(response.status, 400)
            self.assertFalse(self.manager.has_connection(self.project))

    async def test_expired_state_and_changed_group_never_check_token(self):
        payload = self.payload()
        self.clock[0] += 601
        self.assertEqual((await self.post(payload)).status, 400)
        payload = self.payload()
        self.store.configure(42, 'vk_group', 999)
        self.assertEqual((await self.post(payload)).status, 400)
        self.provider.api.assert_not_awaited()

    async def test_disconnect_during_validation_blocks_connection(self):
        async def disconnect(*args):
            self.manager.disconnect(42, self.project)
        self.provider.validate = AsyncMock(side_effect=disconnect)
        response = await self.post(self.payload())
        self.assertEqual(response.status, 400)
        self.assertEqual(self.manager.token_for(42, self.project, 123), '')

    async def test_missing_or_external_origin_does_not_consume_state(self):
        payload = self.payload()
        for headers in ({}, {'Origin': 'https://attacker.example'}, {'Origin': 'null'}):
            response = await self.client.post('/oauth/vk/callback', json=payload, headers=headers)
            self.assertEqual(response.status, 400)
        self.provider.api.assert_not_awaited()
        self.assertEqual((await self.post(payload)).status, 200)

    async def test_get_query_token_and_wrong_post_format_are_rejected(self):
        payload = self.payload()
        response = await self.client.get('/oauth/vk/callback', params=payload)
        self.assertEqual(response.status, 400)
        self.assertNotIn(payload['access_token'], await response.text())
        response = await self.client.post('/oauth/vk/callback', data=payload,
                                          headers={'Origin': self.settings.oauth_public_url})
        self.assertEqual(response.status, 400)
        self.provider.api.assert_not_awaited()

    async def test_denial_consumes_link_without_using_token(self):
        payload = self.payload()
        response = await self.post({**payload, 'denied': True})
        self.assertEqual(response.status, 400)
        self.assertIn('отменили', await response.text())
        self.assertEqual((await self.post(payload)).status, 400)
        self.provider.api.assert_not_awaited()

    async def test_invalid_token_or_expiry_never_saved_or_echoed(self):
        for token, lifetime in [('', 3600), ('private key', 3600), ('private-key', -1),
                                ('private-key', None), ('private-key', True), ('private-key', 'private-expiry')]:
            response = await self.post({**self.payload(), 'access_token': token, 'expires_in': lifetime})
            self.assertEqual(response.status, 400)
            self.assertNotIn('private-key', await response.text())
        self.provider.api.assert_not_awaited()
        self.assertFalse(self.manager.has_connection(self.project))

    async def test_code_callback_cannot_trigger_token_exchange(self):
        payload = self.payload()
        response = await self.client.get('/oauth/vk/callback', params={'state': payload['state'], 'code': 'private-code'})
        self.assertEqual(response.status, 400)
        self.provider.exchange.assert_not_awaited()

    async def test_old_code_flow_state_cannot_be_used_for_implicit(self):
        settings = replace(self.settings, oauth_provider='vkid')
        old = OAuthManager(settings, self.store, self.provider)
        state = parse_qs(urlsplit(old.begin(42, self.project)).query)['state'][0]
        response = await self.post({'state': state, 'access_token': 'private-key', 'expires_in': 3600})
        self.assertEqual(response.status, 400)
        self.provider.api.assert_not_awaited()
