"""SQLite domain store with optimistic revisions and explicit human review."""
import copy
import hashlib
import json
import re
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from urllib.parse import urlsplit
from .auth import utc_now, password_hash

TYPES = {'document','member','task','meeting','decision','budget','character','episode','scene','shot','tool','manual','technical_test','asset','run','review','reference'}
STATUSES = {'draft','todo','in_progress','blocked','pending','done'}
REVIEWS = {'not_submitted','pending','changes_requested','approved'}
ID_RE = re.compile(r'^[A-Za-z0-9_-]{1,120}$')


class ApiError(Exception):
    def __init__(self, status, message, details=None):
        super().__init__(message)
        self.status, self.message, self.details = status, message, details


def uid(prefix):
    return prefix + '_' + secrets.token_hex(10)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def clone(value):
    return copy.deepcopy(value)


STAGES = [
    ('planning','기획·조사','본문·자료·창작 의도','기획 / 조사','검토된 기획 문서','사실·해석·각색·AI 제안을 구분하고 근거를 검토한다.'),
    ('world','세계관·인물','승인된 기획·조사','기획 / 미술','세계관·인물 기준','설정의 일관성과 각색 근거를 검토한다.'),
    ('script','회차·대본','세계관·인물 기준','작가 / 연출','회차·장면·대본','회차 목적, 인물 행동, 대사와 장면 연결을 검토한다.'),
    ('storyboard','콘티·애니매틱','승인된 대본·숏 설계','연출 / 콘티','콘티·애니매틱','화면 연결, 길이와 연출 의도를 검토한다.'),
    ('assets','기준 에셋','승인된 설정·콘티','미술 / 제작','기준 에셋','외형·규격·이용 조건을 검토한다.'),
    ('production','숏·음향 제작','승인된 숏·입력 에셋','영상 / 음향','숏·음향 결과물','입력 버전, 연속성, 음향과 권리를 검토한다.'),
    ('post','편집·후반','검토된 숏·음향','편집 / 후반','편집본·자막','길이, 화면·음향 연결과 출력 규격을 검토한다.'),
    ('delivery','검수·납품','최종 편집본·납품 기준','검수 / 기획','승인된 납품 기록','완료 조건과 최종 승인 이력을 확인한다.'),
]


def default_stages():
    return [dict(id=s[0],name=s[1],input=s[2],role=s[3],output=s[4],criteria=s[5],next=STAGES[i+1][0] if i<7 else None,rework=s[0],description='수정 가능한 초기 제안 · 작품 내용 미정',recommended_tools='후보 미정 · 양식·가이드의 도구 후보를 비교 후 선택하세요.',subtasks='제안 절차: 입력 확인 → 기준 작성 → 작업 등록 → 검토 요청',revision=1) for i,s in enumerate(STAGES)]


