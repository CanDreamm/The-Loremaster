import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from publikot.providers import ProviderError, VkPublisher, YandexText


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_demo_needs_no_credentials(self):
        result = await YandexText('', '', True).generate('Бренд', 'Тема')
        self.assertIn('ДЕМО', result)

    async def test_real_ai_requires_configuration(self):
        with self.assertRaises(ProviderError):
            await YandexText('', '', False).generate('Бренд', 'Тема')

    async def test_built_prompt_is_sent_to_text_provider_without_image_paths(self):
        response = MagicMock(status=200)
        response.json = AsyncMock(return_value={
            'result': {'alternatives': [{'message': {'text': 'Готовый пост'}}]}})
        session = MagicMock()
        session.post.return_value.__aenter__ = AsyncMock(return_value=response)
        with patch('publikot.providers.aiohttp.ClientSession') as factory:
            factory.return_value.__aenter__ = AsyncMock(return_value=session)
            result = await YandexText('test', 'folder', False).generate(
                'Концепция', 'Задание', prompt='Полный промпт из настроек и задания')
        self.assertEqual(result, 'Готовый пост')
        payload = session.post.call_args.kwargs['json']
        self.assertEqual(payload['messages'][0], {
            'role': 'system', 'text': 'Полный промпт из настроек и задания'})

    async def test_vk_without_token_does_not_send(self):
        with self.assertRaises(ProviderError):
            await VkPublisher('').publish(123, 'Текст', None, 1)
