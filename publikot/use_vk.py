"""Return to VK and import the administrator's Telegram project configuration, without sending posts."""
import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import set_key

from .oauth import Vault
from .providers import ProviderError
from .settings import Settings, ConfigError
from .store import Store, Conflict


def import_projects(source, store, source_owner, vk_owner):
    if source_owner <= 0 or vk_owner <= 0:
        raise Conflict('Для переноса нужны числовые OWNER_TELEGRAM_ID и OWNER_VK_ID.')
    destination = Path(store.db.execute('PRAGMA database_list').fetchone()['file']).resolve()
    source = Path(source).resolve()
    if source == destination or source.parent != destination.parent:
        raise Conflict('Перенос выполняется между отдельными базами в одном DATA_DIR с общим platforms.key.')
    report = {'projects': 0, 'already_imported': 0, 'destinations': 0, 'connections_to_reconnect': 0,
              'members_to_assign': 0}
    with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as incoming:
        incoming.row_factory = sqlite3.Row
        incoming.execute('BEGIN')
        tables = {r['name'] for r in incoming.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'projects' not in tables:
            raise Conflict('В базе Telegram не найдены проекты.')
        projects = incoming.execute('SELECT * FROM projects WHERE owner=? ORDER BY id', (source_owner,)).fetchall()
        credentials = {r['provider']: dict(r) for r in incoming.execute(
            'SELECT * FROM personal_credentials WHERE actor=? AND ciphertext<>\'\'', (source_owner,))} if 'personal_credentials' in tables else {}
        # Validate encryption before modifying the destination. Never print decrypted values.
        vault = Vault(source.parent / 'platforms.key', may_create=False) if credentials else None
        secrets = {key: vault.decrypt(row['ciphertext']) for key, row in credentials.items()}
        available = set()
        for key, row in credentials.items():
            existing = store.credential_record(vk_owner, key)
            if not existing or not existing['ciphertext'] or vault.decrypt(existing['ciphertext']) == secrets[key]:
                available.add(key)
        with store.transaction():
            store.db.execute('''CREATE TABLE IF NOT EXISTS vk_transport_imports (
                source TEXT NOT NULL, source_owner INTEGER NOT NULL, source_project INTEGER NOT NULL,
                target_owner INTEGER NOT NULL, target_project INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                PRIMARY KEY(source,source_owner,source_project,target_owner))''')
            for key in available:
                current = store.credential_record(vk_owner, key)
                if not current or not current['ciphertext']:
                    store.put_credential(vk_owner, key, credentials[key]['ciphertext'])
            project_columns = {r['name'] for r in store.db.execute('PRAGMA table_info(projects)')}
            copied_fields = ('concept', 'tone', 'text_prompt', 'image_prompt', 'visual_style',
                             'text_template', 'image_template', 'image_count', 'image_format')
            for project in projects:
                previous = store.db.execute('SELECT target_project FROM vk_transport_imports '
                    'WHERE source=? AND source_owner=? AND source_project=? AND target_owner=?',
                    (str(source), source_owner, project['id'], vk_owner)).fetchone()
                if previous:
                    report['already_imported'] += 1
                    continue
                name = project['name']
                suffix = 0
                while store.db.execute('SELECT 1 FROM projects WHERE owner=? AND name=?', (vk_owner, name)).fetchone():
                    suffix += 1
                    name = f"{project['name'][:65]} (Telegram {project['id']}, {suffix})"
                ident = store.next_id('projects')
                store.db.execute("INSERT INTO projects(id,owner,name,publish_mode) VALUES (?,?,?,'platforms')", (ident, vk_owner, name))
                values = dict(project)
                if (values.get('image_count', 1) not in (0, 1, 2, 3)
                        or values.get('image_format', 'square') not in ('square', 'landscape', 'portrait')):
                    raise Conflict('В исходном проекте некорректные настройки изображений. Перенос отменён.')
                for field in copied_fields:
                    if field in values and field in project_columns:
                        store.db.execute(f'UPDATE projects SET {field}=? WHERE id=?', (values[field], ident))
                store.db.execute("INSERT INTO project_members VALUES (?,?,'member')", (ident, vk_owner))
                if 'project_materials' in tables:
                    for material in incoming.execute('SELECT * FROM project_materials WHERE project_id=?', (project['id'],)):
                        store.db.execute('INSERT INTO project_materials(project_id,kind,path,description) VALUES (?,?,?,?)',
                            (ident, material['kind'], material['path'], material['description']))
                store.consolidate_project_concept(ident)
                if 'project_members' in tables:
                    report['members_to_assign'] += incoming.execute(
                        'SELECT count(*) FROM project_members WHERE project_id=? AND user_id<>?', (project['id'], source_owner)).fetchone()[0]
                if 'project_destinations' in tables:
                    for target in incoming.execute('SELECT * FROM project_destinations WHERE project_id=?', (project['id'],)):
                        target = dict(target)
                        if target['credential_owner'] != source_owner or target['credential_key'] not in available:
                            report['connections_to_reconnect'] += 1
                            continue
                        fields = ('provider', 'credential_key', 'remote_project', 'remote_account', 'channel', 'name')
                        store.db.execute('INSERT INTO project_destinations(project_id,credential_owner,' + ','.join(fields) + ',external_id) '
                            'VALUES (?,?,?,?,?,?,?,?,?)', (ident, vk_owner, *(target[f] for f in fields), target.get('external_id', target['remote_account'])))
                        report['destinations'] += 1
                store.db.execute('INSERT INTO vk_transport_imports VALUES (?,?,?,?,?)',
                                 (str(source), source_owner, project['id'], vk_owner, ident))
                report['projects'] += 1
        # Source jobs, delivery IDs, Telegram users/buttons, and OAuth sessions are never replayed/imported.
    return report


def switch(config_path, settings, source_owner=0, *, import_telegram=True):
    if settings.transport != 'vk':
        raise Conflict('Для переключения загрузите настройки ВК, чтобы не изменить базу Telegram.')
    backup = None
    if settings.database.exists():
        folder = settings.data_dir / 'backups'
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')
        backup = folder / ('before-return-vk-' + stamp + '.sqlite3')
        with sqlite3.connect(settings.database.as_uri() + '?mode=ro', uri=True) as source_db, sqlite3.connect(backup) as target_db:
            source_db.backup(target_db)
        backup.chmod(0o600)
    source = settings.data_dir / 'publikot-telegram.sqlite3'
    if not source.exists():
        source = settings.data_dir / 'publikot.sqlite3'
    report = None
    if import_telegram and source.exists():
        if source_owner <= 0:
            raise Conflict('Для переноса проектов задайте OWNER_TELEGRAM_ID. Для переключения без переноса: --no-import.')
        store = Store(settings.database)
        try:
            report = import_projects(source, store, source_owner, settings.owner_id)
        finally:
            store.db.close()
    set_key(config_path, 'BOT_TRANSPORT', 'vk')
    os.chmod(config_path, 0o600)
    return report, backup


def main():
    parser = argparse.ArgumentParser(description='Переключить Публикота на ВК. Сначала остановите работающего бота.')
    parser.add_argument('--no-import', action='store_true', help='Переключить интерфейс без переноса проектов Telegram')
    args = parser.parse_args()
    try:
        settings = Settings.load(transport='vk')
        source_owner = os.getenv('OWNER_TELEGRAM_ID', '')
        owner = int(source_owner) if source_owner.isdecimal() else 0
        config = Path(__file__).resolve().parent.parent / '.env.python'
        report, backup = switch(config, settings, owner, import_telegram=not args.no_import)
        print('Основной интерфейс: ВКонтакте. Настройки GenAPI и подключения сохранены.')
        if backup:
            print('Создана резервная копия базы ВК в DATA_DIR/backups.')
        if report:
            print(json.dumps(report, ensure_ascii=False))
            print('Участников назначьте по ID ВК. Чужие подключения и конфликтующие токены подключите заново в «Площадках».')
        print('Запуск: .venv/bin/python -m publikot')
    except (ConfigError, Conflict, ProviderError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error, ValueError):
        print('Переключение не завершено. Проверьте базу и конфигурацию; секреты не выводятся.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
