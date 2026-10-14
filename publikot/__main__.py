import asyncio
import logging
import sqlite3
import sys
import uuid

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .providers import AmbiguousPublish, ProviderError, VkPublisher, YandexText
from .settings import ConfigError, Settings
from .store import Conflict, Store

HELP = (
    'Публикот\n/projects — назначенные проекты\n/select НОМЕР — выбрать\n'
    '/concept Описание — концепция выбранного проекта\n/vk НОМЕР — ID сообщества, положительное число\n'
    '/character Имя | Описание — сохранить персонажа (можно с фото)\n/characters — персонажи проекта\n'
    '/settings — настройки проекта\n/tone Тон\n/textprompt Промпт текста\n/imageprompt Промпт изображения\n/style Визуальный стиль\n'
    '/reference Описание — сохранить приложенное фото как референс\n/references — референсы проекта\n'
    '/post Тема и тезисы — подготовить пост\nМожно отправить фото с подписью /post Тема и тезисы.\n'
    '/revise Замечание — переделать текст последнего задания\n/status — состояние последнего задания\n'
    '/jobs — задания проекта\n/job НОМЕР — открыть задание\n/id — ваш Telegram ID\n'
    'После предпросмотра используйте кнопки подтверждения или отмены.\n'
    'Генерация изображений пока не подключена: используются ваши фотографии.'
)


class Access(BaseMiddleware):
    def __init__(self, settings, store):
        self.settings, self.store = settings, store

    async def __call__(self, handler, event, data):
        user = event.from_user
        message = event if isinstance(event, Message) else event.message
        if not user or user.is_bot or not message or message.chat.type != 'private':
            return
        if isinstance(event, Message) and (event.text or '').split('@')[0].strip() == '/id':
            return await event.answer(f'Ваш Telegram ID: {user.id}')
        if not self.store.is_allowed(user.id, self.settings.owner_id):
            if isinstance(event, CallbackQuery):
                return await event.answer('Доступ к боту не разрешён.', show_alert=True)
            if (event.text or '').split('@')[0].strip() == '/start':
                return await event.answer(f'Доступ пока не разрешён. Передайте администратору ваш ID: {user.id}')
            return
        data['actor_id'] = user.id
        return await handler(event, data)


def keyboard(job):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='Опубликовать', callback_data=f"publish:{job['id']}:{job['revision']}"),
        InlineKeyboardButton(text='Отменить', callback_data=f"cancel:{job['id']}:{job['revision']}")]])


