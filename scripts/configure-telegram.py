"""Configure Telegram locally without printing API keys or webhook secrets."""
import argparse
import getpass
import os
import secrets
from pathlib import Path

from dotenv import dotenv_values, set_key


def main():
    parser = argparse.ArgumentParser(description='Настройка @PubliKotBot через Chatbot')
    parser.add_argument('--show-webhook-url', action='store_true', help='Показать секретный URL для панели сервиса')
    parser.add_argument('--updates-mode', choices=('polling', 'webhook'), help='Сменить только способ получения событий, сохранив ключ и ID')
    args = parser.parse_args()
    config = Path(__file__).resolve().parent.parent / '.env.python'
    values = dotenv_values(config) if config.exists() else {}
    if args.updates_mode and args.show_webhook_url:
        parser.error('Используйте один режим настройки за раз.')
    if args.updates_mode:
        if not values.get('CHATBOT_API_KEY'):
            parser.error('Сначала выполните обычную настройку без аргументов.')
        os.chmod(config, 0o600)
        set_key(config, 'BOT_TRANSPORT', 'telegram')
        set_key(config, 'TELEGRAM_UPDATES_MODE', args.updates_mode)
        print(f'Получение событий: {args.updates_mode}. Ключ, ID и данные сохранены. Перезапустите бота.')
        if args.updates_mode == 'polling':
            print('В сервисе отключите backend webhook и разрешите getUpdates. Ожидающие события не удаляются.')
        return
    if args.show_webhook_url:
        secret = values.get('TELEGRAM_WEBHOOK_SECRET')
        public = values.get('OAUTH_PUBLIC_URL', '').rstrip('/')
        if not secret or not public:
            parser.error('Сначала выполните настройку без аргументов.')
        print(f'{public}/telegram/webhook/{secret}')
        return
    api_key = getpass.getpass('API-ключ Chatbot (Enter — сохранить существующий): ').strip() or values.get('CHATBOT_API_KEY', '')
    if not api_key:
        parser.error('Нужен API-ключ сервиса, не токен BotFather.')
    owner = input(f"Ваш Telegram ID [{values.get('OWNER_TELEGRAM_ID') or 'пока неизвестен'}]: ").strip() or values.get('OWNER_TELEGRAM_ID', '')
    if owner and (not owner.isascii() or not owner.isdecimal() or int(owner) <= 0):
        parser.error('Нужен положительный числовой Telegram ID.')
    config.touch(mode=0o600, exist_ok=True)
    os.chmod(config, 0o600)
    entries = {'BOT_TRANSPORT': 'telegram',
               'TELEGRAM_UPDATES_MODE': values.get('TELEGRAM_UPDATES_MODE') or 'polling', 'CHATBOT_API_KEY': api_key,
               'CHATBOT_BOT_ID': '691ae7a7-6786-4d3d-9a35-7a3689263984',
               'OWNER_TELEGRAM_ID': owner,
               'TELEGRAM_WEBHOOK_SECRET': values.get('TELEGRAM_WEBHOOK_SECRET') or secrets.token_urlsafe(32),
               'OAUTH_PUBLIC_URL': values.get('OAUTH_PUBLIC_URL') or 'https://bot.itiscat.ru',
               'VK_OAUTH_CLIENT_ID': values.get('VK_OAUTH_CLIENT_ID') or '54825715'}
    for key, value in entries.items():
        set_key(config, key, value)
    print('Настройки сохранены в .env.python. Секреты не выводятся.')
    if not owner:
        print('Для определения ID запустите python -m publikot --identify и отправьте /id боту после запуска получения событий.')
    if entries['TELEGRAM_UPDATES_MODE'] == 'webhook':
        print('Секретный URL для панели сервиса: python scripts/configure-telegram.py --show-webhook-url')
    else:
        print('В сервисе отключите backend webhook и разрешите getUpdates.')


if __name__ == '__main__':
    main()
