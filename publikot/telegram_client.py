"""Telegram transport through Chatbot Hopop; never calls Telegram directly."""
import os
from dataclasses import dataclass, field
from uuid import UUID, uuid4
from pathlib import Path
from urllib.parse import urlsplit
import json

import aiohttp

from .providers import ProviderError
from .settings import ConfigError


def telegram_error_reason(body):
    """Recognize error categories without exposing arbitrary upstream text."""
    fragments = []
    def collect(value, depth=0):
        if depth > 4 or len(fragments) > 50:
            return
        if isinstance(value, str):
            fragments.append(value[:1024].lower())
        elif isinstance(value, dict):
            for key in ('description', 'message', 'detail', 'details', 'error', 'errors', 'telegram_error', 'response', 'result', 'upstream', 'data', 'reason'):
                if key in value:
                    collect(value[key], depth+1)
        elif isinstance(value, list):
            for item in value[:20]:
                collect(item, depth+1)
    collect(body)
    text = ' '.join(fragments)
    for markers, reason in (
        (("message to delete not found", "message not found", "message_id_invalid"), 'not_found'),
        (("message can't be deleted", "message cannot be deleted", "message_delete_forbidden", "message is too old"), 'undeletable'),
        (("chat not found",), 'chat_not_found'),
        (("message identifier is not specified", "message_id is required", "message_id must"), 'invalid_message_id'),
        (("method_not_supported", "unsupported method", "method is not supported"), 'unsupported_method'),
        (("query is too old", "query id is invalid"), 'expired_callback'),
    ):
        if any(marker in text for marker in markers):
            return reason
    return 'unknown'


class TelegramApiError(ProviderError):
    def __init__(self, method, status, body=None):
        self.status = status
        code = body.get('error_code') if isinstance(body, dict) else None
        self.code = code if type(code) is int else status
        self.reason = telegram_error_reason(body)
        detail = {
            'not_found': 'Сообщение уже удалено или не найдено.',
            'undeletable': 'Telegram не разрешает удалять это сообщение (возраст или права).',
            'chat_not_found': 'Чат не найден: проверьте выбранного бота и получателя.',
            'invalid_message_id': 'Сервис отклонил идентификатор сообщения.',
            'unsupported_method': 'Сервис не поддерживает этот метод.',
            'expired_callback': 'Время ответа на кнопку истекло.',
        }.get(self.reason)
        if not detail:
            detail = ('Проверьте API-ключ, разрешённый IP и методы бота.' if self.code in (401, 403)
                      else 'Сервис отклонил запрос; причина не распознана, проверьте журнал запросов сервиса.')
        super().__init__(f'Сервис Telegram: {method} вернул HTTP {status} (код {self.code}). {detail}')


@dataclass(frozen=True)
class TelegramSettings:
    api_key: str = field(repr=False)
    bot_id: str

    @classmethod
    def load(cls):
        # The application loads .env.python before selecting a transport.
        key = os.getenv('CHATBOT_API_KEY', '').strip()
        bot = os.getenv('CHATBOT_BOT_ID', '').strip()
        if not key or '\r' in key or '\n' in key:
            raise ConfigError('Задайте CHATBOT_API_KEY сервиса Chatbot.')
        try:
            bot = str(UUID(bot))
        except ValueError:
            raise ConfigError('Задайте CHATBOT_BOT_ID: UUID @PubliKotBot из сервиса Chatbot.') from None
        return cls(key, bot)