def build_router(settings, store):
    router = Router()
    text_ai = YandexText(settings.yandex_key, settings.yandex_folder, settings.demo)
    router.message.outer_middleware(Access(settings, store))
    router.callback_query.outer_middleware(Access(settings, store))

    async def preview(message, ident, actor_id):
        job = store.job(actor_id, ident)
        version = store.revision(actor_id, ident)
        label = f"Проект №{job['project_id']}, задание №{ident}, версия {job['revision']}\n"
        label += f"ВК: club{job['target_group']}\n" if job['target_group'] else 'ВК не настроен — публикация заблокирована.\n'
        if version['is_demo']:
            label += 'ДЕМО: публикация заблокирована.\n'
        if job['image_path']:
            await message.answer_photo(FSInputFile(job['image_path']))
        body = version['body']
        await message.answer(label)
        for index in range(0, len(body), 3500):
            await message.answer(body[index:index + 3500])
        await message.answer('Подтвердите эту версию или отправьте /revise Ваши замечания.', reply_markup=keyboard(job))

    @router.message(Command('start', 'help'))
    async def help_command(message: Message, actor_id: int):
        admin = '\nАдминистратор: /new Название, /allow ID, /deny ID, /users, /allprojects, /assign ПРОЕКТ ID, /unassign ПРОЕКТ ID, /members ПРОЕКТ.' if actor_id == settings.owner_id else ''
        await message.answer(HELP + admin + ('\n\nВключён безопасный демо-режим, запросов к ИИ нет.' if settings.demo else ''))

    @router.message(Command('allow', 'deny', 'users'))
    async def access_command(message: Message, command: CommandObject, actor_id: int):
        try:
            if actor_id != settings.owner_id:
                raise Conflict('Эта команда доступна только администратору.')
            if command.command == 'users':
                text = 'Допущены к боту:\n' + '\n'.join(map(str, store.users(actor_id, settings.owner_id)))
                for index in range(0, len(text), 3500):
                    await message.answer(text[index:index+3500])
                return
            user = int((command.args or '').strip())
            store.set_access(actor_id, user, command.command == 'allow', settings.owner_id)
            await message.answer('Доступ обновлён. Проекты назначаются отдельно через /assign ПРОЕКТ ID.')
        except ValueError:
            await message.answer('Нужен числовой Telegram ID: /allow ID или /deny ID.')
        except Conflict as error:
            await message.answer(str(error))

    @router.message(Command('assign', 'unassign', 'members', 'allprojects'))
    async def members(message: Message, command: CommandObject, actor_id: int):
        try:
            if actor_id != settings.owner_id:
                raise Conflict('Эта команда доступна только администратору.')
            if command.command == 'allprojects':
                rows = store.all_projects(actor_id, settings.owner_id)
                text = '\n'.join(f"№{r['id']} — {r['name']}" for r in rows) or 'Проектов нет.'
                for index in range(0, len(text), 3500):
                    await message.answer(text[index:index+3500])
                return
            if command.command == 'members':
                project_id = int((command.args or '').strip())
                rows = store.members(actor_id, project_id, settings.owner_id)
                text = '\n'.join(str(r['user_id']) for r in rows) or 'Нет назначенных участников.'
                for index in range(0, len(text), 3500):
                    await message.answer(text[index:index+3500])
                return
            args = (command.args or '').split()
            if len(args) != 2:
                raise ValueError
            project_id, user = map(int, args)
            if user <= 0:
                raise ValueError
            store.set_member(actor_id, project_id, user, command.command == 'assign', settings.owner_id)
            await message.answer('Участники проекта обновлены.')
        except ValueError:
            await message.answer('Используйте /assign ПРОЕКТ ID, /unassign ПРОЕКТ ID или /members ПРОЕКТ.')
        except Conflict as error:
            await message.answer(str(error))

    @router.message(Command('new'))
    async def new(message: Message, command: CommandObject, actor_id: int):
        if actor_id != settings.owner_id:
            return await message.answer('Проекты создаёт администратор. Ваши проекты: /projects.')
        name = (command.args or '').strip()
        if not name or len(name) > 100:
            return await message.answer('Используйте /new Название — до 100 символов.')
        try:
            ident = store.create_project(actor_id, name)
        except sqlite3.IntegrityError:
            return await message.answer('Проект с таким именем уже существует. Посмотрите /projects.')
        await message.answer(f'Проект №{ident} создан и выбран. Задайте /concept Описание бренда.')

    @router.message(Command('projects'))
    async def projects(message: Message, actor_id: int):
        rows = store.projects(actor_id)
        text = '\n'.join(f"№{p['id']} — {p['name']}" for p in rows) or 'Проектов нет. Отправьте /new Название.'
        for index in range(0, len(text), 3500):
            await message.answer(text[index:index + 3500])

    @router.message(Command('settings', 'tone', 'textprompt', 'imageprompt', 'style'))
    async def project_settings(message: Message, command: CommandObject, actor_id: int):
        fields = {'tone': 'tone', 'textprompt': 'text_prompt', 'imageprompt': 'image_prompt', 'style': 'visual_style'}
        try:
            project = store.project(actor_id)
            if command.command == 'settings':
                labels = {'concept': 'Концепция', 'tone': 'Тон', 'text_prompt': 'Промпт текста', 'image_prompt': 'Промпт изображения', 'visual_style': 'Визуальный стиль'}
                text = f"Проект №{project['id']} — {project['name']}\n"
                text += '\n\n'.join(f'{label}: {project[field] or "не задано"}' for field, label in labels.items())
                for index in range(0, len(text), 3500):
                    await message.answer(text[index:index+3500])
                return
            value = (command.args or '').strip()
            if not value or len(value) > 12000:
                return await message.answer('Введите значение после команды, до 12 000 символов. Для очистки — один дефис.')
            store.configure(actor_id, fields[command.command], '' if value == '-' else value)
            await message.answer('Настройки проекта сохранены. Они применяются к следующим генерациям.')
        except Conflict as error:
            await message.answer(str(error))

    @router.message(Command('reference', 'references'))
    async def references(message: Message, command: CommandObject, actor_id: int):
        try:
            project = store.project(actor_id)
            if command.command == 'references':
                rows = store.references(actor_id)
                if not rows:
                    return await message.answer('Референсов нет. Отправьте фото с подписью /reference Описание.')
                for row in rows[-10:]:
                    await message.answer_photo(FSInputFile(row['path']), caption=row['description'][:1000] or 'Референс проекта')
                return
            store.require(actor_id, project['id'])
            if not message.photo or (message.photo[-1].file_size or 0) > 10 * 1024 * 1024:
                return await message.answer('Нужно фото до 10 МБ с подписью /reference Описание.')
            directory = settings.data_dir / 'references'
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            image = str(directory / (uuid.uuid4().hex + '.jpg'))
            await message.bot.download(message.photo[-1], destination=image)
            store.reference(actor_id, image, (command.args or '')[:4000], project_id=project['id'])
            await message.answer('Референс проекта сохранён. Использование в генераторе изображений ещё не подключено.')
        except Conflict as error:
            await message.answer(str(error))
        except Exception:
            await message.answer('Не удалось сохранить или показать референс.')

    @router.message(Command('select', 'concept', 'vk'))
    async def configure(message: Message, command: CommandObject, actor_id: int):
        value = (command.args or '').strip()
        try:
            if command.command == 'select':
                store.select(actor_id, int(value))
            elif command.command == 'vk':
                ident = int(value)
                if ident <= 0:
                    raise ValueError
                store.configure(actor_id, 'vk_group', ident)
            else:
                if not value or len(value) > 12000:
                    raise ValueError
                store.configure(actor_id, 'concept', value)
            await message.answer('Сохранено. Изменение сообщества применяется только к новым заданиям.')
        except ValueError:
            await message.answer('Для /select и /vk нужен положительный числовой ID. Для /concept — описание до 12 000 символов.')
        except Conflict as error:
            await message.answer(str(error))

    @router.message(Command('post', 'revise'))
    async def generate(message: Message, command: CommandObject, actor_id: int):
        brief = (command.args or '').strip()
        if not brief or len(brief) > 12000:
            return await message.answer('После команды укажите тему и тезисы или замечания — до 12 000 символов.')
        ident = None
        try:
            project = store.project(actor_id)
            previous, feedback = '', ''
            if command.command == 'revise':
                job, old = store.revise(actor_id)
                ident, revision = job['id'], job['revision']
                previous, feedback, brief = old['body'], brief, job['brief']
            else:
                image = None
                if message.photo:
                    photo = message.photo[-1]
                    if (photo.file_size or 0) > 10 * 1024 * 1024:
                        return await message.answer('Фото слишком большое. Максимум 10 МБ.')
                    image_dir = settings.data_dir / 'images'
                    image_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                    image = str(image_dir / (uuid.uuid4().hex + '.jpg'))
                    await message.bot.download(photo, destination=image)
                ident, project = store.create_job(actor_id, brief, image, project_id=project['id'])
                revision = 1
            await message.answer('Готовлю текст. Публикации без подтверждения не будет.')
            context = '\n\n'.join(filter(None, [project['concept'], 'Тон: ' + project['tone'] if project['tone'] else '', project['text_prompt']]))
            body = await text_ai.generate(context, brief, previous, feedback)
            store.save_version(actor_id, ident, revision, body, settings.demo)
        except (Conflict, ProviderError) as error:
            if ident is not None:
                store.finish(actor_id, ident, 'failed')
            return await message.answer(str(error))
        except Exception:
            if ident is not None:
                store.finish(actor_id, ident, 'failed')
            return await message.answer('Не удалось подготовить материал. Проверьте подключение и /status.')
        try:
            await preview(message, ident, actor_id)
        except Exception:
            await message.answer('Материал сохранён, но предпросмотр не доставлен. Повторите через /status.')

    @router.message(Command('character', 'characters'))
    async def character(message: Message, command: CommandObject, actor_id: int):
        try:
            project = store.project(actor_id)
            if command.command == 'characters':
                rows = store.characters(actor_id)
                text = '\n'.join(f"{p['name']}: {p['description']} (фото: {p['images']})" for p in rows) or 'Персонажей нет.'
                for index in range(0, len(text), 3500):
                    await message.answer(text[index:index+3500])
                return
            name, _, description = (command.args or '').partition('|')
            name, description = name.strip(), description.strip()
            if not name or len(name) > 100 or len(description) > 4000:
                return await message.answer('Используйте /character Имя | Описание. Имя до 100, описание до 4000 символов.')
            image = None
            if message.photo:
                if (message.photo[-1].file_size or 0) > 10 * 1024 * 1024:
                    return await message.answer('Максимальный размер фото — 10 МБ.')
                directory = settings.data_dir / 'references'
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                image = str(directory / (uuid.uuid4().hex + '.jpg'))
                await message.bot.download(message.photo[-1], destination=image)
            store.character(actor_id, name, description, image, project_id=project['id'])
            await message.answer('Персонаж сохранён. Для другого ракурса отправьте фото с тем же именем. Генерация по референсам пока не подключена.')
        except Conflict as error:
            await message.answer(str(error))
        except Exception:
            await message.answer('Не удалось сохранить персонажа. Попробуйте ещё раз.')

    @router.message(Command('status'))
    async def status(message: Message, actor_id: int):
        try:
            job = store.job(actor_id)
            await message.answer(f"Задание №{job['id']}: {job['status']}" + ('\n' + job['remote_url'] if job['remote_url'] else ''))
            if job['status'] == 'review':
                await preview(message, job['id'], actor_id)
        except Conflict as error:
            await message.answer(str(error))

    @router.message(Command('jobs', 'job'))
    async def project_jobs(message: Message, command: CommandObject, actor_id: int):
        try:
            if command.command == 'job':
                store.select_job(actor_id, int((command.args or '').strip()))
                job = store.job(actor_id)
                if job['status'] == 'review':
                    return await preview(message, job['id'], actor_id)
                return await message.answer(f"Задание №{job['id']}: {job['status']}")
            rows = store.jobs(actor_id)
            await message.answer('\n'.join(f"№{r['id']}, версия {r['revision']}: {r['status']}" for r in rows) or 'Заданий нет.')
        except ValueError:
            await message.answer('Нужен номер: /job НОМЕР.')
        except Conflict as error:
            await message.answer(str(error))

    @router.callback_query(F.data.regexp(r'^(publish|cancel):\d+:\d+$'))
    async def approve(callback: CallbackQuery, actor_id: int):
        await callback.answer()
        action, ident, revision = callback.data.split(':')
        ident, revision = int(ident), int(revision)
        try:
            if action == 'cancel':
                store.cancel(actor_id, ident, revision)
                return await callback.message.answer('Задание отменено.')
            candidate = store.job(actor_id, ident)
            project = store.require(actor_id, candidate['project_id'])
            token = settings.vk_token_for(actor_id, project['id'])
            if not token:
                raise Conflict('Ключ ВК не настроен. Публикация не выполнялась.')
            job, version = store.claim(actor_id, ident, revision)
        except Conflict as error:
            return await callback.message.answer(str(error))
        try:
            url = await VkPublisher(token).publish(job['target_group'], version['body'], job['image_path'], ident)
        except AmbiguousPublish as error:
            store.finish(actor_id, ident, 'unknown')
            return await callback.message.answer(str(error))
        except ProviderError as error:
            store.finish(actor_id, ident, 'failed')
            return await callback.message.answer(str(error))
        except Exception:
            store.finish(actor_id, ident, 'unknown')
            return await callback.message.answer('Результат неизвестен. Проверьте стену ВК. Автоматического повтора не будет.')
        store.finish(actor_id, ident, 'published', url)
        await callback.message.answer('Опубликовано: ' + url)

    @router.message()
    async def fallback(message: Message, actor_id: int):
        await message.answer('Отправьте /help для списка команд. Фото прикрепляйте с подписью /post Тема и тезисы.')

    return router


