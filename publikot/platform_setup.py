"""Import personal connections locally without sending secrets to the chat."""
import argparse
import asyncio
import getpass
import json
import sys
from pathlib import Path

from .platforms import Platforms
from .providers import ProviderError
from .settings import Settings, ConfigError
from .store import Conflict, Store


async def configure(args):
    settings = Settings.load()
    store = Store(settings.database)
    try:
        manager = Platforms(settings, store)
        if args.disconnect:
            if not store.is_allowed(args.user, settings.owner_id):
                raise Conflict('Пользователь не допущен к боту.')
            manager.disconnect(args.user, args.disconnect)
            print('Личное подключение отключено; уже переданные публикации не отменены.')
        elif args.dzen_file:
            path = args.dzen_file
            if path.is_symlink() or path.stat().st_mode & 0o077:
                raise ConfigError('Файл сеанса должен иметь права 600 и не быть символической ссылкой.')
            auth = json.loads(path.read_text())
            key = manager.import_dzen(args.user, auth)
            if args.project:
                publisher = auth['publisher_id']
                store.attach_destination(args.user, args.project, 'dzen', publisher, publisher,
                                         'Дзен', 'Дзен · ' + publisher, key)
            print('Сеанс Дзена зашифрован и сохранён. Подключите канал в «Площадках» проекта.')
        else:
            token = getpass.getpass('Личный токен Postmypost (ввод скрыт): ')
            await manager.save_token(args.user, token)
            print('Личный токен проверен и зашифрован. Площадки выбираются в боте.')
    finally:
        store.db.close()


def main():
    parser = argparse.ArgumentParser(description='Личные подключения публикаций. Перед запуском остановите бота.')
    parser.add_argument('--user', type=int, required=True, help='ID пользователя ВК, которому принадлежит подключение')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--dzen-file', type=Path, help='Локальный JSON с cookie, csrf_token, publisher_id')
    group.add_argument('--disconnect', help='postmypost или dzen:ID_КАНАЛА')
    parser.add_argument('--project', type=int, help='Необязательно: сразу подключить сеанс Дзена к этому проекту')
    args = parser.parse_args()
    if args.user <= 0:
        parser.error('Нужен положительный ID пользователя.')
    try:
        asyncio.run(configure(args))
    except (ConfigError, Conflict, ProviderError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 1
    except Exception:
        print('Не удалось импортировать подключение. Проверьте путь, формат файла и настройки. Секреты не выводятся.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
