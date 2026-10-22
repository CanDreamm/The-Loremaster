"""Telegram polling or webhook process using the shared publishing workflow."""
import asyncio
import os
import re

import aiohttp
from aiohttp import web

from .oauth import OAuthManager, VkOAuthProvider, create_web_app
from .providers import ProviderError
from .settings import ConfigError
from .store import Store
from .telegram_bot import TelegramBot
from .telegram_client import TelegramSettings, TelegramWorkflowMessages
from .telegram_webhook import TelegramInbox, add_webhook, add_inbox_worker
from .telegram_polling import TelegramPolling


class TelegramVkOAuthProvider(VkOAuthProvider):
    async def validate_identity(self, token, actor, authorization=None):
        # actor is a Telegram ID. OAuth state binds the initiating Telegram user;
        # the VK identity is obtained from VK, never compared to a Telegram ID.
        if self.settings.oauth_provider == 'vkid':
            identity = (authorization or {}).get('user_id')
            if not str(identity).isascii() or not str(identity).isdecimal() or int(identity) <= 0:
                raise ProviderError('VK ID не вернул числовой ID пользователя.')
            identity = int(identity)
        else:
            profile = await self.api(token, 'account.getProfileInfo')
            identity = profile.get('id') if isinstance(profile, dict) else None
            if type(identity) is not int or identity <= 0:
                raise ProviderError('ВК не подтвердил владельца ключа.')
        await super().validate_identity(token, identity, authorization)


async def run_telegram(settings, args):
    config = TelegramSettings.load()
    mode = os.getenv('TELEGRAM_UPDATES_MODE', 'webhook')
    if mode not in ('polling', 'webhook'):
        raise ConfigError('TELEGRAM_UPDATES_MODE: polling или webhook.')
    secret = os.getenv('TELEGRAM_WEBHOOK_SECRET', '')
    if mode == 'webhook' and not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', secret):
        raise ConfigError('Задайте TELEGRAM_WEBHOOK_SECRET: 32–128 случайных букв, цифр, _ или -.')
    if args.identify and settings.owner_id:
        print('OWNER_TELEGRAM_ID уже задан. /id в боте покажет ваш ID.', flush=True)
    async with aiohttp.ClientSession(trust_env=False) as session:
        client = TelegramWorkflowMessages(config, session)
        client.vk_token = settings.bot_token
        await client.get_me()
        if args.check:
            print(f'API сервиса доступен; подтверждён @PubliKotBot. Режим: {mode}. Получение событий и фото проверяются при запуске.')
            return
        store = Store(settings.database)
        runner = None
        try:
            manager = OAuthManager(settings, store, TelegramVkOAuthProvider(settings, session)) if settings.oauth_enabled else None
            bot = TelegramBot(settings, store, client, oauth=manager)
            if args.identify:
                # No administrator is auto-enrolled. /id is available to everyone.
                print('Отправьте /id в @PubliKotBot после запуска приёма событий; затем задайте OWNER_TELEGRAM_ID и перезапустите службу.', flush=True)
            app = create_web_app(manager, notify=client.send) if manager else web.Application(client_max_size=1024*1024)
            inbox = TelegramInbox(store, bot)
            if mode == 'webhook':
                add_webhook(app, inbox, secret)
            else:
                add_inbox_worker(app, inbox)
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            try:
                await web.TCPSite(runner, '127.0.0.1', settings.oauth_port).start()
            except OSError:
                raise ConfigError('Не удалось занять локальный порт бота. Проверьте уже запущенную службу ВК/Telegram.') from None
            if mode == 'polling':
                print(f'Публикот Telegram: getUpdates через сервис, long polling 25 сек. Демо: {settings.demo}', flush=True)
                await TelegramPolling(inbox, client).run()
            else:
                print(f'Публикот Telegram: webhook слушает 127.0.0.1:{settings.oauth_port}. Демо: {settings.demo}', flush=True)
                await asyncio.Event().wait()
        finally:
            if runner:
                await runner.cleanup()
            store.db.close()
