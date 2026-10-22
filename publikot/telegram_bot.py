"""Translate private Telegram updates into the shared project workflow."""
import uuid

from .vk_bot import VkBot, HELP, ADMIN_HELP
from .menus import keyboard, back


class TelegramBot(VkBot):
    async def handle_update(self, update):
        number = update.get('update_id')
        if type(number) is not int or number < 0:
            raise ValueError('update_id')
        callback = update.get('callback_query')
        message = callback.get('message') if isinstance(callback, dict) else update.get('message')
        if not isinstance(message, dict):
            return
        user = callback.get('from') if callback else message.get('from')
        chat = message.get('chat', {})
        if (not isinstance(user, dict) or user.get('is_bot') or type(user.get('id')) is not int
                or user['id'] <= 0 or chat.get('type') != 'private' or chat.get('id') != user['id']):
            return
        actor = user['id']
        if callback:
            payload = self.client.callback_payload(actor, callback.get('data', ''))
            event = {'type': 'message_event', 'group_id': self.settings.group_id, 'object': {
                'user_id': actor, 'peer_id': actor, 'event_id': callback['id'], 'payload': payload}}
        else:
            text = message.get('text') or message.get('caption') or ''
            command, space, args = text.partition(' ')
            if command.startswith('/') and '@' in command:
                command, username = command.split('@', 1)
                if username.lower() != 'publikot_bot':
                    return
                text = command + space + args
            # This is an internal workflow event, never a VK API call.
            event = {'type': 'message_new', 'group_id': self.settings.group_id,
                     'event_id': f'telegram:{number}', 'object': {'message': {
                         'from_id': actor, 'peer_id': actor, 'id': message['message_id'],
                         'text': text, 'telegram_photo': message.get('photo', []),
                         'attachments': [{'type': 'photo'}] if message.get('photo') else []}}}
        await self.handle(event)

    async def photo(self, message, directory):
        photos = message.get('telegram_photo', [])
        if not photos:
            return None
        largest = max(photos, key=lambda photo: photo.get('width', 0) * photo.get('height', 0))
        folder = self.settings.data_dir / directory
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = folder / (uuid.uuid4().hex + '.jpg')
        await self.client.download_photo(largest['file_id'], path)
        return str(path)

    async def command(self, actor, peer, command, args, message):
        if command == '/help':
            text = HELP.replace('ваш ID ВК', 'ваш ID Telegram')
            return await self.client.send(peer, text + (ADMIN_HELP if actor == self.settings.owner_id else ''),
                                          keyboard=keyboard(back()))
        return await super().command(actor, peer, command, args, message)
