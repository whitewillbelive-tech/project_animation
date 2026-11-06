"""Versioned, file-based Site/workstation boundary. Never executes tools."""
from __future__ import annotations
import base64
import binascii
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path, PurePosixPath
from jsonschema import Draft202012Validator, FormatChecker

SCHEMA_VERSION = '1.0'
BUILD_ID = 'animation-site-mvp-v1'
MAX_JSON_BYTES = 10 * 1024 * 1024
MAX_FILE_BYTES = 3 * 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
SAFE_MEDIA = {'image/png', 'image/jpeg', 'image/webp', 'audio/wav', 'audio/mpeg', 'video/mp4'}
SCHEMA_PATH = Path(__file__).resolve().parent.parent / 'workstation-handoff' / 'package.schema.json'
SCHEMA = json.loads(SCHEMA_PATH.read_text())
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())
SECRET_KEYS = {'apikey', 'apitoken', 'accesstoken', 'refreshtoken', 'sessiontoken', 'sessionid', 'password', 'authorization', 'secret', 'privatekey', 'credential', 'credentials'}
SECRET_VALUE = re.compile(r'(?:\bsk-[A-Za-z0-9_-]{16,}|\bgh[pousr]_[A-Za-z0-9]{20,}|\bBearer\s+\S{8,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)', re.I)

class ContractError(ValueError):
    pass

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)

def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()

def relative_path(value):
    if (not isinstance(value, str) or not value or len(value) > 240 or '\\' in value
            or '\x00' in value or re.match(r'^[a-zA-Z]:', value) or '%' in value
            or value.startswith('/') or '://' in value):
        raise ContractError('파일 경로는 안전한 상대 경로여야 합니다.')
    parts = value.split('/')
    if any(part in ('', '.', '..') or part.startswith('.') for part in parts):
        raise ContractError('파일 경로 이탈 또는 숨김 경로는 허용하지 않습니다.')
    return str(PurePosixPath(value))

