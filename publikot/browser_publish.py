"""Publish through visible site controls, without platform publishing APIs."""
import asyncio
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from .providers import AmbiguousPublish, ProviderError


def validate_target(platform, target):
    url = urlsplit(target)
    hosts = {'vk': ('vk.com', 'vk.ru', 'www.vk.com', 'www.vk.ru'),
             'dzen': ('dzen.ru',)}
    if (platform not in hosts or url.scheme != 'https' or url.hostname not in hosts[platform]
            or url.username or url.password or url.port not in (None, 443)
            or url.query or url.fragment):
        raise ProviderError('Нужна HTTPS-ссылка на сообщество ВК или редактор канала dzen.ru без параметров.')
    path = url.path.rstrip('/')
    if platform == 'vk':
        valid = re.fullmatch(r'/[A-Za-z0-9_.]{2,100}', path)
    else:
        valid = re.fullmatch(r'/profile/editor/id/[A-Za-z0-9_-]+', path)
    if not valid:
        raise ProviderError('Для ВК укажите главную страницу сообщества; для Дзена — https://dzen.ru/profile/editor/id/ID_КАНАЛА.')
    return f'https://{url.hostname}{path}'


def profile_path(data_dir, project_id, platform):
    if platform not in ('vk', 'dzen') or type(project_id) is not int or project_id <= 0:
        raise ProviderError('Проверьте площадку и номер проекта.')
    return Path(data_dir) / 'browser' / str(project_id) / platform


# Selectors can be overridden locally when a platform changes its editor.
SELECTORS = {
    'vk': {
        'open': '#post_field',
        'body': '#post_field',
        'file': 'input[type="file"][accept*="image"]',
        'image_ready': '.post_field_upload .page_pics_preview, .post_field_upload .thumb',
        'publish': '#send_post',
        'result': '.wall_item',
        'link': 'a[href*="wall-"]',
    },
    'dzen': {
        'open': 'button:has-text("Создать")',
        'article': 'text="Написать статью"',
        'title': 'textarea[placeholder*="Заголовок"], [contenteditable="true"][data-placeholder*="Заголовок"]',
        'body': '.public-DraftEditor-content[contenteditable="true"]',
        'file': 'input[type="file"][accept*="image"]',
        'image_ready': '.public-DraftEditor-content img',
        'next': 'button:has-text("Опубликовать")',
        'publish': 'button:has-text("Опубликовать сейчас")',
        'result': 'body',
        'link': 'a[href*="/a/"]',
    },
}


