# GitHub 소스 인수인계 · AI 애니메이션

이 문서는 **전체 관리 앱 소스의 GitHub 인계**를 위한 것이다. Sites 등록·배포, 외부 접속 개통, 워크스테이션 개발은 이번 작업에 포함하지 않는다. 원격 개발 환경 내부의 localhost 실행을 배포 성공으로 표현하지 않는다. 실제 접속·배포 차단 상태는 [ACCESS_STATUS.md](ACCESS_STATUS.md)에 기록한다.

## 저장소와 Git 상태

- 대상: `https://github.com/whitewillbelive-tech/project_animation`. origin의 fetch와 push URL을 모두 확인했다.
- 작업 시작 시: 로컬 `work` 브랜치, HEAD `6dd4c31da5b24490bea9b643d14bb4167e582029` (Initial commit). 추적 파일은 README 하나, README는 수정 상태, 실제 앱·문서·테스트는 미추적 상태였다. fetch 후 원격 main도 같은 초기 커밋이었다. 인계 전 로컬에만 있는 추가 커밋은 없었다.
- 인계 브랜치: `handoff/animation-management-mvp`. 기존 `work`와 기존 파일을 보존하여 분기한다. main을 reset/강제 push/자동 병합하지 않는다.
- 정확한 인계 커밋은 해당 브랜치에서 `git rev-parse HEAD`로 확인한다. 자기 자신을 가리키는 SHA를 파일에 하드코딩하지 않는다. push 후 원격 ref와 파일 트리를 별도로 조회하여 로컬 커밋과 일치하는지 확인한다.

## 기술 구성

| 구분 | 구성 |
| --- | --- |
| 프런트엔드 | HTML, CSS, Vanilla JavaScript ES module. React/Vue 등 프레임워크·Node 서버·번들러 없음. `web/index.html`, `web/app.js`, `web/styles.css` |
| 백엔드 | CPython 3.12, 표준 `http.server.ThreadingHTTPServer`/`BaseHTTPRequestHandler`. `app/server.py`가 정적 UI와 인증 JSON API를 제공 |
| 검증 런타임 | Python 3.12.14 / Linux x86_64. Node.js는 JS 문법 검사에만 사용하며 앱 실행 의존성이 아님 |
| 운영 의존성 | `jsonschema==4.26.0` 및 전이 의존성. `requirements.txt`는 직접 의존성, `requirements.lock`은 총 6개 버전·SHA-256 해시 고정 |
| 개발 의존성 | `requirements-dev.txt`, `requirements-dev.lock` (운영 포함 총 9개, Playwright 1.62.0 포함). 브라우저 실행 파일은 Python 패키지에 포함되지 않음 |
| DB | Python sqlite3 / SQLite WAL. 서버 디스크에 저장, localStorage를 공용 기록 저장소로 사용하지 않음 |
| 인증 | 서버 소유자 계정, scrypt 암호 해시, HttpOnly/SameSite Strict 세션 쿠키, CSRF 검사, 프로젝트 소유권 확인. 현재는 소유자 전용 |
| 초기 스키마 | `db/migrations/0001_initial.sql`. 현재 Store 초기 스키마와 정확히 일치하는 데이터 없는 SQL |

## 인계에 포함한 파일

- `app/`: 인증, SQLite 도메인 저장, HTTP API, Notion 파일 가져오기, 기존 패키지 계약·입출력 로직.
- `web/`: 실제 동작하는 한국어 UI. 대시보드·공정·기록·편집·검토·관계·백업·가져오기.
- `requirements.txt`, `requirements.lock`, `requirements-dev.txt`, `requirements-dev.lock`: 직접/전이 의존성 및 해시 잠금. npm 패키지를 사용하지 않으므로 package.json/npm 잠금 파일은 없다.
- `db/migrations/0001_initial.sql`, `docs/DATABASE.md`: 스키마, 자동 초기화, 향후 마이그레이션의 한계와 데이터 보존 안내.
- `tests/`: 단위·실제 HTTP·계약·파일 이관 테스트와 실제 Chromium 브라우저 검사.
- `workstation-handoff/`: 이미 구현된 관리 앱 계약 v1, JSON Schema, 합성 정상/오류 fixture, 기본 사용 조건. 이번에 워크스테이션 개발을 추가한 것이 아니다.
- `README.md`, `.env.example`, `.gitignore`, `docs/`: 실행/접속/검증/데이터 모델/Notion 미이관/백로그 문서.

기본 공정, 가이드, AI 역할·도구 후보는 `app/store.py`에 제안 템플릿으로 포함한다. 작품 내용과 도구 채택을 확정하지 않으며 기존 Notion 양식을 읽어 이관한 것처럼 표시하지 않는다. fixture의 암호·가짜 키·1픽셀 PNG는 자동 검증용 합성 자료이며 실제 자격 증명이나 사용자 미디어가 아니다.

## 실행과 초기화

