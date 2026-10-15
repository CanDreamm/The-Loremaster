"""Apply public deployment settings without asking for or printing credentials."""
import os
from pathlib import Path

from dotenv import set_key


def configure(root):
    target = root / '.env.python'
    # Require the existing bot configuration; do not create an empty installation.
    if not target.is_file() or target.is_symlink():
        raise SystemExit('Не найден обычный файл .env.python установленного бота.')
    os.chmod(target, 0o600)
    for name, value in {
        'VK_OAUTH_CLIENT_ID': '54825414',
        'VK_OAUTH_PROVIDER': 'vkid',
        'OAUTH_PUBLIC_URL': 'https://bot.itiscat.ru',
        'OAUTH_PORT': '8081',
    }.items():
        set_key(target, name, value)
    print('Настройки приложения 54825414 сохранены. Ключи бота и остальные параметры сохранены.')


if __name__ == '__main__':
    configure(Path(__file__).resolve().parent.parent)
