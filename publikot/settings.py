import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    bot_token: str = field(repr=False)
    group_id: int
    owner_id: int
    data_dir: Path
    demo: bool = True
    yandex_key: str = field(default='', repr=False)
    yandex_folder: str = ''
    vk_token: str = field(default='', repr=False)
    oauth_client_id: str = ''
    oauth_client_secret: str = field(default='', repr=False)
    oauth_public_url: str = ''
    oauth_provider: str = 'vkid'
    oauth_port: int = 8081

    @property
    def oauth_enabled(self):
        return bool(self.oauth_client_id and self.oauth_public_url)

    @property
    def oauth_redirect(self):
        return self.oauth_public_url.rstrip('/') + '/oauth/vk/callback'

    @property
    def database(self):
        # Telegram и VK имеют разные пространства ID: базы нельзя смешивать.
        return self.data_dir / 'publikot-vk.sqlite3'

    def vk_token_for(self, user_id, project_id=None):
        project_key = os.getenv(f'VK_ACCESS_TOKEN_PROJECT_{project_id}', '') if project_id is not None else ''
        if project_key:
            return project_key
        if user_id == self.owner_id:
            return self.vk_token
        return os.getenv(f'VK_ACCESS_TOKEN_USER_{user_id}', '')

    @classmethod
    def load(cls, require_owner=True):
        load_dotenv(Path(__file__).resolve().parent.parent / '.env.python', override=False)
        token = os.getenv('VK_BOT_TOKEN', '')
        group = os.getenv('VK_BOT_GROUP_ID', '242305804')
        owner = os.getenv('OWNER_VK_ID', '')
        if not token:
            raise ConfigError('Задайте VK_BOT_TOKEN в .env.python')
        if not group.isdecimal() or int(group) <= 0:
            raise ConfigError('Задайте положительный VK_BOT_GROUP_ID')
        if owner and (not owner.isdecimal() or int(owner) <= 0):
            raise ConfigError('OWNER_VK_ID должен быть положительным числовым ID ВК')
        if require_owner and not owner:
            raise ConfigError('Задайте OWNER_VK_ID. Для определения ID: python -m publikot --identify')
        client = os.getenv('VK_OAUTH_CLIENT_ID', '')
        public = os.getenv('OAUTH_PUBLIC_URL', '').rstrip('/')
        provider = os.getenv('VK_OAUTH_PROVIDER', 'vkid')
        secret = os.getenv('VK_OAUTH_CLIENT_SECRET', '')
        port = os.getenv('OAUTH_PORT', '8081')
        if provider not in ('vkid', 'vk_api') or not port.isdecimal() or not 1024 <= int(port) <= 65535:
            raise ConfigError('Проверьте VK_OAUTH_PROVIDER (vkid или vk_api) и OAUTH_PORT (1024–65535).')
        if client or public or secret:
            url = urlsplit(public)
            if not client.isdecimal() or int(client) <= 0 or url.scheme != 'https' or not url.hostname or url.username or url.password or url.path or url.query or url.fragment:
                raise ConfigError('Для OAuth нужны VK_OAUTH_CLIENT_ID и OAUTH_PUBLIC_URL вида https://bot.example.com без пути.')
            if provider == 'vk_api' and not secret:
                raise ConfigError('Для vk_api нужен VK_OAUTH_CLIENT_SECRET собственного приложения.')
        return cls(token, int(group), int(owner or 0), Path(os.getenv('DATA_DIR', './data')).resolve(),
                   os.getenv('DEMO_MODE', 'true').lower() == 'true',
                   os.getenv('YANDEX_API_KEY', ''), os.getenv('YANDEX_FOLDER_ID', ''),
                   os.getenv('VK_ACCESS_TOKEN', ''), client, secret, public, provider, int(port))
