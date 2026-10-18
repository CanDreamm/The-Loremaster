import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from publikot.oauth import OAuthManager, VkOAuthProvider
from publikot.providers import ProviderError
from publikot.settings import Settings
from publikot.store import Conflict, Store


class ManualConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings('bot-key', 242305804, 42, Path(self.tmp.name))
        self.store = Store(self.settings.database)
        self.provider = VkOAuthProvider(self.settings, None)
        self.provider.api = AsyncMock(side_effect=self.api)
        self.manager = OAuthManager(self.settings, self.store, self.provider)
        self.actor, self.permissions, self.admin_level = 42, 8196, 3

    async def asyncTearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    async def api(self, token, method, **params):
        return {'account.getProfileInfo': {'id': self.actor},
                'account.getAppPermissions': self.permissions,
                'groups.getById': {'groups': [{'id': 123, 'name': 'Кофейня',
                                             'is_admin': 1, 'admin_level': self.admin_level}]}}[method]

    async def test_add_replace_and_restart_without_oauth_configuration(self):
        token = 'private-publishing-token'
        project = await self.manager.add_with_token(42, 123, token)
        self.assertEqual(self.store.project(42)['name'], 'Кофейня')
        self.assertEqual(self.manager.token_for(42, project, 123), token)
        encrypted = self.store.db.execute('SELECT token FROM vk_connections').fetchone()[0]
        self.assertNotIn(token, encrypted)
        self.assertNotIn(token, str(self.store.dialog(42)))
        self.store.configure(42, 'concept', 'Сохранённая концепция')
        same = await self.manager.add_with_token(42, 123, 'replacement-private-token')
        self.assertEqual(project, same)
        self.assertEqual(len(self.store.projects(42)), 1)
        self.store.db.close()
        self.store = Store(self.settings.database)
        manager = OAuthManager(self.settings, self.store, self.provider)
        self.assertEqual(manager.token_for(42, project, 123), 'replacement-private-token')
        self.assertEqual(self.store.project(42)['concept'], 'Сохранённая концепция')

    async def test_wrong_identity_rights_and_community_leave_no_partial_project(self):
        for actor, permissions, level, group in [(99, 8196, 3, 123), (42, 4, 3, 123),
                                                  (42, 8196, 1, 123), (42, 8196, 3, 456)]:
            self.actor, self.permissions, self.admin_level = actor, permissions, level
            with self.assertRaises(Conflict):
                await self.manager.add_with_token(42, group, 'private-publishing-token')
            self.assertEqual(self.store.projects(42), [])
            self.assertEqual(self.store.db.execute('SELECT count(*) FROM vk_connections').fetchone()[0], 0)

    async def test_vk_rejection_does_not_echo_secret_or_save_connection(self):
        response = type('Response', (), {'status': 200,
            'json': AsyncMock(return_value={'error': {'error_code': 5, 'error_msg': 'private-publishing-token'}})})()
        from unittest.mock import MagicMock
        session = MagicMock()
        session.post.return_value.__aenter__ = AsyncMock(return_value=response)
        self.manager.provider = VkOAuthProvider(self.settings, session)
        with self.assertRaises(ProviderError) as error:
            await self.manager.add_with_token(42, 123, 'private-publishing-token')
        self.assertNotIn('private-publishing-token', str(error.exception))
        self.assertEqual(self.store.projects(42), [])

    async def test_allowed_member_adds_own_project_without_access_to_others(self):
        self.store.set_access(42, 99, True, 42)
        self.actor = 99
        project = await self.manager.add_with_token(99, 123, 'member-publishing-token')
        self.assertEqual(len(self.store.projects(99)), 1)
        self.assertEqual(self.store.projects(42), [])
        with self.assertRaises(Conflict):
            self.manager.token_for(42, project, 123)
        self.store.set_access(42, 99, False, 42)
        with self.assertRaises(Conflict):
            self.manager.token_for(99, project, 123)
