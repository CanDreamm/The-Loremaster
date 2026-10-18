"""Contextual VK menus and persistent, project-bound input dialogs."""
import json
import re
from urllib.parse import urlsplit

from .store import Conflict


SHORT_PROMPT = 'Пиши короткий пост до 500 символов. Верни только готовый текст.'
PROJECT_FIELDS = {'concept': 'Концепция', 'tone': 'Тон общения',
                  'text_prompt': 'Правила для текста', 'visual_style': 'Визуальный стиль',
                  'image_prompt': 'Правила изображения',
                  'text_template': 'Промпт для текста', 'image_template': 'Промпт для изображения'}


def button(label, screen, **params):
    return {'action': {'type': 'callback', 'label': label[:40],
                       'payload': json.dumps({'action': 'menu', 'screen': screen, **params})},
            'color': 'secondary'}


def keyboard(*rows):
    return {'inline': True, 'buttons': list(rows)}


def back(project=None):
    return [button('Назад', 'workspace', project=project) if project else button('Главное меню', 'home')]


class BotMenus:
    async def home(self, actor, peer):
        self.store.set_dialog(actor, None)
        rows = [[button('Мои сообщества', 'communities')]]
        if actor == self.settings.owner_id:
            rows.append([button('Управление доступом', 'admin')])
        rows.append([button('Помощь', 'help')])
        await self.client.send(peer, 'Публикот. Выберите сообщество, чтобы подготовить короткий текст.'
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
        footer = [button('Главное меню', 'home')]
        footer.insert(0, button('Добавить сообщество', 'add'))
        rows.append(footer)
        await self.client.send(peer, (f'Мои сообщества · {page + 1}/{pages}. Выберите сообщество.' if projects
                                    else 'Сообществ пока нет. Нажмите «Добавить сообщество» или попросите администратора назначить проект.'),
                               keyboard=keyboard(*rows))

    async def workspace(self, actor, peer, project_id):
        self.store.select(actor, project_id)
        p = self.store.project(actor)
        rows = [[button('Написать текст', 'compose', project=project_id)],
                [button('Мои задания', 'jobs', project=project_id)],
                [button('Настройки проекта', 'settings', project=project_id)],
                [button('Назад', 'communities'), button('Главное меню', 'home')]]
        await self.client.send(peer, f"{p['name']}\n" + (f"Сообщество: vk.ru/club{p['vk_group']}" if p['vk_group'] else
                                                       'Сообщество ещё не указано.')
                               + '\nВыберите действие.', keyboard=keyboard(*rows))

    async def prompt(self, actor, peer, state, text):
        self.store.set_dialog(actor, state)
        project_id, kind = state.get('project'), state['kind']
        if kind == 'admin':
            previous = button('Назад', 'admin')
        elif kind in ('add_group', 'add_key'):
            previous = button('Назад', 'communities' if kind == 'add_group' else 'add')
        elif kind in ('style', 'character'):
            previous = button('Назад', 'style' if kind == 'style' else 'characters', project=project_id)
        elif kind == 'field':
            if state['field'] == 'visual_style':
                raise Conflict('Визуальный стиль теперь задаётся одним фото с подписью. Откройте настройки проекта заново.')
            previous = button('Назад', 'field', project=project_id, field=state['field'])
        elif kind in ('revise', 'image'):
            previous = button('Назад к тексту', 'job', project=project_id, job=state['job'])
        else:
            previous = back(project_id)[0]
        await self.client.send(peer, text, keyboard=keyboard([previous]))

    async def menu(self, actor, peer, payload):
        screen = payload['screen']
        self.store.set_dialog(actor, None)
        if screen == 'home':
            return await self.home(actor, peer)
        if screen == 'help':
            return await self.client.send(peer, '1. Добавьте сообщество: пришлите ссылку, затем ключ доступа VK.\n'
                '2. Выберите его в «Мои сообщества».\n3. Нажмите «Написать текст» и отправьте задание.\n'
                '4. Готовый текст можно исправить по замечанию. Публикация требует отдельного подтверждения.\n'
                'Назад отменяет ввод. Команда /menu всегда открывает главное меню.\n'
                'Полный список команд: /help.', keyboard=keyboard(back()))
        if screen == 'communities':
            return await self.communities(actor, peer, int(payload.get('page', 0)))
        if screen == 'add':
            return await self.prompt(actor, peer, {'kind': 'add_group'}, 'Пришлите ссылку на сообщество VK.')
        if screen == 'admin':
            if actor != self.settings.owner_id:
                raise Conflict('Это меню доступно только администратору.')
            return await self.client.send(peer, 'Управление доступом к боту.', keyboard=keyboard(
                [button('Разрешить доступ', 'admin_allow')],
                [button('Запретить доступ', 'admin_deny')],
                [button('Участники бота', 'admin_users')], back()))
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
        p = self.store.require(actor, project_id)
        self.store.select(actor, project_id)
        if screen == 'workspace':
            return await self.workspace(actor, peer, project_id)
        if screen == 'compose':
            return await self.prompt(actor, peer, {'kind': 'post', 'project': project_id},
                f"Сообщество: {p['name']}. О чём написать?\nОтправьте тему и важные факты одним сообщением. "
                'По умолчанию — короткий текст до 500 символов. Можно приложить одно фото-референс '
                'и описать, что с ним сделать. Фото необязательно и не публикуется автоматически.')
        if screen in ('settings', 'materials'):
            rows = [[button('Концепция', 'field', project=project_id, field='concept'),
                     button('Тон общения', 'field', project=project_id, field='tone')],
                    [button('Правила для текста', 'field', project=project_id, field='text_prompt'),
                     button('Визуальный стиль', 'style', project=project_id)],
                    [button('Персонажи', 'characters', project=project_id)],
                    [button('Промпт для текста', 'field', project=project_id, field='text_template'),
                     button('Промпт для изображения', 'field', project=project_id, field='image_template')],
                    back(project_id)]
            return await self.client.send(peer, f"Настройки проекта: {p['name']}\nВыберите раздел.", keyboard=keyboard(*rows))
        if screen in ('field', 'field_add'):
            field = payload['field']
            if field not in PROJECT_FIELDS:
                raise Conflict('Неизвестная настройка проекта.')
            if field == 'visual_style':
                return await self.menu(actor, peer, {'screen': 'style', 'project': project_id})
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
            return await self.client.send(peer, 'Последние задания.' if jobs else 'Заданий пока нет. Нажмите «Написать текст» в меню сообщества.',
                                          keyboard=keyboard(*rows))
        if screen == 'job':
            job = self.store.job(actor, int(payload['job']))
            if job['project_id'] != project_id:
                raise Conflict('Задание относится к другому сообществу.')
            self.store.select_job(actor, job['id'])
            await self.client.send(peer, f"Задание №{job['id']}: {self.job_label(job['status'])}"
                                   + ('\n' + job['remote_url'] if job['remote_url'] else ''))
            if self.store.revision(actor, job['id']):
                await self.preview(actor, peer, job['id'])
            return
        if screen == 'prompts':
            job = self.store.job(actor, int(payload['job']))
            if job['project_id'] != project_id:
                raise Conflict('Задание относится к другому сообществу.')
            self.store.select_job(actor, job['id'])
            version = self.store.revision(actor, job['id'])
            if not version or not version['text_prompt']:
                raise Conflict('У старого задания нет сохранённых промптов. Создайте новое.')
            await self.client.send(peer, 'Промпт для текста:\n\n' + version['text_prompt'])
            return await self.client.send(peer, 'Промпт для изображения:\n\n' + version['image_prompt'],
                keyboard=keyboard([button('Назад', 'job', project=project_id, job=job['id'])]))
        if screen in ('style', 'characters'):
            return await self.project_images(actor, peer, project_id, screen, int(payload.get('page', 0)))
        if screen in ('style_add', 'character'):
            kind = 'style' if screen == 'style_add' else 'character'
            hint = ('Прикрепите одну фотографию визуального стиля.' if kind == 'style'
                    else 'Прикрепите одну фотографию персонажей.')
            return await self.prompt(actor, peer, {'kind': kind, 'project': project_id},
                                     hint + ' Описание можно добавить в подписи. Новое фото заменит предыдущее.')
        if screen in ('references', 'reference'):
            raise Conflict('Раздел «Референсы» убран. Фото-референс можно приложить к заданию публикации.')
        raise Conflict('Неизвестное меню. Откройте /menu.')

    async def project_images(self, actor, peer, project_id, screen, page=0):
        project = self.store.require(actor, project_id)
        self.store.select(actor, project_id)
        style = screen == 'style'
        row = self.store.material(actor, project_id, 'style' if style else 'character')
        if row:
            await self.client.send_photo(peer, row['path'], row['description'])
        title = 'Визуальный стиль' if style else 'Персонажи'
        state = 'Сохранено одно фото. Новое фото заменит его.' if row else 'Пока ничего не добавлено.'
        if style and project['visual_style']:
            state += '\nОписание стиля: ' + project['visual_style']
        await self.client.send(peer, f"{title} · {project['name']}\n{state}", keyboard=keyboard(
            [button('Добавить', 'style_add' if style else 'character', project=project_id)],
            [button('Назад', 'settings', project=project_id)]))

    @staticmethod
    def job_label(status):
        return {'generating': 'готовится', 'review': 'черновик', 'publishing': 'публикуется',
                'published': 'опубликовано', 'failed': 'ошибка', 'unknown': 'проверьте стену',
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
        if kind == 'add_group':
            group = await self.group_id(text)
            await self.prompt(actor, peer, {'kind': 'add_key', 'group': group},
                'Пришлите ключ доступа VK для публикаций — пользовательский access_token с правами wall и photos.')
            return True
        if kind == 'add_key':
            if self.oauth is None:
                from .oauth import OAuthManager
                self.oauth = OAuthManager(self.settings, self.store, None)
            await self.oauth.add_with_token(actor, state['group'], text)
            self.store.set_dialog(actor, None)
            await self.client.send(peer, 'Сообщество добавлено.')
            await self.communities(actor, peer)
            return True
        if kind == 'admin' and actor != self.settings.owner_id:
            raise Conflict('Это действие доступно только администратору.')
        project_id = state.get('project')
        if project_id:
            self.store.require(actor, project_id)
            if kind not in ('revise', 'image'):
                self.store.select(actor, project_id)
        if kind == 'group':
            group = await self.group_id(text)
            self.store.configure(actor, 'vk_group', group)
            self.store.set_dialog(actor, None)
            await self.client.send(peer, 'Сообщество сохранено. Можно сразу готовить тексты. Для публикаций подключите ВК.',
                keyboard=keyboard([button('Подключить ВК', 'authorize', project=project_id)],
                                  [button('Написать текст', 'compose', project=project_id)], back(project_id)))
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
            if state['field'] == 'visual_style':
                raise Conflict('Визуальный стиль теперь задаётся одним фото с подписью. Откройте настройки проекта заново.')
            self.store.configure(actor, state['field'], '' if text == '-' else text)
            self.store.set_dialog(actor, None)
            await self.menu(actor, peer, {'screen': 'field', 'field': state['field'], 'project': project_id})
        elif kind in ('style', 'character'):
            if len(text) > 4000:
                raise Conflict('Подпись к изображению должна быть не длиннее 4000 символов.')
            image = await self.photo(message, 'materials')
            if not image:
                raise Conflict('Прикрепите изображение.')
            self.store.set_material(actor, project_id, kind, image, text)
            self.store.set_dialog(actor, None)
            await self.project_images(actor, peer, project_id, 'style' if kind == 'style' else 'characters')
        elif kind == 'admin':
            await self.command(actor, peer, state['command'], text, message)
            self.store.set_dialog(actor, None)
            await self.menu(actor, peer, {'screen': 'admin'})
        else:
            raise Conflict('Диалог устарел. Откройте /menu.')
        return True
