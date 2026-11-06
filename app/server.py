"""Private owner-only development server. No rendering or remote tool execution."""
import argparse
import getpass
import importlib
import json
import mimetypes
import os
import secrets
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit
from .auth import new_token, token_hash, verify_password
from .store import Store, ApiError

ROOT = Path(__file__).resolve().parent.parent
MAX_BODY = 10 * 1024 * 1024
SESSION_SECONDS = 12 * 3600


class Handler(BaseHTTPRequestHandler):
    server_version = 'AnimationSite/1'

    @property
    def store(self):
        return self.server.store

    def log_message(self, fmt, *args):
        # Request paths may contain sensitive user-supplied data.
        pass

    def common_headers(self, content_type):
        self.send_header('Content-Type',content_type)
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; media-src 'self' blob: data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")

    def send_json(self, status, data, cookie=None):
        body=json.dumps(data,ensure_ascii=False,allow_nan=False).encode()
        self.send_response(status)
        self.common_headers('application/json; charset=utf-8')
        if cookie: self.send_header('Set-Cookie',cookie)
        self.send_header('Content-Length',str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _cookie(self, token, clear=False):
        secure='; Secure' if self.server.public_origin.startswith('https://') else ''
        return 'animation_session='+token+'; Path=/; HttpOnly; SameSite=Strict; Max-Age='+str(0 if clear else SESSION_SECONDS)+secure

    def _session(self):
        try:
            c=SimpleCookie(self.headers.get('Cookie',''))
            token=c.get('animation_session')
            if not token: return None
            row=self.store.db.execute('SELECT s.*,u.name,u.role FROM sessions s JOIN users u ON u.id=s.user_id WHERE token=? AND expires>?',(token_hash(token.value),time.time())).fetchone()
            return dict(row) if row else None
        except Exception:
            return None

    def _origin(self):
        host=self.headers.get('Host','')
        allowed={'127.0.0.1','localhost','[::1]'}
        public=urlsplit(self.server.public_origin)
        hostname=host.rsplit(':',1)[0] if ':' in host and not host.endswith(']') else host
        if hostname not in allowed and host!=public.netloc:
            raise ApiError(403,'허용되지 않은 요청 호스트입니다. ANIMATION_PUBLIC_ORIGIN 설정을 확인하세요.')
        origin=self.headers.get('Origin')
        if origin:
            request_origin='http://'+host
            if origin not in {request_origin,self.server.public_origin}: raise ApiError(403,'다른 출처의 요청은 허용하지 않습니다.')

    def _payload(self):
        if self.headers.get('Transfer-Encoding'): raise ApiError(400,'전송 인코딩은 지원하지 않습니다.')
        try: length=int(self.headers.get('Content-Length','0'))
        except ValueError: raise ApiError(400,'유효하지 않은 본문 길이입니다.')
        if length<0 or length>MAX_BODY: raise ApiError(413,'입력은 10MB 이하여야 합니다.')
        if length==0: return {}
        if not self.headers.get('Content-Type','').lower().startswith('application/json'): raise ApiError(415,'JSON 형식으로 전송하세요.')
        try: body=json.loads(self.rfile.read(length),parse_constant=lambda _: (_ for _ in ()).throw(ValueError('not finite')))
        except (ValueError,UnicodeDecodeError,RecursionError): raise ApiError(400,'JSON을 읽을 수 없습니다.')
        if not isinstance(body,dict): raise ApiError(400,'JSON 객체가 필요합니다.')
        return body

    def _login(self, body):
        peer=self.client_address[0]
        now=time.time()
        attempts=[t for t in self.server.login_attempts.get(peer,[]) if now-t<60]
        self.server.login_attempts[peer]=attempts
        if len(attempts)>=10: raise ApiError(429,'로그인 시도가 많습니다. 1분 후 다시 시도하세요.')
        attempts.append(now)
        name=body.get('username','')
        password=body.get('password','')
        if not isinstance(name,str) or not isinstance(password,str): raise ApiError(400,'이름과 암호를 입력하세요.')
        row=self.store.db.execute('SELECT * FROM users WHERE name=?',(name,)).fetchone()
        # Same expensive verification path for missing user names.
        encoded=row['password'] if row else self.server.dummy_password
        if not verify_password(password,encoded) or not row: raise ApiError(401,'이름 또는 암호를 확인하세요.')
        token,csrf=new_token(),new_token()
        project=self.store.db.execute('SELECT id FROM projects WHERE owner_id=? ORDER BY rowid LIMIT 1',(row['id'],)).fetchone()
        if not project: raise ApiError(500,'소유자 프로젝트가 없습니다.')
        with self.store.lock:
            old=self._session()
            if old: self.store.db.execute('DELETE FROM sessions WHERE token=?',(old['token'],))
            self.store.db.execute('DELETE FROM sessions WHERE expires<?',(now,))
            self.store.db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)',(token_hash(token),row['id'],csrf,project['id'],now+SESSION_SECONDS))
        self.server.login_attempts.pop(peer,None)
        self.send_json(200,dict(authenticated=True,user={'id':row['id'],'name':row['name'],'role':row['role']},csrf_token=csrf),cookie=self._cookie(token))

    def _dispatch(self, method):
        self._origin()
        path=unquote(urlsplit(self.path).path)
        if '\\' in path or '\x00' in path or '..' in path.split('/'): raise ApiError(400,'유효하지 않은 경로입니다.')
        if path=='/api/session' and method=='GET':
            s=self._session()
            return self.send_json(200,dict(authenticated=bool(s),user={'id':s['user_id'],'name':s['name'],'role':s['role']} if s else None,csrf_token=s['csrf'] if s else None))
        if path=='/api/login' and method=='POST': return self._login(self._payload())
        if not path.startswith('/api/'):
            if method!='GET': raise ApiError(405,'지원하지 않는 요청 방식입니다.')
            # UI contains no user data or secrets; all API calls require a session.
            relative='index.html' if path=='/' else path.lstrip('/')
            web=(ROOT/'web').resolve()
            target=(web/relative).resolve()
            if not target.is_relative_to(web) or not target.is_file() or target.suffix not in {'.html','.js','.css','.svg','.ico','.png','.webp','.woff2'}: raise ApiError(404,'페이지를 찾을 수 없습니다.')
            body=target.read_bytes()
            self.send_response(200)
            self.common_headers((mimetypes.guess_type(target.name)[0] or 'application/octet-stream')+('; charset=utf-8' if target.suffix in {'.html','.js','.css','.svg'} else ''))
            self.send_header('Content-Length',str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        s=self._session()
        if not s: raise ApiError(401,'로그인이 필요합니다.')
        self.user={'id':s['user_id'],'name':s['name'],'role':s['role']}
        self.project_id=s['project_id']
        self.store.authorize(self.user['id'],self.project_id)
        if method!='GET':
            if not secrets.compare_digest(self.headers.get('X-CSRF-Token',''),s['csrf']): raise ApiError(403,'요청 인증이 만료되었습니다. 다시 로그인하세요.')
            body=self._payload()
        else: body={}
        actor=self.user['name']
        parts=path.strip('/').split('/')
        if path=='/api/logout' and method=='POST':
            self.store.db.execute('DELETE FROM sessions WHERE token=?',(s['token'],))
            return self.send_json(200,{'ok':True},cookie=self._cookie('',True))
        if path=='/api/state' and method=='GET': return self.send_json(200,self.store.state(self.project_id,self.user['id']))
        if path=='/api/projects' and method=='POST':
            with self.store.atomic():
                p=self.store.create_project(self.user['id'],body.get('name',''))
                self.store.db.execute('UPDATE sessions SET project_id=? WHERE token=?',(p['id'],s['token']))
            return self.send_json(201,self.store.state(p['id'],self.user['id']))
        if path=='/api/projects/select' and method=='POST':
            project_id=body.get('id','')
            self.store.authorize(self.user['id'],project_id)
            self.store.db.execute('UPDATE sessions SET project_id=? WHERE token=?',(project_id,s['token']))
            return self.send_json(200,self.store.state(project_id,self.user['id']))
        if path=='/api/project' and method=='PATCH': return self.send_json(200,self.store.update_project(self.project_id,body,actor))
        if path=='/api/records' and method=='POST':
            with self.store.atomic(): r=self.store.create_record(self.project_id,body,actor)
            return self.send_json(201,r)
        if len(parts)==3 and parts[:2]==['api','records']:
            if method=='GET': return self.send_json(200,self.store.get_record(self.project_id,parts[2]))
            if method=='PATCH':
                with self.store.atomic(): r=self.store.update_record(self.project_id,parts[2],body,actor)
                return self.send_json(200,r)
        if len(parts)==4 and parts[:2]==['api','records'] and parts[3]=='review' and method=='POST':
            with self.store.atomic(): r=self.store.review_record(self.project_id,parts[2],body,actor)
            return self.send_json(200,r)
        if len(parts)==5 and parts[:2]==['api','records'] and parts[3:]==['instructions','approve'] and method=='POST':
            with self.store.atomic(): r=self.store.approve_instructions(self.project_id,parts[2],body,actor)
            return self.send_json(200,r)
        if path=='/api/templates' and method=='GET': return self.send_json(200,self.store.state(self.project_id)['templates'])
        if len(parts)==4 and parts[:2]==['api','templates'] and parts[3]=='instantiate' and method=='POST':
            with self.store.atomic(): r=self.store.instantiate(self.project_id,parts[2],body,actor)
            return self.send_json(201,r)
        if len(parts)==3 and parts[:2]==['api','stages'] and method=='PATCH':
            with self.store.atomic(): r=self.store.update_stage(self.project_id,parts[2],body,actor)
            return self.send_json(200,r)
        if path=='/api/backup' and method=='GET': return self.send_json(200,self.store.backup(self.project_id))
        if path=='/api/backup/preview' and method=='POST': return self.send_json(200,self.store.validate_backup(self.project_id,body.get('backup',body)))
        if path=='/api/backup/restore' and method=='POST': return self.send_json(200,self.store.restore(self.project_id,body.get('backup'),actor,body.get('confirmation')))
        for module in ('packages','migration'):
            try: extension=importlib.import_module('app.'+module)
            except ModuleNotFoundError as e:
                if e.name!='app.'+module: raise
                continue
            if extension.handle(self,method,path,body) is not False: return
        raise ApiError(404,'요청한 기능을 찾을 수 없습니다.')

    def _run(self, method):
        try:
            with self.store.lock: self._dispatch(method)
        except ApiError as e: self.send_json(e.status,{'error':e.message,'details':e.details})
        except (BrokenPipeError,ConnectionResetError): pass
        except Exception as e:
            # Never expose payloads, stack traces, filesystem paths or credentials.
            print('Request failed:',type(e).__name__,flush=True)
            self.send_json(500,{'error':'서버 처리에 실패했습니다. 입력과 서버 상태를 확인하세요.'})

    def do_GET(self): self._run('GET')
    def do_POST(self): self._run('POST')
    def do_PATCH(self): self._run('PATCH')
    def do_DELETE(self): self._run('DELETE')


def build_server(host='127.0.0.1',port=8000,data_dir=None):
    from .auth import password_hash
    data=Path(data_dir or os.environ.get('ANIMATION_DATA_DIR',ROOT/'data')).resolve()
    data.mkdir(parents=True,exist_ok=True)
    server=ThreadingHTTPServer((host,port),Handler)
    server.daemon_threads=True
    server.store=Store(data/'animation.sqlite3')
    server.public_origin=os.environ.get('ANIMATION_PUBLIC_ORIGIN','').rstrip('/')
    server.login_attempts={}
    server.dummy_password=password_hash(new_token())
    return server


def main():
    parser=argparse.ArgumentParser(description='AI 애니메이션 · 소유자 전용 관리 앱')
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8000)
    parser.add_argument('--init-owner',metavar='NAME')
    args=parser.parse_args()
    if args.init_owner:
        password=os.environ.get('ANIMATION_OWNER_PASSWORD') or getpass.getpass('소유자 암호 (12자 이상): ')
        store=Store(Path(os.environ.get('ANIMATION_DATA_DIR',ROOT/'data'))/'animation.sqlite3')
        try:
            store.init_owner(args.init_owner,password)
            print('소유자 계정과 빈 프로젝트를 생성했습니다. 암호 원문은 저장·출력하지 않습니다.')
        except (ApiError,ValueError) as e:
            parser.exit(1,str(e)+'\n')
        finally: store.db.close()
        return
    server=build_server(args.host,args.port)
    print('AI 애니메이션 관리 앱 실행 중 · '+args.host+':'+str(server.server_port),flush=True)
    if not server.store.db.execute('SELECT 1 FROM users').fetchone(): print('아직 소유자 계정이 없습니다. --init-owner NAME으로 초기화하세요.',flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close();server.store.db.close()


if __name__=='__main__': main()
