"""Apply the confirmed VK application settings without requesting secrets."""
import os
import argparse
from pathlib import Path

from dotenv import set_key


def configure(target, provider=None):
    if provider is not None and provider not in ('vkid', 'vk_api', 'vk_implicit'):
        raise SystemExit('Неизвестный провайдер OAuth.')
    target = Path(target)
    if not target.is_file():
        raise SystemExit('Сначала настройте бота через scripts/setup-python.py: .env.python отсутствует.')
    os.chmod(target, 0o600)
    # Keep the selected provider, client secret, bot token and other settings.
    set_key(target, 'VK_OAUTH_CLIENT_ID', '54825715')
    set_key(target, 'OAUTH_PUBLIC_URL', 'https://bot.itiscat.ru')
    if provider is not None:
        set_key(target, 'VK_OAUTH_PROVIDER', provider)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Настройка текущего приложения ВК без ввода секретов')
    parser.add_argument('--provider', choices=('vkid', 'vk_api', 'vk_implicit'))
    args = parser.parse_args()
    configure(Path(__file__).resolve().parent.parent / '.env.python', args.provider)
    print('OAuth: приложение 54825715, callback https://bot.itiscat.ru/oauth/vk/callback. Настройки сохранены.')
