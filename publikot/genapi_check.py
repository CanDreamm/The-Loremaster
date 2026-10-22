"""Read-only server diagnostic: reconstruct payload metadata and GET an existing result. No POST."""
import argparse
import asyncio
import base64
import hashlib
import json
import sqlite3
from pathlib import Path

import aiohttp

from .genapi import GenAPI, build_text_payload, text_result, truncated
from .prompts import build_prompts
from .providers import ProviderError
from .settings import Settings, ConfigError


def shape(value, depth=0):
    """Only known field names, container sizes and string lengths; no text/URLs/credentials."""
    if isinstance(value, str):
        return {'type': 'str', 'chars': len(value)}
    if depth >= 5:
        return {'type': type(value).__name__}
    if isinstance(value, list):
        return {'type': 'list', 'items': len(value), 'first': [shape(v, depth + 1) for v in value[:2]]}
    if isinstance(value, dict):
        return {'type': 'dict', 'fields': {k: shape(value[k], depth + 1) for k in (
            'result', 'full_response', 'choices', 'message', 'role', 'content', 'text',
            'output', 'output_text', 'response', 'type', 'finish_reason') if k in value}}
    return {'type': type(value).__name__}


def payload_summary(context, model):
    project = context['project']
    prompts = build_prompts(project, context['brief'], context['materials'], context['reference'],
                            context['previous'], context['feedback'])
    payload = build_text_payload(model, prompts.text, prompts.materials)
    references = []
    image_blocks = [block for block in payload['messages'][1]['content'] if block['type'] == 'image_url']
    for material, block in zip(prompts.materials, image_blocks, strict=True):
        uri = block['image_url']['url']
        raw = Path(material['path']).read_bytes()
        references.append({'role': material['role'], 'bytes': len(raw),
                           'base64_matches_file': base64.b64decode(uri.split(',', 1)[1]) == raw})
    return {'model': model, 'method': 'POST (только описание; НЕ отправляется)',
            'path': '/api/v1/networks/' + model, 'is_sync': False,
            'max_tokens': payload['max_tokens'], 'reasoning_effort': payload['reasoning_effort'],
            'system_prompt_chars': len(prompts.text),
            'system_prompt_sha256': hashlib.sha256(prompts.text.encode()).hexdigest(),
            'settings_present': {key: bool(project.get(key)) for key in
                ('concept', 'tone', 'text_prompt', 'text_template', 'visual_style', 'image_prompt', 'image_template')},
            'brief_chars': len(context['brief']), 'previous_chars': len(context['previous']),
            'feedback_chars': len(context['feedback']), 'references': references}


async def check(settings, job_id=None):
    database = settings.database
    if not database.is_file():
        raise ProviderError('База выбранного BOT_TRANSPORT не найдена. Проверьте DATA_DIR.')
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        sql = ("SELECT r.*,j.generation_context FROM ai_requests r JOIN jobs j ON j.id=r.job_id "
               "WHERE r.kind='text'" + (' AND r.job_id=?' if job_id is not None else '') +
               ' ORDER BY r.job_id DESC,r.revision DESC LIMIT 1')
        row = db.execute(sql, (job_id,) if job_id is not None else ()).fetchone()
    if not row:
        raise ProviderError('Сохранённых текстовых запросов GenAPI не найдено.')
    report = {'transport': settings.transport, 'job': row['job_id'], 'revision': row['revision'],
              'request_id': row['request_id'], 'cached_status': row['status']}
    if row['generation_context']:
        try:
            report['outgoing'] = payload_summary(json.loads(row['generation_context']), row['model'])
        except (ProviderError, KeyError, ValueError, OSError):
            report['outgoing'] = {'error': 'Не удалось проверить сохранённые настройки или файлы референсов.'}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not row['request_id'] or not str(row['request_id']).isdecimal():
        raise ProviderError('ID не сохранён. Проверьте историю в кабинете. Новая генерация не отправляется.')
    if not settings.genapi_key:
        raise ProviderError('Для чтения результата добавьте GENAPI_API_KEY на сервере.')
    async with aiohttp.ClientSession(trust_env=True) as session:
        result = await GenAPI(settings, None).request(session, 'GET', '/request/get/' + row['request_id'])
    print(json.dumps({'remote_status': result.get('status') if result.get('status') in ('success', 'processing', 'error') else 'unknown',
        'extracted_text_chars': len(text_result(result)), 'truncated': truncated(result.get('full_response')),
        'response_shape': shape(result)}, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description='Проверка существующего GenAPI запроса без новой генерации и изменения базы.')
    parser.add_argument('--job', type=int, help='Номер задания; по умолчанию последний текстовый запрос.')
    args = parser.parse_args()
    try:
        asyncio.run(check(Settings.load(require_owner=False), args.job))
    except (ConfigError, ProviderError) as error:
        parser.exit(1, str(error) + '\n')
    except (sqlite3.Error, ValueError, OSError):
        parser.exit(1, 'Диагностика не выполнена. Проверьте базу и настройки; секреты не выводятся.\n')


if __name__ == '__main__':
    main()
