import asyncio
import logging
import sqlite3
import sys
import uuid

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from .providers import AmbiguousPublish, ProviderError, VkPublisher, YandexText
from .settings import ConfigError, Settings
from .store import Conflict, Store

HELP = (
    'Публикот\n/new Название — создать проект\n/projects — список\n/select НОМЕР — выбрать\n'
    '/concept Описание — концепция выбранного проекта\n/vk НОМЕР — ID сообщества, положительное число\n'
    '/character Имя | Описание — сохранить персонажа (можно с фото)\n/characters — персонажи проекта\n'
    '/post Тема и тезисы — подготовить пост\nМожно отправить фото с подписью /post Тема и тезисы.\n'
    '/revise Замечание — переделать текст последнего задания\n/status — состояние последнего задания\n'
    'После предпросмотра используйте кнопки подтверждения или отмены.\n'
    'Генерация изображений пока не подключена: используются ваши фотографии.'
)


def keyboard(job):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='Опубликовать', callback_data=f"publish:{job['id']}:{job['revision']}"),
        InlineKeyboardButton(text='Отменить', callback_data=f"cancel:{job['id']}:{job['revision']}")]])


def build_router(settings, store):
    router = Router()
    text_ai = YandexText(settings.yandex_key, settings.yandex_folder, settings.demo)
    publisher = VkPublisher(settings.vk_token)

    # Фильтры применяются ко всем сообщениям и callback, до чтения проектов.
    router.message.filter(F.from_user.id == settings.owner_id, F.chat.type == 'private')
    router.callback_query.filter(F.from_user.id == settings.owner_id, F.message.chat.type == 'private')

    async def preview(message, ident):
        job = store.job(settings.owner_id, ident)
        version = store.revision(settings.owner_id, ident)
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
    async def help_command(message: Message):
        await message.answer(HELP + ('\n\nВключён безопасный демо-режим, запросов к ИИ нет.' if settings.demo else ''))

    @router.message(Command('new'))
    async def new(message: Message, command: CommandObject):
        name = (command.args or '').strip()
        if not name or len(name) > 100:
            return await message.answer('Используйте /new Название — до 100 символов.')
        try:
            ident = store.create_project(settings.owner_id, name)
        except sqlite3.IntegrityError:
            return await message.answer('Проект с таким именем уже существует. Посмотрите /projects.')
        await message.answer(f'Проект №{ident} создан и выбран. Задайте /concept Описание бренда.')

    @router.message(Command('projects'))
    async def projects(message: Message):
        rows = store.projects(settings.owner_id)
        text = '\n'.join(f"№{p['id']} — {p['name']}" for p in rows) or 'Проектов нет. Отправьте /new Название.'
        for index in range(0, len(text), 3500):
            await message.answer(text[index:index + 3500])

    @router.message(Command('select', 'concept', 'vk'))
    async def configure(message: Message, command: CommandObject):
        value = (command.args or '').strip()
        try:
            if command.command == 'select':
                store.select(settings.owner_id, int(value))
            elif command.command == 'vk':
                ident = int(value)
                if ident <= 0:
                    raise ValueError
                store.configure(settings.owner_id, 'vk_group', ident)
            else:
                if not value or len(value) > 12000:
                    raise ValueError
                store.configure(settings.owner_id, 'concept', value)
            await message.answer('Сохранено. Изменение сообщества применяется только к новым заданиям.')
        except ValueError:
            await message.answer('Для /select и /vk нужен положительный числовой ID. Для /concept — описание до 12 000 символов.')
        except Conflict as error:
            await message.answer(str(error))

    @router.message(Command('post', 'revise'))
    async def generate(message: Message, command: CommandObject):
        brief = (command.args or '').strip()
        if not brief or len(brief) > 12000:
            return await message.answer('После команды укажите тему и тезисы или замечания — до 12 000 символов.')
        ident = None
        try:
            project = store.project(settings.owner_id)
            previous, feedback = '', ''
            if command.command == 'revise':
                job, old = store.revise(settings.owner_id)
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
                ident, project = store.create_job(settings.owner_id, brief, image)
                revision = 1
            await message.answer('Готовлю текст. Публикации без подтверждения не будет.')
            body = await text_ai.generate(project['concept'], brief, previous, feedback)
            store.save_version(settings.owner_id, ident, revision, body, settings.demo)
        except (Conflict, ProviderError) as error:
            if ident is not None:
                store.finish(settings.owner_id, ident, 'failed')
            return await message.answer(str(error))
        except Exception:
            if ident is not None:
                store.finish(settings.owner_id, ident, 'failed')
            return await message.answer('Не удалось подготовить материал. Проверьте подключение и /status.')
        try:
            await preview(message, ident)
        except Exception:
            await message.answer('Материал сохранён, но предпросмотр не доставлен. Повторите через /status.')

    @router.message(Command('character', 'characters'))
    async def character(message: Message, command: CommandObject):
        try:
            store.project(settings.owner_id)
            if command.command == 'characters':
                rows = store.characters(settings.owner_id)
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
            store.character(settings.owner_id, name, description, image)
            await message.answer('Персонаж сохранён. Для другого ракурса отправьте фото с тем же именем. Генерация по референсам пока не подключена.')
        except Conflict as error:
            await message.answer(str(error))
        except Exception:
            await message.answer('Не удалось сохранить персонажа. Попробуйте ещё раз.')

    @router.message(Command('status'))
    async def status(message: Message):
        try:
            job = store.job(settings.owner_id)
            await message.answer(f"Задание №{job['id']}: {job['status']}" + ('\n' + job['remote_url'] if job['remote_url'] else ''))
            if job['status'] == 'review':
                await preview(message, job['id'])
        except Conflict as error:
            await message.answer(str(error))

    @router.callback_query(F.data.regexp(r'^(publish|cancel):\d+:\d+$'))
    async def approve(callback: CallbackQuery):
        await callback.answer()
        action, ident, revision = callback.data.split(':')
        ident, revision = int(ident), int(revision)
        try:
            if action == 'cancel':
                store.cancel(settings.owner_id, ident, revision)
                return await callback.message.answer('Задание отменено.')
            if not settings.vk_token:
                raise Conflict('Ключ ВК не настроен. Публикация не выполнялась.')
            job, version = store.claim(settings.owner_id, ident, revision)
        except Conflict as error:
            return await callback.message.answer(str(error))
        try:
            url = await publisher.publish(job['target_group'], version['body'], job['image_path'], ident)
        except AmbiguousPublish as error:
            store.finish(settings.owner_id, ident, 'unknown')
            return await callback.message.answer(str(error))
        except ProviderError as error:
            store.finish(settings.owner_id, ident, 'failed')
            return await callback.message.answer(str(error))
        except Exception:
            store.finish(settings.owner_id, ident, 'unknown')
            return await callback.message.answer('Результат неизвестен. Проверьте стену ВК. Автоматического повтора не будет.')
        store.finish(settings.owner_id, ident, 'published', url)
        await callback.message.answer('Опубликовано: ' + url)

    @router.message()
    async def fallback(message: Message):
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