async def run():
    identify = '--identify' in sys.argv
    # Для определения ID нужен только токен. Остальные команды требуют владельца.
    if identify:
        import os
        from dotenv import load_dotenv
        from pathlib import Path
        load_dotenv(Path(__file__).resolve().parent.parent / '.env.python', override=False)
        token, proxy = os.getenv('TELEGRAM_BOT_TOKEN', ''), os.getenv('TELEGRAM_PROXY') or None
        if not token:
            raise ConfigError('Задайте TELEGRAM_BOT_TOKEN в .env.python')
    else:
        settings = Settings.load()
        token, proxy = settings.token, settings.proxy
    async with Bot(token, session=AiohttpSession(proxy=proxy)) as bot:
        if '--clear-webhook' in sys.argv:
            await bot.delete_webhook(drop_pending_updates=False)
            print('Вебхук отключён, ожидающие обновления сохранены.')
            return
        webhook = await bot.get_webhook_info()
        if webhook.url:
            raise ConfigError('У бота активен вебхук. Отключите старый Telegram workflow и выполните python -m publikot --identify --clear-webhook.')
        if identify:
            print('Отправьте /start боту в личном сообщении. Ожидание 30 секунд.')
            updates = await bot.get_updates(timeout=30, allowed_updates=['message'])
            owners = {u.message.from_user.id for u in updates if u.message and u.message.chat.type == 'private' and u.message.text == '/start' and not u.message.from_user.is_bot}
            for ident in sorted(owners):
                print('Telegram ID отправителя /start:', ident)
            if not owners:
                print('Сообщение не найдено. Отправьте /start и повторите команду.')
            return
        store = Store(settings.data_dir / 'publikot.sqlite3')
        dp = Dispatcher()
        dp.include_router(build_router(settings, store))
        print('Публикот запущен. Демо-режим:', settings.demo, flush=True)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == '__main__':
    # Сетевые исключения библиотек могут содержать адреса с токенами — не печатаем их.
    logging.basicConfig(level=logging.CRITICAL)
    try:
        asyncio.run(run())
    except ConfigError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
    except (KeyboardInterrupt, SystemExit):
        pass
    except Exception as error:
        print(f'Запуск остановлен ({type(error).__name__}). Проверьте доступ к Telegram, прокси и настройки .env. Секреты не выводятся.', file=sys.stderr)
        sys.exit(1)
