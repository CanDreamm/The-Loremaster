"""Configure VK locally, preserving existing secrets and unrelated settings."""
from getpass import getpass
from pathlib import Path
import os
from dotenv import dotenv_values, set_key

root = Path(__file__).resolve().parent.parent
target = root / '.env.python'
if not target.exists():
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write((root / '.env.python.example').read_text())
os.chmod(target, 0o600)
current = dotenv_values(target)
print('Настройка бота ВК. Enter сохраняет существующее значение. Секреты не выводятся.')
token = getpass('Ключ сообщества бота, VK_BOT_TOKEN (ввод скрыт): ').strip()
group = input('ID сообщества бота [242305804]: ').strip()
owner = input('Ваш числовой ID ВК (если неизвестен — Enter): ').strip()
if group and (not group.isdecimal() or int(group) <= 0):
    raise SystemExit('Нужен положительный числовой ID сообщества. Настройки не изменены.')
if owner and (not owner.isdecimal() or int(owner) <= 0):
    raise SystemExit('Нужен положительный числовой ID пользователя. Настройки не изменены.')
for name, value in [('VK_BOT_TOKEN', token), ('VK_BOT_GROUP_ID', group or current.get('VK_BOT_GROUP_ID') or '242305804'), ('OWNER_VK_ID', owner)]:
    if value:
        set_key(target, name, value)
set_key(target, 'BOT_TRANSPORT', 'vk')
print('Настройки ВК сохранены. Старые настройки не удалены; Telegram больше не используется.')
