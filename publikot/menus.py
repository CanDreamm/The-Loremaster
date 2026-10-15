"""Contextual VK menus and persistent, project-bound input dialogs."""
import json
import re
from urllib.parse import urlsplit

from .store import Conflict


SHORT_PROMPT = 'Пиши короткий пост до 500 символов. Верни только готовый текст.'
PROJECT_FIELDS = {'concept': 'Концепция', 'tone': 'Тон общения',
                  'text_prompt': 'Правила для текста', 'visual_style': 'Визуальный стиль',
                  'image_prompt': 'Промпт изображения'}


def button(label, screen, **params):
    return {'action': {'type': 'text', 'label': label[:40],
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
        elif kind in ('reference', 'character'):
            previous = button('Назад', 'references' if kind == 'reference' else 'characters', project=project_id)
        elif kind == 'field':
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
            return await self.client.send(peer, '1. Добавьте сообщество и подключите его через ВК.\n'
                '2. Выберите его в «Мои сообщества».\n3. Нажмите «Написать текст» и отправьте задание.\n'
                '4. Готовый текст можно исправить по замечанию. Публикация требует отдельного подтверждения.\n'
                'Назад отменяет ввод. Команда /menu всегда открывает главное меню.\n'
                'Полный список команд: /help.', keyboard=keyboard(back()))
        if screen == 'communities':
            return await self.communities(actor, peer, int(payload.get('page', 0)))
        if screen == 'add':
            return await self.connect(actor, peer, None)
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
                'По умолчанию — короткий текст до 500 символов. Можно приложить фото.')
        if screen in ('settings', 'materials'):
            rows = [[button('Концепция', 'field', project=project_id, field='concept'),
                     button('Тон общения', 'field', project=project_id, field='tone')],
                    [button('Правила для текста', 'field', project=project_id, field='text_prompt'),
                     button('Визуальный стиль', 'field', project=project_id, field='visual_style')],
                    [button('Референсы', 'references', project=project_id),
                     button('Персонажи', 'characters', project=project_id)], back(project_id)]
            return await self.client.send(peer, f"Настройки проекта: {p['name']}\nВыберите раздел.", keyboard=keyboard(*rows))
        if screen in ('field', 'field_add'):
            field = payload['field']
            if field not in PROJECT_FIELDS:
                raise Conflict('Неизвестная настройка проекта.')
            if screen == 'field':
                value = p[field] or ('Не задано. По умолчанию: ' + SHORT_PROMPT if field == 'text_prompt' else 'Не задано.')
                return await self.client.send(peer, f"{PROJECT_FIELDS[field]} · {p['name']}\n\n{value}", keyboard=keyboard(
                    [button('Добавить', 'field_add', project=project_id, field=field)],
                    [button('Назад', 'settings', project=project_id)]))
            return await self.prompt(actor, peer, {'kind': 'field', 'project': project_id, 'field': field},
                                     PROJECT_FIELDS[field] + ': отправьте новое значение текстом. '
                                     'Оно заменит текущее. Один дефис очищает настройку.')
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
        if screen in ('references', 'characters'):
            return await self.project_images(actor, peer, project_id, screen, int(payload.get('page', 0)))
        if screen in ('reference', 'character'):
            hint = ('Прикрепите изображение референса. Описание можно добавить в подписи.' if screen == 'reference'
                    else 'Прикрепите изображение персонажа. В подписи можно указать Имя | Описание.')
            return await self.prompt(actor, peer, {'kind': screen, 'project': project_id}, hint)
        raise Conflict('Неизвестное меню. Откройте /menu.')

    async def project_images(self, actor, peer, project_id, screen, page=0):
        project = self.store.require(actor, project_id)
        self.store.select(actor, project_id)
        references = screen == 'references'
        rows = self.store.references(actor) if references else self.store.character_media(actor)
        page = min(max(0, page), max(0, (len(rows) - 1) // 4))
        for row in rows[page * 4:page * 4 + 4]:
            caption = row['description'] if references else '\n'.join(filter(None, [row['name'], row['description']]))
            if row['path']:
                await self.client.send_photo(peer, row['path'], caption)
            else:
                await self.client.send(peer, caption + '\nИзображение ещё не добавлено.')
        title = 'Референсы' if references else 'Персонажи'
        controls = []
        nav = []
        if page:
            nav.append(button('Ранее', screen, project=project_id, page=page - 1))
        if (page + 1) * 4 < len(rows):
            nav.append(button('Далее', screen, project=project_id, page=page + 1))
        if nav:
            controls.append(nav)
        controls.extend([[button('Добавить', 'reference' if references else 'character', project=project_id)],
                         [button('Назад', 'settings', project=project_id)]])
        state = f'Сохранено изображений: {sum(bool(row["path"]) for row in rows)}.' if rows else 'Пока ничего не добавлено.'
        await self.client.send(peer, f"{title} · {project['name']}\n{state}", keyboard=keyboard(*controls))

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
        if kind in ('add_name', 'add_group'):
            self.store.set_dialog(actor, None)
            await self.connect(actor, peer, None)
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
            self.store.configure(actor, state['field'], '' if text == '-' else text)
            self.store.set_dialog(actor, None)
            await self.menu(actor, peer, {'screen': 'field', 'field': state['field'], 'project': project_id})
        elif kind in ('reference', 'character'):
            if len(text) > 4000:
                raise Conflict('Подпись к изображению должна быть не длиннее 4000 символов.')
            name, _, description = text.partition('|')
            if kind == 'character' and len(name.strip()) > 100:
                raise Conflict('Имя персонажа должно быть не длиннее 100 символов.')
            image = await self.photo(message, 'references')
            if not image:
                raise Conflict('Прикрепите изображение.')
            if kind == 'reference':
                self.store.reference(actor, image, text, project_id=project_id)
            else:
                if not name.strip():
                    existing = {row['name'] for row in self.store.characters(actor, project_id=project_id)}
                    index = 1
                    while f'Персонаж {index}' in existing:
                        index += 1
                    name = f'Персонаж {index}'
                self.store.character(actor, name.strip(), description.strip(), image, project_id=project_id)
            self.store.set_dialog(actor, None)
            await self.project_images(actor, peer, project_id, 'references' if kind == 'reference' else 'characters')
        elif kind == 'admin':
            await self.command(actor, peer, state['command'], text, message)
            self.store.set_dialog(actor, None)
            await self.menu(actor, peer, {'screen': 'admin'})
        else:
            raise Conflict('Диалог устарел. Откройте /menu.')
        return True
