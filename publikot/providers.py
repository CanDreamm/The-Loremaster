from pathlib import Path
import mimetypes

import aiohttp


class ProviderError(Exception):
    """Safe user-facing error; never includes request URLs or credentials."""


class AmbiguousPublish(ProviderError):
    pass


class PublishRejected(ProviderError):
    """VK explicitly rejected the operation; no post was created."""


class YandexText:
    def __init__(self, key, folder, demo):
        self.key, self.folder, self.demo = key, folder, demo

    async def generate(self, concept, brief, previous='', feedback='', *, prompt=None):
        if self.demo:
            return '[ДЕМО — без обращения к ИИ]\n' + brief + ('\nЗамечание: ' + feedback if feedback else '')
        if not self.key or not self.folder:
            raise ProviderError('Не настроены YANDEX_API_KEY и YANDEX_FOLDER_ID.')
        system = prompt if prompt is not None else (
            'Ты редактор публикаций ВК. По умолчанию пиши короткий пост до 500 символов, '
            'если в задании или правилах проекта не указан другой объём. '
            'Верни только текст поста без пояснений. Не выдумывай факты. Концепция проекта:\n' + concept)
        user = ('Подготовь готовый текст поста по инструкциям. Верни только текст публикации.'
                if prompt is not None else brief + ('\nПредыдущий вариант:\n' + previous +
                    '\nИсправления:\n' + feedback if previous else ''))
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as session:
                async with session.post('https://llm.api.cloud.yandex.net/foundationModels/v1/completion',
                    headers={'Authorization': 'Api-Key ' + self.key},
                    json={'modelUri': f'gpt://{self.folder}/yandexgpt-lite/latest',
                          'completionOptions': {'stream': False, 'temperature': 0.6, 'maxTokens': '1500'},
                          'messages': [{'role': 'system', 'text': system}, {'role': 'user', 'text': user}]}) as response:
                    if response.status != 200:
                        raise ProviderError(f'ИИ вернул HTTP {response.status}. Проверьте доступ и баланс.')
                    result = await response.json()
                    body = result['result']['alternatives'][0]['message']['text'].strip()
                    if not body or len(body) > 15000:
                        raise ProviderError('ИИ вернул пустой или слишком длинный текст.')
                    return body
        except (aiohttp.ClientError, TimeoutError, KeyError, IndexError, ValueError):
            raise ProviderError('Не удалось получить текст от ИИ. Проверьте настройки и доступность сервиса.') from None


class VkPublisher:
    def __init__(self, token, session_factory=None):
        self.token = token
        self.session_factory = session_factory or aiohttp.ClientSession

    async def _call(self, session, method, **params):
        async with session.post('https://api.vk.com/method/' + method,
                                data={**params, 'access_token': self.token, 'v': '5.199'}) as response:
            if response.status != 200:
                raise ProviderError(f'ВК вернул HTTP {response.status}.')
            payload = await response.json()
            if 'error' in payload:
                code = payload['error'].get('error_code', '?')
                code = code if type(code) is int else 'неизвестен'
                raise PublishRejected(f'ВК отклонил {method}, код API {code}. Проверьте права ключа и сообщества.')
            return payload['response']

    async def publish(self, group, body, image_path, ident):
        if not self.token:
            raise ProviderError('Не задан VK_ACCESS_TOKEN.')
        from .media import paths
        images = paths(image_path)
        if any(not Path(path).is_file() for path in images):
            raise ProviderError('Фото публикации не найдено.')
        wall_started = False
        try:
            async with self.session_factory(timeout=aiohttp.ClientTimeout(total=90)) as session:
                attachments = []
                for image_path in images:
                    server = await self._call(session, 'photos.getWallUploadServer', group_id=group)
                    # Загружаем файл только на HTTPS-домен ВК; токен сюда не передаём.
                    from urllib.parse import urlparse
                    url = urlparse(server['upload_url'])
                    if url.scheme != 'https' or not url.hostname or not (url.hostname.endswith('.vk.com') or url.hostname.endswith('.vkuserphoto.ru') or url.hostname.endswith('.userapi.com')):
                        raise ProviderError('ВК вернул неожиданный адрес загрузки. Отправка остановлена.')
                    with Path(image_path).open('rb') as photo:
                        form = aiohttp.FormData()
                        form.add_field('photo', photo, filename=Path(image_path).name,
                                       content_type=mimetypes.guess_type(image_path)[0] or 'image/jpeg')
                        async with session.post(server['upload_url'], data=form, allow_redirects=False) as response:
                            if response.status != 200:
                                raise ProviderError('Не удалось загрузить фото в ВК.')
                            uploaded = await response.json(content_type=None)
                    saved = await self._call(session, 'photos.saveWallPhoto', group_id=group,
                                             photo=uploaded['photo'], server=uploaded['server'], hash=uploaded['hash'])
                    attachments.append(f"photo{saved[0]['owner_id']}_{saved[0]['id']}")
                wall_started = True
                result = await self._call(session, 'wall.post', owner_id=-group, from_group=1,
                                          message=body, attachments=','.join(attachments), guid=str(ident))
                return f"https://vk.com/wall-{group}_{result['post_id']}"
        except PublishRejected:
            raise
        except ProviderError:
            if wall_started:
                raise AmbiguousPublish('Результат публикации не подтверждён. Проверьте стену ВК; автоматического повтора не будет.') from None
            raise
        except (aiohttp.ClientError, TimeoutError, KeyError, IndexError, ValueError, OSError):
            if wall_started:
                raise AmbiguousPublish('Связь прервалась при публикации. Проверьте стену ВК; автоматического повтора не будет.') from None
            raise ProviderError('Не удалось подготовить публикацию ВК. Пост не отправлен.') from None
