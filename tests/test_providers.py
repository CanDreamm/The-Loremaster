import unittest
from publikot.providers import ProviderError, VkPublisher, YandexText


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_demo_needs_no_credentials(self):
        result = await YandexText('', '', True).generate('Бренд', 'Тема')
        self.assertIn('ДЕМО', result)

    async def test_real_ai_requires_configuration(self):
        with self.assertRaises(ProviderError):
            await YandexText('', '', False).generate('Бренд', 'Тема')

    async def test_vk_without_token_does_not_send(self):
        with self.assertRaises(ProviderError):
            await VkPublisher('').publish(123, 'Текст', None, 1)