TEMPLATES = [
    dict(id='document-guide',title='공통 문서 작성',type='document',category='A',purpose='목적과 근거, 검토 기준을 기록하는 제안 템플릿',content='## 목적\n미정\n\n## 사실\n미정\n\n## 원문 해석\n미정\n\n## 창작자의 각색\n미정\n\n## AI 제안 (미승인)\n미정\n\n## 근거와 남은 질문\n미정',checklist=[{'text':'목적과 입력 자료를 기록한다.','done':False},{'text':'사실 / 해석 / 각색 / AI 제안을 구분한다.','done':False},{'text':'완료 조건과 검토자를 지정한다.','done':False}]),
    dict(id='task-guide',title='제작 작업 지시',type='task',category='D',purpose='입력·절차·출력·인수인계를 정의하는 제안 템플릿',content='1. 입력 자료와 기준 버전을 확인한다.\n2. 도구 후보·선택 이유·대안·수동 방법을 기록한다.\n3. 지시문을 작성하고 검토한다.\n4. 별도 워크스테이션에서 실행한다.\n5. 결과를 가져와 검토를 요청한다.\n\nAI 작업 지시문\n목적: {{목적}}\n입력: {{입력}}\n제약: {{제약}}\n출력: {{출력}}\n미정 항목을 사실처럼 확정하지 마세요.',checklist=[{'text':'입력 버전과 선행 조건 확인','done':False},{'text':'출력 규격·완료 조건 작성','done':False},{'text':'인수인계 대상·검토자 지정','done':False}]),
    dict(id='character-guide',title='인물 설정',type='character',category='B',purpose='인물의 기능과 근거를 기록하는 제안 템플릿',content='이름: 미정\n작품 내 역할: 미정\n목표·갈등·변화: 미정\n본문·역사 근거: 미정\n각색 범위: 미정\n외형 기준: 미정',checklist=[{'text':'근거와 창작 설정 구분','done':False},{'text':'관련 회차·장면 연결','done':False}]),
    dict(id='episode-guide',title='회차 설계',type='episode',category='C',purpose='회차의 목적과 장면 구성을 정리하는 제안 템플릿',content='회차명: 미정\n분량: 미정\n목적: 미정\n줄거리: 미정\n장면 구성: 미정\n관련 인물: 미정',checklist=[{'text':'회차 목적과 장면 구성 작성','done':False},{'text':'연결 장면·숏 검토','done':False}]),
    dict(id='shot-guide',title='숏 설계',type='shot',category='C',purpose='연출·입력·출력을 정의하는 제안 템플릿',content='장면: 미정\n프레이밍·움직임: 미정\n길이: 미정\n화면비·해상도: 미정\n인물·배경 에셋: 미정\n음향·자막: 미정',checklist=[{'text':'장면·인물·에셋 연결','done':False},{'text':'출력 규격과 연속성 기준 확인','done':False}]),
    dict(id='reference-guide',title='자료 정리',type='reference',category='F',purpose='출처·이용 조건과 해석을 분리하는 제안 템플릿',content='자료 유형: 성경 본문 / 역사 / 신학 / 시각 / 기술 / 이용 조건\n출처: 미정\n확인일: 미정\n사실: 미정\n원문 해석: 미정\n각색 아이디어: 미정\nAI 제안: 미정\n이용 조건: 미정',checklist=[{'text':'원문 링크·확인일 기록','done':False},{'text':'사실·해석·각색 구분','done':False},{'text':'이용 조건 확인','done':False}]),
    dict(id='manual-guide',title='작업 매뉴얼',type='manual',category='D',purpose='재현 가능한 절차를 정의하는 제안 템플릿',content='목적: 미정\n필요 입력: 미정\n절차:\n1. 입력 확인\n2. 실행\n3. 기술 결과 확인\n4. 검토 요청\n도구 후보·대안: 미정\n수동 방법: 미정\n반려·재작업: 미정',checklist=[{'text':'절차와 재현 조건 기록','done':False},{'text':'완료·반려 기준 기록','done':False}]),
]

# These are suggestions, not installed/connected tools or hired people. Unknown
# capability, pricing and licensing fields stay unknown until separately checked.
for tool_id, title, url, purpose in [
    ('chatgpt','ChatGPT','https://chatgpt.com/','기획·문서·대본 보조 후보'),
    ('notebooklm','NotebookLM','https://notebooklm.google/','자료 정리·근거 검토 후보'),
    ('chatgpt-images','ChatGPT Images','https://openai.com/','이미지 제작 후보'),
    ('runway','Runway','https://runwayml.com/','영상 제작 후보'),
    ('firefly','Adobe Firefly','https://www.adobe.com/products/firefly.html','이미지·영상 제작 후보'),
    ('elevenlabs','ElevenLabs','https://elevenlabs.io/','음성·음향 제작 후보'),
    ('eleven-music','Eleven Music','https://elevenlabs.io/music','음악 제작 후보'),
    ('resolve','DaVinci Resolve','https://www.blackmagicdesign.com/products/davinciresolve','편집·후반 후보'),
    ('comfyui','ComfyUI','https://www.comfy.org/','로컬 작업 흐름 후보'),
    ('notion-ai','Notion AI','https://www.notion.com/product/ai','이관 전 운영 도구 · 새 Site 필수 의존성 아님'),
]:
    TEMPLATES.append(dict(id='tool-'+tool_id,title=title,type='tool',category='D',purpose=purpose,content='제안 템플릿 · 후보 도구입니다. 최신 기능·요금·이용 조건과 실제 사용 권한은 확인되지 않았습니다. 구독과 API 이용 권한은 별개입니다. 외부 실행은 이 Site에서 제공하지 않습니다.',checklist=[{'text':'공식 문서와 이용 조건 확인','done':False},{'text':'별도 기술 테스트와 비용 제한 검토','done':False}],fields=dict(kind='tool_candidate',official_url=url,adoption='candidate',manual='미확인',api='미확인',local='미확인',input='미정',output='미정',cost_unit='미확인',terms='미확인',verified_at='',test_result='미실행',alternatives='미정',manual_method='미정')))

