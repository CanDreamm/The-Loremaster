import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class Conflict(Exception):
    pass


class Store:
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
        # Не повторяем внешние операции с неизвестным результатом после перезапуска.
        self.db.execute("UPDATE jobs SET status='unknown' WHERE status='publishing'")
        self.db.execute("UPDATE jobs SET status='failed' WHERE status='generating'")
        self.db.execute("UPDATE inbox SET status='failed' WHERE status='processing'")

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
            ident = self.db.execute('INSERT INTO projects(owner,name) VALUES (?,?)', (owner, name)).lastrowid
            self.db.execute("INSERT INTO project_members VALUES (?,?,'member')", (ident, owner))
            self.db.execute('INSERT INTO sessions(owner,project_id) VALUES (?,?) ON CONFLICT(owner) DO UPDATE SET project_id=excluded.project_id,job_id=NULL', (owner, ident))
        return ident

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
        if field not in ('concept', 'vk_group', 'tone', 'text_prompt', 'image_prompt', 'visual_style'):
            raise ValueError('Invalid field')
        project = self.project(owner)
        self.require(owner, project['id'])
        self.db.execute(f'UPDATE projects SET {field}=? WHERE id=?', (value, project['id']))

    def create_job(self, owner, brief, image_path=None, project_id=None, reference_path=None):
        project = self.require(owner, project_id) if project_id is not None else self.project(owner)
        self.require(owner, project['id'])
        with self.transaction():
            ident = self.db.execute("INSERT INTO jobs(owner,project_id,brief,image_path,status,target_group) VALUES (?,?,?,?,'generating',?)", (owner, project['id'], brief, image_path, project['vk_group'])).lastrowid
            self.db.execute('UPDATE jobs SET reference_path=? WHERE id=?', (reference_path, ident))
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

    def replace_image(self, actor, ident, revision, path):
        with self.transaction():
            self.expect_review(actor, ident, revision)
            old = self.revision(actor, ident)
            next_revision = revision + 1
            self.db.execute('INSERT INTO versions(job_id,revision,body,is_demo,image_path) VALUES (?,?,?,?,?)', (ident, next_revision, old['body'], old['is_demo'], path))
            self.db.execute('UPDATE versions SET text_prompt=?,image_prompt=?,prompt_materials=? WHERE job_id=? AND revision=?',
                (old['text_prompt'], old['image_prompt'], old['prompt_materials'], ident, next_revision))
            self.db.execute('UPDATE jobs SET revision=?,image_path=? WHERE id=?', (next_revision, path, ident))

    def claim(self, owner, ident, revision):
        with self.transaction():
            job = self.job(owner, ident)
            self.require(owner, job['project_id'])
            if job['status'] != 'review' or job['revision'] != revision:
                raise Conflict('Эта кнопка устарела или задание уже обработано.')
            version = self.revision(owner, ident)
            if version['is_demo']:
                raise Conflict('Демо-текст нельзя публиковать. Настройте ИИ и создайте новый пост.')
            if not job['target_group']:
                raise Conflict('Сообщество не задано. Настройте /vk НОМЕР и создайте новый пост.')
            self.db.execute('INSERT INTO approvals(job_id,revision,owner) VALUES (?,?,?)', (ident, revision, owner))
            self.db.execute("UPDATE jobs SET status='publishing' WHERE id=?", (ident,))
        return job, dict(version)

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
