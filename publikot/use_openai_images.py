"""Select OpenAI GPT Image 2.5 Low in the local environment file. No API calls."""
from pathlib import Path

from dotenv import set_key


def configure(path):
    path = Path(path)
    if not path.is_file():
        raise ValueError('Не найден .env.python. Сначала настройте бота.')
    # Change only the two requested settings; preserve keys, transport and other values.
    set_key(path, 'GENAPI_IMAGE_QUALITY', 'low')
    set_key(path, 'GENAPI_IMAGE_MODEL', 'gpt-image-2-5')
    path.chmod(0o600)


def main():
    try:
        configure(Path(__file__).resolve().parent.parent / '.env.python')
    except (OSError, ValueError):
        raise SystemExit('Не удалось обновить .env.python. Проверьте наличие файла и права записи; секреты не выводятся.') from None
    print('Генерация изображений: GenAPI → GPT Image 2.5 Flare, качество Low. Перезапустите бота.')


if __name__ == '__main__':
    main()
