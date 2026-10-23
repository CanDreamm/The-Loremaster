import json
import sqlite3
import uuid

from .providers import AmbiguousPublish, ProviderError, VkPublisher, YandexText
from .store import Conflict
from .menus import BotMenus, button as menu_button, keyboard, back
from .prompts import build_prompts
from .browser_publish import BrowserPublisher, profile_path
from .platforms import Platforms
from .genapi import GenAPI
from .media import version_images, publication_images

HELP = (
    'Публикот — помощник публикаций\n'
    '/menu — главное меню\n'
    '/id — ваш ID ВК\n/projects — назначенные проекты\n/select НОМЕР — выбрать\n'
    '/settings — настройки\n/concept Описание\n/tone Тон общения\n'
    '/textprompt Правила текста\n/imageprompt Правила картинки\n/style — одно фото визуального стиля с описанием\n'
    '/vk НОМЕР — сообщество для публикаций\n'
    '/browser vk ССЫЛКА — публикация ВК через браузер\n'
    '/browser dzen ССЫЛКА — публикация статьи в Дзен через браузер\n'
    '/postmypost — личный токен Postmypost\n/platforms — площадки проекта\n'
    '/connect — подключить публикации через авторизацию ВК\n/connection — статус подключения\n/disconnect — отключить OAuth-ключ проекта\n'
    '/character Описание — одно фото персонажей\n/characters — показать фото\n'
    '/post Тема и тезисы — подготовить пост; можно приложить фото-референс и описать, что с ним сделать\n'
    '/revise Замечания — исправить текст\n/image НОМЕР — заменить картинку приложенным фото\n'
    '/jobs — задания проекта\n/job НОМЕР — открыть задание\n/status — текущее задание\n'
    'Текст и изображения создаются через GenAPI с настройками проекта. '
    'После согласования текста нажмите «Создать изображения». Количество 0–3 задаётся в настройках. '
    '/resume — проверить прерванную генерацию текста без повторного платного запроса. '
    'Референс не публикуется автоматически.'
)
ADMIN_HELP = '\nАдминистратор: /new Название, /allow ID, /deny ID, /users, /allprojects, /assign ПРОЕКТ ID, /unassign ПРОЕКТ ID, /members ПРОЕКТ.'


def preview_keyboard(job, is_demo=False):
    def button(label, action, color='secondary'):
        payload = json.dumps({'action': action, 'job': job['id'], 'revision': job['revision']})
        return {'action': {'type': 'callback', 'label': label, 'payload': payload}, 'color': color}
    rows = [[button('Изменить текст', 'edittext'),
             button('Заменить фото' if job['image_path'] else 'Добавить фото', 'editimage')]]
    context = json.loads(job.get('generation_context') or '{}')
    count = context.get('project', {}).get('image_count', 0)
    if not is_demo and count:
        rows.append([button('Создать / проверить изображения', 'generateimages')])
        for slot in range(len(version_images(job))):
            rows.append([button(f'Изменить изображение {slot + 1}', f'regenimage{slot}')])
    rows.append(([button('Опубликовать', 'publish', 'positive')] if not is_demo and len(version_images(job)) >= count and (job.get('destinations', '[]') != '[]' if job.get('publish_mode') == 'platforms' else job['target_group'] or job.get('browser_target')) else [])
                + [button('Отменить черновик', 'cancel', 'negative')])
    rows.append(back(job['project_id']))
    rows.insert(-1, [menu_button('Промпты', 'prompts', project=job['project_id'], job=job['id'])])
    return keyboard(*rows)


