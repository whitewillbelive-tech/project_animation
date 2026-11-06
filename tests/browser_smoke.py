"""Real Chromium validation against an isolated live SQLite-backed server."""
import copy
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.server import build_server
from playwright.sync_api import sync_playwright, expect


def main():
    output = ROOT / 'test-results'
    output.mkdir(exist_ok=True)
    checks = []
    with tempfile.TemporaryDirectory() as tmp:
        server = build_server(port=0, data_dir=tmp)
        server.store.init_owner('검증 소유자', 'temporary-browser-test-password')
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(executable_path='/usr/bin/chromium', headless=True,
                                             args=['--no-sandbox', '--disable-dev-shm-usage'])
                context = browser.new_context(viewport={'width':1440,'height':1100})
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda err: errors.append(str(err)))
                page.on('console', lambda msg: errors.append(msg.text) if msg.type == 'error' and 'Content Security Policy' in msg.text else None)
                page.goto(base)
                expect(page.get_by_label('소유자 계정')).to_be_visible()
                assert context.request.get(base + '/api/state').status == 401
                assert context.request.post(base + '/api/records',data={'title':'거부'}).status == 401
                checks.append('권한 없는 읽기·쓰기 거부')
                page.get_by_label('소유자 계정').fill('검증 소유자')
                page.get_by_label('비밀번호',exact=True).fill('temporary-browser-test-password')
                page.get_by_role('button',name='작업 공간에 로그인').click()
                expect(page.get_by_role('heading',name='우리의 이야기가 만들어지는 곳')).to_be_visible()
                page.screenshot(path=str(output/'dashboard-desktop.png'),full_page=True)
                assert page.locator('.stage-card').count()==8
                assert page.locator('.stage-card').first.inner_text().find('작업 미등록')>=0
                checks.append('빈 프로젝트·8개 공정에 가짜 진행률 없음')

                csrf=context.request.get(base+'/api/session').json()['csrf_token']
                def api(path, payload=None, method='POST', expected=200):
                    response=context.request.fetch(base+path,method=method,
                        data=payload,headers={'X-CSRF-Token':csrf})
                    assert response.status==expected,(path,response.status,response.text())
                    return response.json()
                def state(): return context.request.get(base+'/api/state').json()
                original_project=state()['project']['id']
                page.locator('[data-nav="settings"]').first.click()
                page.get_by_role('button',name='새 프로젝트 만들기',exact=True).click()
                page.get_by_label('프로젝트 작업명',exact=True).last.fill('브라우저 검증 프로젝트')
                page.get_by_role('button',name='프로젝트 만들기',exact=True).click()
                expect(page.locator('#modal-root')).to_be_empty()
                assert state()['project']['id']!=original_project
                page.get_by_role('button',name='기록 만들기',exact=True).first.click()
                long_title='한국어 긴 문서 · 사실과 해석, 각색과 AI 제안을 구분하는 작업 기록'
                page.get_by_label('제목',exact=True).fill(long_title)
                page.get_by_label('내용·세부 작성 항목').fill('사실: 미정\n원문 해석: 미정\n창작자의 각색: 미정\nAI 제안: 미승인\n<script>window.pwned=true</script>')
                page.get_by_label('완료·반려 기준').fill('근거와 분류 확인')
                page.get_by_label('검토자',exact=True).fill('검증 소유자')
                page.get_by_role('button',name='기록 저장',exact=True).click()
                expect(page.get_by_role('heading',name=long_title,exact=True)).to_be_visible()
                assert page.evaluate('window.pwned') is None
                saved=next(r for r in state()['records'] if r['title']==long_title)
                page.reload()
                expect(page.get_by_role('heading',name='우리의 이야기가 만들어지는 곳')).to_be_visible()
                page.locator('[data-nav="records"]').first.click()
                page.get_by_role('button',name=long_title,exact=False).first.click()
                expect(page.get_by_role('heading',name=long_title,exact=True)).to_be_visible()
                checks.append('새 프로젝트·문서 UI 저장·재접속 복원·HTML 안전 렌더링')

                # Simulate simultaneous writes; failed save must keep the form and show its error.
                page.get_by_role('button',name='편집',exact=True).click()
                page.get_by_label('제목',exact=True).fill(long_title+' 미저장')
                api('/api/records/'+saved['id'],{'revision':saved['revision'],'purpose':'다른 창의 수정'},'PATCH')
                page.get_by_role('button',name='변경 사항 저장',exact=True).click()
                expect(page.locator('#record-error')).not_to_be_empty()
                expect(page.get_by_label('제목',exact=True)).to_have_value(long_title+' 미저장')
                page.keyboard.press('Escape')
                expect(page.locator('[role="dialog"]')).to_have_count(0)
                checks.append('동시 수정 충돌·저장 실패 안내·미저장 입력 유지·Escape 닫기')
                page.reload();expect(page.locator('[data-nav="records"]').first).to_be_visible()
                page.locator('[data-nav="records"]').first.click()
                page.get_by_role('button',name=long_title,exact=False).first.click()
                page.get_by_role('button',name='보관',exact=True).click()
                expect(page.get_by_role('button',name='보관 해제',exact=True)).to_be_visible()
                page.get_by_role('button',name='보관 해제',exact=True).click()
                expect(page.get_by_role('button',name='보관',exact=True)).to_be_visible()
                checks.append('기록 보관·복구 UI')
                page.locator('[data-nav="category"][data-category="B"]').first.click()
                page.get_by_role('button',name='카테고리 작업 기준 작성',exact=True).click()
                page.get_by_label('제목',exact=True).fill('작품 기획 작업 기준')
                page.get_by_role('button',name='기록 저장',exact=True).click()
                expect(page.get_by_role('heading',name='작품 기획 작업 기준',exact=True)).to_be_visible()
                page.locator('[data-nav="category"][data-category="B"]').first.click()
                expect(page.get_by_role('button',name='작업 패널 열기',exact=True)).to_be_visible()
                assert sum(r.get('fields',{}).get('category_panel')=='B' for r in state()['records'])==1
                checks.append('카테고리 공통 작업 패널·중복 없는 문서 연결')

                # Create related real records via the same API; verify both directions in the UI.
                episode=api('/api/records',{'type':'episode','title':'검증 회차','category':'C'},expected=201)
                scene=api('/api/records',{'type':'scene','title':'검증 장면','category':'C','related_ids':[episode['id']]},expected=201)
                shot=api('/api/records',{'type':'shot','title':'검증 숏','category':'C','related_ids':[scene['id']]},expected=201)
                asset=api('/api/records',{'type':'asset','title':'검증 에셋 메타데이터','category':'E','related_ids':[shot['id']]},expected=201)
                ref=api('/api/records',{'type':'reference','title':'검증 자료','category':'F','related_ids':[shot['id'],episode['id']]},expected=201)
                page.reload(); expect(page.locator('[data-nav="records"]').first).to_be_visible()
                page.locator('[data-nav="records"]').first.click()
                page.get_by_role('button',name='검증 회차',exact=False).first.click()
                expect(page.get_by_role('button',name='검증 장면',exact=False).first).to_be_visible()
                page.get_by_role('button',name='검증 장면',exact=False).first.click()
                expect(page.get_by_role('button',name='검증 회차',exact=False).first).to_be_visible()
                page.get_by_role('button',name='검증 숏',exact=False).first.click()
                expect(page.get_by_role('button',name='검증 에셋 메타데이터',exact=False).first).to_be_visible()
                expect(page.get_by_role('button',name='검증 자료',exact=False).first).to_be_visible()
                checks.append('회차·장면·숏·에셋·자료 양방향 관계 탐색')
                page.locator('[data-nav="records"]').first.click()
                page.get_by_label('기록 검색').fill('검증 숏')
                expect(page.locator('.record-table tbody tr')).to_have_count(1)
                page.get_by_role('button',name='칸반',exact=True).click()
                expect(page.locator('.kanban-card')).to_have_count(1)
                page.get_by_role('button',name='목록',exact=True).click()
                page.get_by_label('기록 검색').fill('')
                page.get_by_label('기록 유형',exact=True).select_option('episode')
                expect(page.locator('.record-table tbody tr')).to_have_count(1)
                checks.append('검색·유형 필터·목록/칸반 전환')

                first=api('/api/templates/document-guide/instantiate',{'title':'양식 복제 1'},expected=201)
                second=api('/api/templates/document-guide/instantiate',{'title':'양식 복제 2'},expected=201)
                checklist=copy.deepcopy(first['checklist']);checklist[0]['done']=True
                api('/api/records/'+first['id'],{'revision':first['revision'],'checklist':checklist},'PATCH')
                assert not next(r for r in state()['records'] if r['id']==second['id'])['checklist'][0]['done']
                checks.append('템플릿 두 번 복제·체크리스트 독립')

                # Actual task UI creation, instruction approval, file download and result import.
                page.locator('[data-nav="pipeline"]').first.click()
                page.get_by_role('button',name='작업 만들기',exact=True).click()
                page.get_by_label('제목',exact=True).fill('계약 브라우저 검증 작업')
                page.get_by_label('내용·세부 작성 항목').fill('테스트용 1픽셀 PNG 메타데이터를 반환한다.')
                page.get_by_label('입력 자료·기준 버전').fill('검증 입력 v1')
                page.get_by_label('완료·반려 기준').fill('해시·규격 검토')
                page.get_by_label('출력 규격',exact=True).fill('PNG 1×1')
                page.get_by_label('검토자',exact=True).fill('검증 소유자')
                page.get_by_label('관련 공정',exact=True).select_option('production')
                page.get_by_role('button',name='기록 저장',exact=True).click()
                expect(page.get_by_role('heading',name='계약 브라우저 검증 작업',exact=True)).to_be_visible()
                task=next(r for r in state()['records'] if r['title']=='계약 브라우저 검증 작업')
                page.get_by_role('button',name='작업 지시 승인',exact=True).click()
                page.get_by_label('승인 이유',exact=False).fill('계약 테스트의 입력·출력 기준을 확인했습니다.')
                page.locator('[role="dialog"]').get_by_role('button',name='작업 지시 승인',exact=True).click()
                expect(page.locator('[role="dialog"]')).to_have_count(0)
                with page.expect_download() as download:
                    page.get_by_role('button',name='작업 패키지',exact=True).click()
                request_file=output/'request.json';download.value.save_as(request_file)
                request=json.loads(request_file.read_text())
                assert request['package_type']=='work_request'
                task_before=next(r for r in state()['records'] if r['id']==task['id'])
                assert task_before['review_state']=='not_submitted' and task_before['status']!='done'
                result=json.loads((ROOT/'workstation-handoff/fixtures/work_result.valid.json').read_text())
                result.update(project_id=request['project_id'],request_package_id=request['package_id'],
                              target=request['target'],used_inputs={'source_revision':request['source_revision'],'input_assets':request['input_assets']})
                result_file=output/'result.json';result_file.write_text(json.dumps(result))
                page.locator('[data-nav="transfer"]').first.click()
                page.get_by_role('button',name='결과 패키지 가져오기',exact=True).click()
                page.locator('#import-file').set_input_files(result_file)
                expect(page.get_by_role('button',name='확인하고 가져오기',exact=True)).to_be_visible()
                page.get_by_role('button',name='확인하고 가져오기',exact=True).click()
                expect(page.locator('[role="dialog"]')).to_have_count(0)
                current=state()
                assert next(r for r in current['records'] if r['id']==task['id'])['review_state']=='not_submitted'
                assert all(r['is_sample'] and r['review_state']=='not_submitted' for r in current['records'] if r['type']=='run')
                page.get_by_role('button',name='결과 패키지 가져오기',exact=True).click()
                page.locator('#import-file').set_input_files(result_file)
                expect(page.locator('#import-preview')).to_contain_text('이미 가져온 패키지')
                expect(page.get_by_role('button',name='확인하고 가져오기',exact=True)).to_have_count(0)
                page.keyboard.press('Escape')
                checks.append('UI 지시 승인·요청 다운로드·fixture 결과 미리보기/가져오기·중복 차단·승인 분리')

                # Final review remains an explicit human action with immutable audit entries.
                latest=next(r for r in state()['records'] if r['id']==task['id'])
                latest=api('/api/records/'+task['id']+'/review',{'revision':latest['revision'],'action':'submit','reason':'결과물 검토 요청'})
                latest=api('/api/records/'+task['id']+'/review',{'revision':latest['revision'],'action':'approve','reason':'테스트 검토 완료'})
                assert latest['review_state']=='approved'
                assert any(e['record_id']==task['id'] and e['reason']=='테스트 검토 완료' and e['actor']=='검증 소유자' and e['created_at'].endswith('Z') for e in state()['events'])
                checks.append('최종 승인에 수행자·이유·UTC 시각 기록')

                with page.expect_download() as download:
                    page.get_by_role('button',name='백업 내려받기',exact=True).click()
                backup_file=output/'backup.json';download.value.save_as(backup_file)
                backup=json.loads(backup_file.read_text())
                api('/api/records',{'title':'백업 후 추가 기록'},expected=201)
                page.get_by_role('button',name='백업 복원',exact=True).click()
                page.locator('#import-file').set_input_files(backup_file)
                page.get_by_role('button',name='확인하고 복원',exact=True).click()
                expect(page.locator('[role="dialog"]')).to_have_count(0)
                assert not any(r['title']=='백업 후 추가 기록' for r in state()['records'])
                assert len(state()['records'])==len(backup['data']['records'])
                checks.append('UI 실제 데이터 백업·미리보기·복원 왕복')

                page.locator('[data-nav="transfer"]').first.click()
                source_file=output/'notion-export.md';source_file.write_text('# 파일 이관 검증\n사실과 해석은 미정. 원본 미접속.',encoding='utf-8')
                page.locator('[data-action="import-notion"]').click()
                page.get_by_label('원본 Notion 페이지 링크').fill('https://app.notion.com/p/3f3e38d0824b817086c4ffa35a1dd7de')
                page.locator('#notion-file').set_input_files(source_file)
                page.get_by_role('button',name='변환·충돌 미리보기').click()
                expect(page.locator('#notion-preview')).to_contain_text('적용 가능')
                page.get_by_role('button',name='확인하고 가져오기',exact=True).click()
                expect(page.locator('[role="dialog"]')).to_have_count(0)
                imported=next(r for r in state()['records'] if r['title']=='파일 이관 검증')
                assert imported['source']['id']=='3f3e38d0824b817086c4ffa35a1dd7de'
                checks.append('Markdown 파일 이관 UI 미리보기·확인·출처 저장')
                with page.expect_download() as download:
                    page.get_by_role('link',name='워크스테이션 handoff 받기').click()
                assert download.value.suggested_filename.endswith('.zip')
                blocker=api('/api/records',{'type':'task','title':'선행 작업 미완료 검증','stage_ids':['production'],'category':'D'},expected=201)
                api('/api/records',{'type':'task','title':'의존 작업 차단 검증','stage_ids':['production'],'prerequisites':[blocker['id']],'category':'D'},expected=201)
                waiting=api('/api/records',{'type':'document','title':'검토 집계 검증','category':'A','reviewer':'검증 소유자'},expected=201)
                api('/api/records/'+waiting['id']+'/review',{'revision':waiting['revision'],'action':'submit','reason':'집계 검증'})
                page.reload();expect(page.locator('[data-nav="dashboard"]').first).to_be_visible()
                page.locator('[data-nav="dashboard"]').first.click()
                expect(page.locator('.stat-card').nth(0).locator('.stat-value')).to_contain_text('3')
                expect(page.locator('.stat-card').nth(2).locator('.stat-value')).to_contain_text('1')
                expect(page.locator('.stat-card').nth(3).locator('.stat-value')).to_contain_text('1')
                expect(page.locator('[data-stage="production"]')).to_contain_text('1 / 3')
                checks.append('공정 전체 작업·승인 완료·검토 대기·선행 차단 실제 집계 일치')
                page.set_viewport_size({'width':390,'height':844})
                expect(page.get_by_role('heading',name='우리의 이야기가 만들어지는 곳')).to_be_visible()
                deadline=time.monotonic()+2
                while page.locator('#sidebar').evaluate('(el)=>el.getBoundingClientRect().right')>1 and time.monotonic()<deadline:
                    page.wait_for_timeout(50)
                assert page.locator('#sidebar').evaluate('(el)=>el.getBoundingClientRect().right')<=1
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'mobile overflow'
                page.screenshot(path=str(output/'dashboard-mobile.png'),full_page=True,animations='disabled')
                page.locator('[data-action="menu"]').click()
                expect(page.locator('#sidebar')).to_have_class(__import__('re').compile('open'))
                page.locator('[data-nav="records"]').first.click()
                expect(page.get_by_role('heading',name='전체 기록',exact=True)).to_be_visible()
                page.get_by_role('button',name=long_title,exact=False).first.click()
                page.get_by_role('button',name='편집',exact=True).click()
                expect(page.locator('[role="dialog"]')).to_be_visible()
                page.keyboard.press('Shift+Tab')
                page.keyboard.press('Tab')
                assert page.evaluate("!!document.activeElement.closest('[role=dialog]')")
                page.screenshot(path=str(output/'detail-mobile.png'),full_page=True,animations='disabled')
                page.keyboard.press('Escape')
                checks.append('handoff ZIP·390px 모바일·긴 한국어 상세·Tab 키 모달 포커스')
                assert not errors,errors
                checks.append('브라우저 JS 오류·CSP 오류 없음')
                browser.close()
        finally:
            server.shutdown();server.server_close();server.store.db.close();thread.join(2)
    (output/'browser-results.json').write_text(json.dumps({'passed':checks,'failed':[]},ensure_ascii=False,indent=2))
    print(json.dumps({'passed_count':len(checks),'passed':checks},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
