"""Interactive local setup. Secrets are entered without echo."""
from getpass import getpass
from pathlib import Path
import os
from dotenv import set_key

root = Path(__file__).resolve().parent.parent
target = root / '.env.python'
if target.exists():
    raise SystemExit('.env.python уже существует. Измените его локально; файл не перезаписан.')
fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as stream:
    stream.write((root / '.env.python.example').read_text())
token = getpass('Токен BotFather (ввод скрыт): ').strip()
proxy = getpass('Адрес прокси целиком, либо Enter без прокси (ввод скрыт): ').strip()
owner = input('Ваш числовой Telegram ID, либо Enter, если пока неизвестен: ').strip()
for name, value in [('TELEGRAM_BOT_TOKEN', token), ('TELEGRAM_PROXY', proxy), ('OWNER_TELEGRAM_ID', owner)]:
    set_key(target, name, value)
print('Файл .env.python сохранён с доступом только владельцу. Демо-режим включён.')
