import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from publikot.platforms import Platforms
from publikot.providers import AmbiguousPublish, ProviderError
from publikot.settings import Settings, ConfigError
from publikot.store import Store, Conflict
from publikot.vk_bot import VkBot


class ProjectsAndPlatformsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings('bot-key', 123, 42, Path(self.tmp.name), demo=False)
        self.store = Store(self.settings.database)
        self.project = self.store.create_project(42, 'Бренд')
        self.store.set_access(42, 99, True, 42)
        self.store.set_member(42, self.project, 99, True, 42)
        self.store.select(99, self.project)
        self.creates = []
        self.status_calls = []
        self.failures = {}
        self.status_value = 1
        self.next_remote = 100
        outer = self
        class FakePMP:
            def __init__(self, token, session, **kwargs):
                self.token = token
            async def projects(self):
                if self.token == 'rejected-private-token':
                    raise ProviderError('Postmypost: HTTP 401')
                return [{'id': 10, 'name': 'PMP проект'}]
            async def accounts(self, project):
                return [{'id': i, 'name': f'Площадка {i}', 'chanel_id': 1,
                         'external_id': str(1000+i), 'connection_status': 1} for i in range(1, 12)]
            async def channels(self):
                return [{'id': 1, 'code': 'vk', 'name': 'ВКонтакте'}]
            async def check_account(self, target):
                return None
            async def create(self, target, body, image):
                outer.creates.append((self.token, dict(target), body, image))
                if int(target['remote_account']) in outer.failures:
                    raise outer.failures[int(target['remote_account'])]
                outer.next_remote += 1
                return {'id': outer.next_remote, 'publication_status': 5}
            async def status(self, remote):
                outer.status_calls.append((self.token, remote))
                return {'id': int(remote), 'publication_status': outer.status_value}
        self.fake_pmp = FakePMP
        self.platforms = Platforms(self.settings, self.store, pmp_factory=FakePMP)
        self.messages = types.SimpleNamespace(send=AsyncMock(), send_photo=AsyncMock())
        self.bot = VkBot(self.settings, self.store, self.messages, platforms=self.platforms)
        await self.platforms.save_token(42, 'owner-private-token')

    async def asyncTearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def attach(self, actor=42, account=1, project=None):
        self.store.attach_destination(actor, project or self.project, 'postmypost', 10, account,
                                      'vk', 'Сообщество ' + str(account), external_id=str(1000+account))

    def draft(self, actor=42, demo=False):
        ident, _ = self.store.create_job(actor, 'Задание')
        self.store.save_version(actor, ident, 1, 'Согласованный текст', demo)
        return ident

    async def test_multiple_destinations_publish_by_member_using_each_owners_token(self):
        self.attach()
        await self.platforms.save_token(99, 'member-private-token')
        self.attach(99, 2)
        ident = self.draft()
        report = await self.platforms.publish(99, ident, 1)
        self.assertEqual([r[0] for r in self.creates], ['owner-private-token', 'member-private-token'])
        self.assertEqual(self.store.job(99, ident)['status'], 'published')
        self.assertIn('Сообщество 1', report)
        self.assertIn('Сообщество 2', report)
        with self.assertRaises(Conflict):
            await self.platforms.publish(42, ident, 1)
        self.assertEqual(len(self.creates), 2)
        self.assertNotIn('private-token', report)

    async def test_tokens_encrypted_isolated_and_failed_replacement_keeps_previous(self):
        ciphertext = self.store.credential_record(42, 'postmypost')['ciphertext']
        self.assertNotIn('owner-private-token', ciphertext)
        self.assertIsNone(self.store.credential_record(99, 'postmypost'))
        with self.assertRaises(ProviderError):
            await self.platforms.save_token(42, 'rejected-private-token')
        self.assertEqual(self.platforms.secret(42, 'postmypost'), 'owner-private-token')
        self.assertEqual((self.settings.data_dir / 'platforms.key').stat().st_mode & 0o777, 0o600)

    async def test_mixed_failure_does_not_repeat_successful_destination(self):
        self.attach(account=1)
        self.attach(account=2)
        self.failures[2] = ProviderError('Отказ сервиса')
        ident = self.draft()
        await self.platforms.publish(42, ident, 1)
        self.assertEqual([r['status'] for r in self.store.delivery_rows(42, ident)], ['published', 'failed'])
        self.assertEqual(self.store.job(42, ident)['status'], 'failed')
        with self.assertRaises(Conflict):
            await self.platforms.publish(99, ident, 1)
        self.assertEqual(len(self.creates), 2)

    async def test_timeout_unknown_without_auto_retry(self):
        self.attach()
        self.failures[1] = AmbiguousPublish('Неизвестно')
        ident = self.draft()
        await self.platforms.publish(42, ident, 1)
        self.assertEqual(self.store.job(42, ident)['status'], 'unknown')
        await self.platforms.refresh(99, ident)
        self.assertEqual(len(self.creates), 1)
        self.assertEqual(self.status_calls, [])

    async def test_failed_upload_restores_same_text_and_three_images_without_ai_or_auto_publish(self):
        from publikot.media import version_images
        self.attach()
        ident = self.draft()
        self.store.replace_images(42, ident, 1, ['one.png', 'two.png', 'three.png'])
        self.failures[1] = ProviderError('Postmypost: неверный адрес загрузки. Файл не отправлен.')
        await self.platforms.publish(42, ident, 2)
        self.assertTrue(self.store.can_restore_upload_failure(99, ident))
        await self.bot.preview(99, 99, ident)
        buttons = self.messages.send.call_args.kwargs['keyboard']['buttons']
        self.assertIn('Восстановить черновик', [b['action']['label'] for row in buttons for b in row])
        await self.bot.action(99, 99, {'action': 'restoredraft', 'job': ident, 'revision': 2})
        new = self.store.job(99)
        self.assertNotEqual(new['id'], ident)
        self.assertEqual(new['status'], 'review')
        self.assertEqual(self.store.revision(99, new['id'])['body'], 'Согласованный текст')
        self.assertEqual(version_images(self.store.revision(99, new['id'])), ['one.png', 'two.png', 'three.png'])
        self.assertEqual(self.store.restore_upload_draft(42, ident, 2), new['id'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM ai_requests').fetchone()[0], 0)
        self.assertEqual(len(self.creates), 1)  # Still only the original failed attempt.
        self.failures.clear()
        await self.platforms.publish(99, new['id'], 1)
        self.assertEqual(self.store.job(99, new['id'])['status'], 'published')
        self.assertEqual(len(self.creates), 2)
        with self.assertRaises(Conflict):
            await self.platforms.publish(99, new['id'], 1)

    async def test_restore_is_blocked_for_ambiguous_or_partially_published_delivery(self):
        self.attach(account=1)
        self.attach(account=2)
        self.failures[2] = ProviderError('Postmypost: фотография не загружена. Пост не отправлен.')
        ident = self.draft()
        await self.platforms.publish(42, ident, 1)
        self.assertFalse(self.store.can_restore_upload_failure(42, ident))
        with self.assertRaises(Conflict):
            self.store.restore_upload_draft(42, ident, 1)
        self.failures[1] = AmbiguousPublish('Результат неизвестен')
        ident = self.draft()
        await self.platforms.publish(42, ident, 1)
        self.assertFalse(self.store.can_restore_upload_failure(42, ident))

    async def test_restore_checks_membership_tokens_and_stale_confirmation(self):
        self.attach()
        self.failures[1] = ProviderError('Postmypost: адрес хранилища не прошёл проверку HTTPS. Файл не отправлен.')
        ident = self.draft()
        await self.platforms.publish(42, ident, 1)
        with self.assertRaises(Conflict):
            self.store.restore_upload_draft(777, ident, 1)
        with self.assertRaises(Conflict):
            self.store.restore_upload_draft(99, ident, 2)
        await self.platforms.save_token(42, 'replacement-private-token')
        with self.assertRaises(Conflict):
            self.store.restore_upload_draft(99, ident, 1)

    async def test_existing_wrong_fields_error_can_restore_without_regeneration(self):
        self.attach()
        self.failures[1] = ProviderError('Postmypost: неверные параметры загрузки.')
        ident = self.draft()
        await self.platforms.publish(42, ident, 1)
        self.assertTrue(self.store.can_restore_upload_failure(99, ident))
        new = self.store.restore_upload_draft(99, ident, 1)
        self.assertEqual(self.store.revision(99, new)['body'], 'Согласованный текст')
        self.assertEqual(self.store.job(99, new)['status'], 'review')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM ai_requests').fetchone()[0], 0)

    async def test_existing_422_rejection_can_restore_and_requires_confirmation(self):
        self.attach()
        self.failures[1] = ProviderError('Postmypost: HTTP 422. Запрос отклонён.')
        ident = self.draft()
        await self.platforms.publish(42, ident, 1)
        self.assertTrue(self.store.can_restore_upload_failure(99, ident))
        new = self.store.restore_upload_draft(99, ident, 1)
        self.assertEqual(self.store.job(99, new)['status'], 'review')
        self.assertEqual(len(self.creates), 1)
        self.failures.clear()
        await self.platforms.publish(99, new, 1)
        self.assertEqual(self.store.job(99, new)['status'], 'published')

    async def test_queued_status_is_read_after_restart_without_resend(self):
        self.attach()
        self.status_value = 5
        ident = self.draft()
        await self.platforms.publish(99, ident, 1)
        self.assertEqual(self.store.job(42, ident)['status'], 'publishing')
        self.store.db.close()
        self.store = Store(self.settings.database)
        self.platforms = Platforms(self.settings, self.store, pmp_factory=self.fake_pmp)
        self.status_value = 1
        await self.platforms.refresh(99, ident)
        self.assertEqual(self.store.job(42, ident)['status'], 'published')
        self.assertEqual(len(self.creates), 1)

    async def test_restart_marks_interrupted_send_unknown_and_unsent_failed(self):
        self.attach(account=1)
        self.attach(account=2)
        ident = self.draft()
        self.store.claim(42, ident, 1)
        first = self.store.delivery_rows(42, ident)[0]
        self.store.delivery_update(42, ident, first['destination_id'], 'sending')
        self.store.db.close()
        self.store = Store(self.settings.database)
        self.platforms = Platforms(self.settings, self.store, pmp_factory=self.fake_pmp)
        await self.platforms.refresh(42, ident)
        self.assertEqual([r['status'] for r in self.store.delivery_rows(42, ident)], ['unknown', 'failed'])
        self.assertEqual(self.creates, [])

    async def test_changed_destinations_token_and_stale_revision_block_before_send(self):
        self.attach()
        ident = self.draft()
        self.attach(account=2)
        with self.assertRaises(Conflict):
            await self.platforms.publish(42, ident, 1)
        ident = self.draft()
        await self.platforms.save_token(42, 'replacement-private-token')
        with self.assertRaises(Conflict):
            await self.platforms.publish(99, ident, 1)
        ident = self.draft()
        self.store.replace_image(42, ident, 1, '/tmp/photo.jpg')
        with self.assertRaises(Conflict):
            await self.platforms.publish(42, ident, 1)
        self.assertEqual(self.creates, [])

    async def test_revoked_connector_membership_blocks_shared_credential(self):
        await self.platforms.save_token(99, 'member-private-token')
        self.attach(99)
        ident = self.draft()
        self.store.set_member(42, self.project, 99, False, 42)
        with self.assertRaises(Conflict):
            await self.platforms.publish(42, ident, 1)
        self.assertEqual(self.store.job(42, ident)['status'], 'review')

    async def test_revoked_connector_access_and_disconnected_token_block(self):
        self.attach()
        ident = self.draft()
        self.platforms.disconnect(42)
        with self.assertRaises(Conflict):
            await self.platforms.publish(99, ident, 1)
        await self.platforms.save_token(99, 'member-private-token')
        self.attach(99, 2)
        ident = self.draft()
        self.store.set_access(42, 99, False, 42)
        with self.assertRaises(Conflict):
            await self.platforms.publish(42, ident, 1)
        self.assertEqual(self.creates, [])

    async def test_demo_blocked_and_photo_version_reused_for_all_receivers(self):
        self.attach(account=1)
        ident = self.draft(demo=True)
        with self.assertRaises(Conflict):
            await self.platforms.publish(42, ident, 1)
        self.attach(account=2)
        ident = self.draft()
        self.store.replace_image(42, ident, 1, 'approved.jpg')
        await self.platforms.publish(42, ident, 2)
        self.assertEqual([r[3] for r in self.creates], ['approved.jpg', 'approved.jpg'])

    async def test_same_physical_destination_cannot_be_connected_twice(self):
        self.attach()
        await self.platforms.save_token(99, 'member-private-token')
        with self.assertRaises(Conflict):
            self.store.attach_destination(99, self.project, 'postmypost', 20, 987, 'vk', 'Дубликат', external_id='1001')

    async def test_owner_assigns_members_and_member_cannot_grant_or_read_other_project(self):
        other = self.store.create_project(99, 'Другой')
        self.store.assign_project_user(99, other, 42, True, 42)
        self.assertEqual(self.store.require(42, other)['id'], other)
        with self.assertRaises(Conflict):
            self.store.assign_project_user(99, self.project, 77, True, 42)
        with self.assertRaises(Conflict):
            self.store.assign_project_user(99, other, 99, False, 42)
        with self.assertRaises(Conflict):
            self.store.destination_rows(777, other)

    async def test_project_deletion_removes_destinations_jobs_but_keeps_personal_token(self):
        self.attach()
        ident = self.draft()
        await self.platforms.publish(42, ident, 1)
        self.store.delete_project(42, self.project, 42)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM deliveries').fetchone()[0], 0)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM project_destinations').fetchone()[0], 0)
        self.assertEqual(self.platforms.secret(42, 'postmypost'), 'owner-private-token')

    async def test_account_picker_uses_own_token_validates_choice_and_paginates(self):
        await self.bot.menu(42, 42, {'screen': 'pmp_projects', 'project': self.project})
        async def click_last_first():
            keys = self.messages.send.call_args.kwargs['keyboard']
            payload = json.loads(keys['buttons'][0][0]['action']['payload'])
            await self.bot.menu(42, 42, payload)
            return payload
        project_payload = await click_last_first()
        keys = self.messages.send.call_args.kwargs['keyboard']
        self.assertLessEqual(len(keys['buttons']), 6)
        first = json.loads(keys['buttons'][0][0]['action']['payload'])
        with self.assertRaises(Conflict):
            await self.bot.menu(99, 99, first)
        await self.bot.menu(42, 42, first)
        self.assertEqual(len(self.store.destination_rows(42, self.project)), 1)
        self.assertNotIn('owner-private-token', str(self.messages.send.call_args_list))
        await self.platforms.save_token(42, 'replacement-private-token')
        with self.assertRaises(Conflict):
            await self.bot.menu(42, 42, first)

    async def test_missing_encryption_key_does_not_silently_replace_it(self):
        key = self.settings.data_dir / 'platforms.key'
        key.unlink()
        manager = Platforms(self.settings, self.store)
        with self.assertRaises(ConfigError):
            manager.secret(42, 'postmypost')

    async def test_direct_dzen_has_separate_per_channel_credentials(self):
        auth = {'publisher_id': 'publisher123', 'cookies': {'session': 'private-session'}, 'csrf_token': 'private-csrf'}
        key = self.platforms.import_dzen(42, auth)
        self.store.attach_destination(42, self.project, 'dzen', auth['publisher_id'], auth['publisher_id'], 'Дзен', 'Канал Дзена', key)
        self.assertNotIn('private-session', self.store.credential_record(42, key)['ciphertext'])
        calls = []
        class Direct:
            def __init__(self, session_auth, session):
                self.auth = session_auth
            async def publish(self, target, body, image, remember):
                calls.append(self.auth)
                remember('article123')
                return 'article123', 'https://dzen.ru/a/article123'
        self.platforms.dzen_factory = Direct
        ident = self.draft()
        await self.platforms.publish(99, ident, 1)
        self.assertEqual(self.store.job(42, ident)['status'], 'published')
        self.assertEqual(calls, [auth])
        self.assertEqual(self.creates, [])