class TelegramMessages:
    BASE_URL = 'https://chatbot.hopop.ru/api/v1'

    def __init__(self, settings, session):
        self.settings, self.session = settings, session

    async def _call(self, method, payload, request_id=None):
        headers = {'Authorization': 'Bearer ' + self.settings.api_key}
        if request_id is not None:
            headers['Idempotency-Key'] = str(request_id)
        url = f'{self.BASE_URL}/bots/{self.settings.bot_id}/methods/{method}'
        try:
            async with self.session.post(url, json=payload, headers=headers,
                                         timeout=aiohttp.ClientTimeout(total=35),
                                         allow_redirects=False) as response:
                if response.status != 200:
                    try:
                        error_body = await response.json()
                    except (aiohttp.ClientError, ValueError):
                        error_body = None
                    raise TelegramApiError(method, response.status, error_body)
                result = await response.json()
                if not isinstance(result, dict) or result.get('ok') is not True or 'result' not in result:
                    raise TelegramApiError(method, response.status, result)
                return result['result']
        except (aiohttp.ClientError, TimeoutError, ValueError):
            if method in ('getMe', 'getUpdates', 'deleteWebhook'):
                raise ProviderError(f'Ответ сервиса на {method} не получен. Проверьте связь и разрешения метода.') from None
            raise ProviderError('Ответ сервиса Telegram не получен. Отправка могла состояться; '
                                'при повторе используйте прежний Idempotency-Key.') from None

    async def get_me(self):
        result = await self._call('getMe', {})
        if not isinstance(result, dict) or result.get('username', '').lower() != 'publikotbot':
            raise ProviderError('В сервисе выбран другой бот: ожидается @PubliKotBot.')
        return result

    async def get_updates(self, offset=None, timeout=25):
        payload = {'timeout': timeout, 'limit': 100, 'allowed_updates': ['message', 'callback_query']}
        if offset is not None:
            payload['offset'] = offset
        result = await self._call('getUpdates', payload)
        if not isinstance(result, list) or any(
                not isinstance(item, dict) or type(item.get('update_id')) is not int
                or not 0 <= item['update_id'] < 2**63-1 for item in result):
            raise ProviderError('Сервис getUpdates вернул некорректный список событий.')
        return result

    async def delete_webhook(self):
        result = await self._call('deleteWebhook', {'drop_pending_updates': False})
        if result is not True:
            raise ProviderError('Сервис не подтвердил отключение webhook. Отключите его в панели сервиса.')

    async def send(self, chat_id, text, *, request_id):
        """Caller persists one UUID per logical send and reuses it on retries."""
        try:
            ident = str(UUID(str(request_id)))
        except ValueError:
            raise ProviderError('Для отправки нужен UUID запроса.') from None
        if not isinstance(text, str) or not 1 <= len(text) <= 4096:
            raise ProviderError('Сообщение Telegram должно содержать от 1 до 4096 символов.')
        if not isinstance(chat_id, (int, str)) or isinstance(chat_id, bool) or not str(chat_id).lstrip('-').isdecimal():
            raise ProviderError('Нужен числовой Chat ID Telegram.')
        result = await self._call('sendMessage', {'chat_id': str(chat_id), 'text': text}, ident)
        if not isinstance(result, dict) or type(result.get('message_id')) is not int:
            raise ProviderError('Сервис Telegram не вернул ID сообщения. При повторе сохраните UUID запроса.')
        return result['message_id']


