"""Experimental direct editor-api adapter. Uses an existing authorized Dzen session."""
import json
import mimetypes
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from .providers import AmbiguousPublish, ProviderError

BASE = 'https://dzen.ru/editor-api/v2/'


def validate_auth(auth):
    if not isinstance(auth, dict):
        raise ProviderError('Нужен JSON с данными сеанса Дзена.')
    publisher = auth.get('publisher_id', '')
    if not isinstance(publisher, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}', publisher):
        raise ProviderError('Проверьте publisher_id канала Дзена.')
    if not isinstance(auth.get('cookies'), dict) or not auth['cookies']:
        raise ProviderError('В сеансе Дзена отсутствуют cookie.')
    for key, value in auth['cookies'].items():
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', key) or not isinstance(value, str) or any(c in value for c in '\r\n;'):
            raise ProviderError('Некорректный формат cookie Дзена.')
    for key in ('csrf_token', 'fp_token', 'user_agent'):
        value = auth.get(key, '')
        if not isinstance(value, str) or '\r' in value or '\n' in value:
            raise ProviderError('Некорректные заголовки сеанса Дзена.')
    if not auth.get('csrf_token'):
        raise ProviderError('В сеансе Дзена отсутствует csrf_token.')
    return auth


def public_url(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ('publicUrl', 'public_url', 'url', 'publicationUrl') and isinstance(item, str):
                parsed = urlsplit(item)
                if (parsed.scheme == 'https' and parsed.hostname == 'dzen.ru' and not parsed.username
                        and not parsed.password and re.fullmatch(r'/(?:a|b)/[A-Za-z0-9_-]+', parsed.path)):
                    return item
            found = public_url(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = public_url(item)
            if found:
                return found
    return ''


class DzenAPI:
    def __init__(self, auth, session):
        self.auth = validate_auth(auth)
        self.session = session
        self.publisher = auth['publisher_id']
        self.headers = {'Origin': 'https://dzen.ru', 'X-Csrf-Token': auth['csrf_token'],
                        'Referer': f'https://dzen.ru/profile/editor/id/{self.publisher}/publications',
                        'Cookie': '; '.join(k + '=' + v for k, v in auth['cookies'].items())}
        if auth.get('fp_token'):
            self.headers['X-FP-Token'] = auth['fp_token']
        if auth.get('user_agent'):
            self.headers['User-Agent'] = auth['user_agent']

    async def request(self, operation, *, json=None, data=None, article=None, final=False):
        params = {'publisherId': self.publisher, 'clientRid': uuid.uuid4().hex[:14]}
        if article:
            params['publicationId'] = article
        headers = dict(self.headers)
        article_id = article or (json or {}).get('id')
        if article_id:
            headers['Referer'] = f'https://dzen.ru/profile/editor/id/{self.publisher}/{article_id}/edit'
        try:
            async with self.session.post(BASE + operation, params=params, json=json, data=data,
                    headers=headers, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=90)) as response:
                if response.status >= 500 and final:
                    raise AmbiguousPublish('Дзен: результат публикации неизвестен. Проверьте канал вручную.')
                if not 200 <= response.status < 300:
                    raise ProviderError(f'Дзен: HTTP {response.status}. Проверьте сеанс, права и совместимость editor API.')
                result = await response.json()
                if not isinstance(result, dict) or result.get('error') or result.get('errors'):
                    if final:
                        raise AmbiguousPublish('Дзен не подтвердил публикацию. Проверьте канал вручную.')
                    raise ProviderError('Дзен отклонил подготовку статьи. Публикация не отправлена.')
                return result
        except (aiohttp.ClientError, TimeoutError, ValueError):
            if final:
                raise AmbiguousPublish('Дзен: результат публикации неизвестен. Автоматического повтора не будет.') from None
            raise ProviderError('Дзен: не удалось подготовить статью. Проверьте сеанс или изменения editor API.') from None

    async def publish(self, target, body, image_path, remember_draft):
        if target['remote_account'] != self.publisher:
            raise ProviderError('Сеанс Дзена принадлежит другому каналу.')
        if not body.strip():
            raise ProviderError('Нельзя отправить пустую статью.')
        if image_path and not Path(image_path).is_file():
            raise ProviderError('Фотография не найдена; статья не отправлена.')
        created = await self.request('add-publication', json={
            'title': '', 'publisherId': self.publisher, 'publicationType': 'article'})
        nested = created.get('data') if isinstance(created.get('data'), dict) else {}
        ident = created.get('publicationId') or created.get('id') or nested.get('id')
        if not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', ident):
            raise ProviderError('Дзен не вернул ID черновика. Финальная отправка не выполнялась.')
        remember_draft(ident)
        image_id = None
        if image_path:
            image = Path(image_path)
            form = aiohttp.FormData()
            with image.open('rb') as handle:
                form.add_field('image', handle, filename=image.name,
                               content_type=mimetypes.guess_type(image.name)[0] or 'image/jpeg')
                uploaded = await self.request('add-image', data=form, article=ident)
            image_id = uploaded.get('id')
            if not isinstance(image_id, str) or not image_id:
                raise ProviderError('Дзен не подтвердил загрузку фото; статья не отправлена.')
        title = body.strip().split('\n')[0][:140]
        blocks = []
        if image_id:
            blocks.append({'key': uuid.uuid4().hex[:5], 'text': '', 'type': 'atomic:image',
                           'depth': 0, 'inlineStyleRanges': [], 'entityRanges': [], 'data': {'image': {'id': image_id}}})
        # Keep all approved text; never interpret it as HTML or silently omit a long title.
        blocks.append({'key': uuid.uuid4().hex[:5], 'text': body, 'type': 'unstyled',
                       'depth': 0, 'inlineStyleRanges': [], 'entityRanges': [], 'data': {}})
        preview = {'title': title, 'snippet': '', 'galleryPreviewImages': []}
        if image_id:
            preview['image'] = {'id': image_id}
        payload = {'id': ident, 'preview': preview, 'snippetFrozen': False, 'hasNativeAds': False,
                   'commentsFlagState': 'on', 'delayedPublicationFlagState': 'off',
                   'visibleComments': 'subscribe-visible', 'visibilityType': 'all',
                   'premiumTariffs': [], 'customCommentsTitle': '',
                   'articleContent': {'contentState': json.dumps({'blocks': blocks, 'entityMap': {}}, ensure_ascii=False)},
                   'tagsInput': {'tags': [], 'detectedTagsShown': False}}
        await self.request('update-publication-content', json=payload)
        result = await self.request('update-publication-content-and-publish',
                                    json={**payload, 'darkPost': False, 'pixelsUpdate': {'active': False, 'pixels': {}}},
                                    final=True)
        url = public_url(result)
        if not url:
            raise AmbiguousPublish('Дзен принял запрос, но не вернул публичную ссылку. Проверьте канал; повтора не будет.')
        return ident, url
