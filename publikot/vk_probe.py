"""Read-only publishing capability probe; never calls wall.post or uploads files."""
import asyncio
from getpass import getpass

import aiohttp


METHODS = ('groups.getTokenPermissions', 'account.getAppPermissions', 'photos.getWallUploadServer')


async def probe(token, group, session):
    async def call(method):
        params = {'group_id': group} if method == 'photos.getWallUploadServer' else {}
        try:
            async with session.post('https://api.vk.com/method/' + method,
                data={**params, 'access_token': token, 'v': '5.199'},
                timeout=aiohttp.ClientTimeout(total=20), allow_redirects=False) as response:
                if response.status != 200:
                    return {'method': method, 'error': f'HTTP {response.status}'}
                result = await response.json()
                if 'error' in result:
                    code = result['error'].get('error_code')
                    return {'method': method, 'error': f'API {code}' if isinstance(code, int) else 'ошибка API'}
                value = result.get('response')
                if method == 'photos.getWallUploadServer':
                    return {'method': method, 'ok': isinstance(value, dict) and bool(value.get('upload_url'))}
                mask = value.get('mask') if isinstance(value, dict) else value
                if not isinstance(mask, int):
                    return {'method': method, 'error': 'неожиданный формат ответа'}
                return {'method': method, 'ok': True, 'wall': bool(mask & 8192), 'photos': bool(mask & 4)}
        except (aiohttp.ClientError, TimeoutError, ValueError, TypeError):
            return {'method': method, 'error': 'сетевая ошибка или некорректный ответ'}
    return await asyncio.gather(*(call(method) for method in METHODS))


def report(results):
    lines = ['Диагностика API ВК (без публикаций и загрузки файлов):']
    names = {'groups.getTokenPermissions': 'Права ключа сообщества',
             'account.getAppPermissions': 'Права пользовательского ключа',
             'photos.getWallUploadServer': 'Получение адреса загрузки фото на стену'}
    for row in results:
        if row.get('error'):
            status = row['error']
        elif row['method'] == 'photos.getWallUploadServer':
            status = 'доступно' if row.get('ok') else 'не подтверждено'
        else:
            status = f"wall={'да' if row.get('wall') else 'нет'}, photos={'да' if row.get('photos') else 'нет'}"
        lines.append(f"{names[row['method']]}: {status}")
    lines.append('Текстовая публикация, загрузка изображения и публикация фото НЕ выполнялись и пока не подтверждены.')
    lines.append('API 27: метод недоступен для ключа сообщества; API 1051: метод недоступен для текущего типа профиля.')
    return '\n'.join(lines)


async def run():
    value = input('Числовой ID сообщества для публикаций: ').strip()
    if not value.isdecimal() or int(value) <= 0:
        raise ValueError
    token = getpass('Ключ этого сообщества/пользователя (ввод скрыт, в файлы не записывается): ').strip()
    if not token:
        raise ValueError
    async with aiohttp.ClientSession(trust_env=False) as session:
        print(report(await probe(token, int(value), session)))


if __name__ == '__main__':
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    except Exception:
        print('Проверка не завершена. Проверьте числовой ID и соединение. Секреты не выводятся.')
        raise SystemExit(1) from None
