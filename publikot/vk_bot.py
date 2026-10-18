import json
import sqlite3
import uuid

from .providers import AmbiguousPublish, ProviderError, VkPublisher, YandexText
from .store import Conflict
from .menus import BotMenus, button as menu_button, keyboard, back
from .prompts import build_prompts

HELP = (
    'Публикот — помощник публикаций\n'
    '/menu — главное меню\n'
    '/id — ваш ID ВК\n/projects — назначенные проекты\n/select НОМЕР — выбрать\n'
    '/settings — настройки\n/concept Описание\n/tone Тон общения\n'
    '/textprompt Правила текста\n/imageprompt Правила картинки\n/style — одно фото визуального стиля с описанием\n'
    '/vk НОМЕР — сообщество для публикаций\n'
    '/connect — подключить публикации через авторизацию ВК\n/connection — статус подключения\n/disconnect — отключить OAuth-ключ проекта\n'
    '/character Описание — одно фото персонажей\n/characters — показать фото\n'
    '/post Тема и тезисы — подготовить пост; можно приложить фото-референс и описать, что с ним сделать\n'
    '/revise Замечания — исправить текст\n/image НОМЕР — заменить картинку приложенным фото\n'
    '/jobs — задания проекта\n/job НОМЕР — открыть задание\n/status — текущее задание\n'
    'Формируются два промпта: для текста и изображения. Генерация изображений пока не подключена. '
    'Референс не публикуется автоматически; готовое фото добавляется отдельно в предпросмотре.'
)
ADMIN_HELP = '\nАдминистратор: /new Название, /allow ID, /deny ID, /users, /allprojects, /assign ПРОЕКТ ID, /unassign ПРОЕКТ ID, /members ПРОЕКТ.'


def preview_keyboard(job, is_demo=False):
    def button(label, action, color='secondary'):
        payload = json.dumps({'action': action, 'job': job['id'], 'revision': job['revision']})
        return {'action': {'type': 'callback', 'label': label, 'payload': payload}, 'color': color}
    rows = [[button('Изменить текст', 'edittext'),
             button('Заменить фото' if job['image_path'] else 'Добавить фото', 'editimage')]]
    rows.append(([button('Опубликовать', 'publish', 'positive')] if not is_demo and job['target_group'] else [])
                + [button('Отменить черновик', 'cancel', 'negative')])
    rows.append(back(job['project_id']))
    rows.insert(-1, [menu_button('Промпты', 'prompts', project=job['project_id'], job=job['id'])])
    return keyboard(*rows)


