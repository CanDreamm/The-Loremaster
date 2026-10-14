import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    token: str
    owner_id: int
    data_dir: Path
    proxy: str | None
    demo: bool
    yandex_key: str
    yandex_folder: str
    vk_token: str

    def vk_token_for(self, user_id, project_id=None):
        # Глобальный ключ принадлежит только администратору, не всем приглашённым.
        project_key = os.getenv(f'VK_ACCESS_TOKEN_PROJECT_{project_id}', '') if project_id is not None else ''
        if project_key:
            return project_key
        if user_id == self.owner_id:
            return self.vk_token
        return os.getenv(f'VK_ACCESS_TOKEN_{user_id}', '')

    @classmethod
    def load(cls):
        load_dotenv(Path(__file__).resolve().parent.parent / '.env.python', override=False)
        token = os.getenv('TELEGRAM_BOT_TOKEN', '')
        owner = os.getenv('OWNER_TELEGRAM_ID', '')
        if not token:
            raise ConfigError('Задайте TELEGRAM_BOT_TOKEN в .env.python')
        if not owner.isdecimal() or int(owner) <= 0:
            raise ConfigError('Задайте положительный OWNER_TELEGRAM_ID в .env.python')
        return cls(token, int(owner), Path(os.getenv('DATA_DIR', './data')).resolve(),
                   os.getenv('TELEGRAM_PROXY') or None,
                   os.getenv('DEMO_MODE', 'true').lower() == 'true',
                   os.getenv('YANDEX_API_KEY', ''), os.getenv('YANDEX_FOLDER_ID', ''),
                   os.getenv('VK_ACCESS_TOKEN', ''))
