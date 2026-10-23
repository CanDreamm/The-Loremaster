"""Contextual VK menus and persistent, project-bound input dialogs."""
import json
import re
from urllib.parse import urlsplit

from .store import Conflict
from .platform_menus import PlatformMenus, SCREENS as PLATFORM_SCREENS


SHORT_PROMPT = 'Пиши короткий пост до 500 символов. Верни только готовый текст.'
PROJECT_FIELDS = {'concept': 'Концепция',
                  'text_prompt': 'Правила для текста',
                  'image_prompt': 'Правила изображения',
                  'text_template': 'Промпт для текста', 'image_template': 'Промпт для изображения'}


def button(label, screen, **params):
    return {'action': {'type': 'callback', 'label': label[:40],
                       'payload': json.dumps({'action': 'menu', 'screen': screen, **params})},
            'color': 'secondary'}


def keyboard(*rows):
    styled_rows = []
    for row in rows:
        styled_row = []
        for item in row:
            item = {**item, 'action': dict(item['action'])}
            if item['action']['type'] == 'open_link':
                item.pop('color', None)
            label = item['action'].get('label', '')
            if label.startswith('Назад'):
                icon, color = '⬅️', 'primary'
            elif label == 'Главное меню':
                icon, color = '☰', 'primary'
            elif label == 'Добавить сообщество':
                icon, color = '➕', 'positive'
            elif label in ('Удалить проект', 'Да, удалить', 'Отменить черновик',
                           'Отключить', 'Да, отключить', 'Запретить доступ'):
                icon, color = '❌', 'negative'
            else:
                styled_row.append(item)
                continue
            item['action']['label'] = f'{icon} {label}'[:40]
            if item['action']['type'] != 'open_link':
                item['color'] = color
            styled_row.append(item)
        styled_rows.append(styled_row)
    return {'inline': True, 'buttons': styled_rows}


def back(project=None):
    return [button('Назад', 'workspace', project=project) if project else button('Главное меню', 'home')]


