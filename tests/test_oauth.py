import asyncio
import base64
import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock, MagicMock

from aiohttp.test_utils import TestClient, TestServer

from publikot.oauth import OAuthManager, VkOAuthProvider, create_web_app
from publikot.providers import ProviderError
from publikot.settings import ConfigError, Settings
from publikot.store import Conflict, Store


class OAuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings('bot-test-key', 242305804, 42, Path(self.temp.name),
            oauth_client_id='12345', oauth_public_url='https://bot.example.com')
        self.store = Store(self.settings.database)
        self.project = self.store.create_project(42, 'Бренд')
        self.store.configure(42, 'vk_group', 123)
        self.clock = [1000]
        self.provider = VkOAuthProvider(self.settings, None)
        self.provider.exchange = AsyncMock(return_value={'access_token': 'private-user-test-token', 'expires_in': 3600,
                                                        'user_id': 42, 'scope': 'wall photos'})
        self.provider.validate = AsyncMock()
        self.manager = OAuthManager(self.settings, self.store, self.provider, now=lambda: self.clock[0])

    async def asyncTearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def state(self, actor=42):
        return parse_qs(urlsplit(self.manager.begin(actor, self.project)).query)['state'][0]

    async def complete(self, state=None):
        return await self.manager.complete(state or self.state(), 'one-use-test-code', 'device-test')

    async def test_connection_is_encrypted_and_project_scoped(self):
        await self.complete()
        row = self.store.db.execute('SELECT * FROM vk_connections').fetchone()
        self.assertNotIn('private-user-test-token', row['token'])
        self.assertEqual(self.manager.token_for(42, self.project, 123), 'private-user-test-token')
        self.assertEqual((self.settings.data_dir/'oauth.key').stat().st_mode & 0o777, 0o600)
        self.provider.validate.assert_awaited_once_with('private-user-test-token', 42, 123,
                                                       self.provider.exchange.return_value)

    async def test_pkce_verifier_is_not_in_authorization_link_or_plain_db(self):
        url = self.manager.begin(42, self.project)
        params = parse_qs(urlsplit(url).query)
        self.assertEqual(urlsplit(url).hostname, 'id.vk.com')
        row = self.store.db.execute('SELECT * FROM oauth_states').fetchone()
        verifier = self.manager.vault.decrypt(row['verifier'])
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        self.assertEqual(params['code_challenge'][0], challenge)
        self.assertNotIn(verifier, url)
        self.assertNotIn('client_secret', params)

    async def test_replayed_state_is_rejected(self):
        state = self.state()
        await self.complete(state)
        with self.assertRaises(Conflict):
            await self.complete(state)
        self.provider.exchange.assert_awaited_once()

    async def test_expired_state_does_not_exchange_code(self):
        state = self.state()
        self.clock[0] += 601
        with self.assertRaises(Conflict):
            await self.complete(state)
        self.provider.exchange.assert_not_awaited()

    async def test_new_link_invalidates_previous_one(self):
        old = self.state()
        new = self.state()
        with self.assertRaises(Conflict):
            await self.complete(old)
        await self.complete(new)

    async def test_new_link_during_validation_prevents_old_connection(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def validate(*args):
            started.set()
            await release.wait()

        self.provider.validate.side_effect = validate
        old = asyncio.create_task(self.complete(self.state()))
        await started.wait()
        try:
            new = self.state()
            self.provider.validate.side_effect = None
            self.provider.exchange.return_value = {'access_token': 'new-connection-token'}
            await self.complete(new)
        finally:
            release.set()
            with self.assertRaises(Conflict):
                await old
        self.assertEqual(self.manager.token_for(42, self.project, 123), 'new-connection-token')

    async def test_unassigned_user_cannot_start_connection(self):
        self.store.set_access(42, 99, True, 42)
        with self.assertRaises(Conflict):
            self.state(99)

    async def test_bot_access_revoked_before_callback(self):
        self.store.set_access(42, 99, True, 42)
        self.store.set_member(42, self.project, 99, True, 42)
        state = self.state(99)
        self.store.set_access(42, 99, False, 42)
        with self.assertRaises(Conflict):
            await self.complete(state)
        self.provider.exchange.assert_not_awaited()

    async def test_project_removed_before_callback(self):
        state = self.state()
        self.store.set_member(42, self.project, 42, False, 42)
        with self.assertRaises(Conflict):
            await self.complete(state)

    async def test_changed_target_cannot_receive_old_credentials(self):
        state = self.state()
        self.store.configure(42, 'vk_group', 999)
        with self.assertRaises(Conflict):
            await self.complete(state)
        self.assertFalse(self.manager.has_connection(self.project))

    async def test_permission_failure_stores_no_token(self):
        self.provider.validate.side_effect = ProviderError('Нет wall и photos')
        with self.assertRaises(ProviderError):
            await self.complete()
        self.assertFalse(self.manager.has_connection(self.project))
        self.assertEqual(self.store.db.execute('SELECT verifier FROM oauth_states').fetchone()['verifier'], '')

    async def test_vk_api_diagnostic_contains_method_and_code_without_raw_response(self):
        for code in (5, 7, 15, 27, 1051, 'private-user-test-token'):
            session = MagicMock()
            response = session.post.return_value.__aenter__.return_value
            response.status = 200
            response.json = AsyncMock(return_value={'error': {
                'error_code': code, 'error_msg': 'private-user-test-token',
                'request_params': [{'key': 'access_token', 'value': 'private-user-test-token'}],
            }})
            provider = VkOAuthProvider(self.settings, session)
            with self.assertRaises(ProviderError) as error:
                await provider.api('private-user-test-token', 'account.getProfileInfo')
            message = str(error.exception)
            self.assertIn('account.getProfileInfo', message)
            self.assertIn(str(code) if isinstance(code, int) else 'неизвестен', message)
            self.assertNotIn('private-user-test-token', message)

    async def test_api_failure_callback_shows_exact_stage_and_does_not_save_token(self):
        session = MagicMock()
        response = session.post.return_value.__aenter__.return_value
        response.status = 200
        response.json = AsyncMock(return_value={'error': {'error_code': 15, 'error_msg': 'private-user-test-token'}})
        provider = VkOAuthProvider(self.settings, session)
        provider.exchange = self.provider.exchange
        self.manager.provider = provider
        client = TestClient(TestServer(create_web_app(self.manager)))
        await client.start_server()
        try:
            response = await client.get('/oauth/vk/callback', params={'state': self.state(), 'code': 'private-code', 'device_id': 'device'})
            self.assertEqual(response.status, 400)
            body = await response.text()
            self.assertIn('groups.getById', body)
            self.assertIn('код API 15', body)
            self.assertNotIn('private-code', body)
            self.assertNotIn('private-user-test-token', body)
            self.assertFalse(self.manager.has_connection(self.project))
        finally:
            await client.close()

    async def test_expired_token_is_not_used(self):
        await self.complete()
        self.clock[0] = 4601
        self.assertEqual(self.manager.token_for(42, self.project, 123), '')

    async def test_disconnect_prevents_fallback_and_invalidates_pending_link(self):
        state = self.state()
        self.manager.disconnect(42, self.project)
        self.assertTrue(self.manager.has_connection(self.project))
        self.assertEqual(self.manager.token_for(42, self.project, 123), '')
        with self.assertRaises(Conflict):
            await self.complete(state)

    async def test_disconnect_during_validation_prevents_reconnection(self):
        async def disconnect(*args):
            self.manager.disconnect(42, self.project)
        self.provider.validate.side_effect = disconnect
        with self.assertRaises(Conflict):
            await self.complete()
        self.assertEqual(self.manager.status(42, self.project)['status'], 'disconnected')

    async def test_missing_encryption_key_fails_closed(self):
        await self.complete()
        (self.settings.data_dir/'oauth.key').unlink()
        with self.assertRaises(ConfigError):
            OAuthManager(self.settings, self.store, self.provider)

    async def test_access_is_rechecked_after_network_calls(self):
        async def revoke(*args):
            self.store.set_member(42, self.project, 42, False, 42)
        self.provider.validate.side_effect = revoke
        with self.assertRaises(Conflict):
            await self.complete()
        self.assertFalse(self.manager.has_connection(self.project))

    async def test_duplicate_callback_during_exchange_is_rejected(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def exchange(*args):
            started.set()
            await release.wait()
            return {'access_token': 'private-user-test-token'}
        self.provider.exchange.side_effect = exchange
        state = self.state()
        first = asyncio.create_task(self.complete(state))
        await started.wait()
        try:
            with self.assertRaises(Conflict):
                await self.complete(state)
        finally:
            release.set()
            await first
        self.provider.exchange.assert_awaited_once()

    async def test_http_callback_returns_no_token_and_has_privacy_headers(self):
        notify = AsyncMock()
        client = TestClient(TestServer(create_web_app(self.manager, notify)))
        await client.start_server()
        try:
            response = await client.get('/oauth/vk/callback', params={'state': self.state(), 'code': 'private-code', 'device_id': 'device'})
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
            self.assertEqual(response.headers['Referrer-Policy'], 'no-referrer')
            body = await response.text()
            self.assertNotIn('private-user-test-token', body)
            self.assertNotIn('private-code', body)
            self.assertNotIn('private-user-test-token', str(notify.call_args))
            notify.assert_awaited_once()
            keys = notify.call_args.kwargs['keyboard']
            import json
            target = json.loads(keys['buttons'][0][0]['action']['payload'])
            self.assertEqual(target, {'action': 'menu', 'screen': 'compose', 'project': self.project})
        finally:
            await client.close()

    async def test_unknown_state_and_duplicate_query_parameters_are_rejected(self):
        client = TestClient(TestServer(create_web_app(self.manager)))
        await client.start_server()
        try:
            response = await client.get('/oauth/vk/callback?state=unknown&code=x')
            self.assertEqual(response.status, 400)
            response = await client.get('/oauth/vk/callback?state=a&state=b&code=x')
            self.assertEqual(response.status, 400)
            self.provider.exchange.assert_not_awaited()
        finally:
            await client.close()

    async def test_real_provider_rejects_wrong_user_missing_scopes_and_moderator(self):
        provider = VkOAuthProvider(replace(self.settings, oauth_provider='vk_api'), None)
        cases = [
            [{'id': 99}],
            [{'id': 42}, 4],
            [{'id': 42}, 8196, {'groups': [{'id': 123, 'is_admin': 1, 'admin_level': 1}]}],
            [{'id': 42}, 8196, {'groups': [{'id': 123, 'is_admin': 1}]}],
            [{'id': 42}, 8196, {'groups': [{'id': 123, 'is_admin': 1, 'admin_level': 4}]}],
            [{'id': 42}, 8196, {'groups': [{'id': 999, 'is_admin': 1, 'admin_level': 3}]}],
        ]
        for responses in cases:
            provider.api = AsyncMock(side_effect=responses)
            with self.assertRaises(ProviderError):
                await provider.validate('test-token', 42, 123)
        provider.api = AsyncMock(side_effect=[{'id': 42}, 8196, {'groups': [{'id': 123, 'is_admin': 1, 'admin_level': 2}]}])
        await provider.validate('test-token', 42, 123)

    async def test_vkid_uses_documented_token_identity_and_scopes(self):
        provider = VkOAuthProvider(self.settings, None)
        provider.api = AsyncMock(return_value={'groups': [{'id': 123, 'is_admin': 1, 'admin_level': 2}]})
        for identity in (42, '42'):
            provider.api.reset_mock()
            await provider.validate('test-token', 42, 123,
                {'access_token': 'test-token', 'user_id': identity, 'scope': 'vkid.personal_info wall photos'})
            provider.api.assert_awaited_once_with('test-token', 'groups.getById', group_id=123)

    async def test_vkid_rejects_wrong_identity_or_missing_scopes_before_api_calls(self):
        provider = VkOAuthProvider(self.settings, None)
        provider.api = AsyncMock()
        valid = {'access_token': 'test-token', 'user_id': 42, 'scope': 'wall photos'}
        cases = [None, {**valid, 'user_id': 99}, {**valid, 'user_id': True},
                 {**valid, 'user_id': None}, {**valid, 'access_token': 'other-token'},
                 {**valid, 'scope': None}, {**valid, 'scope': 'vkid.personal_info'},
                 {**valid, 'scope': 'photos'}, {**valid, 'scope': 'wall'}]
        for authorization in cases:
            with self.assertRaises(ProviderError):
                await provider.validate('test-token', 42, 123, authorization)
        provider.api.assert_not_awaited()

    async def test_vkid_scopes_do_not_bypass_group_editor_check(self):
        provider = VkOAuthProvider(self.settings, None)
        provider.api = AsyncMock(return_value={'groups': [{'id': 123, 'is_admin': 1, 'admin_level': 1}]})
        with self.assertRaises(ProviderError):
            await provider.validate('test-token', 42, 123,
                {'access_token': 'test-token', 'user_id': 42, 'scope': 'wall photos'})

    async def test_real_vkid_validation_saves_only_checked_connection(self):
        provider = VkOAuthProvider(self.settings, None)
        provider.exchange = self.provider.exchange
        provider.api = AsyncMock(return_value={'groups': [{'id': 123, 'is_admin': 1, 'admin_level': 3}]})
        self.manager.provider = provider
        await self.complete()
        self.assertEqual(self.manager.token_for(42, self.project, 123), 'private-user-test-token')

    async def test_legacy_api_provider_does_not_put_client_secret_in_url(self):
        settings = replace(self.settings, oauth_provider='vk_api', oauth_client_secret='test-client-secret')
        manager = OAuthManager(settings, self.store, self.provider)
        url = manager.begin(42, self.project)
        self.assertEqual(urlsplit(url).hostname, 'oauth.vk.com')
        self.assertNotIn('test-client-secret', url)
