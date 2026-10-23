import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .prompts import TEXT_TEMPLATE, IMAGE_TEMPLATE


class Conflict(Exception):
    pass


from .platform_store import PlatformStore


class Store(PlatformStore):
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA busy_timeout=5000')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS projects (
          id INTEGER PRIMARY KEY, owner INTEGER NOT NULL, name TEXT NOT NULL,
          concept TEXT NOT NULL DEFAULT '', vk_group INTEGER,
          UNIQUE(owner, name)
        );
        CREATE TABLE IF NOT EXISTS allowed_users (
          telegram_id INTEGER PRIMARY KEY CHECK(telegram_id > 0),
          added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS project_members (
          project_id INTEGER NOT NULL REFERENCES projects(id), user_id INTEGER NOT NULL,
          role TEXT NOT NULL CHECK(role = 'member'),
          PRIMARY KEY(project_id,user_id)
        );
        CREATE TABLE IF NOT EXISTS project_references (
          id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
          path TEXT NOT NULL, description TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS schema_meta (name TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS id_counters (entity TEXT PRIMARY KEY, value INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS inbox (
          event_id TEXT PRIMARY KEY,
          status TEXT NOT NULL CHECK(status IN ('processing','done','failed'))
        );
        CREATE TABLE IF NOT EXISTS transport_state (name TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions (
          owner INTEGER PRIMARY KEY, project_id INTEGER REFERENCES projects(id), job_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS ui_dialogs (
          actor INTEGER PRIMARY KEY, state TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bot_messages (
          group_id INTEGER NOT NULL, peer_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
          PRIMARY KEY(group_id,peer_id,message_id)
        );
        CREATE TABLE IF NOT EXISTS jobs (
          id INTEGER PRIMARY KEY, owner INTEGER NOT NULL, project_id INTEGER NOT NULL REFERENCES projects(id),
          brief TEXT NOT NULL, image_path TEXT, revision INTEGER NOT NULL DEFAULT 1,
          status TEXT NOT NULL CHECK(status IN ('generating','review','publishing','published','failed','unknown','cancelled')),
          target_group INTEGER, remote_url TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS versions (
          job_id INTEGER NOT NULL REFERENCES jobs(id), revision INTEGER NOT NULL,
          body TEXT NOT NULL, is_demo INTEGER NOT NULL, PRIMARY KEY(job_id,revision)
        );
        CREATE TABLE IF NOT EXISTS approvals (
          job_id INTEGER NOT NULL, revision INTEGER NOT NULL, owner INTEGER NOT NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY(job_id, revision), FOREIGN KEY(job_id, revision) REFERENCES versions(job_id,revision)
        );
        CREATE TABLE IF NOT EXISTS characters (
          id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL REFERENCES projects(id),
          name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', UNIQUE(project_id,name)
        );
        CREATE TABLE IF NOT EXISTS character_images (
          id INTEGER PRIMARY KEY, character_id INTEGER NOT NULL REFERENCES characters(id),
          path TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS project_materials (
          project_id INTEGER NOT NULL REFERENCES projects(id),
          kind TEXT NOT NULL CHECK(kind IN ('style','character')),
          path TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
          PRIMARY KEY(project_id,kind)
        );
        ''')
        # Аддитивная миграция сохраняет базу первой версии и её владельцев.
        columns = {r['name'] for r in self.db.execute('PRAGMA table_info(projects)')}
        for field in ('tone', 'text_prompt', 'image_prompt', 'visual_style'):
            if field not in columns:
                self.db.execute(f"ALTER TABLE projects ADD COLUMN {field} TEXT NOT NULL DEFAULT ''")
        for field, template in (('text_template', TEXT_TEMPLATE), ('image_template', IMAGE_TEMPLATE)):
            if field not in columns:
                default = template.replace("'", "''")
                self.db.execute(f"ALTER TABLE projects ADD COLUMN {field} TEXT NOT NULL DEFAULT '{default}'")
        # Existing projects keep their API connection until explicitly switched.
        for table in ('projects', 'jobs'):
            target_columns = {r['name'] for r in self.db.execute(f'PRAGMA table_info({table})')}
            for name, default in (('publish_mode', 'vk_api'), ('browser_target', '')):
                if name not in target_columns:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} TEXT NOT NULL DEFAULT '{default}'")
        version_columns = {r['name'] for r in self.db.execute('PRAGMA table_info(versions)')}
        if 'image_path' not in version_columns:
            self.db.execute('ALTER TABLE versions ADD COLUMN image_path TEXT')
            self.db.execute('UPDATE versions SET image_path=(SELECT image_path FROM jobs WHERE jobs.id=versions.job_id)')
        for field in ('text_prompt', 'image_prompt', 'prompt_materials'):
            if field not in version_columns:
                self.db.execute(f"ALTER TABLE versions ADD COLUMN {field} TEXT NOT NULL DEFAULT ''")
        if 'reference_path' not in {r['name'] for r in self.db.execute('PRAGMA table_info(jobs)')}:
            self.db.execute('ALTER TABLE jobs ADD COLUMN reference_path TEXT')
        with self.transaction():
            for entity in ('projects', 'jobs'):
                maximum = self.db.execute(f'SELECT COALESCE(MAX(id),0) FROM {entity}').fetchone()[0]
                self.db.execute('INSERT INTO id_counters VALUES (?,?) ON CONFLICT(entity) '
                                'DO UPDATE SET value=MAX(value,excluded.value)', (entity, maximum))
            if not self.db.execute("SELECT 1 FROM schema_meta WHERE name='single_project_materials'").fetchone():
                self.db.execute("INSERT OR IGNORE INTO project_materials(project_id,kind,path,description) "
                    "SELECT c.project_id,'character',i.path,c.name || char(10) || c.description "
                    "FROM character_images i JOIN characters c ON c.id=i.character_id "
                    "WHERE i.id=(SELECT MAX(i2.id) FROM character_images i2 JOIN characters c2 "
                    "ON c2.id=i2.character_id WHERE c2.project_id=c.project_id)")
                self.db.execute("INSERT INTO schema_meta VALUES ('single_project_materials')")
            if not self.db.execute("SELECT 1 FROM schema_meta WHERE name='project_members_backfilled'").fetchone():
                self.db.execute("INSERT OR IGNORE INTO project_members(project_id,user_id,role) SELECT id,owner,'member' FROM projects")
                self.db.execute("INSERT INTO schema_meta VALUES ('project_members_backfilled')")
            if not self.db.execute("SELECT 1 FROM schema_meta WHERE name='concept_consolidated'").fetchone():
                for row in self.db.execute('SELECT id FROM projects').fetchall():
                    self.consolidate_project_concept(row['id'])
                self.db.execute("INSERT INTO schema_meta VALUES ('concept_consolidated')")
        self.init_platforms()
        for field, definition in (('image_count', 'INTEGER NOT NULL DEFAULT 1'),
                                  ('image_format', "TEXT NOT NULL DEFAULT 'square'")):
            if field not in columns:
                self.db.execute(f'ALTER TABLE projects ADD COLUMN {field} {definition}')
        for table in ('jobs', 'versions'):
            existing = {r['name'] for r in self.db.execute(f'PRAGMA table_info({table})')}
            if 'image_paths' not in existing:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN image_paths TEXT NOT NULL DEFAULT '[]'")
        if 'generation_context' not in {r['name'] for r in self.db.execute('PRAGMA table_info(jobs)')}:
            self.db.execute("ALTER TABLE jobs ADD COLUMN generation_context TEXT NOT NULL DEFAULT ''")
        if 'restored_job_id' not in {r['name'] for r in self.db.execute('PRAGMA table_info(jobs)')}:
            self.db.execute('ALTER TABLE jobs ADD COLUMN restored_job_id INTEGER')
        self.db.execute('''CREATE TABLE IF NOT EXISTS ai_requests (
            job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE, revision INTEGER NOT NULL,
            kind TEXT NOT NULL, slot INTEGER NOT NULL, model TEXT NOT NULL, status TEXT NOT NULL,
            request_id TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '',
            local_path TEXT NOT NULL DEFAULT '', PRIMARY KEY(job_id,revision,kind,slot))''')
        if 'request_meta' not in {r['name'] for r in self.db.execute('PRAGMA table_info(ai_requests)')}:
            self.db.execute("ALTER TABLE ai_requests ADD COLUMN request_meta TEXT NOT NULL DEFAULT ''")
        # Не повторяем внешние операции с неизвестным результатом после перезапуска.
        self.db.execute("UPDATE jobs SET status='unknown' WHERE status='publishing'")
        # Image generation never removes the approved text. Its request IDs survive restart.
        self.db.execute("UPDATE jobs SET status='review' WHERE status='generating' AND EXISTS "
                        "(SELECT 1 FROM versions WHERE job_id=jobs.id AND revision=jobs.revision)")
        self.db.execute("UPDATE jobs SET status='failed' WHERE status='generating'")
        self.db.execute("UPDATE inbox SET status='failed' WHERE status='processing'")
        # A queued Postmypost record is recoverable by read-only status checks.
        self.db.execute("UPDATE jobs SET status='publishing' WHERE id IN (SELECT job_id FROM deliveries WHERE status='queued')")

    def begin_event(self, event_id):
        return self.db.execute("INSERT OR IGNORE INTO inbox VALUES (?,'processing')", (event_id,)).rowcount == 1

    def dialog(self, actor):
        row = self.db.execute('SELECT state FROM ui_dialogs WHERE actor=?', (actor,)).fetchone()
        return json.loads(row['state']) if row else None

    def set_dialog(self, actor, state):
        if state is None:
            self.db.execute('DELETE FROM ui_dialogs WHERE actor=?', (actor,))
        else:
            self.db.execute('INSERT INTO ui_dialogs VALUES (?,?) ON CONFLICT(actor) DO UPDATE SET state=excluded.state',
                            (actor, json.dumps(state, ensure_ascii=False)))

    def end_event(self, event_id, success):
        self.db.execute('UPDATE inbox SET status=? WHERE event_id=?', ('done' if success else 'failed', event_id))

    def remember_bot_message(self, group_id, peer_id, message_id):
        if type(message_id) is int and message_id > 0:
            self.db.execute('INSERT OR IGNORE INTO bot_messages VALUES (?,?,?)', (group_id, peer_id, message_id))

    def bot_message_ids(self, group_id, peer_id):
        return [row['message_id'] for row in self.db.execute(
            'SELECT message_id FROM bot_messages WHERE group_id=? AND peer_id=? ORDER BY message_id', (group_id, peer_id))]

    def forget_bot_messages(self, group_id, peer_id, message_ids):
        self.db.executemany('DELETE FROM bot_messages WHERE group_id=? AND peer_id=? AND message_id=?',
                            [(group_id, peer_id, ident) for ident in message_ids])

    def cursor(self, group_id, value=None):
        key = f'vk:{group_id}:ts'
        if value is not None:
            self.db.execute('INSERT INTO transport_state VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value', (key, str(value)))
        row = self.db.execute('SELECT value FROM transport_state WHERE name=?', (key,)).fetchone()
        return row['value'] if row else None

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except Exception:
            self.db.execute('ROLLBACK')
            raise

    def create_project(self, owner, name):
        with self.transaction():
            ident = self.next_id('projects')
            self.db.execute('INSERT INTO projects(id,owner,name) VALUES (?,?,?)', (ident, owner, name))
            self.db.execute("INSERT INTO project_members VALUES (?,?,'member')", (ident, owner))
            self.db.execute('INSERT INTO sessions(owner,project_id) VALUES (?,?) ON CONFLICT(owner) DO UPDATE SET project_id=excluded.project_id,job_id=NULL', (owner, ident))
        return ident

    def next_id(self, entity):
        # Call inside the insert transaction: old buttons must never target new records.
        if entity not in ('projects', 'jobs') or not self.db.in_transaction:
            raise ValueError('Invalid ID allocation')
        maximum = self.db.execute(f'SELECT COALESCE(MAX(id),0) FROM {entity}').fetchone()[0]
        self.db.execute('UPDATE id_counters SET value=MAX(value,?)+1 WHERE entity=?', (maximum, entity))
        return self.db.execute('SELECT value FROM id_counters WHERE entity=?', (entity,)).fetchone()[0]

    def deletable_project(self, actor, project_id, admin):
        if not self.is_allowed(actor, admin):
            raise Conflict('Доступ к боту отозван.')
        project = self.db.execute('SELECT * FROM projects WHERE id=?', (project_id,)).fetchone()
        if not project or actor not in (project['owner'], admin):
            raise Conflict('Удалить проект может только его владелец или администратор бота.')
        return project

    def delete_project(self, actor, project_id, admin):
        with self.transaction():
            self.deletable_project(actor, project_id, admin)
            if self.db.execute("SELECT 1 FROM jobs WHERE project_id=? AND status IN ('generating','publishing')",
                               (project_id,)).fetchone():
                raise Conflict('Дождитесь завершения генерации или публикации, затем удалите проект.')
            self.db.execute('UPDATE sessions SET project_id=NULL,job_id=NULL WHERE project_id=? '
                            'OR job_id IN (SELECT id FROM jobs WHERE project_id=?)', (project_id, project_id))
            for dialog in self.db.execute('SELECT actor,state FROM ui_dialogs').fetchall():
                if json.loads(dialog['state']).get('project') == project_id:
                    self.set_dialog(dialog['actor'], None)
            tables = {r['name'] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ('oauth_states', 'vk_connections'):
                if table in tables:
                    self.db.execute(f'DELETE FROM {table} WHERE project_id=?', (project_id,))
            self.db.execute('DELETE FROM approvals WHERE job_id IN (SELECT id FROM jobs WHERE project_id=?)', (project_id,))
            self.db.execute('DELETE FROM versions WHERE job_id IN (SELECT id FROM jobs WHERE project_id=?)', (project_id,))
            self.db.execute('DELETE FROM deliveries WHERE job_id IN (SELECT id FROM jobs WHERE project_id=?)', (project_id,))
            self.db.execute('DELETE FROM jobs WHERE project_id=?', (project_id,))
            self.db.execute('DELETE FROM project_destinations WHERE project_id=?', (project_id,))
            self.db.execute('DELETE FROM platform_choices WHERE project_id=?', (project_id,))
            self.db.execute('DELETE FROM character_images WHERE character_id IN '
                            '(SELECT id FROM characters WHERE project_id=?)', (project_id,))
            for table in ('characters', 'project_references', 'project_materials', 'project_members'):
                self.db.execute(f'DELETE FROM {table} WHERE project_id=?', (project_id,))
            self.db.execute('DELETE FROM projects WHERE id=?', (project_id,))

    def is_allowed(self, user, admin):
        return user == admin or self.db.execute('SELECT 1 FROM allowed_users WHERE telegram_id=?', (user,)).fetchone() is not None

    def set_access(self, actor, user, allowed, admin):
        if actor != admin:
            raise Conflict('Эта команда доступна только администратору.')
        if user <= 0:
            raise Conflict('Нужен положительный числовой ID пользователя.')
        if user == admin:
            raise Conflict('Администратор всегда имеет доступ.')
        if allowed:
            self.db.execute('INSERT OR IGNORE INTO allowed_users(telegram_id) VALUES (?)', (user,))
        else:
            self.db.execute('DELETE FROM allowed_users WHERE telegram_id=?', (user,))

    def users(self, actor, admin):
        if actor != admin:
            raise Conflict('Эта команда доступна только администратору.')
        return [admin] + [row['telegram_id'] for row in self.db.execute('SELECT telegram_id FROM allowed_users WHERE telegram_id<>? ORDER BY telegram_id', (admin,))]

    def projects(self, owner):
        return self.db.execute('SELECT p.*,m.role FROM projects p JOIN project_members m ON m.project_id=p.id WHERE m.user_id=? ORDER BY p.id', (owner,)).fetchall()

    def require(self, user, project_id):
        row = self.db.execute('SELECT p.*,m.role FROM projects p JOIN project_members m ON m.project_id=p.id WHERE p.id=? AND m.user_id=?', (project_id, user)).fetchone()
        if not row:
            raise Conflict('Нет доступа к проекту или недостаточно прав.')
        return row

    def all_projects(self, actor, admin):
        if actor != admin:
            raise Conflict('Эта команда доступна только администратору.')
        return self.db.execute('SELECT * FROM projects ORDER BY id').fetchall()

    def members(self, actor, project_id, admin):
        if actor != admin:
            raise Conflict('Эта команда доступна только администратору.')
        if not self.db.execute('SELECT 1 FROM projects WHERE id=?', (project_id,)).fetchone():
            raise Conflict('Проект не найден.')
        return self.db.execute('SELECT user_id FROM project_members WHERE project_id=? ORDER BY user_id', (project_id,)).fetchall()

    def set_member(self, actor, project_id, user, allowed, admin):
        if actor != admin:
            raise Conflict('Назначать проекты может только администратор.')
        if user <= 0 or not self.db.execute('SELECT 1 FROM projects WHERE id=?', (project_id,)).fetchone():
            raise Conflict('Проверьте номер проекта и ID участника.')
        if allowed and not self.is_allowed(user, admin):
            raise Conflict('Сначала администратор должен разрешить этому пользователю доступ к боту.')
        with self.transaction():
            if not allowed:
                self.db.execute('DELETE FROM project_members WHERE project_id=? AND user_id=?', (project_id, user))
                self.db.execute('UPDATE sessions SET project_id=NULL,job_id=NULL WHERE owner=? AND project_id=?', (user, project_id))
            else:
                self.db.execute("INSERT OR IGNORE INTO project_members VALUES (?,?,'member')", (project_id, user))

    def reference(self, actor, path, description, project_id=None):
        project = self.require(actor, project_id) if project_id is not None else self.project(actor)
        self.require(actor, project['id'])
        self.db.execute('INSERT INTO project_references(project_id,path,description) VALUES (?,?,?)', (project['id'], path, description))

    def references(self, actor):
        project = self.project(actor)
        return self.db.execute('SELECT * FROM project_references WHERE project_id=? ORDER BY id', (project['id'],)).fetchall()

    def character(self, owner, name, description, image=None, project_id=None):
        project = self.require(owner, project_id) if project_id is not None else self.project(owner)
        if not image:
            raise Conflict('Прикрепите одну фотографию персонажа.')
        self.set_material(owner, project['id'], 'character', image, '\n'.join(filter(None, (name, description))))

    def characters(self, owner, project_id=None):
        project = self.require(owner, project_id) if project_id is not None else self.project(owner)
        row = self.material(owner, project['id'], 'character')
        if not row:
            return []
        name, _, description = row['description'].partition('\n')
        return [{'name': name or 'Персонаж', 'description': description, 'images': 1}]

    def character_media(self, actor):
        project = self.project(actor)
        row = self.material(actor, project['id'], 'character')
        return [dict(row, name='Персонажи')] if row else []

    def material(self, actor, project_id, kind):
        self.require(actor, project_id)
        return self.db.execute('SELECT * FROM project_materials WHERE project_id=? AND kind=?',
                               (project_id, kind)).fetchone()

    def consolidate_project_concept(self, project_id):
        """One-time legacy import; retained style files are archive data, not AI inputs."""
        project = self.db.execute('SELECT concept,tone,visual_style FROM projects WHERE id=?', (project_id,)).fetchone()
        style = self.db.execute("SELECT description FROM project_materials WHERE project_id=? AND kind='style'", (project_id,)).fetchone()
        concept = project['concept'] or ''
        for label, value in (('Манера речи', project['tone']), ('Оформление', project['visual_style']),
                             ('Пожелания к оформлению', style['description'] if style else '')):
            value = (value or '').strip()
            if value and value not in concept:
                concept += ('\n\n' if concept else '') + label + ':\n' + value
        self.db.execute("UPDATE projects SET concept=?,tone='',visual_style='' WHERE id=?", (concept, project_id))

    def set_material(self, actor, project_id, kind, path, description=''):
        self.require(actor, project_id)
        if kind not in ('style', 'character') or not path:
            raise ValueError('Invalid material')
        with self.transaction():
            self.db.execute('INSERT INTO project_materials VALUES (?,?,?,?) ON CONFLICT(project_id,kind) '
                            'DO UPDATE SET path=excluded.path,description=excluded.description',
                            (project_id, kind, path, description))
            if kind == 'style':
                self.db.execute("UPDATE projects SET visual_style='' WHERE id=?", (project_id,))

    def select(self, owner, ident):
        self.require(owner, ident)
        self.db.execute('INSERT INTO sessions(owner,project_id) VALUES (?,?) ON CONFLICT(owner) DO UPDATE SET project_id=excluded.project_id,job_id=NULL', (owner, ident))

    def project(self, owner):
        row = self.db.execute('SELECT p.*,m.role FROM projects p JOIN sessions s ON s.project_id=p.id JOIN project_members m ON m.project_id=p.id AND m.user_id=s.owner WHERE s.owner=?', (owner,)).fetchone()
        if not row:
            raise Conflict('Создайте /new Название или выберите /select НОМЕР.')
        return row

    def configure(self, owner, field, value):
        if field not in ('concept', 'vk_group', 'tone', 'text_prompt', 'image_prompt', 'visual_style',
                         'text_template', 'image_template', 'image_count', 'image_format'):
            raise ValueError('Invalid field')
        if field == 'image_count' and (type(value) is not int or not 0 <= value <= 3):
            raise Conflict('Количество изображений: от 0 до 3.')
        if field == 'image_format' and value not in ('square', 'landscape', 'portrait'):
            raise Conflict('Выберите квадратный, горизонтальный или вертикальный формат.')
        if field in ('text_template', 'image_template') and (not value.strip() or len(value) > 12000):
            raise Conflict('Промпт должен содержать от 1 до 12000 символов.')
        project = self.project(owner)
        self.require(owner, project['id'])
        self.db.execute(f'UPDATE projects SET {field}=? WHERE id=?', (value, project['id']))

    def browser_destination(self, actor, platform, target):
        from .browser_publish import validate_target
        target = validate_target(platform, target)
        project = self.project(actor)
        if actor != project['owner']:
            raise Conflict('Площадку публикации настраивает владелец проекта.')
        with self.transaction():
            self.db.execute('UPDATE projects SET publish_mode=?,browser_target=? WHERE id=?',
                            (platform + '_browser', target, project['id']))

    def create_job(self, owner, brief, image_path=None, project_id=None, reference_path=None):
        project = self.require(owner, project_id) if project_id is not None else self.project(owner)
        self.require(owner, project['id'])
        with self.transaction():
            ident = self.next_id('jobs')
            self.db.execute("INSERT INTO jobs(id,owner,project_id,brief,image_path,status,target_group) VALUES (?,?,?,?,?,'generating',?)", (ident, owner, project['id'], brief, image_path, project['vk_group']))
            self.db.execute('UPDATE jobs SET reference_path=?,publish_mode=?,browser_target=? WHERE id=?',
                            (reference_path, project['publish_mode'], project['browser_target'], ident))
            self.db.execute('UPDATE jobs SET destinations=? WHERE id=?', (self.destination_snapshot(owner, project['id']), ident))
            self.db.execute('UPDATE sessions SET job_id=? WHERE owner=? AND project_id=?', (ident, owner, project['id']))
        return ident, dict(project)

    def job(self, owner, ident=None):
        if ident is None:
            row = self.db.execute('SELECT j.* FROM jobs j JOIN sessions s ON s.job_id=j.id JOIN project_members m ON m.project_id=j.project_id AND m.user_id=s.owner WHERE s.owner=?', (owner,)).fetchone()
        else:
            row = self.db.execute('SELECT j.* FROM jobs j JOIN project_members m ON m.project_id=j.project_id WHERE j.id=? AND m.user_id=?', (ident, owner)).fetchone()
        if not row:
            raise Conflict('Задание не найдено.')
        return dict(row)

    def jobs(self, actor):
        project = self.project(actor)
        return self.db.execute('SELECT id,revision,status FROM jobs WHERE project_id=? ORDER BY id DESC LIMIT 30', (project['id'],)).fetchall()

    def select_job(self, actor, ident):
        job = self.job(actor, ident)
        self.db.execute('INSERT INTO sessions(owner,project_id,job_id) VALUES (?,?,?) ON CONFLICT(owner) DO UPDATE SET project_id=excluded.project_id,job_id=excluded.job_id', (actor, job['project_id'], ident))

    def save_version(self, owner, ident, revision, body, demo, prompts=None):
        with self.transaction():
            job = self.job(owner, ident)
            self.require(owner, job['project_id'])
            if job['status'] != 'generating' or job['revision'] != revision:
                raise Conflict('Задание уже изменилось.')
            self.db.execute('INSERT INTO versions(job_id,revision,body,is_demo,image_path) VALUES (?,?,?,?,?)', (ident, revision, body, int(demo), job['image_path']))
            self.db.execute('UPDATE versions SET image_paths=? WHERE job_id=? AND revision=?',
                            (job['image_paths'], ident, revision))
            if prompts is not None:
                self.db.execute('UPDATE versions SET text_prompt=?,image_prompt=?,prompt_materials=? WHERE job_id=? AND revision=?',
                    (prompts.text, prompts.image, json.dumps(prompts.materials, ensure_ascii=False), ident, revision))
            self.db.execute("UPDATE jobs SET status='review' WHERE id=?", (ident,))

    def revision(self, owner, ident):
        job = self.job(owner, ident)
        return self.db.execute('SELECT * FROM versions WHERE job_id=? AND revision=?', (ident, job['revision'])).fetchone()

    def revise(self, owner):
        with self.transaction():
            job = self.job(owner)
            self.require(owner, job['project_id'])
            if job['status'] != 'review':
                raise Conflict('Исправление возможно только для черновика на согласовании.')
            previous = dict(self.revision(owner, job['id']))
            self.db.execute("UPDATE jobs SET revision=revision+1,status='generating' WHERE id=?", (job['id'],))
        return self.job(owner, job['id']), previous

    def expect_review(self, actor, ident, revision):
        job = self.job(actor, ident)
        if job['status'] != 'review' or job['revision'] != revision:
            raise Conflict('Эта кнопка устарела или задание уже обработано.')
        return job

    def replace_image(self, actor, ident, revision, path, slot=0):
        from .media import version_images
        images = version_images(self.revision(actor, ident))
        if type(slot) is not int or not 0 <= slot <= len(images) or slot > 2:
            raise Conflict('Выберите существующее изображение или следующий свободный номер.')
        if slot == len(images):
            images.append(path)
        else:
            images[slot] = path
        self.replace_images(actor, ident, revision, images)

    def replace_images(self, actor, ident, revision, images, *, retry_unknown=False):
        from .media import paths
        images = paths(images)
        path = images[0] if images else None
        with self.transaction():
            self.expect_review(actor, ident, revision)
            if retry_unknown and not self.db.execute("SELECT 1 FROM ai_requests WHERE job_id=? AND revision=? AND kind IN ('image','image_prompt') AND status='submitting' AND request_id=''", (ident, revision)).fetchone():
                raise Conflict('Нет запроса изображений с потерянным ID. Откройте задание заново.')
            old = self.revision(actor, ident)
            next_revision = revision + 1
            self.db.execute('INSERT INTO versions(job_id,revision,body,is_demo,image_path) VALUES (?,?,?,?,?)', (ident, next_revision, old['body'], old['is_demo'], path))
            self.db.execute('UPDATE versions SET text_prompt=?,image_prompt=?,prompt_materials=? WHERE job_id=? AND revision=?',
                (old['text_prompt'], old['image_prompt'], old['prompt_materials'], ident, next_revision))
            self.db.execute('UPDATE jobs SET revision=?,image_path=? WHERE id=?', (next_revision, path, ident))
            self.db.execute('UPDATE jobs SET image_paths=? WHERE id=?', (json.dumps(images), ident))
            self.db.execute('UPDATE versions SET image_paths=? WHERE job_id=? AND revision=?',
                            (json.dumps(images), ident, next_revision))
            if retry_unknown:
                # Preserve completed/pending known requests, never pay for them again.
                # Unknown requests stay in the previous revision for audit/history checks.
                self.db.execute('''INSERT INTO ai_requests(job_id,revision,kind,slot,model,status,request_id,result,local_path,request_meta)
                    SELECT job_id,?,kind,slot,model,status,request_id,result,local_path,request_meta
                    FROM ai_requests WHERE job_id=? AND revision=? AND kind IN ('image','image_prompt')
                    AND (request_id<>'' OR status='rejected')''', (next_revision, ident, revision))
                self.db.execute('''INSERT INTO ai_requests(job_id,revision,kind,slot,model,status)
                    SELECT job_id,?,kind,slot,model,'retry_authorized' FROM ai_requests
                    WHERE job_id=? AND revision=? AND kind IN ('image','image_prompt') AND status='submitting' AND request_id=''
                    ''', (next_revision, ident, revision))

    def claim(self, owner, ident, revision):
        with self.transaction():
            job = self.job(owner, ident)
            self.require(owner, job['project_id'])
            if job['status'] != 'review' or job['revision'] != revision:
                raise Conflict('Эта кнопка устарела или задание уже обработано.')
            version = self.revision(owner, ident)
            if version['is_demo']:
                raise Conflict('Демо-текст нельзя публиковать. Настройте ИИ и создайте новый пост.')
            context = json.loads(job['generation_context'] or '{}')
            desired = context.get('project', {}).get('image_count', 0)
            from .media import version_images
            if len(version_images(version)) < desired:
                raise Conflict('Сначала создайте изображения для этой публикации. Для публикаций без фото выберите 0 в настройках и создайте новое задание.')
            if job['publish_mode'] == 'vk_api' and not job['target_group']:
                raise Conflict('Сообщество не задано. Настройте /vk НОМЕР и создайте новый пост.')
            if job['publish_mode'] not in ('vk_api', 'platforms') and not job['browser_target']:
                raise Conflict('Настройте площадку браузера и создайте новое задание.')
            if job['publish_mode'] == 'platforms':
                targets = json.loads(job['destinations'])
                if not targets:
                    raise Conflict('Подключите площадки и создайте новое задание.')
                if job['destinations'] != self.destination_snapshot(owner, job['project_id']):
                    raise Conflict('Площадки или токен изменились. Создайте новое задание.')
                if self.require(owner, job['project_id'])['publish_mode'] != 'platforms':
                    raise Conflict('Режим публикации изменился.')
                for target in targets:
                    self.db.execute('INSERT INTO deliveries(job_id,destination_id,target) VALUES (?,?,?)',
                                    (ident, target['id'], json.dumps(target, ensure_ascii=False)))
            self.db.execute('INSERT INTO approvals(job_id,revision,owner) VALUES (?,?,?)', (ident, revision, owner))
            self.db.execute("UPDATE jobs SET status='publishing' WHERE id=?", (ident,))
        return job, dict(version)

    def can_restore_upload_failure(self, actor, ident):
        job = self.job(actor, ident)
        if job['status'] != 'failed' or job['publish_mode'] != 'platforms':
            return False
        rows = self.delivery_rows(actor, ident)
        # These suffixes come only from our pre-publication upload checks, including
        # the previous version's rejected storage URL. Never retry an ambiguous write.
        return bool(rows) and all(r['status'] == 'failed' and not r['remote_id']
            and json.loads(r['target'])['provider'] == 'postmypost'
            and (r['error'].endswith(('Файл не отправлен.', 'Пост не отправлен.'))
                 or r['error'] in ('Postmypost: неверные параметры загрузки.',
                                   'Postmypost: HTTP 422. Запрос отклонён.')
                 or (r['error'].startswith('Postmypost: HTTP 422. ')
                     and r['error'].endswith('Публикация не создана.'))) for r in rows)

    def restore_upload_draft(self, actor, ident, revision):
        with self.transaction():
            job = self.job(actor, ident)
            if job['revision'] != revision or not self.can_restore_upload_failure(actor, ident):
                raise Conflict('Восстановление доступно только после ошибки загрузки до отправки публикации.')
            if job['restored_job_id']:
                return self.job(actor, job['restored_job_id'])['id']
            project = self.require(actor, job['project_id'])
            if project['publish_mode'] != 'platforms' or job['destinations'] != self.destination_snapshot(actor, job['project_id']):
                raise Conflict('Площадки или токены изменились. Восстановление для прежних получателей остановлено.')
            old = dict(self.revision(actor, ident))
            new = self.next_id('jobs')
            fields = ('project_id', 'brief', 'image_path', 'target_group', 'reference_path',
                      'publish_mode', 'browser_target', 'destinations', 'image_paths', 'generation_context')
            self.db.execute('INSERT INTO jobs(id,owner,status,' + ','.join(fields) + ') VALUES (?, ?, \'review\','
                            + ','.join('?' for _ in fields) + ')', (new, actor, *(job[f] for f in fields)))
            fields = ('body', 'is_demo', 'image_path', 'text_prompt', 'image_prompt', 'prompt_materials', 'image_paths')
            self.db.execute('INSERT INTO versions(job_id,revision,' + ','.join(fields) + ') VALUES (?,1,'
                            + ','.join('?' for _ in fields) + ')', (new, *(old[f] for f in fields)))
            self.db.execute('UPDATE jobs SET restored_job_id=? WHERE id=?', (new, ident))
            self.select_job(actor, new)
        return new

    def cancel(self, owner, ident, revision):
        job = self.job(owner, ident)
        self.require(owner, job['project_id'])
        result = self.db.execute("UPDATE jobs SET status='cancelled' WHERE id=? AND revision=? AND status='review'", (ident, revision))
        if result.rowcount != 1:
            raise Conflict('Кнопка устарела или задание уже обработано.')

    def finish(self, owner, ident, status, url=None):
        if status not in ('failed', 'unknown', 'published'):
            raise ValueError('Invalid final status')
        self.job(owner, ident)
        self.db.execute('UPDATE jobs SET status=?,remote_url=? WHERE id=?', (status, url, ident))