class BotMenus(PlatformMenus):
    async def home(self, actor, peer):
        self.store.set_dialog(actor, None)
        rows = [[button('Мои проекты', 'communities')], [button('Мой Postmypost', 'pmp')]]
        if actor == self.settings.owner_id:
            rows.append([button('Управление доступом', 'admin')])
        rows.append([button('Помощь', 'help')])
        await self.client.send(peer, 'Публикот. Выберите проект, чтобы подготовить публикацию или статью.'
                               + ('\nДемо-режим: настоящий ИИ пока не включён.' if self.settings.demo else ''),
                               keyboard=keyboard(*rows))

    async def communities(self, actor, peer, page=0):
        projects = self.store.projects(actor)
        pages = max(1, (len(projects) + 3) // 4)
        page = min(max(0, page), pages - 1)
        rows = [[button(f"{p['name'][:28]} · №{p['id']}", 'workspace', project=p['id'])]
                for p in projects[page * 4:page * 4 + 4]]
        nav = []
        if page:
            nav.append(button('Ранее', 'communities', page=page - 1))
        if page + 1 < pages:
            nav.append(button('Далее', 'communities', page=page + 1))
        if nav:
            rows.append(nav)
        footer = [button('Добавить проект', 'create_project'), button('Главное меню', 'home')]
        rows.append(footer)
        await self.client.send(peer, (f'Мои проекты · {page + 1}/{pages}. Выберите проект.' if projects
                                    else 'Проектов пока нет. Нажмите «Добавить проект» или попросите назначить вам проект.'),
                               keyboard=keyboard(*rows))

    async def workspace(self, actor, peer, project_id):
        self.store.select(actor, project_id)
        p = self.store.project(actor)
        rows = [[button('Написать пост', 'compose', project=project_id)],
                [button('Мои задания', 'jobs', project=project_id)],
                [button('Настройки проекта', 'settings', project=project_id)],
                [button('Площадки', 'platforms', project=project_id), button('Участники', 'participants', project=project_id)],
                [button('Назад', 'communities'), button('Главное меню', 'home')]]
        targets = self.store.destination_rows(actor, project_id)
        await self.client.send(peer, f"{p['name']}\nПлощадок подключено: {len(targets)}."
                               + '\nВыберите действие.', keyboard=keyboard(*rows))

    async def prompt(self, actor, peer, state, text):
        self.store.set_dialog(actor, state)
        project_id, kind = state.get('project'), state['kind']
        if kind == 'pmp_token':
            previous = button('Назад', 'pmp')
        elif kind in ('project_name', 'browser_name'):
            previous = button('Назад', 'communities')
        elif kind in ('participant_add', 'participant_remove'):
            previous = button('Назад', 'participants', project=project_id)
        elif kind == 'admin':
            previous = button('Назад', 'admin')
        elif kind in ('add_group', 'add_key'):
            previous = button('Назад', 'communities' if kind == 'add_group' else 'add')
        elif kind == 'style':
            raise Conflict('Оформление теперь задаётся в концепции проекта. Откройте настройки заново.')
        elif kind == 'character':
            previous = button('Назад', 'characters', project=project_id)
        elif kind == 'field':
            if state['field'] not in PROJECT_FIELDS:
                raise Conflict('Тон и оформление теперь задаются в концепции проекта. Откройте настройки заново.')
            previous = button('Назад', 'field', project=project_id, field=state['field'])
        elif kind in ('revise', 'image', 'ai_image', 'image_task'):
            previous = button('Назад к тексту', 'job', project=project_id, job=state['job'])
        else:
            previous = back(project_id)[0]
        await self.client.send(peer, text, keyboard=keyboard([previous]))

    async def menu(self, actor, peer, payload):
        screen = payload['screen']
        previous_dialog = self.store.dialog(actor)
        self.store.set_dialog(actor, None)
        if screen in PLATFORM_SCREENS:
            return await self.platform_menu(actor, peer, payload, previous_dialog)
        if screen == 'home':
            return await self.home(actor, peer)
        if screen == 'help':
            return await self.client.send(peer, '1. Создайте проект в «Мои проекты».\n'
                '2. В «Площадках» подключите аккаунты через личный токен Postmypost или сеанс Дзена.\n3. Нажмите «Написать пост» и отправьте задание.\n'
                '4. Готовый текст можно исправить по замечанию. Публикация требует отдельного подтверждения.\n'
                'Назад отменяет ввод. Команда /menu всегда открывает главное меню.\n'
                'Полный список команд: /help.', keyboard=keyboard(back()))
        if screen == 'communities':
            return await self.communities(actor, peer, int(payload.get('page', 0)))
        if screen == 'add_browser':
            return await self.prompt(actor, peer, {'kind': 'browser_name'},
                                     'Введите название нового проекта для публикаций через браузер.')
        if screen == 'add':
            return await self.connect(actor, peer, None)
        if screen == 'admin':
            if actor != self.settings.owner_id:
                raise Conflict('Это меню доступно только администратору.')
            return await self.client.send(peer, 'Управление доступом к боту.', keyboard=keyboard(
                [button('Разрешить доступ', 'admin_allow')],
                [button('Запретить доступ', 'admin_deny')],
                [button('Участники бота', 'admin_users')],
                [button('Назначить проект', 'admin_assign'), button('Снять назначение', 'admin_unassign')], back()))
        admin_commands = {'admin_users': '/users', 'admin_projects': '/allprojects',
                          'admin_allow': '/allow', 'admin_deny': '/deny', 'admin_assign': '/assign',
                          'admin_unassign': '/unassign', 'admin_members': '/members'}
        if screen in admin_commands:
            if actor != self.settings.owner_id:
                raise Conflict('Это меню доступно только администратору.')
            command = admin_commands[screen]
            if screen in ('admin_users', 'admin_projects'):
                await self.command(actor, peer, command, '', {})
                return await self.client.send(peer, 'Управление доступом', keyboard=keyboard([button('Назад', 'admin')]))
            hint = ('Введите номер проекта и ID пользователя через пробел.' if screen in ('admin_assign', 'admin_unassign')
                    else 'Введите номер проекта.' if screen == 'admin_members' else 'Введите числовой ID пользователя ВК.')
            return await self.prompt(actor, peer, {'kind': 'admin', 'command': command}, hint)
        project_id = int(payload['project'])
        if screen in ('delete_project_confirm', 'delete_project'):
            project = self.store.deletable_project(actor, project_id, self.settings.owner_id)
            if screen == 'delete_project_confirm':
                self.store.set_dialog(actor, {'kind': 'delete_project', 'project': project_id})
                return await self.client.send(peer,
                    f"Удалить проект «{project['name']}», его настройки, задания и сохранённый ключ доступа?\n"
                    'Проект будет удалён для всех участников. Опубликованные посты в VK останутся.',
                    keyboard=keyboard([button('Да, удалить', 'delete_project', project=project_id)],
                                      [button('Назад', 'settings', project=project_id)]))
            if previous_dialog != {'kind': 'delete_project', 'project': project_id}:
                raise Conflict('Подтверждение устарело. Откройте удаление проекта заново.')
            self.store.delete_project(actor, project_id, self.settings.owner_id)
            await self.client.send(peer, 'Проект удалён.')
            return await self.communities(actor, peer)
        p = self.store.require(actor, project_id)
        self.store.select(actor, project_id)
        if screen == 'workspace':
            return await self.workspace(actor, peer, project_id)
        if screen == 'browser':
            if actor != p['owner']:
                raise Conflict('Площадку публикации настраивает владелец проекта.')
            return await self.client.send(peer,
                'Выберите площадку. Вход в аккаунт выполняется на компьютере с ботом. '
                'После изменения площадки нужно создать новое задание.\n'
                + ('Текущая: ' + p['browser_target'] if p['browser_target'] else 'Сейчас: API ВК.'),
                keyboard=keyboard([button('ВК через браузер', 'browser_vk', project=project_id)],
                                  [button('Дзен через браузер', 'browser_dzen', project=project_id)],
                                  [button('Назад', 'settings', project=project_id)]))
        if screen in ('browser_vk', 'browser_dzen'):
            if actor != p['owner']:
                raise Conflict('Площадку публикации настраивает владелец проекта.')
            platform = screen.removeprefix('browser_')
            return await self.prompt(actor, peer,
                {'kind': 'browser_target', 'project': project_id, 'platform': platform},
                'Отправьте HTTPS-ссылку на сообщество ВК.' if platform == 'vk' else
                'Отправьте ссылку на редактор канала: https://dzen.ru/profile/editor/id/ID_КАНАЛА')
        if screen == 'compose':
            return await self.prompt(actor, peer, {'kind': 'post', 'project': project_id},
                f"Проект: {p['name']}. О чём написать?\nОтправьте тему, важные факты, пожелания к тексту "
                'и нужный объём одним сообщением. Можно приложить одно фото для подготовки текста. '
                'Фото необязательно и не публикуется автоматически.')
        if screen == 'image_options':
            return await self.client.send(peer, f"Изображений на публикацию: {p['image_count']}\n"
                f"Формат: {p['image_format']}\nИзменения применятся к новым заданиям.", keyboard=keyboard(
                    [button(str(n), 'image_count', project=project_id, count=n) for n in range(4)],
                    [button(label, 'image_format', project=project_id, format=value) for label, value in
                     (('Квадрат', 'square'), ('Горизонтально', 'landscape'), ('Вертикально', 'portrait'))],
                    [button('Правила изображения', 'field', project=project_id, field='image_prompt')],
                    [button('Назад', 'settings', project=project_id)]))
        if screen in ('image_count', 'image_format'):
            self.store.configure(actor, screen, int(payload['count']) if screen == 'image_count' else payload['format'])
            return await self.menu(actor, peer, {'screen': 'image_options', 'project': project_id})
        if screen in ('settings', 'materials'):
            def field_button(field):
                label = ('✅ ' if p[field].strip() else '') + PROJECT_FIELDS[field]
                return button(label, 'field', project=project_id, field=field)

            def material_button(label, kind, target):
                if self.store.material(actor, project_id, kind):
                    label = '✅ ' + label
                return button(label, target, project=project_id)

            rows = [[field_button('concept'), field_button('text_prompt')],
                    [material_button('Персонажи', 'character', 'characters')],
                    [button('Генерация изображений', 'image_options', project=project_id)],
                    [field_button('text_template'), field_button('image_template')],
                    back(project_id)]
            if actor in (p['owner'], self.settings.owner_id):
                rows.insert(-1, [button('Удалить проект', 'delete_project_confirm', project=project_id)])
            return await self.client.send(peer, f"Настройки проекта: {p['name']}\nВыберите раздел.", keyboard=keyboard(*rows))
        if screen in ('field', 'field_add'):
            field = payload['field']
            if field in ('tone', 'visual_style'):
                raise Conflict('Тон и оформление теперь задаются в концепции проекта. Откройте настройки заново.')
            if field not in PROJECT_FIELDS:
                raise Conflict('Неизвестная настройка проекта.')
            if screen == 'field':
                value = p[field] or ('Не задано. По умолчанию: ' + SHORT_PROMPT if field == 'text_prompt' else 'Не задано.')
                return await self.client.send(peer, f"{PROJECT_FIELDS[field]} · {p['name']}\n\n{value}", keyboard=keyboard(
                    [button('Изменить' if field in ('text_template', 'image_template') else 'Добавить',
                            'field_add', project=project_id, field=field)],
                    [button('Назад', 'settings', project=project_id)]))
            return await self.prompt(actor, peer, {'kind': 'field', 'project': project_id, 'field': field},
                                     PROJECT_FIELDS[field] + ': отправьте новое значение текстом. '
                                     + ('Оно заменит промпт только этого проекта. Концепция, тон, материалы '
                                        'и задание будут добавляться автоматически.'
                                        if field in ('text_template', 'image_template') else
                                        'Оно заменит текущее. Один дефис очищает настройку.'))
        if screen == 'connection':
            await self.command(actor, peer, '/connection', '', {})
            return await self.client.send(peer, 'Подключение сообщества к публикациям. Для подготовки текста авторизация не обязательна.',
                keyboard=keyboard([button('Авторизоваться в ВК', 'authorize', project=project_id)],
                                  [button('Изменить сообщество', 'group', project=project_id)],
                                  [button('Отключить', 'disconnect_confirm', project=project_id)], back(project_id)))
        if screen == 'authorize':
            return await self.connect(actor, peer, project_id)
        if screen == 'group':
            return await self.prompt(actor, peer, {'kind': 'group', 'project': project_id},
                                     'Отправьте ссылку или числовой ID сообщества ВК.')
        if screen == 'disconnect_confirm':
            return await self.client.send(peer, 'Отключить публикации для этого проекта?', keyboard=keyboard(
                [button('Да, отключить', 'disconnect', project=project_id)], back(project_id)))
        if screen == 'disconnect':
            await self.command(actor, peer, '/disconnect', '', {})
            return await self.workspace(actor, peer, project_id)
        if screen == 'jobs':
            jobs = self.store.jobs(actor)
            page = min(max(0, int(payload.get('page', 0))), max(0, (len(jobs) - 1) // 4))
            rows = [[button(f"№{j['id']} · {self.job_label(j['status'])}", 'job', project=project_id, job=j['id'])]
                    for j in jobs[page * 4:page * 4 + 4]]
            nav = []
            if page:
                nav.append(button('Ранее', 'jobs', project=project_id, page=page - 1))
            if (page + 1) * 4 < len(jobs):
                nav.append(button('Далее', 'jobs', project=project_id, page=page + 1))
            if nav:
                rows.append(nav)
            rows.append(back(project_id))
            return await self.client.send(peer, 'Последние задания.' if jobs else 'Заданий пока нет. Нажмите «Написать пост» в меню проекта.',
                                          keyboard=keyboard(*rows))
        if screen == 'job':
            job = self.store.job(actor, int(payload['job']))
            if job['project_id'] != project_id:
                raise Conflict('Задание относится к другому проекту.')
            self.store.select_job(actor, job['id'])
            await self.client.send(peer, f"Задание №{job['id']}: {self.job_label(job['status'])}"
                                   + ('\n' + job['remote_url'] if job['remote_url'] else ''))
            if self.store.revision(actor, job['id']):
                await self.preview(actor, peer, job['id'])
            return
        if screen == 'prompts':
            job = self.store.job(actor, int(payload['job']))
            if job['project_id'] != project_id:
                raise Conflict('Задание относится к другому проекту.')
            self.store.select_job(actor, job['id'])
            version = self.store.revision(actor, job['id'])
            if not version or not version['text_prompt']:
                raise Conflict('У старого задания нет сохранённых промптов. Создайте новое.')
            await self.client.send(peer, 'Промпт для текста:\n\n' + version['text_prompt'])
            return await self.client.send(peer, 'Промпт для изображения:\n\n' + version['image_prompt'],
                keyboard=keyboard([button('Назад', 'job', project=project_id, job=job['id'])]))
        if screen in ('style', 'style_add'):
            raise Conflict('Оформление теперь задаётся в концепции проекта. Откройте настройки заново.')
        if screen == 'characters':
            return await self.project_images(actor, peer, project_id, screen, int(payload.get('page', 0)))
        if screen == 'character':
            return await self.prompt(actor, peer, {'kind': 'character', 'project': project_id},
                'Прикрепите одну фотографию персонажей. Описание можно добавить в подписи. Новое фото заменит предыдущее.')
        if screen in ('references', 'reference'):
            raise Conflict('Раздел «Референсы» убран. Фото-референс можно приложить к заданию публикации.')
        raise Conflict('Неизвестное меню. Откройте /menu.')

    async def project_images(self, actor, peer, project_id, screen, page=0):
        project = self.store.require(actor, project_id)
        self.store.select(actor, project_id)
        if screen != 'characters':
            raise Conflict('Оформление теперь задаётся в концепции проекта.')
        row = self.store.material(actor, project_id, 'character')
        if row:
            await self.client.send_photo(peer, row['path'], row['description'])
        title = 'Персонажи'
        state = 'Сохранено одно фото. Новое фото заменит его.' if row else 'Пока ничего не добавлено.'
        await self.client.send(peer, f"{title} · {project['name']}\n{state}", keyboard=keyboard(
            [button('Добавить', 'character', project=project_id)],
            [button('Назад', 'settings', project=project_id)]))

    @staticmethod
    def job_label(status):
        return {'generating': 'готовится', 'review': 'черновик', 'publishing': 'публикуется',
                'published': 'опубликовано', 'failed': 'ошибка', 'unknown': 'проверьте площадку',
                'cancelled': 'отменено'}.get(status, status)

    async def group_id(self, text):
        value = text.strip()
        if value.isdecimal() and int(value) > 0:
            return int(value)
        if not value.startswith(('https://', 'http://')):
            value = 'https://' + value
        url = urlsplit(value)
        if url.hostname not in ('vk.com', 'vk.ru', 'www.vk.com', 'www.vk.ru') or url.username or url.password:
            raise Conflict('Нужна ссылка на сообщество ВК или его положительный числовой ID.')
        name = url.path.strip('/')
        match = re.fullmatch(r'(?:club|public|event)([1-9][0-9]*)', name)
        if match:
            return int(match[1])
        if not re.fullmatch(r'[A-Za-z0-9_.]{2,100}', name):
            raise Conflict('Укажите ссылку на главную страницу сообщества ВК.')
        resolved = await self.client.api('utils.resolveScreenName', screen_name=name)
        if not isinstance(resolved, dict) or resolved.get('type') not in ('group', 'page', 'event'):
            raise Conflict('Сообщество не найдено. Укажите ссылку на сообщество, а не на личный профиль.')
        return int(resolved['object_id'])

    async def dialog_input(self, actor, peer, text, message):
        state = self.store.dialog(actor)
        if not state:
            return False
        kind = state['kind']
        if await self.platform_input(actor, peer, text, state):
            return True
        if kind in ('add_name', 'add_group', 'add_key'):
            self.store.set_dialog(actor, None)
            await self.connect(actor, peer, None)
            return True
        if kind == 'admin' and actor != self.settings.owner_id:
            raise Conflict('Это действие доступно только администратору.')
        project_id = state.get('project')
        if project_id:
            self.store.require(actor, project_id)
            if kind not in ('revise', 'image', 'ai_image', 'image_task'):
                self.store.select(actor, project_id)
        if kind == 'browser_name':
            if not text or len(text) > 100:
                raise Conflict('Введите название от 1 до 100 символов.')
            project_id = self.store.create_project(actor, text)
            self.store.set_dialog(actor, None)
            await self.menu(actor, peer, {'screen': 'browser', 'project': project_id})
        elif kind == 'browser_target':
            self.store.browser_destination(actor, state['platform'], text)
            self.store.set_dialog(actor, None)
            await self.client.send(peer,
                'Площадка сохранена. Для входа на компьютере с ботом следуйте docs/browser-publishing.md. '
                'После входа можно создавать задания и подтверждать публикацию.',
                keyboard=keyboard([button('Написать пост', 'compose', project=project_id)], back(project_id)))
        elif kind == 'group':
            group = await self.group_id(text)
            self.store.configure(actor, 'vk_group', group)
            self.store.set_dialog(actor, None)
            await self.client.send(peer, 'Сообщество сохранено. Можно сразу готовить тексты. Для публикаций подключите ВК.',
                keyboard=keyboard([button('Подключить ВК', 'authorize', project=project_id)],
                                  [button('Написать пост', 'compose', project=project_id)], back(project_id)))
        elif kind == 'image_task':
            if not text or len(text) > 12000:
                raise Conflict('Отправьте задание для изображения до 12000 символов.')
            self.store.expect_review(actor, state['job'], state['revision'])
            reference = await self.photo(message, 'images')
            # Recheck after downloading: another participant may have changed the draft.
            with self.store.transaction():
                job = self.store.expect_review(actor, state['job'], state['revision'])
                if job['image_brief']:
                    raise Conflict('Задание для изображения уже сохранено. Откройте черновик заново.')
                self.store.db.execute('UPDATE jobs SET image_brief=?, image_reference_path=? WHERE id=?',
                                      (text, reference or '', state['job']))
            self.store.set_dialog(actor, None)
            await self.generate_images(actor, peer, state['job'], state['revision'],
                                       state.get('slot'), state.get('feedback', ''))
        elif kind == 'ai_image':
            if not text or len(text) > 4000:
                raise Conflict('Отправьте замечания к изображению до 4000 символов.')
            self.store.expect_review(actor, state['job'], state['revision'])
            self.store.set_dialog(actor, None)
            await self.generate_images(actor, peer, state['job'], state['revision'], state['slot'], text)
        elif kind in ('post', 'revise', 'image'):
            if kind != 'image' and (not text or len(text) > 12000):
                raise Conflict('Отправьте текст задания до 12000 символов.')
            if kind != 'post':
                self.store.expect_review(actor, state['job'], state['revision'])
                self.store.select_job(actor, state['job'])
            if kind == 'image' and not any(a.get('type') == 'photo' for a in message.get('attachments', [])):
                raise Conflict('Прикрепите фотографию. Текст поста сохранится.')
            self.store.set_dialog(actor, None)
            await self.command(actor, peer, {'post': '/post', 'revise': '/revise', 'image': '/image'}[kind],
                               str(state['job']) if kind == 'image' else text, message)
        elif kind == 'field':
            if not text or len(text) > 12000:
                raise Conflict('Отправьте значение до 12000 символов.')
            if state['field'] not in PROJECT_FIELDS:
                self.store.set_dialog(actor, None)
                raise Conflict('Тон и оформление теперь задаются в концепции проекта. Откройте настройки заново.')
            self.store.configure(actor, state['field'], '' if text == '-' else text)
            self.store.set_dialog(actor, None)
            await self.menu(actor, peer, {'screen': 'field', 'field': state['field'], 'project': project_id})
        elif kind == 'style':
            self.store.set_dialog(actor, None)
            raise Conflict('Оформление теперь задаётся в концепции проекта. Откройте настройки заново.')
        elif kind == 'character':
            if len(text) > 4000:
                raise Conflict('Подпись к изображению должна быть не длиннее 4000 символов.')
            image = await self.photo(message, 'materials')
            if not image:
                raise Conflict('Прикрепите изображение.')
            self.store.set_material(actor, project_id, kind, image, text)
            self.store.set_dialog(actor, None)
            await self.project_images(actor, peer, project_id, 'characters')
        elif kind == 'admin':
            await self.command(actor, peer, state['command'], text, message)
            self.store.set_dialog(actor, None)
            await self.menu(actor, peer, {'screen': 'admin'})
        else:
            raise Conflict('Диалог устарел. Откройте /menu.')
        return True