class VkBot(BotMenus):
    def __init__(self, settings, store, client, text_ai=None, publisher_factory=VkPublisher, oauth=None):
        self.settings, self.store, self.client = settings, store, client
        self.text_ai = text_ai or YandexText(settings.yandex_key, settings.yandex_folder, settings.demo)
        self.publisher_factory = publisher_factory
        self.oauth = oauth
        if hasattr(client, 'bind_store'):
            client.bind_store(store)

    async def handle(self, event):
        if event.get('group_id') != self.settings.group_id:
            return
        if event.get('type') == 'message_event':
            return await self.callback(event)
        if event.get('type') != 'message_new':
            return
        message = event.get('object', {}).get('message', {})
        actor, peer = message.get('from_id', 0), message.get('peer_id', 0)
        if not isinstance(actor, int) or actor <= 0 or peer != actor or message.get('out'):
            return  # Только личные сообщения сообщества от пользователя.
        number = message.get('conversation_message_id') or message.get('id')
        event_id = event.get('event_id') or (f'{peer}:{number}' if number else None)
        if not event_id or not self.store.begin_event(f'{self.settings.group_id}:{event_id}'):
            return
        key = f'{self.settings.group_id}:{event_id}'
        try:
            text = (message.get('text') or '').strip()
            command, _, args = text.partition(' ')
            command = command.lower()
            if command == '/id':
                await self.client.send(peer, f'Ваш ID ВК: {actor}')
            elif not self.store.is_allowed(actor, self.settings.owner_id):
                if command in ('/start', 'начать', 'начать работу'):
                    await self.client.send(peer, f'Доступ пока не разрешён. Передайте администратору ваш ID ВК: {actor}')
            elif message.get('payload'):
                try:
                    payload = json.loads(message['payload'])
                    if not isinstance(payload, dict):
                        raise ValueError
                    await self.button_action(actor, peer, payload)
                except (ValueError, TypeError, KeyError):
                    await self.client.send(peer, 'Кнопка некорректна. Откройте /status заново.')
            else:
                if command.startswith('/') or command in ('начать', 'начать работу', 'меню'):
                    self.store.set_dialog(actor, None)
                    await self.command(actor, peer, command, args.strip(), message)
                elif not await self.dialog_input(actor, peer, text, message):
                    await self.home(actor, peer)
        except (Conflict, ProviderError) as error:
            await self.client.send(peer, str(error), keyboard=keyboard(back()))
        except ValueError:
            await self.client.send(peer, 'Проверьте числовые ID и аргументы команды. Справка: /help.', keyboard=keyboard(back()))
        except sqlite3.IntegrityError:
            await self.client.send(peer, 'Запись уже существует. Выберите другое название или откройте список сообществ.',
                                   keyboard=keyboard([menu_button('Мои сообщества', 'communities')], back()))
        except Exception:
            self.store.end_event(key, False)
            raise
        self.store.end_event(key, True)

    async def button_action(self, actor, peer, payload):
        previous = self.client.tracked_messages(peer) if hasattr(self.client, 'tracked_messages') else []
        await self.action(actor, peer, payload)
        if previous:
            try:
                await self.client.delete_messages(peer, previous)
            except ProviderError as error:
                # The action already succeeded; cleanup must not repeat it.
                print(f'Предыдущие сообщения не удалены: {error}', flush=True)

    async def callback(self, event):
        data = event.get('object', {})
        if not isinstance(data, dict):
            return
        actor, peer, event_id = data.get('user_id'), data.get('peer_id'), data.get('event_id')
        if type(actor) is not int or actor <= 0 or type(peer) is not int or peer != actor or not isinstance(event_id, str) or not 1 <= len(event_id) <= 256:
            return
        key = f'{self.settings.group_id}:callback:{peer}:{event_id}'
        fresh = self.store.begin_event(key)
        try:
            # Acknowledge before slow generation; retries acknowledge without repeating actions.
            try:
                await self.client.answer_event(actor, peer, event_id)
            except ProviderError as error:
                print(f'Ответ на callback-кнопку не отправлен: {error}', flush=True)
            if not fresh:
                return
            if not self.store.is_allowed(actor, self.settings.owner_id):
                raise Conflict('Доступ к боту отозван.')
            payload = data.get('payload')
            if isinstance(payload, str):
                payload = json.loads(payload)
            if not isinstance(payload, dict):
                raise ValueError
            await self.button_action(actor, peer, payload)
        except (Conflict, ProviderError) as error:
            await self.client.send(peer, str(error), keyboard=keyboard(back()))
        except (ValueError, TypeError, KeyError):
            await self.client.send(peer, 'Кнопка некорректна. Откройте /menu заново.', keyboard=keyboard(back()))
        except Exception:
            if fresh:
                self.store.end_event(key, False)
            raise
        if fresh:
            self.store.end_event(key, True)

    async def photo(self, message, directory):
        attachments = [a['photo'] for a in message.get('attachments', []) if a.get('type') == 'photo' and 'photo' in a]
        if not attachments:
            return None
        if len(attachments) != 1:
            raise Conflict('Для одного материала прикрепите одну фотографию.')
        sizes = attachments[0].get('sizes', [])
        if not sizes:
            raise Conflict('ВК не передал доступную фотографию. Прикрепите её заново.')
        largest = max(sizes, key=lambda item: item.get('width', 0)*item.get('height', 0))
        folder = self.settings.data_dir / directory
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = folder / (uuid.uuid4().hex + '.jpg')
        await self.client.download(largest['url'], path)
        return str(path)

    async def preview(self, actor, peer, ident):
        job = self.store.job(actor, ident)
        version = self.store.revision(actor, ident)
        if version is None:
            raise Conflict('Черновик ещё не готов.')
        label = f"Проект №{job['project_id']}, задание №{ident}, версия {job['revision']}\n"
        label += f"Публикация: vk.ru/club{job['target_group']}\n" if job['target_group'] else 'Сообщество публикации не задано.\n'
        if version['is_demo']:
            label += 'ДЕМО: публикация запрещена.\n'
        if job['reference_path']:
            label += 'Фото-референс сохранено для промпта изображения и не включено в публикацию.\n'
        if version['image_prompt'] and not version['image_path']:
            label += 'Промпт изображения готов. Генератор изображений пока не подключён.\n'
        await self.client.send(peer, label)
        if version['image_path']:
            await self.client.send_photo(peer, version['image_path'])
        if version['is_demo'] and version['text_prompt']:
            await self.client.send(peer, 'Промпт для текста:\n\n' + version['text_prompt'])
            await self.client.send(peer, 'Промпт для изображения:\n\n' + version['image_prompt'])
        else:
            await self.client.send(peer, version['body'])
        if job['status'] == 'review':
            await self.client.send(peer, 'Демо: промпты готовы. Можно внести замечания.' if version['is_demo']
                                   else 'Текст готов. Публикация — только после вашего подтверждения.',
                                   keyboard=preview_keyboard(job, bool(version['is_demo'])))
        else:
            await self.client.send(peer, 'Выберите следующее действие.', keyboard=keyboard(back(job['project_id'])))

    async def command(self, actor, peer, command, args, message):
        s, admin = self.store, self.settings.owner_id
        if command in ('/start', '/menu', 'начать', 'начать работу', 'меню'):
            return await self.home(actor, peer)
        if command == '/help':
            return await self.client.send(peer, HELP + (ADMIN_HELP if actor == admin else '')
                                          + ('\n\nДемо-режим: запросы к ИИ не выполняются.' if self.settings.demo else ''),
                                          keyboard=keyboard(back()))
        if command in ('/allow', '/deny', '/users', '/allprojects', '/new', '/assign', '/unassign', '/members'):
            if actor != admin:
                raise Conflict('Эта команда доступна только администратору.')
            if command in ('/allow', '/deny'):
                s.set_access(actor, int(args), command == '/allow', admin)
                return await self.client.send(peer, 'Доступ обновлён. Назначение проекта: /assign ПРОЕКТ ID.')
            if command == '/users':
                return await self.client.send(peer, 'Допущены к боту:\n' + '\n'.join(map(str, s.users(actor, admin))))
            if command == '/allprojects':
                return await self.client.send(peer, '\n'.join(f"№{p['id']} — {p['name']}" for p in s.all_projects(actor, admin)) or 'Проектов нет.')
            if command == '/new':
                if not args or len(args) > 100:
                    raise Conflict('Используйте /new Название — до 100 символов.')
                ident = s.create_project(actor, args)
                return await self.client.send(peer, f'Проект №{ident} создан и выбран. Задайте /concept Описание бренда.')
            if command == '/members':
                rows = s.members(actor, int(args), admin)
                return await self.client.send(peer, '\n'.join(str(r['user_id']) for r in rows) or 'Нет участников.')
            project, user = map(int, args.split())
            s.set_member(actor, project, user, command == '/assign', admin)
            return await self.client.send(peer, 'Назначения обновлены.')
        if command == '/projects':
            return await self.communities(actor, peer)
        if command == '/select':
            return await self.workspace(actor, peer, int(args))
        if command == '/connect':
            return await self.connect(actor, peer, s.project(actor)['id'])
        if command == '/connection':
            project = s.project(actor)
            row = self.oauth.status(actor, project['id']) if self.oauth else None
            if row:
                state = {'active': 'подключено', 'disconnected': 'отключено'}.get(row['status'], 'не подключено')
                if row['group_id'] != project['vk_group']:
                    state = 'сообщество изменилось — подключите ВК заново'
                elif row['status'] == 'active' and row['expires_at'] and row['expires_at'] <= self.oauth.now():
                    state = 'срок доступа истёк — подключите ВК заново'
                return await self.client.send(peer, f"Публикации: {state}. Сообщество: {row['group_id']}.")
            return await self.client.send(peer, 'ВК ещё не подключён через приложение. Для публикаций нажмите «Авторизоваться в ВК». '
                                          'Ранее заданные вручную ключи проверяются отдельно.')
        if command == '/disconnect':
            project = s.project(actor)
            if not self.oauth:
                raise Conflict('OAuth не настроен. Ключи из .env.python отключаются на сервере.')
            self.oauth.disconnect(actor, project['id'])
            return await self.client.send(peer, 'OAuth-ключ проекта удалён с сервера. При необходимости отзовите доступ приложению также в настройках ВК.')
        fields = {'/concept': 'concept', '/tone': 'tone', '/textprompt': 'text_prompt', '/imageprompt': 'image_prompt'}
        if command in fields:
            if not args or len(args) > 12000:
                raise Conflict('Введите значение после команды. Один дефис очищает настройку.')
            s.configure(actor, fields[command], '' if args == '-' else args)
            return await self.client.send(peer, 'Настройка проекта сохранена.')
        if command == '/vk':
            group = int(args)
            if group <= 0:
                raise ValueError
            s.configure(actor, 'vk_group', group)
            return await self.client.send(peer, 'Сообщество публикаций сохранено. Для авторизации отправьте /connect. Старое OAuth-подключение к другому сообществу не используется.')
        if command == '/settings':
            return await self.menu(actor, peer, {'screen': 'settings', 'project': s.project(actor)['id']})
        if command in ('/reference', '/references'):
            raise Conflict('Референсы проекта убраны. Прикрепите фото к заданию /post и опишите, что с ним сделать.')
        if command in ('/style', '/character', '/characters'):
            project = s.project(actor)
            if command == '/characters':
                return await self.project_images(actor, peer, project['id'], 'characters')
            if len(args) > 4000:
                raise Conflict('Описание должно быть не длиннее 4000 символов.')
            image = await self.photo(message, 'materials')
            if not image:
                raise Conflict('Прикрепите одну фотографию. Описание можно добавить в подписи.')
            kind = 'style' if command == '/style' else 'character'
            s.set_material(actor, project['id'], kind, image, args)
            return await self.project_images(actor, peer, project['id'], 'style' if kind == 'style' else 'characters')
        if command in ('/post', '/revise'):
            return await self.generate(actor, peer, command, args, message)
        if command == '/image':
            job = s.job(actor, int(args))
            s.expect_review(actor, job['id'], job['revision'])
            path = await self.photo(message, 'images')
            if not path:
                raise Conflict('Прикрепите новое фото с подписью /image НОМЕР_ЗАДАНИЯ.')
            s.replace_image(actor, job['id'], job['revision'], path)
            return await self.preview(actor, peer, job['id'])
        if command == '/jobs':
            return await self.menu(actor, peer, {'screen': 'jobs', 'project': s.project(actor)['id']})
        if command in ('/job', '/status'):
            if command == '/job':
                s.select_job(actor, int(args))
            job = s.job(actor)
            await self.client.send(peer, f"Задание №{job['id']}: {job['status']}" + ('\n' + job['remote_url'] if job['remote_url'] else ''))
            if job['status'] == 'review':
                await self.preview(actor, peer, job['id'])
            return
        await self.client.send(peer, 'Список команд: /help. Фото отправляйте с подписью /post Тема.')

    async def generate(self, actor, peer, command, args, message):
        if not args or len(args) > 12000:
            raise Conflict('После команды укажите тему и тезисы либо замечания.')
        s, ident = self.store, None
        try:
            project = s.project(actor)
            previous, feedback, brief = '', '', args
            if command == '/revise':
                job, old = s.revise(actor)
                ident, revision = job['id'], job['revision']
                previous, feedback, brief = '' if old['is_demo'] else old['body'], args, job['brief']
            else:
                image = await self.photo(message, 'images')
                ident, project = s.create_job(actor, brief, project_id=project['id'], reference_path=image)
                revision = 1
            job = s.job(actor, ident)
            project = dict(s.require(actor, job['project_id']))
            materials = [s.material(actor, project['id'], kind) for kind in ('style', 'character')]
            prompts = build_prompts(project, brief, materials, job['reference_path'], previous, feedback)
            await self.client.send(peer, 'Формирую промпты и готовлю текст. Без подтверждения пост не публикуется.')
            body = await self.text_ai.generate(project['concept'], brief, previous, feedback, prompt=prompts.text)
            prompts = build_prompts(project, brief, materials, job['reference_path'], previous, feedback,
                                    '' if self.settings.demo else body)
            s.save_version(actor, ident, revision, body, self.settings.demo, prompts=prompts)
        except Exception:
            if ident is not None:
                s.finish(actor, ident, 'failed')
            raise
        await self.preview(actor, peer, ident)

    async def connect(self, actor, peer, project_id):
        if not self.oauth or not self.oauth.settings.oauth_enabled:
            raise Conflict('Авторизация пока не настроена администратором. Нужны приложение ВК и публичный HTTPS-адрес возврата.')
        if project_id is None:
            url = self.oauth.begin_import(actor)
            return await self.client.send(peer, 'Войдите в ВК тем аккаунтом, из которого пишете боту, и разрешите доступ. '
                'Затем выберите одно или несколько своих сообществ. Названия загрузятся из ВК автоматически. '
                'Ссылка действует 10 минут.', keyboard=keyboard(
                    [{'action': {'type': 'open_link', 'label': 'Выбрать сообщества ВК', 'link': url}}], back()))
        project = self.store.require(actor, project_id)
        url = self.oauth.begin(actor, project_id)
        keys = keyboard([{'action': {'type': 'open_link', 'label': 'Авторизоваться в ВК', 'link': url}}],
                        [menu_button('Проверить подключение', 'connection', project=project_id)], back(project_id))
        await self.client.send(peer, f"Подключение проекта №{project_id} — {project['name']}, сообщество {project['vk_group']}.\nВойдите тем аккаунтом ВК, из которого пишете боту. Ключ останется на сервере и разрешит публикации всем назначенным участникам этого проекта. Ссылка действует 10 минут.", keyboard=keys)

    async def action(self, actor, peer, payload):
        if payload.get('action') == 'menu':
            return await self.menu(actor, peer, payload)
        self.store.set_dialog(actor, None)
        if payload.get('action') == 'connect':
            return await self.connect(actor, peer, int(payload['project']))
        action, ident, revision = payload['action'], int(payload['job']), int(payload['revision'])
        s = self.store
        job = s.expect_review(actor, ident, revision)
        if action == 'cancel':
            s.cancel(actor, ident, revision)
            return await self.client.send(peer, 'Задание отменено.', keyboard=keyboard(back(job['project_id'])))
        if action in ('edittext', 'editimage'):
            s.select_job(actor, ident)
            text = ('Что изменить в тексте? Отправьте замечания одним сообщением.' if action == 'edittext'
                    else 'Прикрепите новое фото. Текст поста сохранится.')
            return await self.prompt(actor, peer, {'kind': 'revise' if action == 'edittext' else 'image',
                'project': job['project_id'], 'job': ident, 'revision': revision}, text)
        if action != 'publish':
            raise Conflict('Неизвестная кнопка.')
        project = s.require(actor, job['project_id'])
        if project['vk_group'] != job['target_group']:
            raise Conflict('Сообщество проекта изменилось. Создайте новое задание для текущего сообщества.')
        if self.oauth and self.oauth.has_connection(job['project_id']):
            token = self.oauth.token_for(actor, job['project_id'], job['target_group'])
        else:
            token = self.settings.vk_token_for(actor, job['project_id'])
        if not token:
            raise Conflict('Нет действующего ключа публикации. Выполните /connect для выбранного проекта. Ключ сообщества бота не используется.')
        job, version = s.claim(actor, ident, revision)
        try:
            url = await self.publisher_factory(token).publish(job['target_group'], version['body'], version['image_path'], ident)
        except AmbiguousPublish:
            s.finish(actor, ident, 'unknown')
            raise
        except ProviderError:
            s.finish(actor, ident, 'failed')
            raise
        except Exception:
            s.finish(actor, ident, 'unknown')
            raise ProviderError('Результат публикации неизвестен. Проверьте стену; повтора не будет.') from None
        s.finish(actor, ident, 'published', url)
        await self.client.send(peer, 'Опубликовано: ' + url, keyboard=keyboard(back(job['project_id'])))