for role_id,title,output in [
    ('pm','PM 보조','작업·위험·의사결정 초안'),('research','조사·고증 보조','근거와 해석을 분리한 조사 초안'),
    ('story','스토리·대본 보조','줄거리·대본 제안'),('art','콘티·미술 보조','콘티·미술 지시 초안'),
    ('video','영상 보조','숏 실행 지시 초안'),('audio','음성·음향 보조','음향 설계·실행 지시 초안'),
    ('editing','편집·검수 보조','검수 체크·수정 제안'),('technical','기술·자동화 보조','테스트·자동화 설계 제안'),
]:
    TEMPLATES.append(dict(id='ai-role-'+role_id,title=title+' · AI 역할',type='tool',category='D',purpose='사람 계정이 아닌 AI 역할 정의 · 제안 템플릿',content='입력: 승인된 자료와 기준 버전\n출력: '+output+'\n지시문: 주어진 입력과 제약을 따르고 사실·원문 해석·각색·AI 제안을 구분하세요. 미정 항목은 질문으로 남기세요.\n검수자: 미정\n도구 후보: 미정\n실행 방식: 미연결\n비용 제한: 미정',checklist=[{'text':'권한·입력 범위·비용 제한 작성','done':False},{'text':'사람 검수자 지정','done':False}],fields=dict(kind='ai_role',permissions='제안만 작성 · 승인 권한 없음',connection='미연결',input='승인된 자료와 기준 버전',output=output,cost_limit='미정',reviewer='미정',candidate_tools=[])))


