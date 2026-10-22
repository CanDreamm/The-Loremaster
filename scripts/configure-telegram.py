"""Configure Telegram locally without printing API keys or webhook secrets."""
import argparse
import getpass
import os
import secrets
from pathlib import Path

from dotenv import dotenv_values, set_key


def main():
    parser = argparse.ArgumentParser(description='Настройка @publikot_bot через Chatbot')
    parser.add_argument('--show-webhook-url', action='store_true', help='Показать секретный URL для панели сервиса')
    args = parser.parse_args()
    config = Path(__file__).resolve().parent.parent / '.env.python'
    values = dotenv_values(config) if config.exists() else {}
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
    entries = {'BOT_TRANSPORT': 'telegram', 'CHATBOT_API_KEY': api_key,
               'CHATBOT_BOT_ID': '5409db73-f74b-4095-aaa4-a2341a4a87db',
               'OWNER_TELEGRAM_ID': owner,
               'TELEGRAM_WEBHOOK_SECRET': values.get('TELEGRAM_WEBHOOK_SECRET') or secrets.token_urlsafe(32),
               'OAUTH_PUBLIC_URL': values.get('OAUTH_PUBLIC_URL') or 'https://bot.itiscat.ru',
               'VK_OAUTH_CLIENT_ID': values.get('VK_OAUTH_CLIENT_ID') or '54825715'}
    for key, value in entries.items():
        set_key(config, key, value)
    print('Настройки сохранены в .env.python. Секреты не выводятся.')
    if not owner:
        print('Для определения ID запустите python -m publikot --identify и настройте webhook. Затем отправьте /id боту.')
    print('Секретный URL для панели сервиса: python scripts/configure-telegram.py --show-webhook-url')


if __name__ == '__main__':
    main()
