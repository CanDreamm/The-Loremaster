"""Long polling through the intermediary, with a durable offset and inbox."""
import asyncio
import json

from .providers import ProviderError


class TelegramPolling:
    def __init__(self, inbox, client):
        self.inbox, self.client = inbox, client
        self.db = inbox.db
        self.db.execute('''CREATE TABLE IF NOT EXISTS telegram_poll_state (
            id INTEGER PRIMARY KEY CHECK(id=1), next_offset INTEGER NOT NULL)''')

    @property
    def offset(self):
        row = self.db.execute('SELECT next_offset FROM telegram_poll_state WHERE id=1').fetchone()
        return row['next_offset'] if row else None

    def save(self, updates):
        if not updates:
            return
        # Commit the entire batch and its next offset together. A later request
        # may acknowledge updates only after they are safely in the local inbox.
        next_offset = max(update['update_id'] for update in updates) + 1
        with self.inbox.store.transaction():
            for update in updates:
                self.db.execute('INSERT OR IGNORE INTO telegram_updates(update_id,body) VALUES (?,?)',
                                (update['update_id'], json.dumps(update, ensure_ascii=False)))
            self.db.execute('''INSERT INTO telegram_poll_state VALUES (1,?)
                ON CONFLICT(id) DO UPDATE SET next_offset=MAX(next_offset,excluded.next_offset)''', (next_offset,))
        self.inbox.wake.set()

    async def run(self):
        delay = 2
        while True:
            try:
                updates = await self.client.get_updates(self.offset)
                self.save(updates)
                delay = 2
                # Some intermediaries return immediately despite timeout=25.
                # A tiny pause bounds empty-request traffic without adding seconds.
                if not updates:
                    await asyncio.sleep(0.2)
            except ProviderError as error:
                print(f'{error} Повтор getUpdates через {delay} сек.', flush=True)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
