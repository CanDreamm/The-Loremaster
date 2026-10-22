"""Personal encrypted credentials, shared destinations and per-destination delivery state."""
import json

from .store import Conflict


class PlatformStore:
    def init_platforms(self):
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS personal_credentials (
          actor INTEGER NOT NULL, provider TEXT NOT NULL, ciphertext TEXT NOT NULL,
          version INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(actor,provider)
        );
        CREATE TABLE IF NOT EXISTS project_destinations (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          project_id INTEGER NOT NULL REFERENCES projects(id), provider TEXT NOT NULL,
          credential_owner INTEGER NOT NULL, credential_key TEXT NOT NULL,
          remote_project TEXT NOT NULL, remote_account TEXT NOT NULL,
          channel TEXT NOT NULL, name TEXT NOT NULL,
          UNIQUE(project_id,provider,credential_owner,remote_project,remote_account)
        );
        CREATE TABLE IF NOT EXISTS platform_choices (
          actor INTEGER NOT NULL, nonce TEXT NOT NULL, kind TEXT NOT NULL,
          project_id INTEGER NOT NULL, value TEXT NOT NULL, expires INTEGER NOT NULL,
          PRIMARY KEY(actor,nonce)
        );
        CREATE TABLE IF NOT EXISTS deliveries (
          job_id INTEGER NOT NULL REFERENCES jobs(id), destination_id INTEGER NOT NULL,
          target TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          remote_id TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '',
          error TEXT NOT NULL DEFAULT '', PRIMARY KEY(job_id,destination_id)
        );
        ''')
        if 'external_id' not in {r['name'] for r in self.db.execute('PRAGMA table_info(project_destinations)')}:
            self.db.execute("ALTER TABLE project_destinations ADD COLUMN external_id TEXT NOT NULL DEFAULT ''")
        if 'destinations' not in {r['name'] for r in self.db.execute('PRAGMA table_info(jobs)')}:
            self.db.execute("ALTER TABLE jobs ADD COLUMN destinations TEXT NOT NULL DEFAULT '[]'")
        # No external write is resumed after an interrupted process.
        self.db.execute("UPDATE deliveries SET status='unknown',error='Отправка прервана; проверьте площадку.' "
                        "WHERE status='sending'")
        self.db.execute("UPDATE deliveries SET status='failed',error='Не отправлено до перезапуска.' "
                        "WHERE status='pending'")

    def credential_record(self, actor, provider):
        return self.db.execute('SELECT * FROM personal_credentials WHERE actor=? AND provider=?',
                               (actor, provider)).fetchone()

    def put_credential(self, actor, provider, encrypted):
        self.db.execute('INSERT INTO personal_credentials(actor,provider,ciphertext) VALUES (?,?,?) '
                        'ON CONFLICT(actor,provider) DO UPDATE SET ciphertext=excluded.ciphertext,version=version+1',
                        (actor, provider, encrypted))

    def destination_rows(self, actor, project):
        self.require(actor, project)
        return [dict(r) for r in self.db.execute(
            'SELECT d.*,COALESCE(c.version,0) AS credential_version FROM project_destinations d '
            'LEFT JOIN personal_credentials c ON c.actor=d.credential_owner AND c.provider=d.credential_key '
            'WHERE d.project_id=? ORDER BY d.id', (project,))]

    def destination_snapshot(self, actor, project):
        return json.dumps(self.destination_rows(actor, project), sort_keys=True, ensure_ascii=False)

    def attach_destination(self, actor, project, provider, remote_project, remote_account, channel, name,
                           credential_key=None, external_id=None):
        self.require(actor, project)
        if provider not in ('postmypost', 'dzen'):
            raise ValueError('Invalid provider')
        key = credential_key or provider
        credential = self.credential_record(actor, key)
        if not credential or not credential['ciphertext']:
            raise Conflict('Сначала подключите личный токен или сеанс площадки.')
        if self.db.execute("SELECT 1 FROM jobs WHERE project_id=? AND status='publishing'", (project,)).fetchone():
            raise Conflict('Дождитесь окончания публикации перед изменением площадок.')
        identity = str(external_id or remote_account)
        with self.transaction():
            duplicate = self.db.execute('SELECT * FROM project_destinations WHERE project_id=? AND provider=? '
                'AND channel=? AND external_id=?', (project, provider, channel, identity)).fetchone()
            if duplicate and (duplicate['credential_owner'] != actor or duplicate['remote_project'] != str(remote_project)
                              or duplicate['remote_account'] != str(remote_account)):
                raise Conflict('Эта площадка уже подключена к проекту. Сначала отключите прежнее подключение.')
            self.db.execute('INSERT INTO project_destinations(project_id,provider,credential_owner,credential_key,'
                'remote_project,remote_account,channel,name,external_id) VALUES (?,?,?,?,?,?,?,?,?) '
                'ON CONFLICT(project_id,provider,credential_owner,remote_project,remote_account) '
                'DO UPDATE SET channel=excluded.channel,name=excluded.name',
                (project, provider, actor, key, str(remote_project), str(remote_account), channel, name[:150], identity))
            self.db.execute("UPDATE projects SET publish_mode='platforms' WHERE id=?", (project,))

    def detach_destination(self, actor, project, ident, admin):
        p = self.require(actor, project)
        row = self.db.execute('SELECT * FROM project_destinations WHERE project_id=? AND id=?',
                              (project, ident)).fetchone()
        if not row or actor not in (p['owner'], admin, row['credential_owner']):
            raise Conflict('Отключить площадку может её подключивший пользователь, владелец проекта или администратор.')
        if self.db.execute("SELECT 1 FROM jobs WHERE project_id=? AND status='publishing'", (project,)).fetchone():
            raise Conflict('Сначала дождитесь завершения публикации.')
        self.db.execute('DELETE FROM project_destinations WHERE id=?', (ident,))

    def project_manager(self, actor, project, admin):
        p = self.require(actor, project)
        if actor not in (p['owner'], admin):
            raise Conflict('Участников назначает владелец проекта или администратор.')
        return p

    def assign_project_user(self, actor, project, user, allowed, admin):
        self.project_manager(actor, project, admin)
        p = self.require(actor, project)
        if user in (p['owner'], admin) and not allowed:
            raise Conflict('Владельца и администратора нельзя исключить из проекта.')
        # Reuse the existing access/session cleanup rules after checking project ownership.
        self.set_member(admin, project, user, allowed, admin)

    def delivery_rows(self, actor, job_id):
        self.job(actor, job_id)
        return [dict(r) for r in self.db.execute('SELECT * FROM deliveries WHERE job_id=? ORDER BY destination_id',
                                                (job_id,))]

    def delivery_update(self, actor, job_id, destination_id, status, *, remote_id=None, url=None, error=''):
        self.job(actor, job_id)
        if status not in ('pending', 'sending', 'queued', 'published', 'failed', 'unknown'):
            raise ValueError('Invalid delivery state')
        self.db.execute('UPDATE deliveries SET status=?,remote_id=COALESCE(?,remote_id),url=COALESCE(?,url),error=? '
                        'WHERE job_id=? AND destination_id=?',
                        (status, remote_id, url, error, job_id, destination_id))

    def aggregate_deliveries(self, actor, job_id):
        rows = self.delivery_rows(actor, job_id)
        statuses = {r['status'] for r in rows}
        if not rows:
            return
        if statuses & {'pending', 'sending', 'queued'}:
            status = 'publishing'
        elif 'unknown' in statuses:
            status = 'unknown'
        elif 'failed' in statuses:
            status = 'failed'
        else:
            status = 'published'
        urls = '\n'.join(r['url'] for r in rows if r['url']) or None
        self.db.execute('UPDATE jobs SET status=?,remote_url=? WHERE id=?', (status, urls, job_id))