class Store:
    now = staticmethod(utc_now)

    @contextmanager
    def atomic(self):
        """Nested transaction boundary, shared by package/migration adapters."""
        with self.lock:
            name='sp_'+secrets.token_hex(8)
            self.db.execute('SAVEPOINT '+name)
            try:
                yield self
                self.db.execute('RELEASE SAVEPOINT '+name)
            except Exception:
                self.db.execute('ROLLBACK TO SAVEPOINT '+name)
                self.db.execute('RELEASE SAVEPOINT '+name)
                raise

    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,name TEXT UNIQUE NOT NULL,password TEXT NOT NULL,role TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,user_id TEXT NOT NULL,csrf TEXT NOT NULL,project_id TEXT,expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS stages(id TEXT NOT NULL,project_id TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(id,project_id));
        CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS imports(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,data TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS records_project ON records(project_id);
        CREATE INDEX IF NOT EXISTS events_project ON events(project_id);
        ''')

    def init_owner(self, name, password):
        if not isinstance(name,str) or not name.strip() or len(name)>100:
            raise ApiError(400,'소유자 이름을 1~100자로 입력하세요.')
        encoded = password_hash(password)
        with self.lock, self.db:
            if self.db.execute('SELECT 1 FROM users').fetchone():
                raise ApiError(409,'소유자 계정이 이미 있습니다. 기존 계정을 변경하지 않았습니다.')
            user_id=uid('user')
            self.db.execute('INSERT INTO users VALUES(?,?,?,?)',(user_id,name.strip(),encoded,'owner'))
            project=self.create_project(user_id,'프로젝트 원피스')
            return project

    def create_project(self, owner_id, name):
        if not isinstance(name,str) or not name.strip() or len(name)>200:
            raise ApiError(400,'프로젝트 이름을 1~200자로 입력하세요.')
        with self.lock:
            project=dict(id=uid('project'),name=name.strip(),workspace_name='AI 애니메이션',revision=1,timezone='Asia/Seoul',created_at=utc_now())
            self.db.execute('INSERT INTO projects VALUES(?,?,?)',(project['id'],owner_id,canonical(project)))
            for s in default_stages():
                self.db.execute('INSERT INTO stages VALUES(?,?,?)',(s['id'],project['id'],canonical(s)))
            return project

    def project(self, project_id):
        row=self.db.execute('SELECT data FROM projects WHERE id=?',(project_id,)).fetchone()
        if not row: raise ApiError(404,'프로젝트를 찾을 수 없습니다.')
        return json.loads(row['data'])

    def authorize(self, user_id, project_id):
        if not self.db.execute('SELECT 1 FROM projects WHERE id=? AND owner_id=?',(project_id,user_id)).fetchone():
            raise ApiError(403,'이 프로젝트에 접근할 권한이 없습니다.')

    def list_json(self, table, project_id):
        if table not in {'records','stages','events','imports'}: raise ValueError('invalid table')
        items=[json.loads(r['data']) for r in self.db.execute('SELECT data FROM '+table+' WHERE project_id=? ORDER BY rowid',(project_id,))]
        if table=='stages':
            for stage in items:
                stage.setdefault('recommended_tools','후보 미정 · 양식·가이드의 도구 후보를 비교 후 선택하세요.')
                stage.setdefault('subtasks','제안 절차: 입력 확인 → 기준 작성 → 작업 등록 → 검토 요청')
        return items

    def state(self, project_id, user_id=None):
        with self.lock:
            p=self.project(project_id)
            if user_id is None:
                user_id=self.db.execute('SELECT owner_id FROM projects WHERE id=?',(project_id,)).fetchone()[0]
            self.authorize(user_id,project_id)
            records=self.list_json('records',project_id)
            index={r['id']:r for r in records}
            for r in records:
                r['stale_inputs']=[key for key,rev in r.get('input_revisions',{}).items() if key in index and index[key]['revision']!=rev]
                r['blocked_by']=[key for key in r.get('prerequisites',[]) if key in index and index[key].get('status')!='done']
            return dict(project=p,projects=[json.loads(r[0]) for r in self.db.execute('SELECT data FROM projects WHERE owner_id=? ORDER BY rowid',(user_id,))],records=records,stages=self.list_json('stages',project_id),templates=clone(TEMPLATES),events=self.list_json('events',project_id),imports=self.list_json('imports',project_id))

    def update_project(self, project_id, payload, actor):
        with self.lock:
            p=self.project(project_id)
            self.check_revision(p,payload)
            name=payload.get('name',p['name'])
            if not isinstance(name,str) or not name.strip() or len(name)>200: raise ApiError(400,'프로젝트 이름이 올바르지 않습니다.')
            tz=payload.get('timezone',p['timezone'])
            try: ZoneInfo(tz)
            except (ZoneInfoNotFoundError,ValueError,TypeError): raise ApiError(400,'유효한 IANA 시간대를 입력하세요.')
            p.update(name=name.strip(),timezone=tz,revision=p['revision']+1)
            self.db.execute('UPDATE projects SET data=? WHERE id=?',(canonical(p),project_id))
            self.event(project_id,'project_updated',None,actor,'일반 설정 변경')
            return p

    def get_record(self, project_id, record_id):
        row=self.db.execute('SELECT data FROM records WHERE id=? AND project_id=?',(record_id,project_id)).fetchone()
        if not row: raise ApiError(404,'기록을 찾을 수 없습니다.')
        return json.loads(row['data'])

    def check_revision(self, obj, payload):
        if not isinstance(payload.get('revision'),int) or isinstance(payload.get('revision'),bool): raise ApiError(400,'기준 revision이 필요합니다.')
        if payload['revision'] != obj['revision']: raise ApiError(409,'다른 수정이 먼저 저장되었습니다. 새로고침 후 변경을 확인하세요.',{'current':obj})

    def validate_record(self, project_id, r, exists=None):
        if not isinstance(r.get('type'),str) or r['type'] not in TYPES: raise ApiError(400,'지원하지 않는 기록 유형입니다.')
        if not isinstance(r.get('category'),str) or r['category'] not in {'A','B','C','D','E','F'}: raise ApiError(400,'카테고리를 A~F 중 선택하세요.')
        if not isinstance(r.get('status'),str) or r['status'] not in STATUSES or not isinstance(r.get('review_state'),str) or r['review_state'] not in REVIEWS: raise ApiError(400,'유효하지 않은 상태입니다.')
        if not isinstance(r.get('title'),str) or not r['title'].strip() or len(r['title'])>300: raise ApiError(400,'제목을 1~300자로 입력하세요.')
        for field in ('content','purpose','acceptance','input_version','output_spec','owner','reviewer','due_date'):
            if not isinstance(r.get(field,''),str) or len(r.get(field,''))>150000: raise ApiError(400,field+' 입력이 올바르지 않습니다.')
        if any(len(r.get(field,''))>200 for field in ('owner','reviewer')): raise ApiError(400,'담당자·검토자는 200자 이하로 입력하세요.')
        for field in ('archived','is_sample'):
            if not isinstance(r.get(field),bool): raise ApiError(400,field+'는 참/거짓이어야 합니다.')
        if r.get('due_date'):
            try:
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',r['due_date']): raise ValueError()
                date.fromisoformat(r['due_date'])
            except ValueError: raise ApiError(400,'기한은 실제 존재하는 YYYY-MM-DD 날짜여야 합니다.')
        if not isinstance(r.get('fields',{}),dict): raise ApiError(400,'fields는 객체여야 합니다.')
        for key in ('stage_ids','related_ids','prerequisites'):
            values=r.get(key,[])
            if not isinstance(values,list) or len(values)>500 or any(not isinstance(x,str) or not ID_RE.fullmatch(x) for x in values) or len(set(values))!=len(values): raise ApiError(400,key+'에 유효한 고유 ID 배열을 입력하세요.')
        stage_ids={s['id'] for s in self.list_json('stages',project_id)}
        if not set(r['stage_ids']).issubset(stage_ids): raise ApiError(400,'연결할 공정을 찾을 수 없습니다.')
        if exists is None: exists={x['id']:x for x in self.list_json('records',project_id)}
        for key in ('related_ids','prerequisites'):
            for rid in r.get(key,[]):
                if rid==r['id'] or rid not in exists: raise ApiError(400,'유효한 같은 프로젝트의 다른 기록만 연결할 수 있습니다.')
        for rid in r['prerequisites']:
            if exists[rid]['type']!='task': raise ApiError(400,'선행 작업에는 작업 유형만 연결하세요.')
        checklist=r.get('checklist',[])
        if not isinstance(checklist,list) or len(checklist)>500 or any(not isinstance(c,dict) or not isinstance(c.get('text'),str) or len(c['text'])>2000 or not isinstance(c.get('done'),bool) for c in checklist): raise ApiError(400,'체크리스트 형식이 올바르지 않습니다.')
        revisions=r.get('input_revisions',{})
        if not isinstance(revisions,dict) or any(k not in exists or type(v)!=int or v<1 for k,v in revisions.items()): raise ApiError(400,'입력 revision 참조가 올바르지 않습니다.')
        source=r.get('source')
        if source is not None:
            if not isinstance(source,dict) or any(not isinstance(source.get(k,''),str) for k in ('id','url')) or any(source.get(k) is not None and not isinstance(source[k],str) for k in ('read_at','modified_at') if k in source) or not isinstance(source.get('warnings',[]),list): raise ApiError(400,'원본 출처 형식이 올바르지 않습니다.')
            if source.get('url'):
                try: u=urlsplit(source['url'])
                except ValueError: raise ApiError(400,'원본 링크가 올바르지 않습니다.')
                if u.scheme!='https' or not u.hostname or u.username or u.password: raise ApiError(400,'원본 링크는 인증정보 없는 HTTPS 주소여야 합니다.')
        # Reject cycles in task prerequisite graph.
        graph={key:value.get('prerequisites',[]) for key,value in exists.items()}
        graph[r['id']]=r['prerequisites']
        visited=set()
        def visit(key, stack):
            if key in stack: raise ApiError(400,'선행 작업의 순환 참조는 허용하지 않습니다.')
            if key in visited: return
            for parent in graph.get(key,[]): visit(parent,stack|{key})
            visited.add(key)
        visit(r['id'],set())
        if len(canonical(r).encode())>600000: raise ApiError(413,'기록이 너무 큽니다.')

    def create_record(self, project_id, payload, actor, record_id=None):
        if not isinstance(payload,dict): raise ApiError(400,'객체 형식의 입력이 필요합니다.')
        with self.lock:
            r=dict(id=uid('rec'),type='document',title='',category='A',stage_ids=[],status='draft',owner='',reviewer='',due_date='',content='',purpose='',input_version='',prerequisites=[],acceptance='',output_spec='',related_ids=[],checklist=[],fields={},review_state='not_submitted',revision=1,archived=False,is_sample=False,source=None,input_revisions={},created_at=utc_now(),updated_at=utc_now())
            if record_id is not None:
                if not isinstance(record_id,str) or not ID_RE.fullmatch(record_id): raise ApiError(400,'기록 ID 형식이 올바르지 않습니다.')
                if self.db.execute('SELECT 1 FROM records WHERE id=?',(record_id,)).fetchone(): raise ApiError(409,'이미 존재하는 기록 ID입니다.')
                r['id']=record_id
            allowed=set(r)-{'id','revision','created_at','updated_at'}
            r.update({k:clone(v) for k,v in payload.items() if k in allowed})
            if isinstance(r.get('fields'),dict) and 'instruction_approval' in r['fields']: raise ApiError(400,'지시 승인 정보는 명시적인 승인 동작으로만 기록합니다.')
            if r['status']=='done' or r['review_state']=='approved': raise ApiError(400,'완료·승인은 명시적인 검토 동작을 통해 기록하세요.')
            self.validate_record(project_id,r)
            self.db.execute('INSERT INTO records VALUES(?,?,?)',(r['id'],project_id,canonical(r)))
            self.event(project_id,'created',r['id'],actor,'기록 생성')
            return r

    def update_record(self, project_id, record_id, payload, actor):
        with self.lock:
            current=self.get_record(project_id,record_id)
            self.check_revision(current,payload)
            r=clone(current)
            allowed=set(r)-{'id','revision','created_at','updated_at','review_state','status'}
            r.update({k:clone(v) for k,v in payload.items() if k in allowed})
            if isinstance(r.get('fields'),dict) and r['fields'].get('instruction_approval') != current.get('fields',{}).get('instruction_approval'):
                raise ApiError(400,'지시 승인 이력은 직접 수정할 수 없습니다.')
            if 'review_state' in payload and payload['review_state']!=current['review_state']: raise ApiError(400,'검토 상태는 검토 요청·승인·반려 동작으로 변경하세요.')
            if 'status' in payload:
                if payload['status']=='done' and current['status']!='done': raise ApiError(400,'완료 처리는 검토 승인이 필요합니다.')
                r['status']=payload['status']
            substantive=any(r.get(k)!=current.get(k) for k in ('type','category','stage_ids','reviewer','owner','title','content','purpose','input_version','related_ids','prerequisites','acceptance','output_spec','checklist','fields','input_revisions'))
            if substantive and isinstance(r.get('fields'),dict):
                r['fields'].pop('instruction_approval',None)
            if current['review_state']=='approved' and substantive:
                r['review_state']='not_submitted'
                r['status']='in_progress'
            self.validate_record(project_id,r)
            r.update(revision=current['revision']+1,updated_at=utc_now())
            self.db.execute('UPDATE records SET data=? WHERE id=? AND project_id=?',(canonical(r),record_id,project_id))
            self.event(project_id,'updated',record_id,actor,'승인 후 변경: 재검토 필요' if substantive and current['review_state']=='approved' else '기록 수정',{'previous_revision':current['revision']})
            return r

    def approve_instructions(self, project_id, record_id, payload, actor):
        """Approve the work brief independently from technical or final review."""
        with self.lock:
            r=self.get_record(project_id,record_id)
            self.check_revision(r,payload)
            if r['type']!='task' or r['archived']: raise ApiError(400,'보관되지 않은 작업의 지시만 승인할 수 있습니다.')
            reason=payload.get('reason','')
            if not isinstance(reason,str) or not reason.strip() or len(reason)>10000: raise ApiError(400,'지시 승인 이유를 입력하세요.')
            if any(not r[k].strip() for k in ('content','acceptance','output_spec','input_version','reviewer')):
                raise ApiError(400,'지시문·완료 조건·출력 규격·입력 버전·검토자를 먼저 작성하세요.')
            stale=[k for k,v in r.get('input_revisions',{}).items() if self.get_record(project_id,k)['revision']!=v]
            if stale: raise ApiError(409,'오래된 입력 버전은 지시 승인할 수 없습니다.',{'stale_inputs':stale})
            r['revision']+=1
            at=utc_now()
            r['updated_at']=at
            r['fields']['instruction_approval']=dict(actor=actor,at=at,created_at=at,reason=reason.strip(),source_revision=r['revision'])
            self.db.execute('UPDATE records SET data=? WHERE id=? AND project_id=?',(canonical(r),record_id,project_id))
            self.event(project_id,'instructions_approved',record_id,actor,reason,{'revision':r['revision'],'separate_from_result_review':True})
            return r

    def review_record(self, project_id, record_id, payload, actor):
        with self.lock:
            r=self.get_record(project_id,record_id)
            self.check_revision(r,payload)
            action=payload.get('action')
            reason=payload.get('reason','')
            if action not in ('submit','approve','reject'): raise ApiError(400,'검토 동작을 선택하세요.')
            if not isinstance(reason,str) or not reason.strip() or len(reason)>10000: raise ApiError(400,'검토 이유를 입력하세요.')
            if r['archived']: raise ApiError(400,'보관된 기록은 먼저 복구하세요.')
            if action=='submit':
                if not r['reviewer'].strip(): raise ApiError(400,'검토자를 지정하세요.')
                r.update(review_state='pending',status='pending')
            else:
                if r['review_state']!='pending': raise ApiError(409,'검토 대기 상태에서만 승인·반려할 수 있습니다.')
                if action=='approve':
                    if not r['acceptance'].strip(): raise ApiError(400,'완료 조건을 먼저 작성하세요.')
                    if any(not c['done'] for c in r['checklist']): raise ApiError(400,'미완료 체크리스트를 확인하세요. 체크 자체가 승인은 아닙니다.')
                    if any(self.get_record(project_id,key)['status']!='done' for key in r['prerequisites']): raise ApiError(400,'완료되지 않은 선행 작업이 있습니다.')
                    stale=[k for k,v in r.get('input_revisions',{}).items() if self.get_record(project_id,k)['revision']!=v]
                    if stale: raise ApiError(409,'입력 버전이 변경되었습니다. 기준 revision을 확인하고 다시 제출하세요.',{'stale_inputs':stale})
                    r.update(review_state='approved',status='done')
                else: r.update(review_state='changes_requested',status='in_progress')
            r.update(revision=r['revision']+1,updated_at=utc_now())
            self.db.execute('UPDATE records SET data=? WHERE id=?',(canonical(r),record_id))
            self.event(project_id,'review_'+action,record_id,actor,reason,{'revision':r['revision'],'review_state':r['review_state']})
            return r

    def event(self, project_id, kind, record_id, actor, reason, details=None):
        e=dict(id=uid('event'),kind=kind,record_id=record_id,actor=actor,created_at=utc_now(),reason=reason,details=details or {})
        self.db.execute('INSERT INTO events VALUES(?,?,?)',(e['id'],project_id,canonical(e)))
        return e

    def instantiate(self, project_id, template_id, payload, actor):
        t=next((clone(t) for t in TEMPLATES if t['id']==template_id),None)
        if not t: raise ApiError(404,'템플릿을 찾을 수 없습니다.')
        t.pop('id')
        t['title']=payload.get('title') or t['title']+' · 새 기록'
        t['category']=payload.get('category',t['category'])
        t['stage_ids']=payload.get('stage_ids',t.get('stage_ids',[]))
        t['fields']={**t.get('fields',{}),'template_id':template_id,'template_revision':1,'template_kind':'제안 템플릿'}
        return self.create_record(project_id,t,actor)

    def update_stage(self, project_id, stage_id, payload, actor):
        with self.lock:
            stages=self.list_json('stages',project_id)
            s=next((s for s in stages if s['id']==stage_id),None)
            if not s: raise ApiError(404,'공정을 찾을 수 없습니다.')
            self.check_revision(s,payload)
            for key in ('name','input','role','output','criteria','description','recommended_tools','subtasks','next','rework'):
                if key in payload:
                    value=payload[key]
                    if key in ('next','rework'):
                        if value is not None and value not in {x['id'] for x in stages}: raise ApiError(400,'다음·재작업 단계 ID를 확인하세요.')
                    elif not isinstance(value,str) or len(value)>10000: raise ApiError(400,'공정 입력이 올바르지 않습니다.')
                    s[key]=value
            if not s['name'].strip(): raise ApiError(400,'공정 이름을 입력하세요.')
            s['revision']+=1
            self.db.execute('UPDATE stages SET data=? WHERE id=? AND project_id=?',(canonical(s),stage_id,project_id))
            self.event(project_id,'stage_updated',stage_id,actor,'초기 공정 제안 수정')
            return s

    def backup(self, project_id):
        with self.lock:
            data={'project':self.project(project_id),**{key:self.list_json(key,project_id) for key in ('records','stages','events','imports')}}
            return dict(schema_version='animation-backup-v1',created_at=utc_now(),sha256=hashlib.sha256(canonical(data).encode()).hexdigest(),data=data)

    def validate_backup(self, project_id, backup):
        if not isinstance(backup,dict) or backup.get('schema_version')!='animation-backup-v1' or not isinstance(backup.get('data'),dict): raise ApiError(400,'지원하지 않는 백업 형식입니다.')
        d=backup['data']
        if hashlib.sha256(canonical(d).encode()).hexdigest()!=backup.get('sha256'): raise ApiError(400,'백업 무결성 해시가 일치하지 않습니다.')
        p=d.get('project')
        if not isinstance(p,dict) or p.get('id')!=project_id: raise ApiError(409,'현재 프로젝트의 백업만 복원할 수 있습니다. 해당 프로젝트로 전환하세요.')
        if type(p.get('revision'))!=int or p['revision']<1 or not isinstance(p.get('name'),str) or not p['name'].strip() or len(p['name'])>200: raise ApiError(400,'백업의 프로젝트 설정이 올바르지 않습니다.')
        try: ZoneInfo(p.get('timezone',''))
        except (ZoneInfoNotFoundError,ValueError,TypeError): raise ApiError(400,'백업 시간대가 올바르지 않습니다.')
        for key in ('records','stages','events','imports'):
            if not isinstance(d.get(key),list) or len(d[key])>100000 or any(not isinstance(r,dict) or not isinstance(r.get('id'),str) or not ID_RE.fullmatch(r['id']) for r in d[key]) or len({r['id'] for r in d[key]})!=len(d[key]): raise ApiError(400,'백업의 '+key+' 목록 또는 ID가 올바르지 않습니다.')
        ids={r['id']:r for r in d['records']}
        for r in d['records']:
            if not isinstance(r.get('revision'),int) or r['revision']<1: raise ApiError(400,'백업 revision이 올바르지 않습니다.')
            self.validate_record(project_id,r,ids)
            collision=self.db.execute('SELECT project_id FROM records WHERE id=?',(r['id'],)).fetchone()
            if collision and collision[0]!=project_id: raise ApiError(409,'백업 ID가 다른 프로젝트의 기록과 충돌합니다.')
        if {s['id'] for s in d['stages']}!={s['id'] for s in self.list_json('stages',project_id)}: raise ApiError(400,'백업 공정 ID가 현재 계약과 다릅니다.')
        for s in d['stages']:
            if type(s.get('revision'))!=int or s['revision']<1 or not isinstance(s.get('name'),str): raise ApiError(400,'백업 공정이 올바르지 않습니다.')
            if any(not isinstance(s.get(k),str) for k in ('input','role','output','criteria','description')) or any(s.get(k) is not None and s[k] not in {x['id'] for x in d['stages']} for k in ('next','rework')): raise ApiError(400,'백업 공정의 절차 또는 연결이 올바르지 않습니다.')
            if any(not isinstance(s.get(k,''),str) or len(s.get(k,''))>10000 for k in ('recommended_tools','subtasks')): raise ApiError(400,'백업 공정의 도구 후보·세부 절차가 올바르지 않습니다.')
        for e in d['events']:
            if any(not isinstance(e.get(k),str) for k in ('kind','actor','created_at','reason')) or not isinstance(e.get('details'),dict): raise ApiError(400,'백업 검토·수정 이력이 올바르지 않습니다.')
        for table in ('events','imports'):
            for e in d[table]:
                collision=self.db.execute('SELECT project_id FROM '+table+' WHERE id=?',(e['id'],)).fetchone()
                if collision and collision[0]!=project_id: raise ApiError(409,'백업의 이력 ID가 다른 프로젝트와 충돌합니다.')
        # Receipts carry idempotency and safe media. Validate them as data, not
        # merely as a list, before replacing any live project state.
        receipts={entry['id']:entry for entry in d['imports']}
        for entry in d['imports']:
            kind=entry.get('kind')
            if kind in ('request','result'):
                from .contracts import validate_package, ContractError, fingerprint
                package=entry.get('package')
                try: validate_package(package,'work_'+kind)
                except ContractError as exc: raise ApiError(400,'백업 패키지 검증 실패: '+str(exc))
                if package['package_id']!=entry['id'] or package['project_id']!=project_id or fingerprint(package)!=entry.get('fingerprint'): raise ApiError(400,'백업 패키지 식별자·프로젝트·지문이 일치하지 않습니다.')
                if kind=='result':
                    request=receipts.get(package['request_package_id'])
                    if not request or request.get('kind')!='request': raise ApiError(400,'백업 결과 패키지의 원본 요청이 누락되었습니다.')
                    created=entry.get('created_ids')
                    if not isinstance(created,list) or any(not isinstance(key,str) or key not in ids for key in created) or len(created)!=len(set(created)): raise ApiError(400,'백업 결과 패키지의 생성 기록 연결이 올바르지 않습니다.')
            elif kind=='notion':
                items=entry.get('items')
                if not isinstance(items,list) or any(not isinstance(item,dict) for item in items): raise ApiError(400,'백업의 이관 대응표가 올바르지 않습니다.')
                seen=set()
                for item in items:
                    source_id=item.get('source_id')
                    rid=item.get('record_id')
                    if not isinstance(source_id,str) or source_id in seen or not isinstance(rid,str) or rid not in ids or not isinstance(item.get('content_hash'),str) or not re.fullmatch(r'[0-9a-f]{64}',item['content_hash']) or type(item.get('revision'))!=int or item['revision']<1: raise ApiError(400,'백업 이관 원본 ID·해시·revision이 올바르지 않습니다.')
                    if (ids[rid].get('source') or {}).get('id')!=source_id: raise ApiError(400,'백업의 이관 원본 대응이 일치하지 않습니다.')
                    seen.add(source_id)
            else:
                raise ApiError(400,'지원하지 않는 백업 가져오기 이력입니다.')
        return dict(valid=True,project_name=d['project']['name'],records=len(d['records']),events=len(d['events']),warning='현재 프로젝트의 기록·공정·이력을 백업 시점으로 교체합니다.')

    def restore(self, project_id, backup, actor, confirmation):
        if confirmation!='복원': raise ApiError(400,'복원 확인 문구가 필요합니다.')
        with self.lock:
            result=self.validate_backup(project_id,backup)
            d=backup['data']
            # A safety copy is retained on disk before destructive replacement.
            current=self.backup(project_id)
            dbpath=Path(self.db.execute('PRAGMA database_list').fetchone()[2])
            safety=dbpath.parent/'recovery'
            safety.mkdir(exist_ok=True)
            safety_path=safety/(uid('before_restore')+'.json')
            safety_path.write_text(canonical(current),encoding='utf-8')
            self.db.execute('BEGIN IMMEDIATE')
            try:
                for table in ('records','stages','events','imports'):
                    self.db.execute('DELETE FROM '+table+' WHERE project_id=?',(project_id,))
                    for r in d[table]: self.db.execute('INSERT INTO '+table+' VALUES(?,?,?)',(r['id'],project_id,canonical(r)))
                self.db.execute('UPDATE projects SET data=? WHERE id=?',(canonical(d['project']),project_id))
                self.event(project_id,'backup_restored',None,actor,'사용자가 백업 복원 확인',{'records':result['records'],'safety_copy':safety_path.name})
                self.db.execute('COMMIT')
            except Exception:
                self.db.execute('ROLLBACK')
                raise
            return result
