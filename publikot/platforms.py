"""Coordinate shared-project publication using each destination's personal connection."""
import json
import re

import aiohttp

from .dzen_api import DzenAPI, validate_auth
from .oauth import Vault
from .postmypost import Postmypost, RequestBudget
from .providers import AmbiguousPublish, ProviderError
from .store import Conflict
from .media import publication_images


class Platforms:
    def __init__(self, settings, store, pmp_factory=Postmypost, dzen_factory=DzenAPI):
        self.settings, self.store = settings, store
        self.pmp_factory, self.dzen_factory = pmp_factory, dzen_factory
        self.budget = RequestBudget()
        self._vault = None

    @property
    def vault(self):
        if self._vault is None:
            exists = self.store.db.execute("SELECT 1 FROM personal_credentials WHERE ciphertext<>''").fetchone()
            self._vault = Vault(self.settings.data_dir / 'platforms.key', may_create=not exists)
        return self._vault

    def secret(self, actor, provider):
        row = self.store.credential_record(actor, provider)
        if not row or not row['ciphertext']:
            raise Conflict('Личный токен или сеанс площадки отключён. Подключите его заново.')
        return self.vault.decrypt(row['ciphertext'])

    def pmp(self, actor, session, token=None):
        return self.pmp_factory(token if token is not None else self.secret(actor, 'postmypost'), session,
                                actor=actor, budget=self.budget)

    async def save_token(self, actor, token):
        if not self.store.is_allowed(actor, self.settings.owner_id):
            raise Conflict('Нет доступа к боту.')
        if not isinstance(token, str) or not 16 <= len(token) <= 4096 or re.search(r'\s', token):
            raise Conflict('Нужен только личный токен Postmypost без пробелов и пояснений.')
        async with aiohttp.ClientSession(trust_env=True) as session:
            await self.pmp(actor, session, token).projects()
        if not self.store.is_allowed(actor, self.settings.owner_id):
            raise Conflict('Доступ отозван.')
        self.store.put_credential(actor, 'postmypost', self.vault.encrypt(token))

    def import_dzen(self, actor, auth):
        if not self.store.is_allowed(actor, self.settings.owner_id):
            raise Conflict('Пользователь не допущен к боту.')
        validate_auth(auth)
        key = 'dzen:' + auth['publisher_id']
        self.store.put_credential(actor, key, self.vault.encrypt(json.dumps(auth)))
        return key

    def disconnect(self, actor, key='postmypost'):
        self.store.put_credential(actor, key, '')

    def target_secret(self, actor, project, target):
        self.store.require(actor, project)
        owner = target['credential_owner']
        if not self.store.is_allowed(owner, self.settings.owner_id):
            raise Conflict('Доступ подключившего площадку пользователя отозван.')
        self.store.require(owner, project)
        row = self.store.credential_record(owner, target['credential_key'])
        if not row or row['version'] != target['credential_version']:
            raise Conflict('Токен площадки изменился. Создайте новое задание.')
        return self.secret(owner, target['credential_key'])

    async def publish(self, actor, ident, revision):
        s = self.store
        job = s.expect_review(actor, ident, revision)
        targets = json.loads(job['destinations'])
        # Preflight all credentials and remote accounts before claiming or sending anything.
        async with aiohttp.ClientSession(trust_env=True) as session:
            for target in targets:
                secret = self.target_secret(actor, job['project_id'], target)
                if target['provider'] == 'postmypost':
                    await self.pmp(target['credential_owner'], session, secret).check_account(target)
                else:
                    validate_auth(json.loads(secret))
            # The claim transaction compares the full destination/token snapshot again.
            job, version = s.claim(actor, ident, revision)
            for target in targets:
                dest = target['id']
                try:
                    secret = self.target_secret(actor, job['project_id'], target)
                    s.delivery_update(actor, ident, dest, 'sending')
                    if target['provider'] == 'postmypost':
                        client = self.pmp(target['credential_owner'], session, secret)
                        result = await client.create(target, version['body'], publication_images(version))
                        remote = result.get('id')
                        if type(remote) is not int or remote <= 0:
                            raise AmbiguousPublish('Postmypost не вернул ID; проверьте кабинет, повтора не будет.')
                        s.delivery_update(actor, ident, dest, 'queued', remote_id=str(remote))
                        self.record_status(actor, ident, dest, result)
                    else:
                        client = self.dzen_factory(json.loads(secret), session)
                        remote, url = await client.publish(target, version['body'], publication_images(version),
                            lambda draft: s.delivery_update(actor, ident, dest, 'sending', remote_id=draft))
                        s.delivery_update(actor, ident, dest, 'published', remote_id=remote, url=url)
                except AmbiguousPublish as error:
                    s.delivery_update(actor, ident, dest, 'unknown', error=str(error))
                except (ProviderError, Conflict) as error:
                    s.delivery_update(actor, ident, dest, 'failed', error=str(error))
                except Exception:
                    s.delivery_update(actor, ident, dest, 'unknown', error='Неизвестный результат; проверьте площадку. Повтора не будет.')
            s.aggregate_deliveries(actor, ident)
        await self.refresh(actor, ident)
        return self.report(actor, ident)

    def record_status(self, actor, ident, destination, response):
        remote_status = response.get('publication_status')
        status = {1: 'published', 2: 'queued', 3: 'failed', 5: 'queued'}.get(remote_status, 'unknown')
        error = 'Postmypost сообщил об ошибке публикации. Подробности — в кабинете.' if status == 'failed' else (
            'Postmypost вернул неподдерживаемый статус. Проверьте кабинет.' if status == 'unknown' else '')
        # A service record ID is not a VK permalink. Do not fabricate a public URL.
        self.store.delivery_update(actor, ident, destination, status, error=error)

    async def refresh(self, actor, ident):
        rows = self.store.delivery_rows(actor, ident)
        if not any(r['status'] == 'queued' for r in rows):
            self.store.aggregate_deliveries(actor, ident)
            return
        job = self.store.job(actor, ident)
        async with aiohttp.ClientSession(trust_env=True) as session:
            for row in rows:
                if row['status'] != 'queued':
                    continue
                target = json.loads(row['target'])
                try:
                    # Read-only status checks can use a refreshed personal token.
                    owner = target['credential_owner']
                    if not self.store.is_allowed(owner, self.settings.owner_id):
                        raise Conflict('Подключение пользователя отключено.')
                    self.store.require(owner, job['project_id'])
                    result = await self.pmp(owner, session).status(row['remote_id'])
                    self.record_status(actor, ident, row['destination_id'], result)
                except (ProviderError, Conflict):
                    # A failed status request never triggers another publication.
                    self.store.delivery_update(actor, ident, row['destination_id'], 'queued',
                                               error='Не удалось проверить статус; отправка не повторяется.')
        self.store.aggregate_deliveries(actor, ident)

    def report(self, actor, ident):
        labels = {'queued': 'ожидает/публикуется', 'published': 'опубликовано', 'failed': 'ошибка',
                  'unknown': 'проверьте площадку', 'sending': 'отправляется', 'pending': 'ожидает отправки'}
        lines = [f'Задание №{ident}. Результаты по площадкам:']
        for row in self.store.delivery_rows(actor, ident):
            target = json.loads(row['target'])
            line = f"• {target['name']} ({target['channel']}): {labels[row['status']]}"
            if row['remote_id']:
                line += f" · ID {row['remote_id']}"
            if row['url']:
                line += '\n' + row['url']
            if row['error']:
                line += '\n' + row['error']
            lines.append(line)
        return '\n'.join(lines)
