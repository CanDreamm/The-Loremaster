"""Durable webhook inbox: acknowledge only after SQLite commit."""
import asyncio
import hmac
import json

from aiohttp import web


class TelegramInbox:
    def __init__(self, store, bot):
        self.store, self.db, self.bot = store, store.db, bot
        self.wake = asyncio.Event()
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS telegram_updates (
                update_id INTEGER PRIMARY KEY, body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending');
            UPDATE telegram_updates SET status='failed',body='' WHERE status='processing';
        ''')

    async def receive(self, request):
        try:
            update = await request.json()
        except (ValueError, UnicodeError):
            raise web.HTTPBadRequest()
        if not isinstance(update, dict) or type(update.get('update_id')) is not int or not 0 <= update['update_id'] < 2**63:
            raise web.HTTPBadRequest()
        self.db.execute('INSERT OR IGNORE INTO telegram_updates(update_id,body) VALUES (?,?)',
                        (update['update_id'], json.dumps(update, ensure_ascii=False)))
        self.wake.set()
        return web.json_response({'ok': True})

    async def worker(self):
        while True:
            self.wake.clear()
            row = self.db.execute("SELECT * FROM telegram_updates WHERE status='pending' ORDER BY update_id LIMIT 1").fetchone()
            if row is None:
                await self.wake.wait()
                continue
            self.db.execute("UPDATE telegram_updates SET status='processing' WHERE update_id=?", (row['update_id'],))
            status = 'done'
            try:
                await self.bot.handle_update(json.loads(row['body']))
            except asyncio.CancelledError:
                raise
            except Exception:
                # No automatic replay of generation/publication with unknown outcome.
                status = 'failed'
                print('Событие Telegram не завершено. Проверьте /status; отправьте команду заново.', flush=True)
            self.db.execute('UPDATE telegram_updates SET status=?,body=\'\' WHERE update_id=?', (status, row['update_id']))


def add_webhook(app, inbox, secret):
    async def receive(request):
        if not hmac.compare_digest(request.match_info['secret'], secret):
            raise web.HTTPNotFound()
        return await inbox.receive(request)

    app.router.add_post('/telegram/webhook/{secret}', receive)
    add_inbox_worker(app, inbox)


def add_inbox_worker(app, inbox):
    async def lifecycle(app):
        task = asyncio.create_task(inbox.worker())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app.cleanup_ctx.append(lifecycle)
