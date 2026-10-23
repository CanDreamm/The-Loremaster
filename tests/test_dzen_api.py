import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from publikot.dzen_api import DzenAPI, public_url, validate_auth
from publikot.providers import AmbiguousPublish, ProviderError


class DzenDirectTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.auth = {'publisher_id': 'publisher123', 'cookies': {'session': 'private-cookie'}, 'csrf_token': 'private-csrf'}
        self.api = DzenAPI(self.auth, MagicMock())
        self.target = {'remote_account': 'publisher123'}

    async def test_article_and_image_are_sent_through_editor_api(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / 'approved.jpg'
            image.write_bytes(b'photo')
            self.api.request = AsyncMock(side_effect=[{'publicationId': 'article123'}, {'id': 'image123'},
                {}, {'data': {'publicUrl': 'https://dzen.ru/a/article123'}}])
            saved = []
            result = await self.api.publish(self.target, 'Заголовок\n\nТекст <не HTML>', str(image), saved.append, title='Заголовок')
            self.assertEqual(saved, ['article123'])
            self.assertEqual(result, ('article123', 'https://dzen.ru/a/article123'))
            calls = self.api.request.call_args_list
            self.assertEqual([c.args[0] for c in calls], ['add-publication', 'add-image',
                'update-publication-content', 'update-publication-content-and-publish'])
            payload = calls[-1].kwargs['json']
            self.assertTrue(calls[-1].kwargs['final'])
            state = json.loads(payload['articleContent']['contentState'])
            self.assertEqual(state['blocks'][0]['type'], 'atomic:image')
            self.assertEqual(state['blocks'][1]['text'], 'Текст <не HTML>')
            self.assertEqual(payload['preview']['title'], 'Заголовок')
            self.assertEqual(payload['preview']['image']['id'], 'image123')

    async def test_no_public_link_is_unknown_not_published(self):
        self.api.request = AsyncMock(side_effect=[{'id': 'article123'}, {}, {'ok': True}])
        with self.assertRaises(AmbiguousPublish):
            await self.api.publish(self.target, 'Заголовок\n\nТекст', None, lambda _: None, title='Заголовок')
        self.assertEqual(self.api.request.await_count, 3)

    async def test_failed_image_stops_before_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / 'photo.jpg'
            image.write_bytes(b'photo')
            self.api.request = AsyncMock(side_effect=[{'id': 'article123'}, {'error': 'upload'}])
            with self.assertRaises(ProviderError):
                await self.api.publish(self.target, 'Заголовок\n\nТекст', str(image), lambda _: None, title='Заголовок')
            self.assertEqual(self.api.request.await_count, 2)

    async def test_wrong_channel_cannot_use_another_session(self):
        self.api.request = AsyncMock()
        with self.assertRaises(ProviderError):
            await self.api.publish({'remote_account': 'otherpublisher'}, 'Заголовок\n\nТекст', None, lambda _: None, title='Заголовок')
        self.api.request.assert_not_awaited()

    async def test_missing_or_mismatched_title_stops_before_creating_a_draft(self):
        self.api.request = AsyncMock()
        for body, title in [('Просто начало статьи', ''),
                            ('Начало статьи\n\nПродолжение', ''),
                            ('Один заголовок\n\nТекст', 'Другой заголовок')]:
            with self.subTest(body=body), self.assertRaises(ProviderError):
                await self.api.publish(self.target, body, None, lambda _: None, title=title)
        self.api.request.assert_not_awaited()

    def test_auth_header_injection_and_editor_links_are_rejected(self):
        with self.assertRaises(ProviderError):
            validate_auth({**self.auth, 'csrf_token': 'bad\r\nHeader: value'})
        with self.assertRaises(ProviderError):
            validate_auth({**self.auth, 'cookies': {'session': 'bad;cookie'}})
        self.assertEqual(public_url({'url': 'https://evil.test/a/article123'}), '')
        self.assertEqual(public_url({'url': 'https://dzen.ru/profile/editor/id/article123'}), '')