def _sensitive(value, depth=0):
    if depth > 25:
        raise ContractError('패키지 중첩 깊이를 초과했습니다.')
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = re.sub(r'[^a-z0-9]', '', str(key).lower())
            if normalized in SECRET_KEYS or normalized.endswith(('apikey', 'accesstoken', 'sessiontoken', 'privatekey')):
                raise ContractError('비밀키·인증 정보 필드는 패키지에 포함할 수 없습니다.')
            if key.endswith('_path') or key == 'path':
                relative_path(item)
            _sensitive(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _sensitive(item, depth + 1)
    elif isinstance(value, str):
        if SECRET_VALUE.search(value):
            raise ContractError('인증 정보로 보이는 값은 패키지에 포함할 수 없습니다.')
        # Prompt/config values must not leak local filesystem locations either.
        if re.search(r'(?:^|\s)(?:[A-Za-z]:[\\/]|/(?:home|Users|workspace|tmp|var|etc)/)', value):
            raise ContractError('로컬 절대 경로는 패키지에 포함할 수 없습니다.')

def _check_magic(data, mime):
    valid = {
        'image/png': data.startswith(b'\x89PNG\r\n\x1a\n'),
        'image/jpeg': data.startswith(b'\xff\xd8\xff'),
        'image/webp': data.startswith(b'RIFF') and data[8:12] == b'WEBP',
        'audio/wav': data.startswith(b'RIFF') and data[8:12] == b'WAVE',
        'audio/mpeg': data.startswith(b'ID3') or (len(data) > 2 and data[0] == 0xff and data[1] & 0xe0 == 0xe0),
        'video/mp4': len(data) >= 12 and data[4:8] == b'ftyp',
    }
    if not valid.get(mime):
        raise ContractError('첨부 파일의 실제 형식과 MIME 유형이 일치하지 않습니다.')

def validate_package(package, expected_type=None):
    """Validate without side effects; return decoded safe media keyed by path."""
    try:
        size = len(canonical(package).encode())
    except (TypeError, ValueError, RecursionError):
        raise ContractError('유효한 JSON 패키지가 아닙니다.')
    if size > MAX_JSON_BYTES:
        raise ContractError('패키지는 10 MiB 이하여야 합니다.')
    if not isinstance(package, dict) or package.get('schema_version') != SCHEMA_VERSION:
        raise ContractError('지원하지 않는 스키마 버전입니다. 현재 버전은 1.0입니다.')
    _sensitive(package)
    errors = list(VALIDATOR.iter_errors(package))
    if errors:
        # Avoid returning possibly sensitive payload fragments in jsonschema errors.
        detail = errors[0]
        if detail.context:
            variant = 0 if package.get('package_type') == 'work_request' else 1
            matching = [e for e in detail.context if e.schema_path and e.schema_path[0] == variant]
            if matching:
                detail = matching[0]
        field = '.'.join(str(p) for p in detail.absolute_path) or 'package'
        raise ContractError(f'패키지 스키마 검증 실패: {field} ({detail.validator}).')
    if expected_type and package['package_type'] != expected_type:
        raise ContractError('결과 가져오기는 work_result 패키지만 허용합니다.')
    assets = package.get('input_assets', []) if package['package_type'] == 'work_request' else package['output_assets']
    used_assets = package.get('used_inputs', {}).get('input_assets', [])
    for group in (assets, used_assets):
        ids = [a['asset_id'] for a in group]
        if len(ids) != len(set(ids)):
            raise ContractError('패키지 안에 중복 에셋 ID가 있습니다.')
    decoded, total = {}, 0
    for item in package.get('files', []):
        path = relative_path(item['path'])
        if path in decoded:
            raise ContractError('첨부 파일 경로가 중복됩니다.')
        try:
            content = base64.b64decode(item['content_base64'], validate=True)
        except (binascii.Error, ValueError):
            raise ContractError('첨부 파일의 Base64 형식이 올바르지 않습니다.')
        total += len(content)
        if len(content) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            raise ContractError('검토용 파일은 개별 3 MiB, 전체 8 MiB 이하여야 합니다.')
        if hashlib.sha256(content).hexdigest() != item['sha256']:
            raise ContractError('첨부 파일의 SHA-256 체크섬이 일치하지 않습니다.')
        _check_magic(content, item['mime_type'])
        decoded[path] = content
    referenced = set()
    for asset in assets:
        if asset.get('file_path'):
            path = relative_path(asset['file_path'])
            referenced.add(path)
            file = next((f for f in package.get('files', []) if f['path'] == path), None)
            if file is None:
                raise ContractError('에셋이 참조한 첨부 파일이 누락되었습니다.')
            if file['sha256'] != asset['sha256'] or file['mime_type'] != asset['mime_type']:
                raise ContractError('에셋과 첨부 파일의 해시 또는 유형이 다릅니다.')
    if set(decoded) != referenced:
        raise ContractError('에셋에 연결되지 않은 첨부 파일이 있습니다.')
    if package['package_type'] == 'work_result':
        execution = package['execution']
        if execution['finished_at'] and datetime.fromisoformat(execution['finished_at'].replace('Z','+00:00')) < datetime.fromisoformat(execution['started_at'].replace('Z','+00:00')):
            raise ContractError('실행 종료 시각은 시작 시각보다 빠를 수 없습니다.')
        if package['job_state'] in ('succeeded', 'failed', 'cancelled', 'interrupted') and not execution['finished_at']:
            raise ContractError('종료된 실행에는 finished_at이 필요합니다.')
        cost = package['cost']
        if cost['kind'] == 'unknown' and (cost['amount'] is not None or cost['currency'] is not None):
            raise ContractError('미상 비용에는 금액·통화를 기록할 수 없습니다.')
        if cost['kind'] != 'unknown' and (cost['amount'] is None or cost['currency'] is None):
            raise ContractError('실제·추정 비용에는 금액과 통화가 필요합니다.')
    return decoded
