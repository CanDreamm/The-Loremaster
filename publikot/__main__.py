import argparse
import asyncio
import errno
import logging
import sys

import aiohttp
from aiohttp import web

from .oauth import OAuthManager, VkOAuthProvider, create_web_app
from .providers import ProviderError
from .settings import ConfigError, Settings
from .store import Store
from .vk_bot import VkBot
from .vk_client import VkMessages


def advance_server(server, response):
    """VK failed=1 updates ts; failed=2/3 requires a new key/server."""
    failure = response.get('failed')
    if failure in (2, 3):
        return None
    if failure not in (None, 1):
        raise ProviderError('Long Poll вернул неизвестный код ошибки.')
    if 'ts' not in response:
        raise ProviderError('Long Poll не вернул курсор.')
    return {**server, 'ts': str(response['ts'])}


def event_failure(error):
    # ProviderError contains only our safe messages, never raw VK responses.
    detail = str(error) if isinstance(error, ProviderError) else type(error).__name__
    return f'Событие не завершено: {detail} Повтора не будет; отправьте команду заново после исправления причины.'


async def run(args):
    settings = Settings.load(require_owner=not (args.identify or args.check))
    if settings.transport == 'telegram':
        from .telegram_runtime import run_telegram
        return await run_telegram(settings, args)
    async with aiohttp.ClientSession(trust_env=False) as session:
        client = VkMessages(settings.bot_token, settings.group_id, session)
        if not (args.identify or args.check):
            await client.enable_callback_events()
        server = await client.long_poll_server()
        if args.check:
            print(f'API ВК и сервер Bots Long Poll доступны. Сообщество: {settings.group_id}.')
            print('Получение сообщений ещё нужно проверить командой --identify.')
            return
        if args.identify:
            print('Отправьте /id в личные сообщения сообщества. Ожидаю сообщение (Ctrl+C — остановить).', flush=True)
            while True:
                response = await client.poll(server)
                next_server = advance_server(server, response)
                if next_server is None:
                    server = await client.long_poll_server()
                    continue
                for event in response.get('updates', []):
                    message = event.get('object', {}).get('message', {})
                    user = message.get('from_id', 0)
                    if (event.get('type') == 'message_new' and event.get('group_id') == settings.group_id
                            and user > 0 and message.get('peer_id') == user and (message.get('text') or '').strip() == '/id'):
                        print(f'ID ВК отправителя /id: {user}', flush=True)
                        await client.send(user, f'Ваш ID ВК: {user}')
                        return
                server = next_server
        store = Store(settings.database)
        saved = store.cursor(settings.group_id)
        if saved:
            server['ts'] = saved
        manager = OAuthManager(settings, store, VkOAuthProvider(settings, session))
        app = VkBot(settings, store, client, oauth=manager)
        runner = None
        delay = 2
        try:
            if settings.oauth_enabled:
                runner = web.AppRunner(create_web_app(manager, notify=client.send), access_log=None)
                await runner.setup()
                try:
                    await web.TCPSite(runner, '127.0.0.1', settings.oauth_port).start()
                except OSError as error:
                    if error.errno == errno.EADDRINUSE:
                        raise ConfigError(f'Запуск остановлен: порт 127.0.0.1:{settings.oauth_port} занят. '
                                          'Проверьте уже работающий экземпляр бота.') from None
                    raise
                app.oauth = manager
                print(f'OAuth: приложение {settings.oauth_client_id}, режим {settings.oauth_provider}, callback {settings.oauth_redirect}', flush=True)
                print('OAuth-обработчик запущен локально; публичный HTTPS настраивается отдельно.', flush=True)
            print(f'Публикот запущен в сообщениях ВК, сообщество {settings.group_id}. Демо: {settings.demo}', flush=True)
            while True:
                try:
                    response = await client.poll(server)
                    next_server = advance_server(server, response)
                    if next_server is None:
                        server = await client.long_poll_server()
                        store.cursor(settings.group_id, server['ts'])
                        continue
                    if response.get('failed') == 1:
                        print('Long Poll: история недоступна, курсор обновлён. Проверьте /status для незавершённых заданий.', flush=True)
                    for event in response.get('updates', []):
                        try:
                            await app.handle(event)
                        except Exception as error:
                            # Не выводим исключения с URL, ключами и пользовательскими материалами.
                            print(event_failure(error), flush=True)
                    server = next_server
                    store.cursor(settings.group_id, server['ts'])
                    delay = 2
                except (ProviderError, aiohttp.ClientError, TimeoutError):
                    print(f'Связь с ВК прервана. Повтор подключения через {delay} сек.', flush=True)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 30)
        finally:
            if runner:
                await runner.cleanup()
            store.db.close()


def main():
    parser = argparse.ArgumentParser(description='Публикот — интерфейс ВК или Telegram через сервис')
    parser.add_argument('--identify', action='store_true', help='Определить свой ID через /id (для Telegram запускает приём событий)')
    parser.add_argument('--check', action='store_true', help='Проверить API выбранного транспорта')
    args = parser.parse_args()
    logging.basicConfig(level=logging.CRITICAL)
    try:
        asyncio.run(run(args))
    except (ConfigError, ProviderError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        print(f'Запуск остановлен ({type(error).__name__}). Проверьте настройки бота. Секреты не выводятся.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
