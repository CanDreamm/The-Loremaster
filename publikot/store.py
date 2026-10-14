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
        CREATE TABLE IF NOT EXISTS sessions (
          owner INTEGER PRIMARY KEY, project_id INTEGER REFERENCES projects(id), job_id INTEGER
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
        ''')
        # Не повторяем внешние операции с неизвестным результатом после перезапуска.
        self.db.execute("UPDATE jobs SET status='unknown' WHERE status='publishing'")
        self.db.execute("UPDATE jobs SET status='failed' WHERE status='generating'")

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
            self.db.execute('INSERT INTO sessions(owner,project_id) VALUES (?,?) ON CONFLICT(owner) DO UPDATE SET project_id=excluded.project_id,job_id=NULL', (owner, ident))
        return ident

    def projects(self, owner):
        return self.db.execute('SELECT * FROM projects WHERE owner=? ORDER BY id', (owner,)).fetchall()

    def character(self, owner, name, description, image=None):
        project = self.project(owner)
        with self.transaction():
            self.db.execute('INSERT INTO characters(project_id,name,description) VALUES (?,?,?) ON CONFLICT(project_id,name) DO UPDATE SET description=CASE WHEN excluded.description <> \'\' THEN excluded.description ELSE characters.description END', (project['id'], name, description))
            row = self.db.execute('SELECT id FROM characters WHERE project_id=? AND name=?', (project['id'], name)).fetchone()
            if image:
                self.db.execute('INSERT INTO character_images(character_id,path) VALUES (?,?)', (row['id'], image))

    def characters(self, owner):
        project = self.project(owner)
        return self.db.execute('SELECT c.*, (SELECT count(*) FROM character_images i WHERE i.character_id=c.id) AS images FROM characters c WHERE c.project_id=? ORDER BY c.id', (project['id'],)).fetchall()

    def select(self, owner, ident):
        if not self.db.execute('SELECT 1 FROM projects WHERE owner=? AND id=?', (owner, ident)).fetchone():
            raise Conflict('Проект не найден.')
        self.db.execute('INSERT INTO sessions(owner,project_id) VALUES (?,?) ON CONFLICT(owner) DO UPDATE SET project_id=excluded.project_id,job_id=NULL', (owner, ident))

    def project(self, owner):
        row = self.db.execute('SELECT p.* FROM projects p JOIN sessions s ON s.project_id=p.id WHERE s.owner=? AND p.owner=?', (owner, owner)).fetchone()
        if not row:
            raise Conflict('Создайте /new Название или выберите /select НОМЕР.')
        return row

    def configure(self, owner, field, value):
        if field not in ('concept', 'vk_group'):
            raise ValueError('Invalid field')
        project = self.project(owner)
        self.db.execute(f'UPDATE projects SET {field}=? WHERE id=? AND owner=?', (value, project['id'], owner))

    def create_job(self, owner, brief, image_path=None):
        project = self.project(owner)
        with self.transaction():
            ident = self.db.execute("INSERT INTO jobs(owner,project_id,brief,image_path,status,target_group) VALUES (?,?,?,?,'generating',?)", (owner, project['id'], brief, image_path, project['vk_group'])).lastrowid
            self.db.execute('UPDATE sessions SET job_id=? WHERE owner=?', (ident, owner))
        return ident, dict(project)

    def job(self, owner, ident=None):
        if ident is None:
            row = self.db.execute('SELECT j.* FROM jobs j JOIN sessions s ON s.job_id=j.id WHERE s.owner=? AND j.owner=?', (owner, owner)).fetchone()
        else:
            row = self.db.execute('SELECT * FROM jobs WHERE id=? AND owner=?', (ident, owner)).fetchone()
        if not row:
            raise Conflict('Задание не найдено.')
        return dict(row)

    def save_version(self, owner, ident, revision, body, demo):
        with self.transaction():
            job = self.job(owner, ident)
            if job['status'] != 'generating' or job['revision'] != revision:
                raise Conflict('Задание уже изменилось.')
            self.db.execute('INSERT INTO versions VALUES (?,?,?,?)', (ident, revision, body, int(demo)))
            self.db.execute("UPDATE jobs SET status='review' WHERE id=?", (ident,))

    def revision(self, owner, ident):
        job = self.job(owner, ident)
        return self.db.execute('SELECT * FROM versions WHERE job_id=? AND revision=?', (ident, job['revision'])).fetchone()

    def revise(self, owner):
        with self.transaction():
            job = self.job(owner)
            if job['status'] != 'review':
                raise Conflict('Исправление возможно только для черновика на согласовании.')
            previous = dict(self.revision(owner, job['id']))
            self.db.execute("UPDATE jobs SET revision=revision+1,status='generating' WHERE id=?", (job['id'],))
        return self.job(owner, job['id']), previous

    def claim(self, owner, ident, revision):
        with self.transaction():
            job = self.job(owner, ident)
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
        result = self.db.execute("UPDATE jobs SET status='cancelled' WHERE owner=? AND id=? AND revision=? AND status='review'", (owner, ident, revision))
        if result.rowcount != 1:
            raise Conflict('Кнопка устарела или задание уже обработано.')

    def finish(self, owner, ident, status, url=None):
        if status not in ('failed', 'unknown', 'published'):
            raise ValueError('Invalid final status')
        self.db.execute('UPDATE jobs SET status=?,remote_url=? WHERE id=? AND owner=?', (status, url, ident, owner))
