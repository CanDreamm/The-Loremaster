"""Interactive local login. Never asks for passwords or cookies in the bot."""
import argparse
import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv

from .browser_publish import profile_lock, profile_path, validate_target
from .providers import ProviderError


async def login(args):
    from playwright.async_api import async_playwright
    target = validate_target(args.platform, args.target)
    profile = profile_path(args.data_dir.resolve(), args.project, args.platform)
    with profile_lock(profile):
        async with async_playwright() as driver:
            context = await driver.chromium.launch_persistent_context(str(profile), headless=False)
            try:
                page = await context.new_page()
                await page.goto(target)
                await asyncio.to_thread(input,
                    'Войдите в аккаунт, откройте нужное сообщество/канал и убедитесь в праве публикации. '
                    'Затем нажмите Enter здесь (пароль вводите только на сайте): ')
                marker = profile / '.login-complete'
                marker.touch(mode=0o600)
            finally:
                await context.close()
    print('Профиль сохранён. Закройте другие окна этого профиля перед запуском бота.')


def main():
    load_dotenv(Path(__file__).resolve().parent.parent / '.env.python', override=False)
    parser = argparse.ArgumentParser(description='Вход в браузер публикаций Публикота')
    parser.add_argument('--project', type=int, required=True)
    parser.add_argument('--platform', choices=('vk', 'dzen'), required=True)
    parser.add_argument('--target', required=True)
    parser.add_argument('--data-dir', type=Path, default=Path(os.getenv('DATA_DIR', './data')))
    args = parser.parse_args()
    try:
        asyncio.run(login(args))
    except (ProviderError, ImportError) as error:
        print(str(error) if isinstance(error, ProviderError) else 'Установите requirements-browser.lock и Chromium.')
        return 1
    except KeyboardInterrupt:
        return 1
    except Exception:
        print('Не удалось открыть браузер. Нужен графический сеанс, Chromium и системные зависимости. См. docs/browser-publishing.md.')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
