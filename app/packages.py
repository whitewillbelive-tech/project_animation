"""Authenticated HTTP adapter and domain services for contract v1 packages."""
from __future__ import annotations
import hashlib
import json
import re
import io
from pathlib import Path
import zipfile
from datetime import datetime, timezone
from uuid import uuid4
from .contracts import BUILD_ID, ContractError, canonical, fingerprint, validate_package


def utcnow():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

def error(status, message, details=None):
    from .store import ApiError
    raise ApiError(status, message, details or {})

def _imports(store, project_id):
    return [json.loads(row[0]) for row in store.db.execute('SELECT data FROM imports WHERE project_id = ?', (project_id,))]

def _find(store, project_id, package_id):
    row = store.db.execute('SELECT data FROM imports WHERE project_id = ? AND id = ?', (project_id, package_id)).fetchone()
    return json.loads(row[0]) if row else None

def _task(store, project_id, task_id):
    if not isinstance(task_id,str): error(422,'유효한 작업 ID가 필요합니다.')
    task = store.get_record(project_id, task_id)
    if task['type'] != 'task' or task.get('archived'):
        error(422, '활성 작업 기록을 선택해 주세요.')
    return task

def export_request(store, project_id, task_id, actor):
    with store.atomic():
        task = _task(store, project_id, task_id)
        approval=task.get('fields',{}).get('instruction_approval')
        if not isinstance(approval,dict) or approval.get('source_revision')!=task['revision']:
            error(422, '현재 기준 revision의 지시 승인을 받은 작업만 내보낼 수 있습니다. 결과 승인은 별도입니다.')
        for field, label in [('content', '승인된 지시문'), ('output_spec', '출력 규격'), ('acceptance', '완료 기준')]:
            if not str(task.get(field, '')).strip():
                error(422, f'{label}을 작업에 입력하고 다시 승인해 주세요.')
        records = {r['id']:r for r in store.state(project_id)['records']}
        input_assets=[]
        for asset_id in task.get('related_ids', []):
            asset=records.get(asset_id)
            if not asset or asset.get('type') != 'asset' or asset.get('archived'):
                continue
            fields=asset.get('fields',{})
            sha=fields.get('sha256','')
            if not isinstance(sha,str) or not re.fullmatch('[a-f0-9]{64}',sha):
                error(422, f'입력 에셋 「{asset["title"]}」의 SHA-256이 필요합니다.')
            input_assets.append({'asset_id':asset_id,'revision':asset['revision'],'sha256':sha,'title':asset['title'],'mime_type':fields.get('mime_type','application/octet-stream')})
        target={'task_id':task['id']}
        for key in ('episode_id','scene_id','shot_id'):
            value=task.get('fields',{}).get(key)
            expected=key[:-3]
            if value is None:
                value=next((rid for rid in task.get('related_ids',[]) if records.get(rid,{}).get('type')==expected),None)
            if value is not None and (not isinstance(value,str) or records.get(value,{}).get('type') != expected):
                error(422, f'{key} 관계가 올바르지 않습니다.')
            target[key]=value
        p={'schema_version':'1.0','package_id':'pkg-'+uuid4().hex,'package_type':'work_request','project_id':project_id,'created_at':utcnow(),'producer':{'name':'animation-site','version':BUILD_ID,'is_fixture':bool(task.get('is_sample'))},'target':target,'source_revision':task['revision'],'input_assets':input_assets,'approved_instruction':{'text':task['content'],'settings':task.get('fields',{}).get('settings',{}),'approved_by':approval['actor'],'approved_at':approval['at']},'output_spec':task['output_spec'],'acceptance':task['acceptance'],'files':[]}
        try:
            validate_package(p)
        except ContractError as exc:
            error(422, str(exc))
        saved={'id':p['package_id'],'kind':'request','package':p,'fingerprint':fingerprint(p),'created_at':utcnow()}
        store.db.execute('INSERT INTO imports(id,project_id,data) VALUES(?,?,?)',(p['package_id'],project_id,canonical(saved)))
        store.event(project_id,'package.export',task['id'],actor,'승인된 작업 패키지 내보내기',{'package_id':p['package_id']})
        return p

