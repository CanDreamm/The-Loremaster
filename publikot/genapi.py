"""GenAPI native async API. Paid POSTs are never retried automatically."""
import asyncio
import base64
import ipaddress
import json
import os
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from aiohttp.resolver import DefaultResolver

from .providers import ProviderError

BASE = 'https://api.gen-api.ru/api/v1'
ROLES = {'style': 'Референс визуального стиля', 'character': 'Референс персонажей',
         'reference': 'Референс из задания'}
FORMATS = {'square': (1024, 1024), 'landscape': (1536, 1024), 'portrait': (1024, 1536)}


def truncated(value):
    if isinstance(value, dict):
        return value.get('finish_reason') in ('length', 'max_tokens') or any(truncated(v) for v in value.values())
    return isinstance(value, list) and any(truncated(v) for v in value)


def image_type(data):
    if data.startswith(b'\xff\xd8\xff'):
        return 'jpeg'
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png'
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return 'webp'
    raise ProviderError('Нужна фотография JPEG, PNG или WebP.')


def image_uri(path):
    try:
        file = Path(path)
        if not file.is_file() or file.stat().st_size > 10 * 1024 * 1024:
            raise ProviderError('Референс отсутствует или превышает 10 МБ.')
        data = file.read_bytes()
        return 'data:image/' + image_type(data) + ';base64,' + base64.b64encode(data).decode('ascii')
    except OSError:
        raise ProviderError('Не удалось прочитать референс проекта.') from None


