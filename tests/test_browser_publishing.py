import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from publikot.browser_publish import BrowserPublisher, profile_lock, profile_path, validate_target
from publikot.providers import AmbiguousPublish, ProviderError
from publikot.settings import Settings
from publikot.store import Conflict, Store
from publikot.vk_bot import VkBot, preview_keyboard


class BrowserFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings('messages-only', 123, 42, Path(self.tmp.name), demo=False)
        self.store = Store(self.settings.database)
        self.project = self.store.create_project(42, 'Бренд')
        self.client = types.SimpleNamespace(send=AsyncMock())
        self.publisher = types.SimpleNamespace(check_ready=MagicMock(),
            publish=AsyncMock(return_value='https://dzen.ru/a/newpost'))
        self.factory = MagicMock(return_value=self.publisher)
        self.api_factory = MagicMock(side_effect=AssertionError('Publishing API must not be used'))
        self.bot = VkBot(self.settings, self.store, self.client, browser_factory=self.factory,
                         publisher_factory=self.api_factory)

    async def asyncTearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def draft(self, platform='dzen', demo=False):
        target = 'https://dzen.ru/profile/editor/id/channel' if platform == 'dzen' else 'https://vk.ru/club456'
        self.store.browser_destination(42, platform, target)
        ident, _ = self.store.create_job(42, 'Задание')
        self.store.save_version(42, ident, 1, 'Заголовок\nТекст', demo)
        return ident

    async def publish(self, ident, revision=1):
        await self.bot.button_action(42, 42, {'action': 'publish', 'job': ident, 'revision': revision})

    async def test_both_platforms_publish_without_api_token_and_reject_duplicate(self):
        for platform in ('vk', 'dzen'):
            ident = self.draft(platform)
            job = self.store.job(42, ident)
            self.assertIn('Опубликовать', json.dumps(preview_keyboard(job), ensure_ascii=False))
            await self.publish(ident)
            self.assertEqual(self.store.job(42, ident)['status'], 'published')
            self.assertEqual(self.publisher.publish.call_args.args[0], job['browser_target'])
            self.factory.assert_called_with(self.settings.data_dir, self.project, platform)
            with self.assertRaises(Conflict):
                await self.publish(ident)
        self.api_factory.assert_not_called()
        self.assertEqual(self.publisher.publish.await_count, 2)

    async def test_changed_destination_and_stale_revision_cannot_publish(self):
        ident = self.draft()
        self.store.replace_image(42, ident, 1, 'photo.jpg')
        with self.assertRaises(Conflict):
            await self.publish(ident)
        self.store.browser_destination(42, 'dzen', 'https://dzen.ru/profile/editor/id/other')
        with self.assertRaises(Conflict):
            await self.publish(ident, 2)
        self.publisher.publish.assert_not_awaited()

    async def test_demo_and_missing_profile_cannot_publish(self):
        ident = self.draft(demo=True)
        with self.assertRaises(Conflict):
            await self.publish(ident)
        ident = self.draft()
        self.publisher.check_ready.side_effect = ProviderError('Нужен вход')
        with self.assertRaises(ProviderError):
            await self.publish(ident)
        self.assertEqual(self.store.job(42, ident)['status'], 'review')
        self.publisher.publish.assert_not_awaited()

    async def test_timeout_remains_unknown_after_restart_and_cannot_repeat(self):
        ident = self.draft()
        self.publisher.publish.side_effect = AmbiguousPublish('Проверьте площадку')
        with self.assertRaises(AmbiguousPublish):
            await self.publish(ident)
        self.store.db.close()
        self.store = Store(self.settings.database)
        self.bot.store = self.store
        self.assertEqual(self.store.job(42, ident)['status'], 'unknown')
        with self.assertRaises(Conflict):
            await self.publish(ident)
        self.assertEqual(self.publisher.publish.await_count, 1)

    async def test_browser_project_can_be_added_from_menu_without_oauth(self):
        await self.bot.menu(42, 42, {'screen': 'add_browser'})
        await self.bot.dialog_input(42, 42, 'Новый проект', {})
        project = self.store.project(42)['id']
        await self.bot.menu(42, 42, {'screen': 'browser_dzen', 'project': project})
        await self.bot.dialog_input(42, 42, 'https://dzen.ru/profile/editor/id/channel', {})
        self.assertEqual(self.store.project(42)['publish_mode'], 'dzen_browser')

    async def test_member_cannot_replace_account_destination(self):
        self.draft()
        self.store.set_access(42, 77, True, 42)
        self.store.set_member(42, self.project, 77, True, 42)
        self.store.select(77, self.project)
        with self.assertRaises(Conflict):
            self.store.browser_destination(77, 'vk', 'https://vk.ru/club999')

    async def test_api_project_and_old_jobs_keep_original_mode(self):
        self.store.configure(42, 'vk_group', 123)
        ident, _ = self.store.create_job(42, 'Старое задание')
        self.draft()
        self.assertEqual(self.store.job(42, ident)['publish_mode'], 'vk_api')
        self.assertEqual(self.store.job(42, ident)['browser_target'], '')


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_browser_edits_title_body_uploads_photo_and_checks_result(self):
        with tempfile.TemporaryDirectory() as temp:
            publisher = BrowserPublisher(temp, 1, 'dzen')
            profile = publisher.profile
            profile.mkdir(parents=True)
            (profile / '.login-complete').touch()
            photo = Path(temp) / 'photo.png'
            photo.write_bytes(b'photo')
            page = MagicMock()
            page.url = 'https://dzen.ru/profile/editor/id/channel'
            page.goto = AsyncMock()
            controls = {}
            def locator(selector):
                if selector not in controls:
                    controls[selector] = MagicMock()
                    for method in ('click', 'fill', 'wait_for', 'set_input_files'):
                        setattr(controls[selector], method, AsyncMock())
                    controls[selector].evaluate_all = AsyncMock(return_value=[])
                return controls[selector]
            page.locator.side_effect = locator
            context = MagicMock(new_page=AsyncMock(return_value=page), close=AsyncMock())
            driver = types.SimpleNamespace(chromium=types.SimpleNamespace(
                launch_persistent_context=AsyncMock(return_value=context)))
            manager = MagicMock()
            manager.__aenter__ = AsyncMock(return_value=driver)
            manager.__aexit__ = AsyncMock(return_value=False)
            module = types.ModuleType('playwright.async_api')
            module.async_playwright = lambda: manager
            modules = {'playwright': types.ModuleType('playwright'), 'playwright.async_api': module}
            with patch.dict('sys.modules', modules):
                with patch.object(publisher, '_result', AsyncMock(return_value='https://dzen.ru/a/post')):
                    result = await publisher.publish(page.url, 'Заголовок\nСодержимое', str(photo), 1)
                self.assertEqual(result, 'https://dzen.ru/a/post')
                locator(publisher.selectors['title']).fill.assert_awaited_with('Заголовок')
                locator(publisher.selectors['body']).fill.assert_awaited_with('Содержимое')
                locator(publisher.selectors['image_ready']).wait_for.assert_awaited()
                locator(publisher.selectors['publish']).click.assert_awaited_once()
                locator(publisher.selectors['publish']).click.side_effect = TimeoutError()
                with self.assertRaises(AmbiguousPublish):
                    await publisher.publish(page.url, 'Текст', None, 2)
                locator(publisher.selectors['open']).click.side_effect = TimeoutError()
                with self.assertRaises(ProviderError) as error:
                    await publisher.publish(page.url, 'Текст', None, 3)
                self.assertNotIsInstance(error.exception, AmbiguousPublish)

    async def test_no_success_for_old_post_or_channel_link(self):
        with tempfile.TemporaryDirectory() as temp:
            publisher = BrowserPublisher(temp, 1, 'dzen')
            page = MagicMock()
            page.url = 'https://dzen.ru/profile/editor/id/channel'
            page.locator.return_value.filter.return_value.locator.return_value.evaluate_all = AsyncMock(
                return_value=['https://dzen.ru/a/old', 'https://dzen.ru/id/channel'])
            with patch('publikot.browser_publish.asyncio.sleep', AsyncMock()):
                with self.assertRaises(AmbiguousPublish):
                    await publisher._result(page, page.url, {'https://dzen.ru/a/old'}, 'Текст')

    def test_untrusted_targets_and_profile_traversal_are_rejected(self):
        for platform, target in [('vk', 'https://evil.test/club123'),
                                 ('vk', 'https://vk.ru@evil.test/club123'),
                                 ('dzen', 'https://dzen.ru/a/article'),
                                 ('dzen', 'http://dzen.ru/profile/editor/id/channel'),
                                 ('dzen', 'https://dzen.ru/profile/editor/id/channel?x=1')]:
            with self.assertRaises(ProviderError):
                validate_target(platform, target)
        with self.assertRaises(ProviderError):
            profile_path('/tmp', 1, '../vk')

    def test_profile_lock_prevents_two_publishers(self):
        with tempfile.TemporaryDirectory() as temp:
            with profile_lock(Path(temp)):
                with self.assertRaises(ProviderError):
                    with profile_lock(Path(temp)):
                        self.fail('Second publisher acquired profile')
