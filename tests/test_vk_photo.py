import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock

from PIL import Image

from publikot.providers import ProviderError
from publikot.vk_client import VkMessages
from publikot.vk_photo import preview_jpeg
from test_vk_client import Response


class PhotoSession:
    def __init__(self, bodies):
        self.bodies = iter(bodies)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return Response(next(self.bodies))


class VkPhotoTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_png_webp_and_jpeg_become_valid_jpeg_without_changing_original(self):
        with tempfile.TemporaryDirectory() as temp:
            for format in ('PNG', 'WEBP', 'JPEG'):
                path = Path(temp) / 'misleading-extension.jpg'
                image = Image.new('RGB' if format == 'JPEG' else 'RGBA', (96, 64), 'orange')
                image.save(path, format=format)
                original = path.read_bytes()
                encoded = preview_jpeg(path)
                with Image.open(BytesIO(encoded)) as preview:
                    preview.load()
                    self.assertEqual(preview.format, 'JPEG')
                    self.assertEqual(preview.size, (96, 64))
                    self.assertEqual(preview.mode, 'RGB')
                self.assertEqual(path.read_bytes(), original)

    async def test_transparency_is_flattened_and_large_preview_preserves_aspect_ratio(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'transparent.png'
            Image.new('RGBA', (3000, 1500), (0, 0, 0, 0)).save(path)
            with Image.open(BytesIO(preview_jpeg(path))) as preview:
                self.assertEqual(preview.size, (2560, 1280))
                self.assertEqual(preview.getpixel((0, 0)), (255, 255, 255))

    async def test_broken_file_never_reaches_vk(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'bad.png'
            path.write_bytes(b'\x89PNG\r\n\x1a\nbroken')
            client = VkMessages('private-key', 123, None)
            client.api = AsyncMock()
            with self.assertRaises(ProviderError):
                await client.send_photo(42, path)
            client.api.assert_not_awaited()

    async def test_empty_or_invalid_upload_data_never_reaches_save_messages_photo(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'valid.png'
            Image.new('RGB', (96, 64)).save(path)
            for invalid in ({'photo': '[]', 'server': 0, 'hash': ''},
                            {'photo': [], 'server': 1, 'hash': 'private-hash'},
                            {'photo': 'uploaded', 'server': 1, 'hash': ''},
                            {'files': {'file1': {'secret': 'private-secret'}}}):
                session = PhotoSession([{'response': {'upload_url': 'https://sun.userapi.com/upload'}}, invalid])
                with self.assertRaises(ProviderError) as error:
                    await VkMessages('private-key', 123, session).send_photo(42, path)
                self.assertEqual(len(session.calls), 2)
                self.assertNotIn('private-', str(error.exception))
                self.assertFalse(any(url.endswith('/photos.saveMessagesPhoto') for url, _ in session.calls))

    async def test_upload_result_is_preserved_exactly_when_saved(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'valid.webp'
            Image.new('RGB', (96, 64)).save(path)
            photo = json.dumps([{'photo': 'opaque-private-photo', 'sizes': []}])
            session = PhotoSession([{'response': {'upload_url': 'https://sun.userapi.com/upload'}},
                {'photo': photo, 'server': '123', 'hash': 'opaque-private-hash'},
                {'response': [{'owner_id': -123, 'id': 7, 'access_key': 'private-access'}]},
                {'response': 99}])
            self.assertEqual(await VkMessages('private-key', 123, session).send_photo(42, path), [99])
            fields = session.calls[2][1]['data']
            self.assertEqual((fields['photo'], fields['server'], fields['hash']),
                             (photo, 123, 'opaque-private-hash'))
            self.assertEqual(session.calls[3][1]['data']['attachment'], 'photo-123_7_private-access')
            self.assertNotIn('headers', session.calls[1][1])