def preview_result(store, project_id, package):
    try:
        validate_package(package,'work_result')
    except ContractError as exc:
        error(422,str(exc))
    if package['project_id']!=project_id:
        error(403,'현재 프로젝트에 속한 패키지만 가져올 수 있습니다.')
    digest=fingerprint(package)
    out={'status':'ready','fingerprint':digest,'warnings':list(package['warnings']),'reasons':[],'summary':{'job_state':package['job_state'],'outputs':len(package['output_assets']),'task_title':'','is_sample':bool(package['producer'].get('is_fixture')),'run_id':package['run_id'],'attempt_id':package['attempt_id']}}
    with store.lock:
        existing=_find(store,project_id,package['package_id'])
        if not existing and store.db.execute('SELECT 1 FROM imports WHERE id=?',(package['package_id'],)).fetchone():
            out['status']='conflict'; out['reasons'].append('패키지 ID가 이미 사용되었습니다.'); return out
        if existing:
            out['status']='duplicate' if existing.get('fingerprint')==digest else 'conflict'
            out['reasons'].append('이미 가져온 동일한 패키지입니다.' if out['status']=='duplicate' else '동일 package_id의 내용이 기존 기록과 다릅니다.')
            return out
        request_entry=_find(store,project_id,package['request_package_id'])
        if not request_entry or request_entry.get('kind')!='request':
            out['status']='conflict'; out['reasons'].append('대응하는 Site 내보내기 요청을 찾을 수 없습니다.'); return out
        req=request_entry['package']
        task=_task(store,project_id,req['target']['task_id'])
        out['summary']['task_title']=task['title']
        if package['target']!=req['target']:
            out['status']='conflict'; out['reasons'].append('요청과 결과의 대상 작업·숏 관계가 다릅니다.')
        if package['used_inputs']['source_revision']!=req['source_revision'] or task['revision']!=req['source_revision']:
            if out['status']=='ready': out['status']='stale'
            out['reasons'].append('작업 기준 revision이 달라졌습니다. 오래된 입력은 자동 반영하지 않습니다.')
        if package['used_inputs']['input_assets']!=req['input_assets']:
            out['status']='conflict'; out['reasons'].append('실제로 사용한 입력 에셋·revision·체크섬이 요청과 다릅니다.')
        records={r['id']:r for r in store.state(project_id)['records']}
        for asset in req['input_assets']:
            current=records.get(asset['asset_id'])
            if not current or current.get('archived') or current['revision']!=asset['revision'] or current.get('fields',{}).get('sha256')!=asset['sha256']:
                if out['status']=='ready': out['status']='stale'
                out['reasons'].append('입력 에셋 버전이 변경되었거나 보관되었습니다.')
        for entry in _imports(store,project_id):
            prev=entry.get('package',{})
            if prev.get('package_type')=='work_result' and prev.get('run_id')==package['run_id'] and prev.get('attempt_id')==package['attempt_id']:
                out['status']='conflict'; out['reasons'].append('동일 run_id/attempt_id가 다른 패키지에 이미 사용되었습니다.')
        for asset in package['output_assets']:
            if asset['asset_id']==_run_record_id(package) or store.db.execute('SELECT 1 FROM records WHERE id=?',(asset['asset_id'],)).fetchone():
                out['status']='conflict'; out['reasons'].append('출력 에셋 ID가 기존 기록과 충돌합니다. 덮어쓰지 않습니다.')
        if store.db.execute('SELECT 1 FROM records WHERE id=?',(_run_record_id(package),)).fetchone():
            out['status']='conflict'; out['reasons'].append('실행 ID가 기존 기록과 충돌합니다.')
        if any('file_path' not in asset for asset in package['output_assets']):
            out['warnings'].append('메타데이터만 포함된 에셋은 미디어가 저장되지 않아 미리보기가 표시되지 않습니다.')
        if out['summary']['is_sample']:
            out['warnings'].append('샘플 fixture입니다. 생성 기록·에셋에 샘플 배지를 표시하며 실데이터 집계에서 제외합니다.')
        out['warnings'].append('기술 실행 결과를 가져와도 사람의 검토·승인 상태는 변경되지 않습니다.')
    return out

def _run_record_id(package):
    return 'run_' + hashlib.sha256((package['run_id']+'\x00'+package['attempt_id']).encode()).hexdigest()[:40]

def _record(record_id,project_id,type_,title,task,package,actor,fields):
    now=utcnow()
    return {'id':record_id,'project_id':project_id,'type':type_,'title':title,'category':'E','stage_ids':list(task.get('stage_ids',[])),'status':'draft','owner':task.get('owner',''),'reviewer':task.get('reviewer',''),'due_date':'','content':'','purpose':'워크스테이션 결과 수신. 기술 결과이며 승인 전입니다.','input_version':str(package['used_inputs']['source_revision']),'prerequisites':[],'acceptance':'검토자의 별도 승인 필요','output_spec':'','related_ids':[task['id']],'checklist':[],'fields':fields,'review_state':'not_submitted','revision':1,'archived':False,'is_sample':bool(package['producer'].get('is_fixture')),'source':{},'input_revisions':{},'created_at':now,'updated_at':now,'created_by':actor}

