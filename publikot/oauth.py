"""Project-scoped VK connections, encrypted tokens and optional OAuth."""
import base64
import hashlib
import html
import json
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
from .menus import SHORT_PROMPT, button, keyboard

SECURE_HEADERS = {
    'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
    'X-Publikot-OAuth': '1',
    'Content-Security-Policy': "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
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

    async def validate_identity(self, token, actor, authorization=None):
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

    async def communities(self, token, actor, authorization=None):
        await self.validate_identity(token, actor, authorization)
        groups, offset = {}, 0
        while True:
            result = await self.api(token, 'groups.get', filter='admin,editor', extended=1,
                                    fields='admin_level', count=1000, offset=offset)
            if not isinstance(result, dict) or not isinstance(result.get('items'), list):
                raise ProviderError('Не удалось получить список сообществ ВК.')
            items = result['items']
            for group in items:
                if group.get('is_admin') and group.get('admin_level') in (2, 3):
                    groups[int(group['id'])] = {'id': int(group['id']), 'name': str(group['name'])}
            offset += len(items)
            if offset >= result.get('count', offset):
                return list(groups.values())
            if not items:
                raise ProviderError('ВК вернул неполный список сообществ. Повторите подключение.')

    async def validate(self, token, actor, group, authorization=None):
        await self.validate_identity(token, actor, authorization)
        groups = await self.api(token, 'groups.getById', group_id=group, fields='admin_level')
        if isinstance(groups, dict):
            groups = groups.get('groups', [])
        if not isinstance(groups, list) or len(groups) != 1:
            raise ProviderError('Не удалось проверить сообщество.')
        selected = groups[0]
        if selected.get('id') != group or not selected.get('is_admin') or selected.get('admin_level', 0) not in (2, 3):
            raise ProviderError('Для публикации нужны права редактора или администратора выбранного сообщества.')

    async def validate_selected(self, token, actor, selected, authorization=None):
        await self.validate_identity(token, actor, authorization)
        for offset in range(0, len(selected), 500):
            batch = selected[offset:offset + 500]
            result = await self.api(token, 'groups.getById', group_ids=','.join(map(str, batch)), fields='admin_level')
            groups = result.get('groups', []) if isinstance(result, dict) else result
            if not isinstance(groups, list):
                raise ProviderError('Не удалось проверить выбранные сообщества.')
            allowed = {g.get('id') for g in groups if g.get('is_admin') and g.get('admin_level') in (2, 3)}
            if not set(batch) <= allowed:
                raise ProviderError('Права на одно из выбранных сообществ отозваны. Повторите подключение.')


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
        CREATE TABLE IF NOT EXISTS oauth_imports (
          state_hash TEXT PRIMARY KEY, actor INTEGER NOT NULL, expires_at INTEGER NOT NULL,
          verifier TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          token TEXT NOT NULL DEFAULT '', token_expiry INTEGER NOT NULL DEFAULT 0,
          groups_json TEXT NOT NULL DEFAULT '[]', csrf TEXT NOT NULL DEFAULT '',
          flow TEXT NOT NULL DEFAULT 'code', authorization_json TEXT NOT NULL DEFAULT '{}'
        );
        ''')
        state_columns = {row['name'] for row in self.db.execute('PRAGMA table_info(oauth_states)')}
        if 'flow' not in state_columns:
            self.db.execute("ALTER TABLE oauth_states ADD COLUMN flow TEXT NOT NULL DEFAULT 'code'")
        self.db.execute("UPDATE oauth_imports SET token='',verifier='',csrf='',status='failed' WHERE expires_at<=?", (int(self.now()),))
        existing = self.db.execute("SELECT 1 FROM vk_connections WHERE token<>'' LIMIT 1").fetchone()
        existing = existing or self.db.execute("SELECT 1 FROM oauth_imports WHERE token<>'' OR verifier<>'' LIMIT 1").fetchone()
        self.vault = Vault(settings.data_dir / 'oauth.key', may_create=not existing)

    async def add_with_token(self, actor, group, token):
        if not self.store.is_allowed(actor, self.settings.owner_id):
            raise Conflict('Доступ к боту отозван.')
        if not isinstance(token, str) or not 16 <= len(token) <= 4096 or any(c.isspace() for c in token):
            raise Conflict('Пришлите только ключ доступа VK (access_token), без ссылки и пояснений.')
        if self.provider is None:
            async with aiohttp.ClientSession() as session:
                return await self._add_with_token(actor, group, token, VkOAuthProvider(self.settings, session))
        return await self._add_with_token(actor, group, token, self.provider)

    async def _add_with_token(self, actor, group, token, provider):
        profile = await provider.api(token, 'account.getProfileInfo')
        if not isinstance(profile, dict) or profile.get('id') != actor:
            raise Conflict('Нужен пользовательский ключ доступа вашего аккаунта VK с правами wall и photos.')
        permissions = await provider.api(token, 'account.getAppPermissions')
        if type(permissions) is not int or permissions & 8196 != 8196:
            raise Conflict('У ключа нет прав wall и photos. Пришлите ключ с правами публикации текста и фото.')
        result = await provider.api(token, 'groups.getById', group_id=group, fields='admin_level')
        groups = result.get('groups', []) if isinstance(result, dict) else result
        if not isinstance(groups, list) or len(groups) != 1 or not isinstance(groups[0], dict):
            raise ProviderError('Не удалось проверить сообщество. Подключение не сохранено.')
        selected = groups[0]
        if selected.get('id') != group or not selected.get('is_admin') or selected.get('admin_level') not in (2, 3):
            raise Conflict('У владельца ключа нет прав редактора или администратора этого сообщества.')
        encrypted = self.vault.encrypt(token)
        with self.store.transaction():
            if not self.store.is_allowed(actor, self.settings.owner_id):
                raise Conflict('Доступ к боту отозван.')
            existing = self.db.execute('SELECT p.id FROM projects p JOIN project_members m ON m.project_id=p.id '
                'WHERE m.user_id=? AND p.vk_group=? ORDER BY p.id LIMIT 1', (actor, group)).fetchone()
            if existing:
                project_id = existing['id']
            else:
                base = str(selected.get('name') or f'Сообщество {group}')[:100]
                name, index = base, 1
                while self.db.execute('SELECT 1 FROM projects WHERE owner=? AND name=?', (actor, name)).fetchone():
                    name = f'{base[:75]} · {group} · {index}'
                    index += 1
                project_id = self.db.execute('INSERT INTO projects(owner,name,vk_group,text_prompt) VALUES (?,?,?,?)',
                    (actor, name, group, SHORT_PROMPT)).lastrowid
                self.db.execute("INSERT INTO project_members VALUES (?,?,'member')", (project_id, actor))
            self.db.execute("INSERT INTO vk_connections(project_id,group_id,actor,token,expires_at,status) "
                "VALUES (?,?,?,?,0,'active') ON CONFLICT(project_id) DO UPDATE SET group_id=excluded.group_id,"
                "actor=excluded.actor,token=excluded.token,expires_at=0,status='active'",
                (project_id, group, actor, encrypted))
            self.db.execute('INSERT INTO sessions(owner,project_id) VALUES (?,?) ON CONFLICT(owner) '
                'DO UPDATE SET project_id=excluded.project_id,job_id=NULL', (actor, project_id))
        return project_id

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
        return self.authorization_url(state, verifier)

    def authorization_url(self, state, verifier, include_groups=False):
        params = {'client_id': self.settings.oauth_client_id, 'redirect_uri': self.settings.oauth_redirect,
                  'response_type': 'code', 'state': state}
        scopes = ['wall', 'photos'] + (['groups'] if include_groups else [])
        if self.settings.oauth_provider == 'vkid':
            params.update(scope=' '.join(scopes), code_challenge_method='S256',
                          code_challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode())
            endpoint = 'https://id.vk.com/authorize'
        else:
            params.update(scope=','.join(scopes), v='5.199', display='page')
            if self.settings.oauth_provider == 'vk_implicit':
                params.update(response_type='token', scope='wall,photos,groups,offline', revoke='1')
            endpoint = 'https://oauth.vk.com/authorize'
        return endpoint + '?' + urlencode(params)

    def import_guard(self, actor):
        if not self.store.is_allowed(actor, self.settings.owner_id):
            raise Conflict('Доступ к боту отозван.')

    def begin_import(self, actor):
        self.expire_imports()
        self.import_guard(actor)
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        with self.store.transaction():
            self.db.execute("UPDATE oauth_imports SET status='failed',token='',verifier='',csrf='' WHERE actor=?", (actor,))
            implicit = self.settings.oauth_provider == 'vk_implicit'
            self.db.execute('INSERT INTO oauth_imports(state_hash,actor,expires_at,verifier,flow) VALUES (?,?,?,?,?)',
                (hashlib.sha256(state.encode()).hexdigest(), actor, int(self.now()) + 600,
                 '' if implicit else self.vault.encrypt(verifier), 'implicit' if implicit else 'code'))
        return self.authorization_url(state, verifier, include_groups=True)

    def expire_imports(self):
        self.db.execute("UPDATE oauth_imports SET status='failed',token='',verifier='',csrf='' WHERE expires_at<=? AND status<>'done'", (int(self.now()),))

    def import_session(self, state, status):
        if not isinstance(state, str) or not 32 <= len(state) <= 128:
            raise Conflict('Ссылка подключения недействительна.')
        digest = hashlib.sha256(state.encode()).hexdigest()
        row = self.db.execute('SELECT * FROM oauth_imports WHERE state_hash=?', (digest,)).fetchone()
        if not row or row['status'] != status or row['expires_at'] <= self.now():
            raise Conflict('Ссылка истекла или уже использована. Нажмите «Добавить сообщество» в боте.')
        if row['flow'] != ('implicit' if self.settings.oauth_provider == 'vk_implicit' else 'code'):
            raise Conflict('Способ авторизации изменился. Запросите новую ссылку в боте.')
        self.import_guard(row['actor'])
        return row

    async def prepare_import(self, state, code, device_id='', denied=False, *, implicit_token=None, implicit_lifetime=None):
        self.expire_imports()
        with self.store.transaction():
            row = self.import_session(state, 'pending')
            self.db.execute("UPDATE oauth_imports SET status='processing' WHERE state_hash=?", (row['state_hash'],))
        try:
            if denied:
                raise Conflict('Вы отменили авторизацию. Сообщества не добавлены.')
            if self.settings.oauth_provider == 'vk_implicit':
                if not isinstance(implicit_token, str) or not 1 <= len(implicit_token) <= 4096 or any(c.isspace() for c in implicit_token):
                    raise Conflict('ВК не передал корректный пользовательский ключ. Повторите подключение.')
                if type(implicit_lifetime) not in (str, int) or not re.fullmatch(r'[0-9]{1,10}', str(implicit_lifetime)):
                    raise Conflict('ВК не передал корректный срок действия ключа.')
                result = {'access_token': implicit_token, 'expires_in': int(implicit_lifetime)}
            else:
                if not code or len(code) > 4096:
                    raise Conflict('ВК не передал код авторизации.')
                result = await self.provider.exchange(code, self.vault.decrypt(row['verifier']), device_id, state)
            groups = await self.provider.communities(result['access_token'], row['actor'], result)
            if not groups:
                raise Conflict('В этом аккаунте нет сообществ с правами редактора или администратора.')
            lifetime = int(result.get('expires_in', 0))
            if lifetime < 0:
                raise ProviderError('ВК выдал недействительный срок ключа.')
            csrf = secrets.token_urlsafe(32)
            with self.store.transaction():
                self.import_session(state, 'processing')
                self.db.execute("UPDATE oauth_imports SET status='choosing',verifier='',token=?,token_expiry=?,groups_json=?,csrf=?,authorization_json=? WHERE state_hash=?",
                    (self.vault.encrypt(result['access_token']), int(self.now()) + lifetime if lifetime else 0,
                     json.dumps(groups, ensure_ascii=False), csrf,
                     json.dumps({k: result[k] for k in ('user_id', 'scope') if k in result}), row['state_hash']))
            return self.import_session(state, 'choosing')
        except Exception:
            self.db.execute("UPDATE oauth_imports SET status='failed',verifier='',token='',csrf='' WHERE state_hash=?", (row['state_hash'],))
            raise

    async def finish_import(self, state, csrf, selected):
        self.expire_imports()
        with self.store.transaction():
            row = self.import_session(state, 'choosing')
            if not secrets.compare_digest(row['csrf'], csrf):
                raise Conflict('Недействительное подтверждение выбора.')
            groups = {g['id']: g for g in json.loads(row['groups_json'])}
            if not selected or not set(selected) <= groups.keys():
                raise Conflict('Выберите сообщества из списка.')
            if row['token_expiry'] and row['token_expiry'] <= self.now():
                raise Conflict('Разрешение ВК истекло. Повторите авторизацию.')
            self.db.execute("UPDATE oauth_imports SET status='saving' WHERE state_hash=?", (row['state_hash'],))
        try:
            token = self.vault.decrypt(row['token'])
            authorization = {**json.loads(row['authorization_json']), 'access_token': token}
            await self.provider.validate_selected(token, row['actor'], sorted(set(selected)), authorization)
            projects = []
            with self.store.transaction():
                self.import_session(state, 'saving')
                for group in sorted(set(selected)):
                    existing = self.db.execute('SELECT p.id FROM projects p JOIN project_members m ON m.project_id=p.id WHERE m.user_id=? AND p.vk_group=? ORDER BY p.id LIMIT 1', (row['actor'], group)).fetchone()
                    if existing:
                        project_id = existing['id']
                    else:
                        name = groups[group]['name'][:100] or str(group)
                        if self.db.execute('SELECT 1 FROM projects WHERE owner=? AND name=?', (row['actor'], name)).fetchone():
                            name = f'{name} · {group}'
                        project_id = self.db.execute('INSERT INTO projects(owner,name,vk_group,text_prompt) VALUES (?,?,?,?)', (row['actor'], name, group, SHORT_PROMPT)).lastrowid
                        self.db.execute("INSERT INTO project_members VALUES (?,?,'member')", (project_id, row['actor']))
                    self.db.execute("INSERT INTO vk_connections(project_id,group_id,actor,token,expires_at,status) VALUES (?,?,?,?,?,'active') ON CONFLICT(project_id) DO UPDATE SET group_id=excluded.group_id,actor=excluded.actor,token=excluded.token,expires_at=excluded.expires_at,status='active'", (project_id, group, row['actor'], row['token'], row['token_expiry']))
                    projects.append(project_id)
                self.db.execute('INSERT INTO sessions(owner,project_id) VALUES (?,?) ON CONFLICT(owner) DO UPDATE SET project_id=excluded.project_id,job_id=NULL', (row['actor'], projects[0]))
                self.db.execute("UPDATE oauth_imports SET status='done',token='',csrf='' WHERE state_hash=?", (row['state_hash'],))
            return row['actor'], projects
        except Exception:
            self.db.execute("UPDATE oauth_imports SET status='failed',token='',csrf='' WHERE state_hash=?", (row['state_hash'],))
            raise

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
    def is_import(state):
        if not isinstance(state, str) or not 32 <= len(state) <= 128:
            return False
        return manager.db.execute('SELECT 1 FROM oauth_imports WHERE state_hash=?',
                                  (hashlib.sha256(state.encode()).hexdigest(),)).fetchone() is not None

    def set_import_cookie(response, row):
        response.set_cookie('vk_import', row['csrf'], max_age=max(1, int(row['expires_at'] - manager.now())),
                            secure=True, httponly=True, samesite='Strict', path='/oauth/vk/select')
        return response

    def render_picker(state, row):
        choices = ''.join(f'<p><label><input type="checkbox" name="group" value="{g["id"]}"> '
                          f'{html.escape(g["name"])} (ID {g["id"]})</label></p>'
                          for g in json.loads(row['groups_json']))
        body = ('<!doctype html><html lang="ru"><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                '<title>Добавить сообщества — Публикот</title><main><h1>Выберите сообщества</h1>'
                '<p>Доступны сообщества, где вы редактор или администратор. Можно выбрать несколько.</p>'
                '<p>Каждый пост публикуется только после подтверждения в боте.</p>'
                '<form method="post" action="/oauth/vk/select">'
                f'<input type="hidden" name="state" value="{html.escape(state, quote=True)}">'
                f'<input type="hidden" name="csrf" value="{row["csrf"]}">'
                + choices + '<button type="submit">Подключить выбранные сообщества</button></form>'
                '<p>Чтобы отменить подключение, закройте страницу. Ссылка действует 10 минут с начала подключения.</p></main></html>')
        return web.Response(text=body, content_type='text/html', headers=SECURE_HEADERS)

    async def picker(request):
        try:
            if set(request.query) != {'state'} or len(request.query.getall('state')) != 1:
                raise Conflict('Некорректная ссылка выбора сообществ.')
            state = request.query['state']
            row = manager.import_session(state, 'choosing')
            if not secrets.compare_digest(request.cookies.get('vk_import', ''), row['csrf']):
                raise Conflict('Откройте подключение заново из сообщений бота.')
            return render_picker(state, row)
        except Conflict as error:
            return web.Response(status=400, text=str(error), headers=SECURE_HEADERS)

    async def select(request):
        try:
            data = await request.post()
            if len(data.getall('state', [])) != 1 or len(data.getall('csrf', [])) != 1:
                raise Conflict('Некорректное подтверждение выбора.')
            csrf = data.get('csrf', '')
            if not csrf or not secrets.compare_digest(request.cookies.get('vk_import', ''), csrf):
                raise Conflict('Откройте подключение заново из сообщений бота.')
            selected = [int(value) for value in data.getall('group', [])]
            actor, projects = await manager.finish_import(data['state'], csrf, selected)
        except (Conflict, ProviderError) as error:
            return web.Response(status=400, text=str(error), headers=SECURE_HEADERS)
        except Exception:
            return web.Response(status=400, text='Подключение не завершено. Нажмите «Добавить сообщество» в боте.', headers=SECURE_HEADERS)
        if notify:
            try:
                await notify(actor, f'Подключено сообществ: {len(projects)}. Выберите сообщество для работы.',
                             keyboard=keyboard([button('Мои сообщества', 'communities')]))
            except Exception:
                pass
        response = web.Response(text=f'Подключено сообществ: {len(projects)}. Вернитесь в сообщения бота → «Мои сообщества».', headers=SECURE_HEADERS)
        response.del_cookie('vk_import', path='/oauth/vk/select')
        return response

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
                if is_import(payload.get('state')):
                    row = await manager.prepare_import(payload['state'], '', denied=payload.get('denied') is True,
                        implicit_token=payload.get('access_token'), implicit_lifetime=payload.get('expires_in'))
                    response = web.json_response({'selection_url': '/oauth/vk/select?' + urlencode({'state': payload['state']})},
                                                 headers=SECURE_HEADERS)
                    return set_import_cookie(response, row)
                row = await manager.complete_implicit(payload.get('state'), payload.get('access_token'),
                    payload.get('expires_in'), denied=payload.get('denied') is True)
            elif implicit and 'code' not in request.query:
                if set(request.query) - {'state', 'error', 'error_description'}:
                    raise Conflict('Ключи авторизации не принимаются в URL запроса.')
                nonce = secrets.token_urlsafe(24)
                body = Path(__file__).with_name('implicit_callback.html').read_text().replace('__NONCE__', nonce)
                headers = {**SECURE_HEADERS, 'Content-Security-Policy':
                    SECURE_HEADERS['Content-Security-Policy'].replace("form-action 'self'", "form-action 'none'")
                    + f"; script-src 'nonce-{nonce}'; connect-src 'self'"}
                return web.Response(text=body, content_type='text/html', headers=headers)
            else:
                state = request.query.get('state', '')
                if is_import(state):
                    if implicit:
                        raise Conflict('Ожидался ключ Implicit Flow. Запросите новую ссылку в боте.')
                    row = await manager.prepare_import(state, request.query.get('code', ''),
                        request.query.get('device_id', ''), denied='error' in request.query)
                    return set_import_cookie(render_picker(state, row), row)
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
    app.router.add_get('/oauth/vk/select', picker)
    app.router.add_post('/oauth/vk/select', select)
    return app