저장소를 받은 개발 환경의 루트에서 실행한다. 아래 명령은 개발용이며 사용자 PC에서 접근할 배포 URL을 만들지 않는다. 기존 작업 환경을 사용할 경우 새 worktree를 만들 필요가 없다.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python -m app.server --init-owner 영고
# 12자 이상 소유자 암호를 터미널에 입력. 실제 암호를 코드/명령 이력/문서에 쓰지 않는다.
.venv/bin/python -m app.server --host 127.0.0.1 --port 8000
```

초기 소유자 생성은 빈 DB에서만 한 번 수행하고 기존 계정을 덮어쓰지 않는다. Python의 getpass를 사용한다. 기본 암호와 실제 `.env`는 인계하지 않는다. `.env.example`은 변수명/비밀값 없는 예시이며 자동 로드되지 않는다. 필요하면 실행 프로세스에 `ANIMATION_DATA_DIR`을 지정한다. HTTPS 서비스 환경에서는 실제 비공개 출처를 `ANIMATION_PUBLIC_ORIGIN`에 지정한다. 임의 도메인을 설정해도 배포나 접근 권한이 생기지 않는다.

DB는 앱 시작 시 자동 생성되며 기존 스키마를 `CREATE ... IF NOT EXISTS`로 유지한다. `0001_initial.sql`은 최초 스키마 기록이지 현재 DB를 초기화하라는 지시가 아니다. 아직 버전 관리되는 마이그레이션 러너는 없다. 기존 데이터가 있는 환경에서는 먼저 [DATABASE.md](DATABASE.md)의 보존 절차를 따른다.

## 이번 인계 재현 검증

2026-11-06에 기존 환경의 전역 패키지에 의존하지 않는 새 임시 venv를 만들고 검증했다.

1. 운영 잠금 파일의 6개 패키지를 `pip install --require-hashes -r requirements.lock`으로 설치: 통과. 개발 잠금 파일의 9개 패키지도 같은 방식으로 설치: 통과. 두 잠금 파일의 운영 의존성 버전 일치 확인.
2. 새 venv에서 `python -m unittest discover -s tests -v`: **39개 통과**, 실패/건너뜀 없음. SQLite 재접속, HTTP 인증/CSRF/권한, revision 충돌, 관계, 승인, 백업/복원, 파일 이관, 기존 계약 검증 포함.
3. 초기 SQL과 런타임 Store로 별도 임시 DB 생성 후 sqlite_master 비교: 테이블 7개·명시 인덱스 2개 일치, SQL 재실행 가능, 데이터 0행, 두 DB 무결성 `ok`.
4. 새 venv에서 `python tests/browser_smoke.py`: **16개 흐름 통과**. 실제 HTTP 서버의 UI 저장·재접속·관계·검토·백업·모바일·키보드·기존 파일 패키지 교환과 오류 거부를 확인했다. 배포 주소에서 실행한 테스트는 아니다.
5. `node --check web/app.js`: 통과. 원격 push 검증의 최종 커밋과 파일 확인 결과는 인계 응답에 함께 제공한다. 브라우저 명령은 아래와 같다.

```sh
.venv/bin/python -m pip install --require-hashes -r requirements-dev.lock
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python tests/browser_smoke.py
```

브라우저 검사는 현재 Linux의 `/usr/bin/chromium`을 사용한다. 다른 OS의 Chromium 경로/시스템 패키지 설치는 미검증이다. 테스트 계정·DB는 임시 디렉터리에 만들고, 결과 JSON/스크린샷만 Git에서 제외한 `test-results/`에 남긴다. 기본 운영 DB를 테스트 자료로 덮어쓰지 않는다.

## 커밋하지 않은 데이터와 별도 이관

| 기존 위치 | 취급 및 남은 작업 |
| --- | --- |
| `/workspace/project_animation/data/` | 운영 SQLite 본체·WAL·SHM·복구 사본용 디렉터리. Git 제외. 현재 인스턴스 읽기 전용 검사에서 무결성 `ok`, 사용자/프로젝트/기록/세션 등 모든 테이블 0건. 다른 저장 경로·인스턴스의 데이터 유무를 뜻하지 않음 |
| `/workspace/preservation/site-access-20261106T090153Z/` | 이전 접속 진단 전 소스 보존 아카이브·SQLite 백업·체크섬. 그대로 보존, Git 제외 |
| `/workspace/attachments/1154b60a-e385-41b7-8c01-749cc5937703/` | 사용자가 올린 요청 원본 파일. 그대로 보존하며 커밋하지 않음. 필요 시 사용자가 별도 보안 경로로 전달 |
| `/workspace/project_animation/test-results/` | 합성 테스트 내보내기·백업·화면·검증 산출물. 배포용 데이터가 아니며 Git 제외 |
| 실제 `.env`, API 키·암호·세션·개인 자격 증명 | 인계본에 포함하지 않음. 필요한 환경에서 별도 보안 설정. 계정이나 세션 값을 프런트엔드로 전달하지 않음 |

현재 확인된 기본 DB는 비어 있으므로 옮길 실제 작업 기록은 없다. 향후 다른 환경의 실데이터가 발견되면 Git과 분리해 SQLite 온라인 백업 또는 검증된 앱 백업 방식으로 이관해야 한다. 실행 중인 DB 본체만 복사하고 WAL을 버리지 않는다. Notion 원본 내용은 여전히 미열람·미이관이며 별도 Markdown/CSV 자료 또는 정식 읽기 접근이 필요하다.

## 미검증과 배포 전 필수 사항

Sites 도구/프로젝트 조회/비공개 배포를 이번에 시도하지 않았다. 실제 배포 URL, 플랫폼 인증 연동, 비공개 접근, HTTPS 프록시, 영구 볼륨, 운영 부하/복구와 다중 사용자 협업은 검증하지 않았다. 현재 표준 라이브러리 HTTP 서버를 운영 호스팅에 그대로 올려도 적합하다고 보증하지 않는다. 향후 지원되는 호스팅 절차에 맞춰 서비스 수명주기·영구 저장·인증을 검증해야 한다.

GitHub에 소스를 push한 사실과 Site를 배포한 사실은 별개다. 코드 인계 후에도 사용자 PC에서 원격 localhost에 접속할 수 있는 것은 아니다.