class BrowserPublisher:
    def __init__(self, data_dir, project_id, platform):
        self.platform = platform
        self.profile = profile_path(data_dir, project_id, platform)
        self.selectors = dict(SELECTORS[platform])
        config = Path(data_dir) / 'browser-selectors.json'
        if config.exists():
            try:
                overrides = json.loads(config.read_text()).get(platform, {})
                if (not isinstance(overrides, dict) or set(overrides) - set(self.selectors)
                        or any(not isinstance(v, str) or not v.strip() for v in overrides.values())):
                    raise ValueError
                self.selectors.update(overrides)
            except (ValueError, TypeError, AttributeError):
                raise ProviderError('Проверьте data/browser-selectors.json.') from None

    def check_ready(self):
        if not (self.profile / '.login-complete').is_file():
            raise ProviderError('Сначала выполните вход через python -m publikot.browser_login на компьютере с ботом. См. docs/browser-publishing.md.')
        try:
            from playwright.async_api import async_playwright  # noqa: F401
        except ImportError:
            raise ProviderError('Установите requirements-browser.lock и браузер Chromium. См. docs/browser-publishing.md.') from None

    async def publish(self, target, body, image_path, ident):
        target = validate_target(self.platform, target)
        self.check_ready()
        if not body.strip():
            raise ProviderError('Пустой текст нельзя публиковать.')
        from .media import paths
        images = paths(image_path)
        if any(not Path(path).is_file() for path in images):
            raise ProviderError('Фото публикации не найдено.')
        from playwright.async_api import async_playwright
        clicked = False
        # OS lock also protects the profile from a second process or login utility.
        with profile_lock(self.profile):
            try:
                async with async_playwright() as driver:
                    context = await driver.chromium.launch_persistent_context(
                        str(self.profile), headless=os.getenv('BROWSER_HEADLESS', 'true').lower() == 'true',
                        accept_downloads=False)
                    try:
                        page = await context.new_page()
                        page.set_default_timeout(20000)
                        await page.goto(target, wait_until='domcontentloaded')
                        # Do not interact with an external login/redirect destination.
                        if urlsplit(page.url).hostname != urlsplit(target).hostname:
                            raise ProviderError('Вход истёк или площадка перенаправила браузер. Выполните вход заново.')
                        old_links = set(await page.locator(self.selectors['link']).evaluate_all(
                            '(nodes) => nodes.map(n => n.href)'))
                        await page.locator(self.selectors['open']).click()
                        if self.platform == 'dzen':
                            await page.locator(self.selectors['article']).click()
                            # The first nonempty line becomes the article title.
                            title, _, content = body.strip().partition('\n')
                            await page.locator(self.selectors['title']).fill(title[:140])
                            await page.locator(self.selectors['body']).fill(
                                body.strip() if len(title) > 140 else content.strip() or body.strip())
                        else:
                            await page.locator(self.selectors['body']).fill(body)
                        if images:
                            files = [str(Path(path).resolve()) for path in images]
                            await page.locator(self.selectors['file']).set_input_files(files if len(files) > 1 else files[0])
                            await page.locator(self.selectors['image_ready']).wait_for(state='visible')
                        if self.platform == 'dzen':
                            clicked = True  # A changed UI may publish at this step already.
                            await page.locator(self.selectors['next']).click()
                        final = page.locator(self.selectors['publish'])
                        await final.wait_for(state='visible')
                        # Any error during/after the final click may mean a successful send.
                        clicked = True
                        await final.click()
                        return await self._result(page, target, old_links, body)
                    finally:
                        await context.close()
            except (ProviderError, AmbiguousPublish):
                raise
            except Exception:
                if clicked:
                    raise AmbiguousPublish('Результат публикации неизвестен. Проверьте площадку вручную; автоматического повтора не будет.') from None
                raise ProviderError('Браузер не смог подготовить публикацию. Проверьте вход, права и селекторы редактора. Пост не отправлялся.') from None

    async def _result(self, page, target, old_links, body):
        # A mere click or an editor URL is never treated as successful publication.
        pattern = (r'https://(?:www\.)?vk\.(?:com|ru)/wall-\d+_\d+'
                   if self.platform == 'vk' else r'https://dzen\.ru/a/[A-Za-z0-9_-]+')
        for _ in range(30):
            if self.platform == 'dzen' and re.fullmatch(pattern, page.url):
                if await page.get_by_text(body.strip().split('\n')[0][:140], exact=False).count():
                    return page.url
            containers = page.locator(self.selectors['result']).filter(
                has_text=body.strip().split('\n')[0][:100])
            hrefs = await containers.locator(self.selectors['link']).evaluate_all('(nodes) => nodes.map(n => n.href)')
            for href in hrefs:
                absolute = urljoin(target, href).split('?')[0].split('#')[0]
                if re.fullmatch(pattern, absolute) and href not in old_links:
                    if self.platform == 'vk':
                        group = re.fullmatch(r'.*/(?:club|public|event)(\d+)', target)
                        if group and not absolute.split('/')[-1].startswith('wall-' + group[1] + '_'):
                            continue
                    return absolute
            await asyncio.sleep(1)
        raise AmbiguousPublish('Нет подтверждённой ссылки на новый пост. Проверьте площадку вручную; повтора не будет.')


@contextmanager
def profile_lock(profile):
    import fcntl
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    profile.chmod(0o700)
    with (profile / '.publikot.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ProviderError('Профиль браузера занят. Закройте окно входа или дождитесь публикации.') from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