class TelegramWorkflowMessages(TelegramMessages):
    """Shared workflow transport, with durable short callback tokens."""
    def bind_store(self, store):
        self.store = store
        store.db.execute('''CREATE TABLE IF NOT EXISTS telegram_buttons (
            token TEXT PRIMARY KEY, actor INTEGER NOT NULL, payload TEXT NOT NULL,
            created INTEGER NOT NULL DEFAULT (unixepoch()))''')

    def remember_message(self, peer, message_id):
        self.store.remember_bot_message(0, peer, message_id)

    def tracked_messages(self, peer):
        return self.store.bot_message_ids(0, peer)

    async def delete_messages(self, peer, message_ids):
        # Only our recorded messages in this chat may be deleted.
        known = set(self.tracked_messages(peer))
        first_error = None
        # Recent screens first: an old undeletable message must not block them.
        for ident in sorted({value for value in message_ids if type(value) is int and value in known}, reverse=True):
            try:
                result = await self._call('deleteMessage', {'chat_id': peer, 'message_id': ident}, uuid4())
                if result is not True:
                    raise ProviderError('Сервис не подтвердил удаление сообщения Telegram.')
            except TelegramApiError as error:
                if error.code == 400 and error.reason in ('not_found', 'undeletable'):
                    # Retire only a confirmed, permanent per-message rejection.
                    self.store.forget_bot_messages(0, peer, [ident])
                    print(f'Пропущено сообщение Telegram: {error}', flush=True)
                    continue
                if error.code == 400:
                    first_error = first_error or error
                    continue
                raise
            # Persist each success even if a later deletion fails.
            self.store.forget_bot_messages(0, peer, [ident])
        if first_error:
            raise first_error

    def callback_payload(self, actor, token):
        row = self.store.db.execute('SELECT payload FROM telegram_buttons WHERE token=? AND actor=?',
                                    (token, actor)).fetchone()
        return json.loads(row['payload']) if row else {}

    def markup(self, actor, keyboard):
        rows = []
        self.store.db.execute('DELETE FROM telegram_buttons WHERE created < unixepoch()-2592000')
        for row in keyboard.get('buttons', []):
            buttons = []
            for button in row:
                action = button['action']
                item = {'text': action['label']}
                if action['type'] == 'open_link':
                    item['url'] = action['link']
                else:
                    token = uuid4().hex
                    self.store.db.execute('INSERT INTO telegram_buttons(token,actor,payload) VALUES (?,?,?)',
                                          (token, actor, action['payload']))
                    item['callback_data'] = token
                buttons.append(item)
            rows.append(buttons)
        return {'inline_keyboard': rows}

    async def send(self, peer, text, keyboard=None, attachment=None):
        # UI wording only; VK publishing targets keep their own terminology.
        text = text.replace('ваш ID ВК', 'ваш ID Telegram').replace('Ваш ID ВК', 'Ваш ID Telegram')
        text = text.replace('ID пользователя ВК', 'ID пользователя Telegram')
        text = text.replace('тем аккаунтом ВК, из которого пишете боту', 'аккаунтом ВК с правами на сообщество')
        text = text.replace('тем аккаунтом, из которого пишете боту', 'аккаунтом с правами на сообщество')
        chunks = [text[i:i+3500] for i in range(0, len(text), 3500)] or [' ']
        result = []
        for index, chunk in enumerate(chunks):
            payload = {'chat_id': str(peer), 'text': chunk}
            if keyboard and index == len(chunks)-1:
                payload['reply_markup'] = self.markup(peer, keyboard)
            sent = await self._call('sendMessage', payload, uuid4())
            if not isinstance(sent, dict) or type(sent.get('message_id')) is not int:
                raise ProviderError('Сервис Telegram не вернул ID сообщения.')
            self.remember_message(peer, sent['message_id'])
            result.append(sent['message_id'])
        return result

    async def answer_event(self, actor, peer, event_id):
        return await self._call('answerCallbackQuery', {'callback_query_id': event_id}, uuid4())

    async def api(self, method, **params):
        # Resolving a VK community uses VK API, not the Telegram intermediary.
        if method != 'utils.resolveScreenName':
            raise ProviderError('Неподдерживаемый метод поиска сообщества.')
        if not self.vk_token:
            raise ProviderError('Для короткого адреса сообщества нужен VK_BOT_TOKEN. Укажите числовой ID или ссылку club123.')
        from .vk_client import VkMessages
        return await VkMessages(self.vk_token, 0, self.session).api(method, **params)

    async def download_photo(self, file_id, target):
        result = await self._call('getFile', {'file_id': file_id})
        path = result.get('file_path') if isinstance(result, dict) else None
        # A standard Telegram relative path cannot be fetched without BotFather's
        # token. Only accept a download URL hosted by the intermediary itself.
        url = urlsplit(path) if isinstance(path, str) else None
        if (not url or url.scheme != 'https' or url.hostname != 'chatbot.hopop.ru'
                or url.username or url.password or url.port not in (None, 443)):
            raise ProviderError('Сервис должен вернуть HTTPS-ссылку на файл на chatbot.hopop.ru. '
                                'Стандартного относительного file_path Telegram недостаточно; нужен API скачивания сервиса.')
        created = False
        try:
            async with self.session.get(path, headers={'Authorization': 'Bearer ' + self.settings.api_key},
                                        allow_redirects=False, timeout=aiohttp.ClientTimeout(total=60)) as response:
                if response.status != 200 or not response.headers.get('Content-Type', '').startswith('image/'):
                    raise ProviderError('Сервис не вернул фотографию.')
                size = 0
                with Path(target).open('xb') as output:
                    created = True
                    async for block in response.content.iter_chunked(65536):
                        size += len(block)
                        if size > 10 * 1024 * 1024:
                            raise ProviderError('Фото больше 10 МБ.')
                        output.write(block)
        except Exception:
            if created:
                Path(target).unlink(missing_ok=True)
            raise ProviderError('Не удалось получить фото через сервис Telegram.') from None

    async def send_photo(self, peer, path, caption=''):
        # Standard Bot API multipart request, forwarded by the intermediary.
        with Path(path).open('rb') as photo:
            form = aiohttp.FormData()
            form.add_field('chat_id', str(peer))
            form.add_field('caption', caption[:1024])
            form.add_field('photo', photo, filename='photo.jpg', content_type='image/jpeg')
            try:
                async with self.session.post(
                    f'{self.BASE_URL}/bots/{self.settings.bot_id}/methods/sendPhoto', data=form,
                    headers={'Authorization': 'Bearer ' + self.settings.api_key, 'Idempotency-Key': str(uuid4())},
                    timeout=aiohttp.ClientTimeout(total=60), allow_redirects=False) as response:
                    result = await response.json()
                    if response.status != 200 or not isinstance(result, dict) or result.get('ok') is not True:
                        raise ProviderError('Сервис не подтвердил отправку фото. Проверьте разрешение sendPhoto и multipart.')
            except (aiohttp.ClientError, TimeoutError, ValueError):
                raise ProviderError('Ответ на отправку фото не получен. Автоматического повтора нет.') from None
        sent = result.get('result')
        if not isinstance(sent, dict) or type(sent.get('message_id')) is not int or sent['message_id'] <= 0:
            raise ProviderError('Сервис Telegram не вернул ID фотографии.')
        self.remember_message(peer, sent['message_id'])
        return sent['message_id']