class PublicResolver(DefaultResolver):
    async def resolve(self, host, port=0, family=0):
        records = await super().resolve(host, port, family)
        proxies = {urlsplit(os.getenv(name, '')).hostname for name in ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy')}
        if host not in proxies and any(not ipaddress.ip_address(r['host']).is_global for r in records):
            raise OSError('Non-public media host')
        return records


class GenAPI:
    def __init__(self, settings, store, *, session_factory=None, poll_interval=3, poll_attempts=60):
        self.settings, self.store = settings, store
        self.session_factory = session_factory or aiohttp.ClientSession
        self.poll_interval, self.poll_attempts = poll_interval, poll_attempts

    def check_ready(self):
        if not self.settings.genapi_key:
            raise ProviderError('Добавьте GENAPI_API_KEY в .env.python на сервере и перезапустите бота.')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', self.settings.genapi_text_model):
            raise ProviderError('Проверьте GENAPI_TEXT_MODEL.')
        if self.settings.genapi_image_model not in ('seedream-v5-lite', 'nano-banana-lite'):
            raise ProviderError('GENAPI_IMAGE_MODEL: seedream-v5-lite или nano-banana-lite.')

    async def request(self, session, method, path, payload=None):
        try:
            async with session.request(method, BASE + path, json=payload,
                    headers={'Authorization': 'Bearer ' + self.settings.genapi_key},
                    allow_redirects=False, timeout=aiohttp.ClientTimeout(total=90)) as response:
                if not 200 <= response.status < 300:
                    hint = {401: 'Недействительный ключ.', 402: 'Проверьте баланс.',
                            403: 'Проверьте доступ.', 422: 'Проверьте параметры модели.',
                            429: 'Превышен лимит запросов.'}.get(response.status, 'Сервис недоступен.')
                    raise ProviderError(f'GenAPI: HTTP {response.status}. {hint} Автоматического повтора нет.')
                result = await response.json()
                if not isinstance(result, dict):
                    raise ValueError
                return result
        except (aiohttp.ClientError, TimeoutError, ValueError):
            raise ProviderError('GenAPI: ответ не получен. Платный запрос не повторяется. '
                                'Если ID сохранён, можно проверить результат через бота.') from None

    async def run(self, actor, job, revision, kind, slot, model, payload):
        self.check_ready()
        if not self.store.is_allowed(actor, self.settings.owner_id):
            raise ProviderError('Доступ к боту отозван.')
        self.store.require(actor, self.store.job(actor, job)['project_id'])
        key = (job, revision, kind, slot)
        row = self.store.db.execute('SELECT * FROM ai_requests WHERE job_id=? AND revision=? AND kind=? AND slot=?', key).fetchone()
        if row and row['status'] == 'success':
            return json.loads(row['result'])
        if row and not row['request_id']:
            raise ProviderError('GenAPI: ID предыдущего запроса не получен. Проверьте историю в кабинете. '
                                'Повторная платная генерация автоматически не выполняется.')
        if row and row['status'] == 'error':
            raise ProviderError('GenAPI завершил этот запрос с ошибкой. Проверьте кабинет; создайте новое задание для новой попытки.')
        async with self.session_factory(trust_env=True) as session:
            if not row:
                # Persist intent BEFORE the external write: crash/timeout cannot resubmit it.
                self.store.db.execute('INSERT INTO ai_requests(job_id,revision,kind,slot,model,status) VALUES (?,?,?,?,?,?)',
                                      (*key, model, 'submitting'))
                result = await self.request(session, 'POST', '/networks/' + model, {**payload, 'is_sync': False})
                ident = result.get('request_id')
                if type(ident) is not int or ident <= 0:
                    raise ProviderError('GenAPI не вернул ID запроса. Проверьте кабинет; платного повтора не будет.')
                self.store.db.execute("UPDATE ai_requests SET request_id=?,status='processing' WHERE job_id=? AND revision=? AND kind=? AND slot=?", (str(ident), *key))
            else:
                ident = row['request_id']
            for attempt in range(self.poll_attempts):
                if not self.store.is_allowed(actor, self.settings.owner_id):
                    raise ProviderError('Доступ к боту отозван.')
                self.store.require(actor, self.store.job(actor, job)['project_id'])
                result = await self.request(session, 'GET', '/request/get/' + str(ident))
                if result.get('status') == 'error':
                    self.store.db.execute("UPDATE ai_requests SET status='error' WHERE job_id=? AND revision=? AND kind=? AND slot=?", key)
                    raise ProviderError(f'GenAPI: генерация №{ident} завершилась с ошибкой. Подробности — в кабинете.')
                if result.get('status') == 'success':
                    if not isinstance(result.get('result'), list) or not result['result']:
                        raise ProviderError(f'GenAPI: запрос №{ident} вернул пустой результат.')
                    if truncated(result.get('full_response')):
                        self.store.db.execute("UPDATE ai_requests SET status='error' WHERE job_id=? AND revision=? AND kind=? AND slot=?", key)
                        raise ProviderError('GenAPI: ответ обрезан лимитом токенов. Сократите объём статьи; публикация остановлена.')
                    saved = {'result': result['result'], 'cost': result.get('cost')}
                    self.store.db.execute("UPDATE ai_requests SET status='success',result=? WHERE job_id=? AND revision=? AND kind=? AND slot=?", (json.dumps(saved), *key))
                    return saved
                if result.get('status') != 'processing':
                    raise ProviderError(f'GenAPI: неизвестный статус запроса №{ident}. Отправка не повторяется.')
                if attempt + 1 < self.poll_attempts:
                    await asyncio.sleep(self.poll_interval)
        raise ProviderError(f'GenAPI: запрос №{ident} ещё не готов. Проверьте результат кнопкой в боте; повторной оплаты запроса не будет.')

    async def generate(self, concept, brief, previous='', feedback='', *, prompt=None, materials=(), request_key):
        content = [{'type': 'text', 'text': 'Подготовь готовую публикацию по инструкциям. Верни только её текст.'}]
        for material in materials:
            content.extend([{'type': 'text', 'text': ROLES[material['role']]},
                            {'type': 'image_url', 'image_url': {'url': image_uri(material['path'])}}])
        payload = {'model': self.settings.genapi_text_model, 'messages': [
            {'role': 'system', 'content': prompt or concept + '\n' + brief},
            {'role': 'user', 'content': content}], 'max_tokens': 10000, 'reasoning_effort': 'low'}
        result = await self.run(*request_key, 'text', 0, self.settings.genapi_text_model, payload)
        texts = []
        for item in result['result']:
            if isinstance(item, str):
                texts.append(item)
            elif isinstance(item, dict):
                message = item.get('message', item)
                if isinstance(message, dict) and isinstance(message.get('content'), str):
                    texts.append(message['content'])
        body = '\n'.join(texts).strip()
        if not body or len(body) > 30000:
            raise ProviderError('GenAPI: пустой текст или более 30000 символов. Уточните объём задания.')
        return body

    async def generate_image(self, actor, job, revision, slot, prompt, materials, image_format):
        model = self.settings.genapi_image_model
        payload = {'prompt': prompt, 'image_urls': [image_uri(m['path']) for m in materials], 'num_images': 1}
        if model == 'seedream-v5-lite':
            width, height = FORMATS[image_format]
            payload.update(width=width, height=height, max_images=1, translate_input=False)
        else:
            payload.update(aspect_ratio={'square': '1:1', 'landscape': '3:2', 'portrait': '2:3'}[image_format], output_format='png')
        result = await self.run(actor, job, revision, 'image', slot, model, payload)
        if len(result['result']) != 1 or not isinstance(result['result'][0], str):
            raise ProviderError('GenAPI вернул неожиданное количество изображений. Проверьте кабинет.')
        row = self.store.db.execute("SELECT local_path FROM ai_requests WHERE job_id=? AND revision=? AND kind='image' AND slot=?", (job, revision, slot)).fetchone()
        if row['local_path'] and Path(row['local_path']).is_file():
            return row['local_path']
        path = await self.download(result['result'][0])
        self.store.db.execute("UPDATE ai_requests SET local_path=? WHERE job_id=? AND revision=? AND kind='image' AND slot=?", (path, job, revision, slot))
        return path

    async def download(self, url):
        try:
            parsed = urlsplit(url)
            if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                    or parsed.port not in (None, 443) or parsed.hostname.lower() == 'localhost'):
                raise ValueError
            try:
                address = ipaddress.ip_address(parsed.hostname)
            except ValueError:
                address = None
            if address is not None and not address.is_global:
                raise ProviderError('GenAPI вернул недопустимый адрес изображения.')
            folder = self.settings.data_dir / 'generated'
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            # No bearer header is ever sent to media/CDN servers. Never follow redirects.
            async with self.session_factory(trust_env=True, connector=aiohttp.TCPConnector(resolver=PublicResolver())) as session:
                async with session.get(url, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=90)) as response:
                    if response.status != 200:
                        raise ProviderError('Не удалось скачать изображение GenAPI. Повторите проверку результата.')
                    data = bytearray()
                    async for block in response.content.iter_chunked(65536):
                        data.extend(block)
                        if len(data) > 10 * 1024 * 1024:
                            raise ProviderError('Изображение GenAPI превышает 10 МБ.')
            suffix = image_type(data)
            path = folder / (uuid.uuid4().hex + '.' + suffix)
            path.write_bytes(data)
            path.chmod(0o600)
            return str(path)
        except (aiohttp.ClientError, TimeoutError, ValueError, OSError):
            raise ProviderError('Не удалось сохранить изображение GenAPI. Платная генерация не повторяется.') from None