def import_result(store,project_id,package,expected_fingerprint,actor):
    with store.lock:
        preview=preview_result(store,project_id,package)
        if expected_fingerprint!=preview['fingerprint']:
            error(409,'미리보기 이후 패키지 내용이 변경되었습니다. 다시 미리보기를 확인해 주세요.')
        if preview['status']=='duplicate':
            return {'status':'duplicate','created_ids':[],'warnings':preview['warnings']}
        if preview['status']!='ready':
            error(409,'충돌 또는 오래된 입력으로 가져오기를 중단했습니다.',preview)
        task=_task(store,project_id,package['target']['task_id'])
        run=_record(_run_record_id(package),project_id,'run',('샘플 · ' if preview['summary']['is_sample'] else '')+'실행 결과: '+task['title'][:270],task,package,actor,{'run_id':package['run_id'],'attempt_id':package['attempt_id'],'package_id':package['package_id'],'request_package_id':package['request_package_id'],'job_state':package['job_state'],'execution':package['execution'],'cost':package['cost'],'errors':package['errors'],'warnings':package['warnings']})
        records=[run]
        for asset in package['output_assets']:
            f={'sha256':asset['sha256'],'mime_type':asset['mime_type'],'source_revision':asset['revision'],'metadata_only':not bool(asset.get('file_path')),'package_id':package['package_id'],'run_id':package['run_id']}
            if asset.get('file_path'):
                f.update(media_url='/api/media/'+asset['asset_id'],media_path=asset['file_path'])
            r=_record(asset['asset_id'],project_id,'asset',asset['title'],task,package,actor,f)
            r['related_ids'].append(run['id']); records.append(r)
        run['related_ids'] += [r['id'] for r in records[1:]]
        saved={'id':package['package_id'],'kind':'result','package':package,'fingerprint':preview['fingerprint'],'created_at':utcnow(),'created_ids':[r['id'] for r in records]}
        event={'id':'event-'+uuid4().hex,'kind':'package.import','record_id':task['id'],'actor':actor,'reason':'워크스테이션 결과 가져오기 (승인 상태 유지)','created_at':utcnow(),'details':{'package_id':package['package_id'],'created_ids':saved['created_ids']}}
        validation_index={r['id']:r for r in store.state(project_id)['records']}
        validation_index.update({r['id']:r for r in records})
        for r in records:
            store.validate_record(project_id,r,validation_index)
        try:
            store.db.execute('BEGIN IMMEDIATE')
            for r in records:
                store.db.execute('INSERT INTO records(id,project_id,data) VALUES(?,?,?)',(r['id'],project_id,canonical(r)))
            store.db.execute('INSERT INTO imports(id,project_id,data) VALUES(?,?,?)',(package['package_id'],project_id,canonical(saved)))
            store.db.execute('INSERT INTO events(id,project_id,data) VALUES(?,?,?)',(event['id'],project_id,canonical(event)))
            store.db.commit()
        except Exception:
            store.db.rollback(); raise
        return {'status':'imported','created_ids':saved['created_ids'],'warnings':preview['warnings']}

def handle(handler,method,path,payload):
    if path=='/api/handoff' and method=='GET':
        root=Path(__file__).resolve().parent.parent/'workstation-handoff'
        buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,'w',compression=zipfile.ZIP_DEFLATED) as archive:
            for item in sorted(root.rglob('*')):
                if item.is_file() and not item.is_symlink() and item.suffix in ('.json','.md') and item.resolve().is_relative_to(root.resolve()):
                    archive.writestr('workstation-handoff/'+item.relative_to(root).as_posix(),item.read_bytes())
        content=buffer.getvalue()
        handler.send_response(200); handler.send_header('Content-Type','application/zip'); handler.send_header('Content-Disposition','attachment; filename=workstation-handoff-v1.zip'); handler.send_header('Content-Length',str(len(content))); handler.send_header('Cache-Control','no-store'); handler.send_header('X-Content-Type-Options','nosniff'); handler.end_headers(); handler.wfile.write(content)
        return True
    if path.startswith('/api/media/') and method=='GET':
        asset_id=path.rsplit('/',1)[-1]
        with handler.store.lock:
            record=handler.store.get_record(handler.project_id,asset_id)
            if record['type']!='asset' or record.get('archived'):
                error(404,'미디어를 찾을 수 없습니다.')
            entry=_find(handler.store,handler.project_id,record.get('fields',{}).get('package_id'))
            package=(entry or {}).get('package',{})
            media=next((f for f in package.get('files',[]) if f['path']==record.get('fields',{}).get('media_path')),None)
            if not media: error(404,'이 에셋에는 저장된 검토용 미디어가 없습니다.')
            # Validation on read protects restored/untrusted databases too.
            decoded=validate_package(package,'work_result'); content=decoded[media['path']]
        handler.send_response(200); handler.send_header('Content-Type',media['mime_type']); handler.send_header('Content-Length',str(len(content))); handler.send_header('Cache-Control','no-store'); handler.send_header('X-Content-Type-Options','nosniff'); handler.send_header('Content-Security-Policy',"default-src 'none'"); handler.end_headers(); handler.wfile.write(content)
        return True
    if not path.startswith('/api/packages/') or method!='POST': return False
    actor=handler.user['name']
    if path=='/api/packages/export':
        handler.send_json(200,export_request(handler.store,handler.project_id,payload.get('task_id',''),actor))
    elif path=='/api/packages/preview':
        handler.send_json(200,preview_result(handler.store,handler.project_id,payload.get('package')))
    elif path=='/api/packages/import':
        handler.send_json(200,import_result(handler.store,handler.project_id,payload.get('package'),payload.get('fingerprint'),actor))
    else: return False
    return True
