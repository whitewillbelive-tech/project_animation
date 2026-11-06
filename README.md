# AI 애니메이션 · 제작 관리실

구약 포로기 모티프를 검토하는 「프로젝트 원피스」의 제작 **관리 앱**이다. 기존 ONE PIECE 작품의 캐릭터·세계관·브랜딩은 사용하지 않는다. 작품 내용·화풍·길이·일정·예산·도구 채택은 미정이다. 렌더링이나 AI 모델을 실행하는 워크스테이션은 별도다.

## 실행

Python 3.12 이상이 필요하다. 새 worktree 없이 기존 체크아웃에서 실행한다.

```sh
cd /workspace/project_animation
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock
.venv/bin/python -m app.server --init-owner 영고
# 12자 이상의 본인 암호를 터미널에 입력한다. 기본 암호는 없다.
.venv/bin/python -m app.server --host 127.0.0.1 --port 8000
```

이미 `jsonschema==4.26.0`이 설치된 현재 환경에서는 `python3 -m app.server`로도 실행할 수 있다. **이 명령은 원격 클라우드 환경 안에서 실행된다. `http://127.0.0.1:8000`은 그 환경 내부의 개발 주소이며 사용자 PC에서 접속할 Site 주소가 아니다.** 소유자 생성 전에는 로그인할 수 없다. 암호는 scrypt 해시만 저장한다. 초기화 명령은 기존 계정을 덮어쓰지 않는다. 개발 프로세스가 다음 대화까지 유지된다고 가정하지 않는다.

**Sites 배포는 수행하지 않았으며 공개 접속 주소는 없다.** 현재 세션에 Sites 개발·배포 도구가 없다. 운영하려면 비공개 호스팅, HTTPS 리버스 프록시, 영구 볼륨을 마련하고 `ANIMATION_PUBLIC_ORIGIN`을 실제 HTTPS 출처로 지정한다. HTTPS 출처에서는 Secure 쿠키를 사용한다. 루프백 바인딩을 기본으로 하며, 다중 사용자 초대·역할 부여는 아직 지원하지 않는다.

현재 접속·배포 차단 단계와 재검증 결과는 [접속 상태 보고서](docs/ACCESS_STATUS.md)에 기록한다. 환경 게시와 관리 Site의 등록·배포는 별개다.

`.env.example`은 변수 문서다. `.env`를 자동으로 읽지는 않는다. 필요한 변수는 실행 프로세스에 주입한다. `ANIMATION_DATA_DIR` 기본값은 저장소의 `data/`다. SQLite와 복원 전 안전 사본은 이 디렉터리에 저장되고 Git에서 제외된다. 앱을 종료해도 기록은 유지되지만 프로세스는 재시작해야 한다.

## 기능

- 여섯 카테고리와 별도 축인 8개 수정 가능한 초기 공정. 실제 작업/검토/차단 집계, 작업 미등록 표시.
- 문서 및 15종 반복 기록의 생성·조회·수정·보관·복구. 검색/필터/칸반, 회차·장면·숏 및 양방향 관련 항목 탐색.
- 목적·담당자·검토자·기한·선행 작업·입력 버전·완료 조건·출력 규격·절차·체크리스트·유형별 메타데이터.
- 제안 템플릿 독립 복제. 사실/원문 해석/각색/AI 제안을 구분한다. 도구 후보와 AI 역할 정의는 실제 채택·연결·고용 상태가 아니다.
- 명시적 검토 요청·승인·반려. 수행자/이유/시각/개정 이력을 보존한다. 체크나 실행 성공만으로 승인하지 않으며 승인 후 기준 변경은 재검토가 필요하다.
- 서버 인증·프로젝트 소유권 검사·CSRF·revision 충돌·입력 검증·백업 미리보기/복원 및 안전 사본.
- Notion Markdown/CSV 파일 이관 미리보기·중복 방지·수정 충돌. 실제 Notion 원본 이관은 미완료다.
- 계약 v1 JSON 작업 요청 다운로드와 결과 미리보기→충돌 확인→가져오기. fixture는 샘플로 표시하고 실제 집계에서 제외한다. 워크스테이션은 미연결이다.

## 검증

```sh
python3 -m unittest discover -s tests -v
```

테스트는 임시 데이터베이스를 사용한다. 선택적인 브라우저 테스트에는 Python `playwright`와 Chromium이 필요하다. `requirements-dev.lock`으로 개발 의존성을 설치한다(`python3 -m pip install --require-hashes -r requirements-dev.lock`). `python3 tests/browser_smoke.py`는 실제 서버에서 로그인·저장·재접속·관계·검토·백업·모바일·키보드를 확인한다. 현재 브라우저 테스트는 Linux의 `/usr/bin/chromium` 경로를 사용한다. 테스트 자료는 기본 프로젝트에 넣지 않는다. [실행 결과](docs/TEST_RESULTS.md)에서 통과/미검증 범위를 구분한다.

## 인수인계

- [데이터 모델](docs/DATA_MODEL.md)
- [GitHub 배포 인수인계](docs/DEPLOYMENT_HANDOFF.md)
- [DB 스키마·초기화·이관 안내](docs/DATABASE.md)
- [Notion 이관 보고서](docs/MIGRATION.md): 원본 내용 미확인·미이관
- [개발 백로그](docs/BACKLOG.md)
- [워크스테이션 계약·fixture](workstation-handoff/README.md)

다음 워크스테이션 개발에는 `workstation-handoff/` 전체를 전달한다. 사용자 미디어·비밀키·개인정보는 포함하지 않는다. GitHub 소스 인계는 별도 인계 브랜치로 수행한다. 환경/사이트 게시, 외부 도구 가입/결제, 유료 AI 호출, 팀 초대와 추가 워크스테이션 개발은 수행하지 않았다.
