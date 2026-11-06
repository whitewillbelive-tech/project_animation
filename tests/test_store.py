import copy
import hashlib
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from app.auth import password_hash, verify_password
from app.store import Store, ApiError, TYPES, canonical
from app.server import build_server


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'animation.sqlite3'
        self.store=Store(self.path)
        self.project=self.store.init_owner('영고','owner-only-test-12345')
        self.pid=self.project['id']

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def record(self, **payload):
        return self.store.create_record(self.pid,{'title':'테스트 기록',**payload},'영고')

    def patch(self, r, **payload):
        return self.store.update_record(self.pid,r['id'],{'revision':r['revision'],**payload},'영고')

    def review(self,r,action='submit',reason='기준 검토'):
        return self.store.review_record(self.pid,r['id'],{'revision':r['revision'],'action':action,'reason':reason},'영고')

    def test_empty_project_and_proposed_templates(self):
        state=self.store.state(self.pid)
        self.assertEqual(state['records'],[])
        self.assertEqual(len(state['stages']),8)
        self.assertEqual(state['project']['name'],'프로젝트 원피스')
        self.assertEqual(len([t for t in state['templates'] if t.get('fields',{}).get('kind')=='ai_role']),8)
        self.assertEqual(len([t for t in state['templates'] if t.get('fields',{}).get('kind')=='tool_candidate']),10)
        self.assertTrue(all(s['recommended_tools'] and s['subtasks'] for s in state['stages']))

    def test_all_types_save_and_persist_after_reconnect(self):
        records=[self.record(type=kind,title=kind) for kind in TYPES]
        self.store.db.close()
        self.store=Store(self.path)
        self.assertEqual({r['id'] for r in self.store.state(self.pid)['records']},{r['id'] for r in records})

    def test_optimistic_revision_conflict(self):
        r=self.record()
        updated=self.patch(r,title='먼저 저장')
        self.assertEqual(updated['revision'],2)
        with self.assertRaises(ApiError) as error: self.patch(r,title='오래된 화면')
        self.assertEqual(error.exception.status,409)
        self.assertEqual(self.store.get_record(self.pid,r['id'])['title'],'먼저 저장')

    def test_template_checklists_are_independent(self):
        a=self.store.instantiate(self.pid,'task-guide',{'title':'가'},'영고')
        b=self.store.instantiate(self.pid,'task-guide',{'title':'나'},'영고')
        a['checklist'][0]['done']=True
        self.patch(a,checklist=a['checklist'])
        self.assertFalse(self.store.get_record(self.pid,b['id'])['checklist'][0]['done'])
        self.assertFalse(self.store.state(self.pid)['templates'][1]['checklist'][0]['done'])

    def test_relations_and_prerequisite_cycles_rejected(self):
        episode=self.record(type='episode')
        scene=self.record(type='scene',related_ids=[episode['id']])
        shot=self.record(type='shot',related_ids=[scene['id']])
        self.assertEqual(self.store.get_record(self.pid,shot['id'])['related_ids'],[scene['id']])
        a=self.record(type='task')
        b=self.record(type='task',prerequisites=[a['id']])
        with self.assertRaises(ApiError): self.patch(a,prerequisites=[b['id']])
        with self.assertRaises(ApiError): self.record(related_ids=['missing'])

    def test_generated_success_does_not_approve_and_explicit_review_audits(self):
        run=self.record(type='run',fields={'job_state':'succeeded'})
        self.assertEqual(run['review_state'],'not_submitted')
        with self.assertRaises(ApiError): self.record(status='done')
        with self.assertRaises(ApiError): self.patch(run,review_state='approved')
        r=self.record(type='task',reviewer='영고',acceptance='기준을 사람에게 확인받는다.',checklist=[{'text':'기술 결과 확인','done':True}])
        r=self.review(r)
        r=self.review(r,'approve','완료 기준을 확인했습니다.')
        self.assertEqual((r['review_state'],r['status']),('approved','done'))
        event=self.store.state(self.pid)['events'][-1]
        self.assertEqual(event['actor'],'영고')
        self.assertTrue(event['created_at'].endswith('Z'))
        self.assertEqual(event['reason'],'완료 기준을 확인했습니다.')
        changed=self.patch(r,content='승인 후 내용 수정')
        self.assertEqual(changed['review_state'],'not_submitted')
        self.assertNotEqual(changed['status'],'done')

    def test_checklist_and_predecessor_enforce_completion(self):
        pre=self.record(type='task')
        r=self.record(type='task',reviewer='영고',acceptance='승인 기준',prerequisites=[pre['id']])
        r=self.review(r)
        with self.assertRaises(ApiError): self.review(r,'approve')
        another=self.record(type='task',reviewer='영고',acceptance='기준',checklist=[{'text':'확인','done':False}])
        another=self.review(another)
        with self.assertRaises(ApiError): self.review(another,'approve')

    def test_changed_input_requires_revalidation(self):
        source=self.record(type='asset')
        task=self.record(type='task',reviewer='영고',acceptance='기준',related_ids=[source['id']],input_revisions={source['id']:source['revision']})
        source=self.patch(source,content='새 입력 버전')
        state=self.store.state(self.pid)
        self.assertEqual(next(r for r in state['records'] if r['id']==task['id'])['stale_inputs'],[source['id']])
        task=self.review(task)
        with self.assertRaises(ApiError) as error: self.review(task,'approve')
        self.assertEqual(error.exception.status,409)

    def test_archive_restore_and_project_authorization(self):
        r=self.record()
        r=self.patch(r,archived=True)
        self.assertTrue(r['archived'])
        self.assertFalse(self.patch(r,archived=False)['archived'])
        with self.assertRaises(ApiError) as e: self.store.authorize('different-user',self.pid)
        self.assertEqual(e.exception.status,403)

    def test_atomic_rollback_and_internal_stable_id(self):
        with self.assertRaises(ApiError):
            with self.store.atomic():
                self.store.create_record(self.pid,{'title':'이관'},'영고',record_id='notion_stable')
                self.record(related_ids=['missing'])
        self.assertEqual(self.store.state(self.pid)['records'],[])
        self.assertEqual(self.store.state(self.pid)['events'],[])
        r=self.store.create_record(self.pid,{'title':'이관'},'영고',record_id='notion_stable')
        self.assertEqual(r['id'],'notion_stable')

    def test_backup_restore_integrity_and_recovery_copy(self):
        r=self.record(content='백업 시점')
        backup=self.store.backup(self.pid)
        self.patch(r,content='변경')
        self.assertTrue(self.store.validate_backup(self.pid,backup)['valid'])
        self.store.restore(self.pid,backup,'영고','복원')
        self.assertEqual(self.store.get_record(self.pid,r['id'])['content'],'백업 시점')
        self.assertEqual(len(list((Path(self.tmp.name)/'recovery').glob('*.json'))),1)
        broken=copy.deepcopy(backup)
        broken['data']['records'][0]['title']='위조'
        with self.assertRaises(ApiError): self.store.validate_backup(self.pid,broken)
        broken=copy.deepcopy(backup)
        broken['data']['records'][0]['related_ids']=['missing']
        broken['sha256']=hashlib.sha256(canonical(broken['data']).encode()).hexdigest()
        with self.assertRaises(ApiError): self.store.restore(self.pid,broken,'영고','복원')
        self.assertEqual(self.store.get_record(self.pid,r['id'])['content'],'백업 시점')

    def test_source_and_schema_input_validation(self):
        for payload in ({'type':[]},{'category':[]},{'status':{}},{'source':{'url':'javascript:alert(1)'}},{'due_date':'2026-99-99'},{'due_date':'2026-02-29'},{'owner':'가'*201},{'reviewer':'나'*201}):
            with self.assertRaises(ApiError): self.record(**payload)
        r=self.record(source={'id':'notion-id','url':'https://app.notion.com/p/id','read_at':'2026-11-06T00:00:00Z','modified_at':None,'warnings':['미확인']})
        self.assertIsNone(r['source']['modified_at'])
        self.assertEqual(self.record(due_date='2028-02-29')['due_date'],'2028-02-29')
        with self.assertRaises(ApiError): self.patch(r,fields=[])

    def test_backup_rejects_malformed_import_receipt(self):
        backup=self.store.backup(self.pid)
        backup['data']['imports']=[{'id':'unknown_import','kind':'request','package':{},'fingerprint':'0'*64}]
        backup['sha256']=hashlib.sha256(canonical(backup['data']).encode()).hexdigest()
        with self.assertRaises(ApiError): self.store.validate_backup(self.pid,backup)
        self.assertEqual(self.store.state(self.pid)['imports'],[])

    def test_password_hashes_and_no_reinitialization(self):
        encoded=password_hash('valid-test-password')
        self.assertNotIn('valid-test-password',encoded)
        self.assertTrue(verify_password('valid-test-password',encoded))
        self.assertFalse(verify_password('wrong-password',encoded))
        with self.assertRaises(ApiError): self.store.init_owner('다른 소유자','another-test-password')

    def test_instruction_approval_is_separate_and_invalidated_by_change(self):
        r=self.record(type='task',content='실행 지시',acceptance='검토 기준',output_spec='PNG',input_version='v1',reviewer='영고')
        with self.assertRaises(ApiError): self.patch(r,fields={'instruction_approval':{'actor':'위조'}})
        r=self.store.approve_instructions(self.pid,r['id'],{'revision':r['revision'],'reason':'작업 지시 확인'},'영고')
        self.assertEqual(r['fields']['instruction_approval']['source_revision'],r['revision'])
        self.assertEqual(r['review_state'],'not_submitted')
        self.assertNotEqual(r['status'],'done')
        changed=self.patch(r,content='지시 변경')
        self.assertNotIn('instruction_approval',changed['fields'])

    def test_review_scope_changes_invalidate_both_approvals(self):
        for change in ({'type':'document'},{'category':'B'},{'stage_ids':['planning']},{'reviewer':'다른 검토자'}):
            with self.subTest(change=change):
                r=self.record(type='task',content='지시',acceptance='기준',output_spec='PNG',input_version='v1',reviewer='영고')
                r=self.store.approve_instructions(self.pid,r['id'],{'revision':r['revision'],'reason':'지시 확인'},'영고')
                r=self.review(r)
                r=self.review(r,'approve')
                changed=self.patch(r,**change)
                self.assertEqual(changed['review_state'],'not_submitted')
                self.assertNotEqual(changed['status'],'done')
                self.assertNotIn('instruction_approval',changed['fields'])

    def test_stage_guidance_update_and_backup_validation(self):
        stage=self.store.state(self.pid)['stages'][0]
        updated=self.store.update_stage(self.pid,stage['id'],{'revision':stage['revision'],'recommended_tools':'수동 조사 · 후보 도구 비교','subtasks':'1. 입력 확인\n2. 검토 요청'},'영고')
        self.assertEqual(updated['recommended_tools'],'수동 조사 · 후보 도구 비교')
        self.assertTrue(self.store.validate_backup(self.pid,self.store.backup(self.pid))['valid'])
        with self.assertRaises(ApiError): self.store.update_stage(self.pid,stage['id'],{'revision':updated['revision'],'subtasks':['배열 아님']},'영고')


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.server=build_server('127.0.0.1',0,self.tmp.name)
        self.server.store.init_owner('영고','owner-only-test-12345')
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.cookie=''
        self.csrf=''

    def tearDown(self):
        self.server.shutdown();self.thread.join()
        self.server.server_close();self.server.store.db.close()
        self.tmp.cleanup()

    def request(self,method,path,body=None,headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        h={'Content-Type':'application/json'}
        if self.cookie: h['Cookie']=self.cookie
        if self.csrf: h['X-CSRF-Token']=self.csrf
        h.update(headers or {})
        conn.request(method,path,json.dumps(body) if body is not None else None,h)
        response=conn.getresponse()
        content=response.read()
        result=json.loads(content) if 'application/json' in response.getheader('Content-Type','') else content
        output=(response.status,result,dict(response.getheaders()))
        conn.close()
        return output

    def login(self):
        status,body,headers=self.request('POST','/api/login',{'username':'영고','password':'owner-only-test-12345'})
        self.assertEqual(status,200)
        self.cookie=headers['Set-Cookie'].split(';',1)[0]
        self.csrf=body['csrf_token']
        self.assertIn('HttpOnly',headers['Set-Cookie'])
        self.assertIn('SameSite=Strict',headers['Set-Cookie'])

    def test_anonymous_read_write_and_csrf_rejected(self):
        self.assertEqual(self.request('GET','/api/state')[0],401)
        self.assertEqual(self.request('POST','/api/records',{'title':'익명'})[0],401)
        self.login()
        self.assertEqual(self.request('GET','/api/state')[0],200)
        self.assertEqual(self.request('POST','/api/records',{'title':'잘못된 요청'},headers={'X-CSRF-Token':'wrong'})[0],403)
        self.assertEqual(self.request('POST','/api/records',{'title':'외부 출처'},headers={'Origin':'https://evil.example'})[0],403)
        self.assertEqual(self.request('GET','/api/state',headers={'Host':'evil.example'})[0],403)

    def test_saves_errors_and_session_survives_new_connection(self):
        self.login()
        status,r,_=self.request('POST','/api/records',{'title':'실제 저장'})
        self.assertEqual(status,201)
        self.assertEqual(self.request('PATCH','/api/records/'+r['id'],{'revision':0,'title':'오래된 수정'})[0],409)
        status,state,_=self.request('GET','/api/state')
        self.assertEqual(status,200)
        self.assertEqual(state['records'][0]['title'],'실제 저장')
        self.assertEqual(self.request('GET','/api/session')[1]['user']['name'],'영고')
        self.assertEqual(self.request('GET','/api/../data/animation.sqlite3')[0],400)
        self.assertEqual(self.request('GET','/data/animation.sqlite3')[0],404)
        self.assertEqual(self.request('POST','/api/logout',{})[0],200)
        self.assertEqual(self.request('GET','/api/state')[0],401)

    def test_project_selection_and_settings(self):
        self.login()
        first=self.request('GET','/api/state')[1]['project']
        status,state,_=self.request('POST','/api/projects',{'name':'새 프로젝트'})
        self.assertEqual(status,201)
        self.assertNotEqual(state['project']['id'],first['id'])
        self.assertEqual(len(state['records']),0)
        status,state,_=self.request('POST','/api/projects/select',{'id':first['id']})
        self.assertEqual(status,200)
        self.assertEqual(state['project']['id'],first['id'])
        status,p,_=self.request('PATCH','/api/project',{'revision':first['revision'],'name':'변경한 작업명','timezone':'Asia/Seoul'})
        self.assertEqual((status,p['name']),(200,'변경한 작업명'))
        self.assertEqual(self.request('POST','/api/projects/select',{'id':'private-foreign-project'})[0],403)


if __name__=='__main__': unittest.main()
