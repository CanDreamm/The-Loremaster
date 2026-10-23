"""VK messages transport. The bot-community key is never used for wall.post."""
import json
import mimetypes
import re
import secrets
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from .providers import ProviderError


class VkApiError(ProviderError):
    def __init__(self, method, code):
        self.code = code if type(code) is int else None
        self.method = method if re.fullmatch(r'[A-Za-z]+\.[A-Za-z]+', method) else 'API'
        hints = {
            5: 'Проверьте ключ сообщества бота.',
            7: 'Проверьте права ключа сообщества бота.',
            15: 'Проверьте права ключа и доступ к сообществу.',
            100: 'ВК отклонил параметры запроса.',
            901: 'Пользователь должен разрешить сообщения от сообщества.',
            902: 'Пользователь запретил сообщения от сообщества.',
            911: 'ВК отклонил формат клавиатуры бота.',
            912: 'Включите «Возможности ботов» в настройках сообщений сообщества.',
        }
        super().__init__(f'ВК: {self.method}, ошибка API {self.code if self.code is not None else "неизвестна"}. '
                         + hints.get(self.code, 'Проверьте права ключа и настройки сообщества.'))


def vk_keyboard(value):
    """Fit inline menus into VK's 6 rows, 5 columns and 10 buttons, without losing actions."""
    if not value.get('inline'):
        return value
    rows = [list(row[offset:offset + 5]) for row in value.get('buttons', [])
            for offset in range(0, len(row), 5)]
    if sum(map(len, rows)) > 10:
        raise ProviderError('Меню ВК содержит больше 10 кнопок. Разделите его на страницы.')
    while len(rows) > 6:
        candidates = [(len(rows[i]) + len(rows[i + 1]), i) for i in range(len(rows) - 1)
                      if len(rows[i]) + len(rows[i + 1]) <= 5]
        if not candidates:
            raise ProviderError('Меню ВК не помещается в 6 строк.')
        _, index = min(candidates)
        rows[index:index + 2] = [rows[index] + rows[index + 1]]
    return {**value, 'buttons': rows}


def trusted_vk_url(value):
    url = urlparse(value)
    return (url.scheme == 'https' and not url.username and not url.password
            and url.port in (None, 443) and url.hostname
            and any(url.hostname == host or url.hostname.endswith('.' + host)
                    for host in ('vk.com', 'vk.ru', 'userapi.com', 'vkuserphoto.ru', 'vkuserphoto.net')))


class VkMessages:
    def __init__(self, token, group_id, session):
        self.token, self.group_id, self.session = token, group_id, session
        self.message_store = None

    def bind_store(self, store):
        self.message_store = store

    def tracked_messages(self, peer):
        return self.message_store.bot_message_ids(self.group_id, peer) if self.message_store else []

    async def delete_messages(self, peer, message_ids):
        # Only IDs returned by our own sends in this conversation are eligible.
        known = set(self.tracked_messages(peer))
        selected = sorted(set(message_ids) & known)
        unconfirmed = False
        for offset in range(0, len(selected), 100):
            batch = selected[offset:offset + 100]
            result = await self.api('messages.delete', peer_id=peer,
                                    message_ids=','.join(map(str, batch)), delete_for_all=1)
            if isinstance(result, dict):
                deleted = [ident for ident in batch if result.get(str(ident)) == 1]
            elif isinstance(result, list):
                deleted = [item['message_id'] for item in result if isinstance(item, dict)
                           and item.get('peer_id', peer) == peer and item.get('message_id') in batch
                           and item.get('response') == 1 and not item.get('error')]
            else:
                deleted = []
            self.message_store.forget_bot_messages(self.group_id, peer, deleted)
            if set(deleted) != set(batch):
                unconfirmed = True
        if unconfirmed:
            raise ProviderError('ВК не подтвердил удаление части предыдущих сообщений (messages.delete).')

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
                    raise VkApiError(method, code)
                return result['response']
        except (aiohttp.ClientError, TimeoutError, KeyError, ValueError):
            raise ProviderError('Ошибка связи с API ВК. Секреты не выводятся.') from None

    async def long_poll_server(self):
        result = await self.api('groups.getLongPollServer', group_id=self.group_id)
        if not trusted_vk_url(result['server']):
            raise ProviderError('ВК вернул неожиданный адрес Long Poll.')
        return result

    async def enable_callback_events(self):
        current = await self.api('groups.getLongPollSettings', group_id=self.group_id)
        if not isinstance(current, dict) or not isinstance(current.get('events'), dict):
            raise ProviderError('Не удалось проверить настройки Long Poll для callback-кнопок.')
        events = current['events']
        version = current.get('api_version', '')
        try:
            supported = tuple(map(int, version.split('.'))) >= (5, 120)
        except (AttributeError, ValueError):
            supported = False
        if current.get('enabled') == 1 and events.get('message_new') == 1 and events.get('message_event') == 1 and supported:
            return
        # Preserve existing subscriptions when enabling the two events this bot consumes.
        params = {name: int(value) for name, value in events.items()
                  if isinstance(name, str) and re.fullmatch(r'[a-z][a-z_]*', name) and name not in ('group_id', 'enabled', 'api_version')
                  and type(value) in (int, bool) and value in (0, 1)}
        params.update(group_id=self.group_id, enabled=1, api_version=version if supported else '5.199',
                      message_new=1, message_event=1)
        result = await self.api('groups.setLongPollSettings', **params)
        if result != 1:
            raise ProviderError('ВК не подтвердил включение callback-кнопок в Long Poll.')

    async def answer_event(self, actor, peer, event_id):
        return await self.api('messages.sendMessageEventAnswer', event_id=event_id, user_id=actor, peer_id=peer)

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
        message_ids = []
        for index, chunk in enumerate(chunks):
            params = {'peer_id': peer, 'message': chunk, 'random_id': secrets.randbelow(2**31-1)+1}
            if index == len(chunks)-1:
                if keyboard:
                    params['keyboard'] = json.dumps(vk_keyboard(keyboard), ensure_ascii=False)
                if attachment:
                    params['attachment'] = attachment
            try:
                ident = await self.api('messages.send', **params)
            except VkApiError as error:
                if error.code != 912 or 'keyboard' not in params:
                    raise
                # VK explicitly rejected the keyboard; send a plain explanation.
                # Other errors and network failures must not cause a resend.
                params.pop('keyboard')
                params['message'] = (chunk + '\n\nВК отклонил кнопки (API 912). В управлении '
                    'сообществом откройте «Сообщения» → «Настройки для бота» и включите '
                    '«Возможности ботов». Затем отправьте /start заново. Команды: /help.')
                ident = await self.api('messages.send', **params)
            if type(ident) is int and ident > 0:
                message_ids.append(ident)
                if self.message_store:
                    self.message_store.remember_bot_message(self.group_id, peer, ident)
        return message_ids

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
            data.add_field('photo', image, filename=Path(path).name,
                           content_type=mimetypes.guess_type(path)[0] or 'image/jpeg')
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
        return await self.send(peer, caption or 'Изображение', attachment=attachment)
