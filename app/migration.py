"""Explicit user-file imports. Never reads or writes a remote Notion page."""
import csv
import hashlib
import io
import json
import re
from contextlib import nullcontext
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .store import ApiError

TYPES = {'document', 'member', 'task', 'meeting', 'decision', 'budget', 'character',
         'episode', 'scene', 'shot', 'tool', 'manual', 'technical_test', 'asset',
         'run', 'review', 'reference'}
MAX_BYTES = 2 * 1024 * 1024


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


def source_url(value):
    value = str(value or '').strip()
    if not value:
        return ''
    parsed = urlsplit(value)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ApiError(400, '원본 링크는 인증정보가 없는 HTTPS 주소여야 합니다.')
    if len(value) > 2000:
        raise ApiError(400, '원본 링크가 너무 깁니다.')
    return value


def iso_date(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if dt.tzinfo is None:
            raise ValueError()
        return dt.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')
    except (ValueError, TypeError):
        raise ApiError(400, '원본 수정 시각은 시간대가 있는 ISO 8601 값이어야 합니다.')


def parse(payload):
    content = payload.get('content')
    if not isinstance(content, str) or not content.strip():
        raise ApiError(400, '가져올 파일 내용이 비어 있습니다.')
    if len(content.encode()) > MAX_BYTES:
        raise ApiError(413, '이관 파일은 2MB 이하만 지원합니다.')
    filename = str(payload.get('filename') or 'import.md')
    if len(filename) > 240 or '/' in filename or '\\' in filename:
        raise ApiError(400, '파일명에는 경로를 사용할 수 없습니다.')
    url = source_url(payload.get('source_url'))
    modified = iso_date(payload.get('source_modified_at'))
    fmt = payload.get('format')
    if fmt == 'markdown':
        match = re.search(r'^#\s+(.+)$', content, re.M)
        title = match.group(1).strip() if match else filename.rsplit('.', 1)[0]
        rows = [{'title': title, 'content': content, 'url': url, 'modified_at': modified}]
    elif fmt == 'csv':
        try:
            reader = csv.DictReader(io.StringIO(content.lstrip('\ufeff')))
            if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)):
                raise ApiError(400, 'CSV 헤더가 없거나 중복되어 있습니다.')
            rows = list(reader)
        except csv.Error as exc:
            raise ApiError(400, 'CSV 형식이 올바르지 않습니다.') from exc
        if not rows or len(rows) > 500:
            raise ApiError(400, 'CSV는 1~500개 행을 지원합니다.')
    else:
        raise ApiError(400, 'Markdown 또는 CSV 파일만 지원합니다.')
    items, seen = [], set()
    for number, row in enumerate(rows, 1):
        if fmt == 'csv' and (None in row or any(v is None for v in row.values())):
            raise ApiError(400, f'{number}행의 열 수가 헤더와 다릅니다.')
        def val(*keys, default=''):
            return next((row[k] for k in keys if row.get(k)), default)
        item_url = source_url(val('url', '원본 링크', default=url))
        item_id = val('id', '원본 ID')
        warnings = []
        if not item_id:
            notion_id = re.search(r'([0-9a-f]{32})(?:\?|$)', item_url) if fmt == 'markdown' else None
            item_id = notion_id.group(1) if notion_id else f'file:{filename}:row:{number}'
            warnings.append('원본 ID 미제공: 링크 또는 파일명·행 번호 기반 식별자입니다.')
        if len(item_id) > 300 or item_id in seen:
            raise ApiError(400, '원본 ID가 중복되거나 너무 깁니다.')
        seen.add(item_id)
        title = str(val('title', '제목', 'Name', '이름')).strip()
        if not title or len(title) > 300:
            raise ApiError(400, f'{number}행의 제목은 1~300자여야 합니다.')
        kind = val('type', '유형', default='document')
        if kind not in TYPES:
            warnings.append(f'유형 {kind}은 미지원 값이므로 문서로 변환했습니다.')
            kind = 'document'
        category = val('category', '카테고리', default='A')
        if category not in 'ABCDEF' or len(category) != 1:
            raise ApiError(400, f'{number}행의 카테고리는 A~F여야 합니다.')
        item_modified = iso_date(val('modified_at', '원본 수정 시각', default=modified))
        if not item_modified:
            warnings.append('원본 수정 시각을 확인하지 못했습니다.')
        text = val('content', '내용', '본문')
        if len(text) > 100000:
            raise ApiError(400, f'{number}행의 본문은 100,000자 이하여야 합니다.')
        items.append({'source_id': item_id, 'title': title, 'type': kind,
                      'category': category, 'content': text,
                      'url': item_url, 'modified_at': item_modified, 'warnings': warnings})
    return items