class VkBot(BotMenus):
    def __init__(self, settings, store, client, text_ai=None, publisher_factory=VkPublisher, oauth=None, browser_factory=BrowserPublisher, platforms=None, image_ai=None):
        self.settings, self.store, self.client = settings, store, client
        self.genapi = GenAPI(settings, store)
        self.text_ai = text_ai or (YandexText('', '', True) if settings.demo else self.genapi)
        self.image_ai = image_ai or self.genapi
        self.publisher_factory = publisher_factory
        self.browser_factory = browser_factory
        self.oauth = oauth
        self.platforms = platforms or Platforms(settings, store)
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
            await self.client.send(peer, 'Запись уже существует. Выберите другое название или откройте список проектов.',
                                   keyboard=keyboard([menu_button('Мои проекты', 'communities')], back()))
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
        if job['publish_mode'] == 'platforms' and job['status'] not in ('review', 'generating', 'cancelled'):
            await self.platforms.refresh(actor, ident)
            job = self.store.job(actor, ident)
        version = self.store.revision(actor, ident)
        if version is None:
            if job['status'] == 'failed' and job['generation_context']:
                return await self.client.send(peer, f'Задание №{ident}: генерация текста не завершена. '
                    'Можно проверить сохранённый запрос без повторной отправки.', keyboard=keyboard(
                        [{'action': {'type': 'callback', 'label': 'Проверить результат ИИ',
                           'payload': json.dumps({'action': 'resume', 'job': ident, 'revision': job['revision']})}}], back(job['project_id'])))
            raise Conflict('Черновик ещё не готов.')
        label = f"Проект №{job['project_id']}, задание №{ident}, версия {job['revision']}\n"
        if job['publish_mode'] == 'platforms':
            targets = json.loads(job['destinations'])
            label += 'Получатели:\n' + ('\n'.join(f"• {t['name']} ({t['channel']})" for t in targets) or 'Площадки не подключены.') + '\n'
        else:
            label += f"Публикация: vk.ru/club{job['target_group']}\n" if job['target_group'] else 'Площадки публикации не заданы.\n'
        if version['is_demo']:
            label += 'ДЕМО: публикация запрещена.\n'
        if job['reference_path']:
            label += 'Фото-референс сохранено для промпта изображения и не включено в публикацию.\n'
        context = json.loads(job['generation_context'] or '{}')
        desired = context.get('project', {}).get('image_count', 0)
        label += f'Изображений: {len(version_images(version))}' + (f' из {desired}' if desired else '') + '.\n'
        await self.client.send(peer, label)
        for path in version_images(version):
            await self.client.send_photo(peer, path)
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
            report = self.platforms.report(actor, ident) if job['publish_mode'] == 'platforms' else 'Выберите следующее действие.'
            rows = [back(job['project_id'])]
            if self.store.can_restore_upload_failure(actor, ident):
                rows.insert(0, [{'action': {'type': 'callback', 'label': 'Восстановить черновик',
                    'payload': json.dumps({'action': 'restoredraft', 'job': ident, 'revision': job['revision']})}}])
            await self.client.send(peer, report, keyboard=keyboard(*rows))

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
        if command == '/postmypost':
            return await self.menu(actor, peer, {'screen': 'pmp'})
        if command == '/platforms':
            return await self.menu(actor, peer, {'screen': 'platforms', 'project': s.project(actor)['id']})
        if command == '/browser':
            platform, _, target = args.partition(' ')
            s.browser_destination(actor, platform, target.strip())
            project = s.project(actor)
            return await self.client.send(peer,
                f"Площадка сохранена: {project['browser_target']}\n"
                'Вход в аккаунт выполняется на компьютере с ботом: см. docs/browser-publishing.md. '
                'После входа создайте новое задание. Ключ публикации не нужен.')
        if command == '/connect':
            return await self.connect(actor, peer, s.project(actor)['id'])
        if command == '/connection':
            project = s.project(actor)
            if project['publish_mode'] == 'platforms':
                return await self.menu(actor, peer, {'screen': 'platforms', 'project': project['id']})
            if project['publish_mode'] != 'vk_api':
                platform = project['publish_mode'].removesuffix('_browser')
                ready = (profile_path(self.settings.data_dir, project['id'], platform) / '.login-complete').is_file()
                return await self.client.send(peer,
                    f"Браузер · {platform}: {project['browser_target']}\n"
                    + ('Профиль создан; вход и права проверяются при публикации.' if ready else
                       'Сначала выполните вход на компьютере с ботом: docs/browser-publishing.md.'))
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
        if command in ('/post', '/revise', '/resume'):
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
            if job['publish_mode'] == 'platforms' or (job['status'] == 'failed' and job['generation_context']):
                return await self.preview(actor, peer, job['id'])
            await self.client.send(peer, f"Задание №{job['id']}: {job['status']}" + ('\n' + job['remote_url'] if job['remote_url'] else ''))
            if job['status'] == 'review':
                await self.preview(actor, peer, job['id'])
            return
        await self.client.send(peer, 'Список команд: /help. Фото отправляйте с подписью /post Тема.')

    async def generate(self, actor, peer, command, args, message):
        if command != '/resume' and (not args or len(args) > 12000):
            raise Conflict('После команды укажите тему и тезисы либо замечания.')
        s, ident = self.store, None
        try:
            project = s.project(actor)
            previous, feedback, brief = '', '', args
            if command == '/resume':
                job = s.job(actor)
                if job['status'] != 'failed' or not job['generation_context']:
                    raise Conflict('Нет прерванной генерации текста. Откройте нужное задание.')
                ident, revision = job['id'], job['revision']
                context = json.loads(job['generation_context'])
                if not isinstance(self.text_ai, GenAPI):
                    raise Conflict('Возобновление доступно для GenAPI.')
                s.db.execute("UPDATE jobs SET status='generating' WHERE id=?", (ident,))
            elif command == '/revise':
                job, old = s.revise(actor)
                ident, revision = job['id'], job['revision']
                previous, feedback, brief = '' if old['is_demo'] else old['body'], args, job['brief']
            else:
                image = await self.photo(message, 'images')
                ident, project = s.create_job(actor, brief, project_id=project['id'], reference_path=image)
                revision = 1
            job = s.job(actor, ident)
            if command != '/resume':
                project = dict(s.require(actor, job['project_id']))
                if command == '/revise' and job['generation_context']:
                    original = json.loads(job['generation_context'])['project']
                    project.update({key: original[key] for key in ('image_count', 'image_format')})
                materials = [s.material(actor, project['id'], kind) for kind in ('style', 'character')]
                context = {'project': project, 'materials': [dict(m) for m in materials if m],
                           'brief': brief, 'previous': previous, 'feedback': feedback,
                           'reference': job['reference_path']}
                s.db.execute('UPDATE jobs SET generation_context=? WHERE id=?',
                             (json.dumps(context, ensure_ascii=False), ident))
            project, materials = context['project'], context['materials']
            brief, previous, feedback = context['brief'], context['previous'], context['feedback']
            prompts = build_prompts(project, brief, materials, context['reference'], previous, feedback)
            await self.client.send(peer, 'Формирую промпты и готовлю текст. Без подтверждения пост не публикуется.')
            options = {'materials': prompts.materials, 'request_key': (actor, ident, revision)} if isinstance(self.text_ai, GenAPI) else {}
            body = await self.text_ai.generate(project['concept'], brief, previous, feedback, prompt=prompts.text, **options)
            prompts = build_prompts(project, brief, materials, context['reference'], previous, feedback,
                                    '' if self.settings.demo else body)
            s.save_version(actor, ident, revision, body, self.settings.demo, prompts=prompts)
        except Exception:
            if ident is not None:
                s.finish(actor, ident, 'failed')
            raise
        await self.preview(actor, peer, ident)

    async def generate_images(self, actor, peer, ident, revision, slot=None, feedback=''):
        s = self.store
        job = s.expect_review(actor, ident, revision)
        old = s.revision(actor, ident)
        if self.settings.demo or old['is_demo']:
            raise Conflict('В демо-режиме платная генерация изображений отключена.')
        context = json.loads(job['generation_context'] or '{}')
        project = context.get('project', dict(s.require(actor, job['project_id'])))
        count = project['image_count']
        if not count:
            raise Conflict('В настройках этого задания выбрано 0 изображений.')
        images = version_images(old)
        if slot is not None and not 0 <= slot < len(images):
            raise Conflict('Изображение не найдено.')
        saved_slots = [r[0] for r in s.db.execute("SELECT slot FROM ai_requests WHERE job_id=? AND revision=? AND kind IN ('image','image_prompt')", (ident, revision))]
        if s.db.execute("SELECT 1 FROM ai_requests WHERE job_id=? AND revision=? AND kind IN ('image','image_prompt') AND status='submitting' AND request_id=''", (ident, revision)).fetchone():
            return await self.client.send(peer,
                'У предыдущего запроса изображений не сохранён ID. Старый бот не различал отказ HTTP 422 и потерю ответа. '
                'Проверьте историю в кабинете GenAPI. Если запрос был отклонён или вы согласны на новую оплату, '
                'можно создать новую попытку изображений. Текст и известные результаты сохранятся.',
                keyboard=keyboard([{'action': {'type': 'callback', 'label': 'Создать новую попытку',
                    'payload': json.dumps({'action': 'newimages', 'job': ident, 'revision': revision})}}], back(job['project_id'])))
        slots = [slot] if slot is not None else sorted(set(saved_slots) | set(range(len(images), count)))
        if not slots:
            return await self.preview(actor, peer, ident)
        # Freeze each operation; a second participant cannot start another paid call.
        s.db.execute("UPDATE jobs SET status='generating' WHERE id=?", (ident,))
        completed = False
        try:
            await self.client.send(peer, 'Создаю изображения по согласованному тексту и референсам проекта. Это может занять несколько минут.')
            materials = json.loads(old['prompt_materials'] or '[]')
            for index in slots:
                prompt = old['image_prompt'] + f'\n\nИллюстрация {index + 1} из {count}. '
                prompt += 'Выбери отдельный эпизод публикации; сохрани общих персонажей, стиль и палитру. '
                prompt += 'Входные изображения по порядку: ' + ', '.join(m['role'] for m in materials) + '.'
                if feedback:
                    prompt += '\nЗамечания к этому изображению:\n' + feedback
                inputs = list(materials)
                if index < len(images):
                    prompt += '\nПоследний входной референс — предыдущий вариант этой картинки. Внеси изменения, сохранив остальные детали.'
                    inputs.append({'role': 'reference', 'path': images[index]})
                path = await self.image_ai.generate_image(actor, ident, revision, index, prompt, inputs, project['image_format'])
                if index < len(images):
                    images[index] = path
                elif index == len(images):
                    images.append(path)
                else:
                    raise ProviderError('Нарушен порядок изображений. Проверьте сохранённые результаты.')
            completed = True
        finally:
            s.db.execute("UPDATE jobs SET status='review' WHERE id=? AND status='generating'", (ident,))
        if completed:
            s.replace_images(actor, ident, revision, images)
        await self.preview(actor, peer, ident)

    async def connect(self, actor, peer, project_id):
        if not self.oauth or not self.oauth.settings.oauth_enabled:
            return await self.client.send(peer, 'Для публикаций без API добавьте проект и выберите площадку. Вход выполняется через браузер на компьютере с ботом.',
                keyboard=keyboard([menu_button('Добавить проект без API', 'add_browser')], back()))
        if project_id is None:
            url = self.oauth.begin_import(actor, short=True)
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
        if action == 'restoredraft':
            new = s.restore_upload_draft(actor, ident, revision)
            await self.client.send(peer, 'Текст и изображения сохранены в восстановленном черновике. '
                'ИИ повторно не вызывается. Проверьте и подтвердите публикацию.')
            return await self.preview(actor, peer, new)
        if action == 'resume':
            job = s.job(actor, ident)
            if job['revision'] != revision or job['status'] != 'failed':
                raise Conflict('Эта кнопка устарела.')
            s.select_job(actor, ident)
            return await self.generate(actor, peer, '/resume', '', {})
        job = s.expect_review(actor, ident, revision)
        if action == 'newimages':
            s.replace_images(actor, ident, revision, version_images(s.revision(actor, ident)), retry_unknown=True)
            return await self.generate_images(actor, peer, ident, revision + 1)
        if action == 'generateimages':
            return await self.generate_images(actor, peer, ident, revision)
        if action.startswith('regenimage'):
            slot = int(action.removeprefix('regenimage'))
            if not 0 <= slot < len(version_images(s.revision(actor, ident))):
                raise Conflict('Изображение не найдено.')
            return await self.prompt(actor, peer, {'kind': 'ai_image', 'project': job['project_id'],
                'job': ident, 'revision': revision, 'slot': slot},
                f'Что изменить в изображении {slot + 1}? Перегенерируется только оно, остальные сохранятся.')
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
        if job['publish_mode'] == 'platforms':
            report = await self.platforms.publish(actor, ident, revision)
            return await self.client.send(peer, report, keyboard=keyboard(back(job['project_id'])))
        if any(project[key] != job[key] for key in ('publish_mode', 'browser_target')):
            raise Conflict('Площадка проекта изменилась. Создайте новое задание.')
        browser = job['publish_mode'] != 'vk_api'
        token = None
        if browser:
            platform = job['publish_mode'].removesuffix('_browser')
            publisher = self.browser_factory(self.settings.data_dir, job['project_id'], platform)
            publisher.check_ready()
        else:
            if project['vk_group'] != job['target_group']:
                raise Conflict('Сообщество проекта изменилось. Создайте новое задание для текущего сообщества.')
            if self.oauth and self.oauth.has_connection(job['project_id']):
                token = self.oauth.token_for(actor, job['project_id'], job['target_group'])
            else:
                token = self.settings.vk_token_for(actor, job['project_id'])
            if not token:
                raise Conflict('Нет действующего ключа публикации. Выполните /connect или настройте /browser vk ССЫЛКА.')
            publisher = self.publisher_factory(token)
        job, version = s.claim(actor, ident, revision)
        try:
            publish_id = f'telegram-{ident}' if self.settings.transport == 'telegram' and not browser else ident
            url = await publisher.publish(job['browser_target'] if browser else job['target_group'],
                                          version['body'], publication_images(version), publish_id)
        except AmbiguousPublish:
            s.finish(actor, ident, 'unknown')
            raise
        except ProviderError:
            s.finish(actor, ident, 'failed')
            raise
        except Exception:
            s.finish(actor, ident, 'unknown')
            raise ProviderError('Результат публикации неизвестен. Проверьте площадку; повтора не будет.') from None
        s.finish(actor, ident, 'published', url)
        await self.client.send(peer, 'Опубликовано: ' + url, keyboard=keyboard(back(job['project_id'])))
