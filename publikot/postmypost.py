"""Small Postmypost v4.1 client; no writes are automatically repeated."""
import asyncio
import mimetypes
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from .providers import AmbiguousPublish, ProviderError

BASE = 'https://api.postmypost.io/v4.1'


class RequestBudget:
    def __init__(self):
        self.calls = defaultdict(deque)
        self.lock = asyncio.Lock()

    async def acquire(self, actor, path, method):
        category, limit = ('upload', 10) if path.startswith('/upload/') else (
            ('create', 10) if path == '/publications' and method == 'POST' else ('read', 30))
        while True:
            async with self.lock:
                now = time.monotonic()
                history = self.calls[actor, category]
                while history and now - history[0] >= 60:
                    history.popleft()
                if len(history) < limit:
                    history.append(now)
                    return
                delay = max(.05, 60 - (now - history[0]))
            await asyncio.sleep(min(delay, 10))


class Postmypost:
    def __init__(self, token, session, *, actor=0, budget=None):
        self.token, self.session, self.actor = token, session, actor
        self.budget = budget or RequestBudget()

    async def request(self, method, path, *, params=None, json=None):
        if not path.startswith('/') or '?' in path or '://' in path:
            raise ValueError('Invalid API path')
        await self.budget.acquire(self.actor, path, method)
        creating = method == 'POST' and path == '/publications'
        try:
            async with self.session.request(method, BASE + path, params=params, json=json,
                    headers={'Authorization': 'Bearer ' + self.token}, allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=60)) as response:
                if response.status >= 500 and creating:
                    raise AmbiguousPublish('Postmypost: результат создания неизвестен. Автоматического повтора не будет.')
                if not 200 <= response.status < 300:
                    hint = {401: 'Личный токен недействителен.', 403: 'Проверьте тариф API и права.',
                            429: 'Превышен лимит запросов; повторите позже.'}.get(response.status, 'Запрос отклонён.')
                    raise ProviderError(f'Postmypost: HTTP {response.status}. {hint}')
                result = await response.json()
                if not isinstance(result, (dict, list)):
                    raise ValueError
                return result
        except (aiohttp.ClientError, TimeoutError, ValueError):
            if creating:
                raise AmbiguousPublish('Postmypost: результат создания неизвестен. Проверьте кабинет; повтора не будет.') from None
            raise ProviderError('Postmypost: ошибка связи или формата ответа. Секреты не выводятся.') from None

    async def list_all(self, path, **params):
        result = []
        for page in range(1, 201):
            response = await self.request('GET', path, params={**params, 'page': page, 'per_page': 50})
            items = response if isinstance(response, list) else response.get('data')
            if not isinstance(items, list) or any(not isinstance(i, dict) for i in items):
                raise ProviderError('Postmypost: некорректный список площадок или проектов.')
            result.extend(items)
            pages = response.get('pages', {}) if isinstance(response, dict) else {}
            total = pages.get('total_pages') if isinstance(pages, dict) else None
            if not items or (type(total) is int and page >= total) or (total is None and len(items) < 50):
                return result
        raise ProviderError('Postmypost: слишком много страниц. Список не загружен полностью.')

    async def projects(self):
        return await self.list_all('/projects')

    async def accounts(self, project):
        return await self.list_all('/accounts', project_id=int(project))

    async def channels(self):
        return await self.list_all('/channels')

    async def check_account(self, target):
        rows = await self.accounts(target['remote_project'])
        selected = next((r for r in rows if str(r.get('id')) == str(target['remote_account'])), None)
        if not selected or selected.get('connection_status') != 1:
            raise ProviderError('Postmypost: площадка отключена или требует повторной авторизации.')

    async def upload(self, project, path):
        image = Path(path)
        if not image.is_file():
            raise ProviderError('Готовая фотография не найдена. Пост не отправлен.')
        init = await self.request('POST', '/upload/init', json={
            'project_id': int(project), 'name': image.name, 'size': image.stat().st_size})
        ident = init.get('id')
        action = init.get('action', '')
        try:
            url = urlsplit(action)
            safe = (url.scheme == 'https' and not url.username and not url.password and url.port in (None, 443)
                    and url.hostname and (url.hostname.endswith('.amazonaws.com') or url.hostname.endswith('.postmypost.io')))
        except ValueError:
            safe = False
        if not ident or not safe:
            raise ProviderError('Postmypost: неверный адрес загрузки. Файл не отправлен.')
        fields = init.get('fields')
        if not isinstance(fields, list) or any(not isinstance(f, dict) or set(('name','value')) - f.keys() for f in fields):
            raise ProviderError('Postmypost: неверные параметры загрузки.')
        form = aiohttp.FormData()
        for field in fields:
            form.add_field(str(field['name']), str(field['value']))
        try:
            with image.open('rb') as handle:
                form.add_field('file', handle, filename=image.name,
                               content_type=mimetypes.guess_type(image.name)[0] or 'application/octet-stream')
                # No API Authorization header is ever sent to object storage.
                async with self.session.post(action, data=form, allow_redirects=False,
                                             timeout=aiohttp.ClientTimeout(total=120)) as response:
                    if not 200 <= response.status < 300:
                        raise ProviderError('Postmypost: фотография не загружена. Пост не отправлен.')
        except (aiohttp.ClientError, TimeoutError, OSError):
            raise ProviderError('Postmypost: загрузка фотографии прервана. Пост не отправлен.') from None
        await self.request('POST', '/upload/complete', params={'id': ident})
        for _ in range(15):
            result = await self.request('GET', '/upload/status', params={'id': ident})
            if result.get('status') == 1 and type(result.get('file_id')) is int:
                return result['file_id']
            if result.get('status') == 2:
                break
            await asyncio.sleep(2)
        raise ProviderError('Postmypost: фото не готово к публикации. Пост не отправлен.')

    async def create(self, target, body, image_path):
        from .media import paths
        images = paths(image_path)
        files = [await self.upload(target['remote_project'], path) for path in images]
        return await self.request('POST', '/publications', json={
            'project_id': int(target['remote_project']), 'account_ids': [int(target['remote_account'])],
            'post_at': datetime.now(timezone.utc).isoformat(), 'publication_status': 5,
            'details': [{'account_id': int(target['remote_account']), 'publication_type': 'post',
                         'content': body, 'file_ids': files}]})

    async def status(self, remote_id):
        return await self.request('GET', '/publications/' + str(int(remote_id)))
