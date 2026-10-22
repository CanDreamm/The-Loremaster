"""Project destinations and personal connection menus; content workflows live elsewhere."""
import json
import secrets
import time

import aiohttp

from .store import Conflict

SCREENS = {'pmp', 'pmp_token', 'pmp_disconnect_confirm', 'pmp_disconnect', 'create_project',
           'platforms', 'pmp_projects', 'pmp_project', 'pmp_account', 'pmp_account_page', 'dzen_channels', 'dzen_account',
           'destination', 'destination_remove', 'participants', 'participant_add', 'participant_remove'}


class PlatformMenus:
    def remember_choice(self, actor, project, kind, value):
        nonce = secrets.token_hex(6)
        now = int(time.time())
        self.store.db.execute('DELETE FROM platform_choices WHERE expires<?', (now,))
        self.store.db.execute('INSERT INTO platform_choices VALUES (?,?,?,?,?,?)',
                              (actor, nonce, kind, project, json.dumps(value), now + 600))
        return nonce

    def read_choice(self, actor, project, kind, nonce):
        row = self.store.db.execute('SELECT * FROM platform_choices WHERE actor=? AND nonce=? AND kind=? '
            'AND project_id=? AND expires>?', (actor, nonce, kind, project, int(time.time()))).fetchone()
        if not row:
            raise Conflict('Список устарел. Откройте подключение площадки заново.')
        value = json.loads(row['value'])
        if 'credential_version' in value:
            current = self.store.credential_record(actor, 'postmypost')
            if not current or current['version'] != value['credential_version'] or not current['ciphertext']:
                raise Conflict('Личный токен изменился. Откройте список заново.')
        return value

    async def choice_page(self, actor, peer, project, choices, title, screen, page, list_screen):
        from .menus import button, keyboard
        page = max(0, min(page, max(0, (len(choices) - 1) // 4)))
        rows = []
        for item in choices[page*4:page*4+4]:
            nonce = self.remember_choice(actor, project, screen, item)
            rows.append([button(item['label'], screen, project=project, choice=nonce)])
        nav = []
        if page:
            nav.append(button('Ранее', list_screen, project=project, page=page-1))
        if (page+1)*4 < len(choices):
            nav.append(button('Далее', list_screen, project=project, page=page+1))
        if nav:
            rows.append(nav)
        rows.append([button('Назад', 'platforms', project=project)])
        await self.client.send(peer, title if choices else 'Доступных площадок нет. Подключите их в личном кабинете Postmypost.',
                               keyboard=keyboard(*rows))

    async def platform_menu(self, actor, peer, payload, previous_dialog):
        from .menus import button, keyboard, back
        screen = payload['screen']
        s, manager = self.store, self.platforms
        if screen == 'create_project':
            return await self.prompt(actor, peer, {'kind': 'project_name'}, 'Введите название нового проекта.')
        if screen in ('pmp', 'pmp_token', 'pmp_disconnect_confirm', 'pmp_disconnect'):
            if screen == 'pmp_token':
                return await self.prompt(actor, peer, {'kind': 'pmp_token'},
                    'Отправьте личный токен Postmypost одним сообщением. Он будет зашифрован в базе '
                    'и не показывается другим пользователям. После подключения удалите исходное сообщение с токеном.')
            if screen == 'pmp_disconnect_confirm':
                s.set_dialog(actor, {'kind': 'pmp_disconnect'})
                return await self.client.send(peer,
                    'Отключить ваш токен? Связанные с ним площадки всех ваших проектов перестанут отправлять новые посты. '
                    'Уже переданные в Postmypost публикации останутся там.', keyboard=keyboard(
                        [button('Да, отключить', 'pmp_disconnect')], [button('Назад', 'pmp')]))
            if screen == 'pmp_disconnect':
                if previous_dialog != {'kind': 'pmp_disconnect'}:
                    raise Conflict('Подтверждение устарело.')
                manager.disconnect(actor)
            row = s.credential_record(actor, 'postmypost')
            connected = bool(row and row['ciphertext'])
            return await self.client.send(peer,
                'Ваш Postmypost: ' + ('токен сохранён.' if connected else 'не подключён.')
                + '\nУ каждого пользователя свой токен. Подключёнными площадками могут пользоваться участники проекта.',
                keyboard=keyboard([button('Заменить токен' if connected else 'Добавить токен', 'pmp_token')],
                                  *([[button('Отключить', 'pmp_disconnect_confirm')]] if connected else []), back()))
        project = int(payload['project'])
        p = s.require(actor, project)
        s.select(actor, project)
        if screen == 'platforms':
            rows = s.destination_rows(actor, project)
            page = max(0, min(int(payload.get('page', 0)), max(0, (len(rows)-1)//3)))
            keys = [[button(f"{r['channel']} · {r['name']}", 'destination', project=project, destination=r['id'])]
                    for r in rows[page*3:page*3+3]]
            nav = []
            if page:
                nav.append(button('Ранее', screen, project=project, page=page-1))
            if (page+1)*3 < len(rows):
                nav.append(button('Далее', screen, project=project, page=page+1))
            if nav:
                keys.append(nav)
            keys.append([button('Добавить из Postmypost', 'pmp_projects', project=project),
                         button('Подключить Дзен', 'dzen_channels', project=project)])
            keys.append(back(project))
            text = f"Площадки проекта «{p['name']}»: {len(rows)}.\n" + (
                'Выберите площадку или добавьте новую. Новый список получателей применяется к новым заданиям.' if rows else
                'Подключите аккаунты из вашего Postmypost или сеанс Дзена.')
            return await self.client.send(peer, text, keyboard=keyboard(*keys))
        if screen in ('destination', 'destination_remove'):
            row = next((r for r in s.destination_rows(actor, project) if r['id'] == int(payload['destination'])), None)
            if not row:
                raise Conflict('Площадка уже отключена.')
            if screen == 'destination_remove':
                if previous_dialog != {'kind': 'destination_remove', 'project': project, 'destination': row['id']}:
                    raise Conflict('Подтверждение устарело.')
                s.detach_destination(actor, project, row['id'], self.settings.owner_id)
                return await self.platform_menu(actor, peer, {'screen': 'platforms', 'project': project}, None)
            keys = []
            if actor in (p['owner'], self.settings.owner_id, row['credential_owner']):
                s.set_dialog(actor, {'kind': 'destination_remove', 'project': project, 'destination': row['id']})
                keys.append([button('Отключить площадку', 'destination_remove', project=project, destination=row['id'])])
            keys.append([button('Назад', 'platforms', project=project)])
            return await self.client.send(peer, f"{row['name']} · {row['channel']}\n"
                f"Подключил пользователь {row['credential_owner']}. Участники проекта могут подтверждать отправку.\n"
                'Отключение не отменяет уже переданные публикации.', keyboard=keyboard(*keys))
        if screen == 'pmp_projects':
            async with aiohttp.ClientSession(trust_env=True) as session:
                projects = await manager.pmp(actor, session).projects()
            s.require(actor, project)
            version = s.credential_record(actor, 'postmypost')['version']
            choices = [{'label': str(r.get('name') or r['id']), 'remote_project': r['id'],
                        'credential_version': version} for r in projects]
            return await self.choice_page(actor, peer, project, choices, 'Выберите проект в вашем Postmypost.',
                                          'pmp_project', int(payload.get('page', 0)), screen)
        if screen == 'pmp_project':
            selected = self.read_choice(actor, project, screen, payload['choice'])
            # Store a project-bound server-side pointer for account pagination.
            s.set_dialog(actor, None)
            async with aiohttp.ClientSession(trust_env=True) as session:
                client = manager.pmp(actor, session)
                accounts = await client.accounts(selected['remote_project'])
                channels = await client.channels()
            s.require(actor, project)
            self.read_choice(actor, project, screen, payload['choice'])
            names = {str(r['id']): str(r.get('code') or r.get('name') or r['id']) for r in channels}
            choices = [{'label': str(r.get('name') or r['id']), 'remote_account': r['id'],
                        'remote_project': selected['remote_project'], 'credential_version': selected['credential_version'],
                        'external_id': str(r.get('external_id') or r['id']),
                        'channel': names.get(str(r.get('chanel_id', r.get('channel_id'))), 'Postmypost')}
                       for r in accounts if r.get('connection_status') == 1]
            # Account list is cached briefly; every choice is revalidated at attachment.
            nonce = self.remember_choice(actor, project, 'account_page', {'choices': choices})
            return await self.account_choice_page(actor, peer, project, nonce, int(payload.get('page', 0)))
        if screen == 'pmp_account_page':
            return await self.account_choice_page(actor, peer, project, payload['choice'], int(payload.get('page', 0)))
        if screen == 'pmp_account':
            item = self.read_choice(actor, project, screen, payload['choice'])
            async with aiohttp.ClientSession(trust_env=True) as session:
                await manager.pmp(actor, session).check_account(item)
            self.read_choice(actor, project, screen, payload['choice'])
            s.attach_destination(actor, project, 'postmypost', item['remote_project'], item['remote_account'],
                                 item['channel'], item['label'], external_id=item['external_id'])
            return await self.platform_menu(actor, peer, {'screen': 'platforms', 'project': project}, None)
        if screen == 'dzen_channels':
            credentials = s.db.execute("SELECT provider,version FROM personal_credentials WHERE actor=? "
                                      "AND provider LIKE 'dzen:%' AND ciphertext<>''", (actor,)).fetchall()
            if not credentials:
                return await self.client.send(peer,
                    'Для прямой публикации в Дзен нужен ваш авторизованный сеанс. '
                    'Администратор импортирует его на сервере по инструкции docs/project-platforms.md. '
                    'После импорта здесь появятся ваши каналы. Cookie и пароли в чат не отправляйте.',
                    keyboard=keyboard([button('Назад', 'platforms', project=project)]))
            choices = [{'label': 'Дзен · ' + r['provider'].split(':',1)[1], 'key': r['provider'],
                        'version': r['version']} for r in credentials]
            return await self.choice_page(actor, peer, project, choices, 'Выберите ваш канал Дзена.',
                                          'dzen_account', int(payload.get('page', 0)), screen)
        if screen == 'dzen_account':
            item = self.read_choice(actor, project, screen, payload['choice'])
            row = s.credential_record(actor, item['key'])
            if not row or not row['ciphertext'] or row['version'] != item['version']:
                raise Conflict('Сеанс Дзена изменился. Откройте список заново.')
            publisher = item['key'].split(':',1)[1]
            s.attach_destination(actor, project, 'dzen', publisher, publisher, 'Дзен', item['label'], item['key'])
            return await self.platform_menu(actor, peer, {'screen': 'platforms', 'project': project}, None)
        if screen in ('participants', 'participant_add', 'participant_remove'):
            can_manage = actor in (p['owner'], self.settings.owner_id)
            if screen != 'participants':
                s.project_manager(actor, project, self.settings.owner_id)
                return await self.prompt(actor, peer,
                    {'kind': screen, 'project': project}, 'Введите числовой ID пользователя ВК. '
                    'Для назначения администратор должен заранее разрешить ему доступ к боту.')
            members = s.db.execute('SELECT user_id FROM project_members WHERE project_id=? ORDER BY user_id',
                                   (project,)).fetchall()
            text = 'Участники проекта:\n' + '\n'.join(str(r['user_id']) for r in members)
            keys = [[button('Добавить участника', 'participant_add', project=project),
                     button('Исключить участника', 'participant_remove', project=project)]] if can_manage else []
            keys.append(back(project))
            return await self.client.send(peer, text, keyboard=keyboard(*keys))
        raise Conflict('Неизвестное действие с площадкой.')

    async def account_choice_page(self, actor, peer, project, nonce, page):
        from .menus import button, keyboard
        items = self.read_choice(actor, project, 'account_page', nonce)['choices']
        page = max(0, min(page, max(0, (len(items)-1)//4)))
        rows = []
        for item in items[page*4:page*4+4]:
            key = self.remember_choice(actor, project, 'pmp_account', item)
            rows.append([button(item['channel'] + ' · ' + item['label'], 'pmp_account', project=project, choice=key)])
        nav = []
        if page:
            nav.append(button('Ранее', 'pmp_account_page', project=project, choice=nonce, page=page-1))
        if (page+1)*4 < len(items):
            nav.append(button('Далее', 'pmp_account_page', project=project, choice=nonce, page=page+1))
        if nav:
            rows.append(nav)
        rows.append([button('Назад', 'platforms', project=project)])
        await self.client.send(peer, 'Выберите площадку для подключения.' if items else
                               'Подключённых аккаунтов нет. Добавьте площадки в кабинете Postmypost.', keyboard=keyboard(*rows))

    async def platform_input(self, actor, peer, text, state):
        from .menus import button, keyboard, back
        kind = state['kind']
        if kind == 'pmp_token':
            await self.platforms.save_token(actor, text)
            self.store.set_dialog(actor, None)
            await self.client.send(peer, 'Личный токен проверен и сохранён. Удалите исходное сообщение с токеном.',
                                   keyboard=keyboard([button('Мои проекты', 'communities')]))
        elif kind == 'project_name':
            if not text or len(text) > 100:
                raise Conflict('Введите название от 1 до 100 символов.')
            project = self.store.create_project(actor, text)
            self.store.db.execute("UPDATE projects SET publish_mode='platforms' WHERE id=?", (project,))
            self.store.set_dialog(actor, None)
            await self.workspace(actor, peer, project)
        elif kind in ('participant_add', 'participant_remove'):
            if not text.isdecimal() or int(text) <= 0:
                raise Conflict('Нужен положительный числовой ID пользователя.')
            self.store.assign_project_user(actor, state['project'], int(text), kind == 'participant_add', self.settings.owner_id)
            self.store.set_dialog(actor, None)
            await self.platform_menu(actor, peer, {'screen': 'participants', 'project': state['project']}, None)
        else:
            return False
        return True
