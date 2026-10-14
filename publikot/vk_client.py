"""VK messages transport. The bot-community key is never used for wall.post."""
import json
import secrets
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from .providers import ProviderError


def trusted_vk_url(value):
    url = urlparse(value)
    return (url.scheme == 'https' and not url.username and not url.password
            and url.port in (None, 443) and url.hostname
            and any(url.hostname == host or url.hostname.endswith('.' + host)
                    for host in ('vk.com', 'vk.ru', 'userapi.com', 'vkuserphoto.ru', 'vkuserphoto.net')))


class VkMessages:
    def __init__(self, token, group_id, session):
        self.token, self.group_id, self.session = token, group_id, session

    async def api(self, method, **params):
        try:
            async with self.session.post('https://api.vk.com/method/' + method,
                data={**params, 'access_token': self.token, 'v': '5.199'},
                timeout=aiohttp.ClientTimeout(total=60), allow_redirects=False) as response:
                if response.status != 200:
                    raise ProviderError(f'ВК: HTTP {response.status}.')
                result = await response.json()
                if 'error' in result:
                    code = result['error'].get('error_code', '?')
                    raise ProviderError(f'ВК: ошибка API {code}. Проверьте права ключа и настройки сообщества.')
                return result['response']
        except (aiohttp.ClientError, TimeoutError, KeyError, ValueError):
            raise ProviderError('Ошибка связи с API ВК. Секреты не выводятся.') from None

    async def long_poll_server(self):
        result = await self.api('groups.getLongPollServer', group_id=self.group_id)
        if not trusted_vk_url(result['server']):
            raise ProviderError('ВК вернул неожиданный адрес Long Poll.')
        return result

    async def poll(self, server):
        if not trusted_vk_url(server['server']):
            raise ProviderError('Недопустимый адрес Long Poll.')
        try:
            async with self.session.get(server['server'], params={
                'act': 'a_check', 'key': server['key'], 'ts': server['ts'], 'wait': 25},
                timeout=aiohttp.ClientTimeout(total=35), allow_redirects=False) as response:
                if response.status != 200:
                    raise ProviderError(f'Long Poll: HTTP {response.status}.')
                return await response.json()
        except (aiohttp.ClientError, TimeoutError, ValueError):
            raise ProviderError('Связь с Long Poll прервалась.') from None

    async def send(self, peer, text, keyboard=None, attachment=None):
        chunks = [text[i:i+3500] for i in range(0, len(text), 3500)] or [' ']
        for index, chunk in enumerate(chunks):
            params = {'peer_id': peer, 'message': chunk, 'random_id': secrets.randbelow(2**31-1)+1}
            if index == len(chunks)-1:
                if keyboard:
                    params['keyboard'] = json.dumps(keyboard, ensure_ascii=False)
                if attachment:
                    params['attachment'] = attachment
            await self.api('messages.send', **params)

    async def download(self, url, target):
        if not trusted_vk_url(url):
            raise ProviderError('Недопустимый адрес фотографии.')
        target = Path(target)
        created = False
        try:
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=60), allow_redirects=False) as response:
                if response.status != 200 or not response.headers.get('Content-Type', '').startswith('image/'):
                    raise ProviderError('Не удалось скачать фотографию из ВК.')
                size = 0
                with target.open('xb') as output:
                    created = True
                    async for block in response.content.iter_chunked(65536):
                        size += len(block)
                        if size > 10 * 1024 * 1024:
                            raise ProviderError('Фото больше 10 МБ.')
                        output.write(block)
        except Exception:
            if created:
                target.unlink(missing_ok=True)
            raise

    async def send_photo(self, peer, path, caption=''):
        upload = await self.api('photos.getMessagesUploadServer', peer_id=peer)
        if not trusted_vk_url(upload['upload_url']):
            raise ProviderError('ВК вернул неожиданный адрес загрузки.')
        with Path(path).open('rb') as image:
            data = aiohttp.FormData()
            data.add_field('photo', image, filename='photo.jpg', content_type='image/jpeg')
            async with self.session.post(upload['upload_url'], data=data, allow_redirects=False,
                                         timeout=aiohttp.ClientTimeout(total=60)) as response:
                if response.status != 200:
                    raise ProviderError('Не удалось загрузить фото предпросмотра.')
                result = await response.json(content_type=None)
        saved = await self.api('photos.saveMessagesPhoto', photo=result['photo'], server=result['server'], hash=result['hash'])
        photo = saved[0]
        attachment = f"photo{photo['owner_id']}_{photo['id']}"
        if photo.get('access_key'):
            attachment += '_' + photo['access_key']
        await self.send(peer, caption or 'Изображение', attachment=attachment)
