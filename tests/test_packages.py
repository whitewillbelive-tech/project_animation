import copy
import base64
import io
import zipfile
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from app.contracts import ContractError, SCHEMA, VALIDATOR, fingerprint, relative_path, validate_package
from app.packages import export_request, import_result, preview_result, handle
from app.store import ApiError, Store

FIXTURES=Path(__file__).resolve().parent.parent/'workstation-handoff'/'fixtures'

def fixture(name='work_result.valid.json'):
    return json.loads((FIXTURES/name).read_text())

class ContractValidationTests(unittest.TestCase):
    def test_schema_and_valid_examples(self):
        VALIDATOR.check_schema(SCHEMA)
        for name in ('work_request.valid.json','work_result.valid.json','work_result.metadata-only.json'):
            validate_package(fixture(name))

    def test_all_invalid_fixtures(self):
        examples=list(FIXTURES.glob('*.invalid-*.json'))
        self.assertGreaterEqual(len(examples),6)
        for p in examples:
            with self.subTest(p=p.name), self.assertRaises(ContractError):
                validate_package(json.loads(p.read_text()))

    def test_relative_paths(self):
        for path in ('/etc/passwd','../x','a/../x','C:\\x','C:/x','a\\b','a//b','.hidden','a/%2e%2e/b','https://example.com/x'):
            with self.subTest(path=path),self.assertRaises(ContractError): relative_path(path)
        self.assertEqual(relative_path('review/preview.png'),'review/preview.png')

    def test_duplicate_paths_unsupported_media_and_secret_values(self):
        p=fixture(); p['files'].append(copy.deepcopy(p['files'][0]))
        with self.assertRaises(ContractError): validate_package(p)
        p=fixture(); p['files'][0]['mime_type']='image/svg+xml'
        with self.assertRaises(ContractError): validate_package(p)
        p=fixture(); p['execution']['prompt']='Bearer '+('a'*20)
        with self.assertRaises(ContractError): validate_package(p)
        p=fixture(); p['execution']['settings']={'directory':'/home/me/private'}
        with self.assertRaises(ContractError): validate_package(p)

    def test_oversize_and_media_signature_rejected(self):
        p=fixture(); p['execution']['settings']['oversized']='x'*(10*1024*1024)
        with self.assertRaises(ContractError): validate_package(p)
        p=fixture(); data=b'<html>not a png</html>'; sha=hashlib.sha256(data).hexdigest()
        p['files'][0].update(content_base64=base64.b64encode(data).decode(),sha256=sha)
        p['output_assets'][0]['sha256']=sha
        with self.assertRaises(ContractError): validate_package(p)

    def test_timestamp_cost_format_and_missing_references(self):
        for change in ({'created_at':'2026-11-06T09:00:00+09:00'},{'request_package_id':None},{'job_state':'approved'}):
            p=fixture(); p.update(change)
            with self.assertRaises(ContractError): validate_package(p)
        p=fixture(); p['cost']={'kind':'unknown','amount':3,'currency':'USD'}
        with self.assertRaises(ContractError): validate_package(p)

class PackageRoundtripTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.tmp.name)/'site.sqlite3')
        self.project=self.store.init_owner('fixture-owner','temporary test password for isolated fixture')
        self.pid=self.project['id']
        self.task=self.store.create_record(self.pid,{'type':'task','title':'샘플 계약 테스트','category':'D','content':'검증용 1픽셀 PNG를 반환한다.','reviewer':'fixture-owner','acceptance':'해시와 출력 규격 확인','output_spec':'PNG 1×1','input_version':'fixture-v1','is_sample':True},'fixture-owner')

    def tearDown(self):
        self.store.db.close(); self.tmp.cleanup()

    def approved_request(self):
        self.task=self.store.approve_instructions(self.pid,self.task['id'],{'revision':self.task['revision'],'reason':'실행 지시 승인. 결과 검토와 분리.'},'fixture-owner')
        self.assertEqual(self.task['review_state'],'not_submitted')
        self.assertNotEqual(self.task['status'],'done')
        return export_request(self.store,self.pid,self.task['id'],'fixture-owner')

    def result(self,req):
        p=fixture(); p.update(project_id=self.pid,request_package_id=req['package_id'],target=copy.deepcopy(req['target']),used_inputs={'source_revision':req['source_revision'],'input_assets':copy.deepcopy(req['input_assets'])})
        return p

    def test_unapproved_export_denied(self):
        with self.assertRaises(ApiError) as caught:
            export_request(self.store,self.pid,self.task['id'],'fixture-owner')
        self.assertEqual(caught.exception.status,422)
        self.assertEqual(len(self.store.state(self.pid)['imports']),0)

    def test_request_result_roundtrip_dedup_and_approval_separation(self):
        req=self.approved_request(); validate_package(req,'work_request')
        self.assertEqual(req['approved_instruction']['approved_by'],'fixture-owner')
        p=self.result(req)
        before=self.store.get_record(self.pid,self.task['id'])
        preview=preview_result(self.store,self.pid,p)
        self.assertEqual(preview['status'],'ready')
        self.assertEqual(len(self.store.state(self.pid)['records']),1,'preview has no mutations')
        result=import_result(self.store,self.pid,p,preview['fingerprint'],'fixture-owner')
        self.assertEqual(result['status'],'imported')
        self.assertEqual(self.store.get_record(self.pid,self.task['id']),before)
        imported=[self.store.get_record(self.pid,rid) for rid in result['created_ids']]
        self.assertTrue(all(r['review_state']=='not_submitted' for r in imported))
        self.assertTrue(all(r['is_sample'] for r in imported))
        self.assertTrue(next(r for r in imported if r['type']=='asset')['fields']['media_url'].startswith('/api/media/'))
        self.assertEqual(preview_result(self.store,self.pid,p)['status'],'duplicate')
        self.assertEqual(import_result(self.store,self.pid,p,fingerprint(p),'fixture-owner')['created_ids'],[])
        self.assertEqual(len(self.store.state(self.pid)['records']),3)
        backup=self.store.backup(self.pid)
        self.assertTrue(self.store.validate_backup(self.pid,backup)['valid'])
        self.store.restore(self.pid,backup,'fixture-owner','복원')
        self.assertEqual(preview_result(self.store,self.pid,p)['status'],'duplicate')

    def test_media_and_handoff_adapter_responses(self):
        req=self.approved_request(); p=self.result(req)
        import_result(self.store,self.pid,p,fingerprint(p),'fixture-owner')
        class Handler:
            def __init__(h):
                h.store=self.store; h.project_id=self.pid; h.user={'name':'fixture-owner'}; h.wfile=io.BytesIO(); h.headers={}
            def send_response(h,status): h.status=status
            def send_header(h,name,value): h.headers[name]=value
            def end_headers(h): pass
        h=Handler()
        self.assertTrue(handle(h,'GET','/api/media/'+p['output_assets'][0]['asset_id'],{}))
        self.assertEqual(h.headers['Content-Type'],'image/png')
        self.assertEqual(hashlib.sha256(h.wfile.getvalue()).hexdigest(),p['output_assets'][0]['sha256'])
        h=Handler(); self.assertTrue(handle(h,'GET','/api/handoff',{}))
        self.assertEqual(h.headers['Content-Type'],'application/zip')
        with zipfile.ZipFile(io.BytesIO(h.wfile.getvalue())) as archive:
            names=archive.namelist()
            self.assertIn('workstation-handoff/package.schema.json',names)
            self.assertIn('workstation-handoff/README.md',names)
            self.assertTrue(all(name.startswith('workstation-handoff/') for name in names))
            self.assertFalse(any('data/' in name or '.sqlite' in name for name in names))

    def test_final_review_does_not_replace_instruction_approval(self):
        r=self.task
        r=self.store.review_record(self.pid,r['id'],{'revision':r['revision'],'action':'submit','reason':'결과 검토'},'fixture-owner')
        r=self.store.review_record(self.pid,r['id'],{'revision':r['revision'],'action':'approve','reason':'결과 승인'},'fixture-owner')
        with self.assertRaises(ApiError): export_request(self.store,self.pid,r['id'],'fixture-owner')

    def test_changed_id_or_content_is_conflict(self):
        req=self.approved_request(); p=self.result(req)
        import_result(self.store,self.pid,p,fingerprint(p),'fixture-owner')
        changed=copy.deepcopy(p); changed['warnings'].append('다른 내용')
        self.assertEqual(preview_result(self.store,self.pid,changed)['status'],'conflict')
        changed=copy.deepcopy(p); changed['package_id']='another-package'
        self.assertEqual(preview_result(self.store,self.pid,changed)['status'],'conflict')

    def test_stale_revision_unknown_request_foreign_project(self):
        req=self.approved_request(); p=self.result(req)
        self.store.update_record(self.pid,self.task['id'],{'revision':self.task['revision'],'content':'변경된 입력'},'fixture-owner')
        preview=preview_result(self.store,self.pid,p)
        self.assertEqual(preview['status'],'stale')
        with self.assertRaises(ApiError): import_result(self.store,self.pid,p,fingerprint(p),'fixture-owner')
        p=self.result(req); p['request_package_id']='missing-request'
        self.assertEqual(preview_result(self.store,self.pid,p)['status'],'conflict')
        p=self.result(req); p['project_id']='another-project'
        with self.assertRaises(ApiError) as caught: preview_result(self.store,self.pid,p)
        self.assertEqual(caught.exception.status,403)

    def test_preview_fingerprint_and_metadata_only(self):
        req=self.approved_request(); p=self.result(req)
        p['files']=[]; p['output_assets'][0].pop('file_path')
        preview=preview_result(self.store,self.pid,p)
        self.assertEqual(preview['status'],'ready')
        self.assertTrue(any('미디어' in w for w in preview['warnings']))
        with self.assertRaises(ApiError): import_result(self.store,self.pid,p,'0'*64,'fixture-owner')
        self.assertEqual(len(self.store.state(self.pid)['records']),1)

    def test_failed_job_remains_unapproved_and_cost_distinct(self):
        req=self.approved_request(); p=self.result(req)
        p.update(job_state='failed',output_assets=[],files=[],errors=['fixture failure'],cost={'kind':'unknown','amount':None,'currency':None})
        r=import_result(self.store,self.pid,p,fingerprint(p),'fixture-owner')
        run=self.store.get_record(self.pid,r['created_ids'][0])
        self.assertEqual(run['fields']['job_state'],'failed')
        self.assertEqual(run['fields']['cost']['kind'],'unknown')
        self.assertEqual(run['review_state'],'not_submitted')

if __name__=='__main__': unittest.main()