def preview(store, project_id, payload):
    parsed = parse(payload)
    state = store.state(project_id)
    existing = {r['id']: r for r in state['records']}
    receipts = {}
    for receipt in state.get('imports', []):
        if receipt.get('kind') == 'notion':
            receipts.update({r['source_id']: r for r in receipt.get('items', [])})
    output = []
    for item in parsed:
        item = dict(item)
        item['record_id'] = 'notion_' + digest([project_id, item['source_id']])[:32]
        item['content_hash'] = digest({k: v for k, v in item.items()
                                       if k not in ('warnings', 'record_id')})
        receipt = receipts.get(item['source_id'])
        record = existing.get(item['record_id'])
        item['action'] = 'create'
        item['current_revision'] = record.get('revision') if record else None
        if record:
            if (receipt and receipt.get('content_hash') == item['content_hash']
                    and receipt.get('revision') == record['revision']):
                item['action'] = 'unchanged'
            else:
                item['action'] = 'conflict'
                item['warnings'] = [*item['warnings'], '원본 변경 또는 사용자 편집이 있습니다. 자동 덮어쓰기를 하지 않습니다.']
        output.append(item)
    actions = {x['action'] for x in output}
    status = 'conflict' if 'conflict' in actions else ('duplicate' if actions == {'unchanged'} else 'ready')
    return {'status': status, 'items': output,
            'fingerprint': digest({'project': project_id, 'items': output}),
            'warnings': ['파일에서 제공된 정보만 이관하며 원본 Notion의 전체 구조·관계와 대조하지 않았습니다.']}


def import_file(store, project_id, payload, actor):
    with store.lock:
        result = preview(store, project_id, payload)
        if payload.get('fingerprint') != result['fingerprint']:
            raise ApiError(409, '미리보기 후 데이터가 변경되었습니다. 다시 확인하세요.')
        if result['status'] == 'conflict':
            raise ApiError(409, '이관 충돌을 먼저 해결해야 합니다.', result)
        if result['status'] == 'duplicate':
            return {'created': 0, 'unchanged': len(result['items']), 'status': 'duplicate'}
        receipt_id = 'migration_' + digest([project_id, result['fingerprint']])[:32]
        timestamp = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
        created, receipts = 0, []
        with store.atomic() if hasattr(store, 'atomic') else nullcontext():
            for item in result['items']:
                if item['action'] == 'create':
                    record = store.create_record(project_id, {
                        'title': item['title'], 'type': item['type'],
                        'category': item['category'], 'content': item['content'],
                        'fields': {'fact_kind': '미정'},
                        'source': {'url': item['url'], 'id': item['source_id'],
                                   'read_at': timestamp, 'modified_at': item['modified_at'],
                                   'warnings': item['warnings']},
                    }, actor, record_id=item['record_id'])
                    item['current_revision'] = record['revision']
                    created += 1
                receipts.append({'source_id': item['source_id'], 'record_id': item['record_id'],
                                 'content_hash': item['content_hash'], 'revision': item['current_revision']})
            receipt = {'id': receipt_id, 'kind': 'notion', 'created_at': timestamp,
                       'filename': payload.get('filename'), 'items': receipts}
            store.db.execute('INSERT INTO imports(id,project_id,data) VALUES(?,?,?)',
                             (receipt_id, project_id, json.dumps(receipt, ensure_ascii=False)))
            store.event(project_id, 'migration_imported', None, actor,
                        '사용자가 선택한 파일 이관', {'created': created, 'receipt_id': receipt_id})
        return {'created': created, 'unchanged': len(result['items']) - created, 'status': 'imported'}


def handle(handler, method, path, payload):
    if path not in ('/api/migration/preview', '/api/migration/import') or method != 'POST':
        return False
    if not isinstance(payload, dict):
        raise ApiError(400, 'JSON 객체가 필요합니다.')
    if path.endswith('/preview'):
        handler.send_json(200, preview(handler.store, handler.project_id, payload))
    else:
        handler.send_json(200, import_file(handler.store, handler.project_id, payload, handler.user['name']))
    return True
