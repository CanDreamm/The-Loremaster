"""Apply the confirmed VK application settings without requesting secrets."""
import os
from pathlib import Path

from dotenv import set_key


def configure(target):
    target = Path(target)
    if not target.is_file():
        raise SystemExit('Сначала настройте бота через scripts/setup-python.py: .env.python отсутствует.')
    os.chmod(target, 0o600)
    # Keep the selected provider, client secret, bot token and other settings.
    set_key(target, 'VK_OAUTH_CLIENT_ID', '54825414')
    set_key(target, 'OAUTH_PUBLIC_URL', 'https://bot.itiscat.ru')


if __name__ == '__main__':
    configure(Path(__file__).resolve().parent.parent / '.env.python')
    print('OAuth: приложение 54825414, callback https://bot.itiscat.ru/oauth/vk/callback. Настройки сохранены.')
