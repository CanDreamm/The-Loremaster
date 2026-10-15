"""Project-scoped VK authorization. No tokens are accepted via bot messages."""
import base64
import hashlib
import os
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

import aiohttp
from aiohttp import web
from cryptography.fernet import Fernet, InvalidToken

from .providers import ProviderError
from .settings import ConfigError
from .store import Conflict
from .menus import button, keyboard

SECURE_HEADERS = {
    'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
    'X-Publikot-OAuth': '1',
    'Content-Security-Policy': "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
    'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY',
}


class Vault:
    def __init__(self, path, may_create=True):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if not path.exists():
            if not may_create:
                raise ConfigError('Ключ шифрования OAuth утрачен. Восстановите data/oauth.key из резервной копии.')
            try:
                descriptor = os.open(path, flags, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(descriptor, 'wb') as output:
                    output.write(Fernet.generate_key())
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise ConfigError('data/oauth.key должен быть обычным файлом с правами 600.')
        try:
            self.cipher = Fernet(path.read_bytes())
        except (ValueError, OSError):
            raise ConfigError('Не удалось открыть ключ шифрования OAuth.') from None

    def encrypt(self, value):
        return self.cipher.encrypt(value.encode()).decode()

    def decrypt(self, value):
        try:
            return self.cipher.decrypt(value.encode()).decode()
        except (InvalidToken, ValueError, UnicodeError):
            raise ConfigError('Ключ OAuth не удалось расшифровать. Проверьте data/oauth.key.') from None


class VkOAuthProvider:
    def __init__(self, settings, session):
        self.settings, self.session = settings, session

    async def exchange(self, code, verifier, device_id, state):
        config = self.settings
        if config.oauth_provider == 'vk_implicit':
            raise ProviderError('Implicit Flow возвращает ключ в браузер, а не код для обмена.')
        data = {'client_id': config.oauth_client_id, 'redirect_uri': config.oauth_redirect,
                'code': code, 'grant_type': 'authorization_code'}
        if config.oauth_provider == 'vkid':
            if not device_id:
                raise ProviderError('VK ID не передал device_id. Повторите подключение через /connect.')
            endpoint = 'https://id.vk.com/oauth2/auth'
            data.update(code_verifier=verifier, device_id=device_id, state=state)
        else:
            endpoint = 'https://oauth.vk.com/access_token'
            data['client_secret'] = config.oauth_client_secret
        try:
            async with self.session.post(endpoint, data=data, allow_redirects=False,
                                         timeout=aiohttp.ClientTimeout(total=30)) as response:
                result = await response.json(content_type=None)
                if response.status != 200 or result.get('error') or not result.get('access_token'):
                    raise ProviderError('ВК не выдал ключ. Проверьте приложение, redirect URI и разрешения API.')
                return result
        except (aiohttp.ClientError, TimeoutError, ValueError):
            raise ProviderError('Не удалось завершить авторизацию ВК. Повторите /connect.') from None

    async def api(self, token, method, **params):
        safe_method = method if re.fullmatch(r'[A-Za-z]+\.[A-Za-z]+', method) else 'API'
        try:
            async with self.session.post('https://api.vk.com/method/' + method,
                data={**params, 'access_token': token, 'v': '5.199'}, allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status != 200:
                    raise ProviderError(f'Проверка подключения ВК: {safe_method}, HTTP {response.status}.')
                result = await response.json()
                if not isinstance(result, dict):
                    raise ProviderError(f'Проверка подключения ВК: {safe_method}, некорректный формат ответа.')
                if result.get('error'):
                    error = result['error']
                    code = error.get('error_code') if isinstance(error, dict) else None
                    code = code if type(code) is int else 'неизвестен'
                    raise ProviderError(f'Проверка подключения ВК: {safe_method}, код API {code}. '
                                        'ВК отклонил проверку пользовательского ключа. Подключение не сохранено.')
                if 'response' not in result:
                    raise ProviderError(f'Проверка подключения ВК: {safe_method}, ответ API отсутствует.')
                return result['response']
        except (aiohttp.ClientError, TimeoutError, ValueError):
            raise ProviderError(f'Не удалось выполнить проверку ВК: {safe_method}. Сетевая ошибка или некорректный JSON.') from None

    async def validate(self, token, actor, group, authorization=None):
        if self.settings.oauth_provider == 'vkid':
            # VK ID returns user_id and a space-separated scope in its token
            # response. Do not use legacy account.* calls to inspect this token.
            if not isinstance(authorization, dict) or authorization.get('access_token') != token:
                raise ProviderError('VK ID: нет подтверждённого ответа обмена кода. Повторите подключение.')
            user_id = authorization.get('user_id')
            if type(user_id) is int:
                identity = user_id
            elif isinstance(user_id, str) and user_id.isascii() and user_id.isdecimal():
                identity = int(user_id)
            else:
                raise ProviderError('VK ID не вернул корректный user_id. Подключение не сохранено.')
            if identity != actor:
                raise ProviderError('В браузере открыт другой аккаунт ВК. Войдите тем пользователем, который начал подключение.')
            scope = authorization.get('scope')
            if not isinstance(scope, str):
                raise ProviderError('VK ID не вернул список выданных разрешений scope. Подключение не сохранено.')
            granted = set(scope.split())
            if not {'wall', 'photos'} <= granted:
                raise ProviderError('VK ID не выдал права публикации: '
                    f'wall — {"да" if "wall" in granted else "нет"}, photos — {"да" if "photos" in granted else "нет"}. '
                    'Нужен доступ приложения к этим API; вход в аккаунт сам по себе его не даёт.')
        else:
            profile = await self.api(token, 'account.getProfileInfo')
            if profile.get('id') != actor:
                raise ProviderError('В браузере открыт другой аккаунт ВК. Войдите тем пользователем, который начал подключение.')
            permissions = await self.api(token, 'account.getAppPermissions')
            if not isinstance(permissions, int) or permissions & (8192 | 4) != (8192 | 4):
                raise ProviderError('Приложение не выдало wall и photos. Вход VK ID сам по себе не разрешает публикации.')
        groups = await self.api(token, 'groups.getById', group_id=group)
        if isinstance(groups, dict):
            groups = groups.get('groups', [])
        if not isinstance(groups, list) or len(groups) != 1:
            raise ProviderError('Не удалось проверить сообщество.')
        selected = groups[0]
        if selected.get('id') != group or not selected.get('is_admin') or selected.get('admin_level', 0) not in (2, 3):
            raise ProviderError('Для публикации нужны права редактора или администратора выбранного сообщества.')


class OAuthManager:
    def __init__(self, settings, store, provider, now=time.time):
        self.settings, self.store, self.provider, self.now = settings, store, provider, now
        self.db = store.db
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS oauth_states (
          state_hash TEXT PRIMARY KEY, actor INTEGER NOT NULL,
          project_id INTEGER NOT NULL REFERENCES projects(id), group_id INTEGER NOT NULL,
          expires_at INTEGER NOT NULL, verifier TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending'
        );
        CREATE TABLE IF NOT EXISTS vk_connections (
          project_id INTEGER PRIMARY KEY REFERENCES projects(id), group_id INTEGER NOT NULL,
          actor INTEGER NOT NULL, token TEXT NOT NULL, expires_at INTEGER NOT NULL,
          status TEXT NOT NULL DEFAULT 'active'
        );
        ''')
        state_columns = {row['name'] for row in self.db.execute('PRAGMA table_info(oauth_states)')}
        if 'flow' not in state_columns:
            self.db.execute("ALTER TABLE oauth_states ADD COLUMN flow TEXT NOT NULL DEFAULT 'code'")
        existing = self.db.execute("SELECT 1 FROM vk_connections WHERE token<>'' LIMIT 1").fetchone()
        self.vault = Vault(settings.data_dir / 'oauth.key', may_create=not existing)

    def guard(self, actor, project_id, group=None):
        if not self.store.is_allowed(actor, self.settings.owner_id):
            raise Conflict('Доступ к боту отозван.')
        project = self.store.require(actor, project_id)
        if not project['vk_group'] or group is not None and project['vk_group'] != group:
            raise Conflict('Сообщество проекта не задано или изменилось. Выполните /vk ID и повторите /connect.')
        return project

    def begin(self, actor, project_id):
        project = self.guard(actor, project_id)
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        digest = hashlib.sha256(state.encode()).hexdigest()
        expiry = int(self.now()) + 600
        with self.store.transaction():
            self.db.execute("UPDATE oauth_states SET status='failed',verifier='' WHERE actor=? AND project_id=? AND status IN ('pending','processing')", (actor, project_id))
            implicit = self.settings.oauth_provider == 'vk_implicit'
            self.db.execute('INSERT INTO oauth_states(state_hash,actor,project_id,group_id,expires_at,verifier,flow) VALUES (?,?,?,?,?,?,?)',
                (digest, actor, project_id, project['vk_group'], expiry,
                 '' if implicit else self.vault.encrypt(verifier), 'implicit' if implicit else 'code'))
        params = {'client_id': self.settings.oauth_client_id, 'redirect_uri': self.settings.oauth_redirect,
                  'response_type': 'code', 'state': state}
        if self.settings.oauth_provider == 'vkid':
            params.update(scope='wall photos', code_challenge_method='S256',
                          code_challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode())
            endpoint = 'https://id.vk.com/authorize'
        else:
            params.update(scope='wall,photos', v='5.199', display='page')
            if self.settings.oauth_provider == 'vk_implicit':
                params.update(response_type='token', scope='wall,photos,groups,offline', revoke='1')
            endpoint = 'https://oauth.vk.com/authorize'
        return endpoint + '?' + urlencode(params)

    def claim_state(self, state):
        if not isinstance(state, str) or not 32 <= len(state) <= 128:
            raise Conflict('Ссылка подключения недействительна.')
        digest = hashlib.sha256(state.encode()).hexdigest()
        with self.store.transaction():
            row = self.db.execute('SELECT * FROM oauth_states WHERE state_hash=?', (digest,)).fetchone()
            if not row or row['status'] != 'pending' or row['expires_at'] <= self.now():
                raise Conflict('Ссылка истекла или уже использована. Повторите /connect в боте.')
            expected_flow = 'implicit' if self.settings.oauth_provider == 'vk_implicit' else 'code'
            if row['flow'] != expected_flow:
                raise Conflict('Способ авторизации изменился. Запросите новую ссылку в боте.')
            self.guard(row['actor'], row['project_id'], row['group_id'])
            self.db.execute("UPDATE oauth_states SET status='processing' WHERE state_hash=?", (digest,))
        return dict(row)

    async def complete(self, state, code, device_id='', denied=False):
        if self.settings.oauth_provider == 'vk_implicit':
            raise Conflict('Ожидался ключ Implicit Flow, но ВК вернул код. Запросите новую ссылку подключения.')
        row = self.claim_state(state)
        try:
            if denied:
                raise Conflict('Вы отменили авторизацию. Сообщество не подключено.')
            if not isinstance(code, str) or not code or len(code) > 4096:
                raise Conflict('ВК не передал код авторизации. Повторите /connect.')
            result = await self.provider.exchange(code, self.vault.decrypt(row['verifier']), device_id, state)
            return await self.save_connection(row, result)
        except Exception:
            self.db.execute("UPDATE oauth_states SET status='failed',verifier='' WHERE state_hash=?", (row['state_hash'],))
            raise

    async def complete_implicit(self, state, token, lifetime, denied=False):
        if self.settings.oauth_provider != 'vk_implicit':
            raise Conflict('Приём ключа Implicit Flow не включён.')
        row = self.claim_state(state)
        try:
            if denied:
                raise Conflict('Вы отменили авторизацию. Сообщество не подключено.')
            if not isinstance(token, str) or not 1 <= len(token) <= 4096 or any(c.isspace() for c in token):
                raise Conflict('ВК не передал корректный пользовательский ключ. Повторите подключение.')
            if type(lifetime) not in (str, int) or not re.fullmatch(r'[0-9]{1,10}', str(lifetime)):
                raise Conflict('ВК не передал корректный срок действия ключа.')
            return await self.save_connection(row, {'access_token': token, 'expires_in': int(lifetime)})
        except Exception:
            self.db.execute("UPDATE oauth_states SET status='failed',verifier='' WHERE state_hash=?", (row['state_hash'],))
            raise

    async def save_connection(self, row, result):
        token = result['access_token']
        await self.provider.validate(token, row['actor'], row['group_id'], result)
        lifetime = int(result.get('expires_in', 0))
        if lifetime < 0:
            raise ProviderError('ВК выдал недействительный срок ключа.')
        expiry = int(self.now()) + lifetime if lifetime else 0
        with self.store.transaction():
            self.guard(row['actor'], row['project_id'], row['group_id'])
            current = self.db.execute('SELECT status FROM oauth_states WHERE state_hash=?', (row['state_hash'],)).fetchone()
            if not current or current['status'] != 'processing':
                raise Conflict('Подключение отменено. Повторите /connect.')
            self.db.execute("INSERT INTO vk_connections(project_id,group_id,actor,token,expires_at,status) VALUES (?,?,?,?,?,'active') ON CONFLICT(project_id) DO UPDATE SET group_id=excluded.group_id,actor=excluded.actor,token=excluded.token,expires_at=excluded.expires_at,status='active'", (row['project_id'], row['group_id'], row['actor'], self.vault.encrypt(token), expiry))
            self.db.execute("UPDATE oauth_states SET status='done',verifier='' WHERE state_hash=?", (row['state_hash'],))
        return row

    def has_connection(self, project_id):
        return self.db.execute('SELECT 1 FROM vk_connections WHERE project_id=?', (project_id,)).fetchone() is not None

    def token_for(self, actor, project_id, group_id):
        self.guard(actor, project_id, group_id)
        row = self.db.execute('SELECT * FROM vk_connections WHERE project_id=?', (project_id,)).fetchone()
        if not row or row['status'] != 'active' or row['group_id'] != group_id:
            return ''
        if row['expires_at'] and row['expires_at'] <= self.now():
            return ''
        return self.vault.decrypt(row['token'])

    def disconnect(self, actor, project_id):
        project = self.store.require(actor, project_id)
        with self.store.transaction():
            self.db.execute("INSERT INTO vk_connections(project_id,group_id,actor,token,expires_at,status) VALUES (?,?,?,'',0,'disconnected') ON CONFLICT(project_id) DO UPDATE SET status='disconnected',token=''", (project_id, project['vk_group'] or 0, actor))
            self.db.execute("UPDATE oauth_states SET status='failed',verifier='' WHERE project_id=? AND status IN ('pending','processing')", (project_id,))

    def status(self, actor, project_id):
        self.store.require(actor, project_id)
        row = self.db.execute('SELECT group_id,actor,expires_at,status FROM vk_connections WHERE project_id=?', (project_id,)).fetchone()
        return dict(row) if row else None


def create_web_app(manager, notify=None):
    async def callback(request):
        if any(len(request.query.getall(key, [])) > 1 for key in ('state', 'code', 'device_id', 'error')):
            return web.Response(status=400, text='Некорректный ответ авторизации.', headers=SECURE_HEADERS)
        try:
            implicit = manager.settings.oauth_provider == 'vk_implicit'
            if request.method == 'POST':
                if not implicit or request.query or request.headers.get('Origin') != manager.settings.oauth_public_url.rstrip('/'):
                    raise Conflict('Недопустимый источник запроса подключения.')
                if request.content_type != 'application/json':
                    raise Conflict('Недопустимый формат запроса подключения.')
                try:
                    payload = await request.json()
                except (ValueError, web.HTTPRequestEntityTooLarge):
                    raise Conflict('Некорректный запрос подключения.') from None
                if not isinstance(payload, dict) or set(payload) - {'state', 'access_token', 'expires_in', 'denied'}:
                    raise Conflict('Некорректный запрос подключения.')
                row = await manager.complete_implicit(payload.get('state'), payload.get('access_token'),
                    payload.get('expires_in'), denied=payload.get('denied') is True)
            elif implicit and 'code' not in request.query:
                if set(request.query) - {'state', 'error', 'error_description'}:
                    raise Conflict('Ключи авторизации не принимаются в URL запроса.')
                nonce = secrets.token_urlsafe(24)
                html = Path(__file__).with_name('implicit_callback.html').read_text().replace('__NONCE__', nonce)
                headers = {**SECURE_HEADERS, 'Content-Security-Policy':
                    SECURE_HEADERS['Content-Security-Policy'] + f"; script-src 'nonce-{nonce}'; connect-src 'self'; form-action 'none'"}
                return web.Response(text=html, content_type='text/html', headers=headers)
            else:
                row = await manager.complete(request.query.get('state', ''), request.query.get('code', ''),
                                             request.query.get('device_id', ''), denied='error' in request.query)
        except (Conflict, ProviderError) as error:
            return web.Response(status=400, text=str(error), headers=SECURE_HEADERS)
        except Exception:
            return web.Response(status=400, text='Подключение не завершено. Повторите /connect в боте.', headers=SECURE_HEADERS)
        if notify:
            try:
                await notify(row['actor'], f"Сообщество {row['group_id']} подключено к проекту №{row['project_id']}. Теперь можно готовить текст.",
                             keyboard=keyboard([button('Написать текст', 'compose', project=row['project_id'])],
                                               [button('Меню сообщества', 'workspace', project=row['project_id'])]))
            except Exception:
                pass  # Подключение сохранено; доставка уведомления не должна его отменять.
        return web.Response(text='Сообщество подключено. Вернитесь в сообщения бота. Проверить статус: /connection.', headers=SECURE_HEADERS)

    app = web.Application(client_max_size=8192)
    app.router.add_get('/oauth/vk/callback', callback)
    app.router.add_post('/oauth/vk/callback', callback)
    return app
