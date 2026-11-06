# 데이터베이스 인수인계

현재 앱은 Python 표준 라이브러리 `sqlite3`를 사용한다. 기본 경로는 저장소 루트의 `data/animation.sqlite3`이며, `ANIMATION_DATA_DIR`을 지정하면 해당 디렉터리의 `animation.sqlite3`를 사용한다. 운영 환경에서는 소스와 분리된 비공개 영구 볼륨에 두어야 한다. 이 문서는 스키마와 보존 방법만 설명하며 실제 DB나 사용자 자료를 포함하지 않는다.

## 초기화와 마이그레이션 상태

[초기 스키마 SQL](../db/migrations/0001_initial.sql)은 현재 [Store](../app/store.py)의 테이블·인덱스 정의와 동일하다. 테이블 생성문과 연결 설정만 있으며 계정, 암호 해시, 세션, 프로젝트, 자료 행은 없다.

앱은 시작할 때 `Store`에서 `CREATE TABLE/INDEX IF NOT EXISTS`를 실행해 없는 스키마를 자동 생성한다. 운영자가 이 SQL을 별도로 실행할 필요는 없다. 빈 DB에 스키마만 생성해도 소유자나 프로젝트는 만들어지지 않는다. 소유자 초기화는 별도 CLI 동작이며, 기존 소유자 계정이 있으면 덮어쓰지 않는다.

**현재 스키마 변경은 버전 관리되는 마이그레이션 러너로 적용되지 않는다.** `schema_migrations` 테이블이나 `PRAGMA user_version` 갱신도 없다. `0001`은 초기 스키마를 기록하는 파일 번호이며 실제 DB에 적용됐다는 버전 표식이 아니다. `CREATE ... IF NOT EXISTS`는 이미 존재하는 테이블의 열·제약을 변경하지 않는다. 이후 스키마 변경 시에는 기존 DB 백업, 적용 버전 기록, 순차 이관과 실패 복구 절차를 먼저 설계해야 한다. 기존 DB 삭제·초기화·새 빈 DB로 교체하는 방식으로 업그레이드하지 않는다.

이 인수인계에서는 기존 DB를 초기화하거나 가져오지 않았고 데이터 행도 수정하지 않았다.

## 테이블

| 테이블 | 기본 키 | 보관하는 정보 |
| --- | --- | --- |
| `users` | `id` | 소유자 이름, scrypt 암호 해시, 역할. 이름은 고유하다. |
| `sessions` | `token` | 원문 세션 토큰의 SHA-256, 사용자 ID, CSRF 토큰, 선택 프로젝트 ID, 만료 시각. |
| `projects` | `id` | 소유자 ID와 프로젝트 설정 JSON. |
| `records` | `id` | 프로젝트 ID와 문서·작업 등 기록 JSON. |
| `stages` | `(id, project_id)` | 프로젝트별 공정 JSON. |
| `events` | `id` | 수정·지시 승인·검토·이관 등의 감사 이력 JSON. |
| `imports` | `id` | Notion 파일 이관 대응표, 워크스테이션 요청·결과 패키지, 지문과 선택한 검토 미디어. |

명시적 조회 인덱스는 `records_project(project_id)`, `events_project(project_id)` 두 개다. 기본 키와 사용자 이름의 고유 제약에 필요한 SQLite 자동 인덱스도 생성된다. 프로젝트 접근 권한, 관계의 유효성, revision 충돌, 검토 전이는 서버 도메인 계층에서 검증한다. 연결에서 `foreign_keys=ON`을 설정하지만 현재 초기 스키마에는 `REFERENCES` 외래 키 제약을 선언하지 않았다.

JSON 필드의 구조와 데이터 소유권은 [데이터 모델](DATA_MODEL.md), 파일 패키지 계약은 [워크스테이션 인수인계](../workstation-handoff/README.md)를 따른다. JSON 안의 `revision`은 기록의 수정 기준이며 DB 스키마 버전과 다르다.

## 기존 자료 보존

- `data/` 전체와 `ANIMATION_DATA_DIR`의 실제 저장 경로는 비공개 데이터다. DB·WAL·SHM·복구 사본·소유자가 내보낸 백업을 Git이나 공개 정적 경로에 넣지 않는다.
- WAL 모드에서는 `animation.sqlite3-wal`에 아직 체크포인트되지 않은 최신 기록이 있을 수 있다. 실행 중인 DB 본체만 복사하거나 WAL·SHM 파일을 임의로 삭제하지 않는다. 일관된 물리 백업에는 SQLite 온라인 백업 API 또는 서비스를 정상 종료한 뒤 검증한 전체 스냅샷을 사용한다.
- 앱의 백업 내보내기는 선택한 프로젝트의 설정·기록·공정·감사 이력·가져오기 대응표를 포함한다. 인증 계정과 세션은 프로젝트 백업에 포함하지 않는다. 전체 운영 복구에는 프로젝트 백업 외에 보호된 SQLite 백업이 필요하다.
- 앱의 프로젝트 복원은 미리보기 검증과 `복원` 확인을 요구하고, 현재 프로젝트 데이터의 안전 사본을 `<데이터 디렉터리>/recovery/`에 남긴다. 복원은 기존 기록을 교체하므로 일반 배포·시작 절차로 실행하지 않는다.
- 배포 대상이 바뀌어도 기존 저장 경로와 파일을 먼저 확인한다. 현재 인스턴스에서 빈 DB가 관찰됐다는 사실은 다른 인스턴스나 다른 저장 경로에 자료가 없다는 뜻이 아니다.

## 초기 스키마 재현 검증

다음은 저장소 루트에서 실행하는 검증이다. 두 개의 임시 DB에 각각 SQL과 현재 `Store`를 적용하고 테이블·인덱스 정의, 빈 데이터, 무결성, SQL 재실행 가능성을 비교한다. 운영 DB를 읽거나 수정하지 않는다.

```sh
python3 - <<'PY'
import sqlite3
import tempfile
from pathlib import Path
from app.store import Store

sql = Path('db/migrations/0001_initial.sql').read_text(encoding='utf-8')

def schema(conn):
    return conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE type IN ('table', 'index') ORDER BY type, name"
    ).fetchall()

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    migrated = sqlite3.connect(root / 'migration.sqlite3')
    migrated.executescript(sql)
    runtime = Store(root / 'runtime.sqlite3')
    runtime.db.row_factory = None
    expected = schema(runtime.db)
    assert schema(migrated) == expected
    migrated.executescript(sql)
    assert schema(migrated) == expected
    tables = [r[1] for r in expected if r[0] == 'table']
    assert len(tables) == 7
    for table in tables:
        assert migrated.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0] == 0
    assert migrated.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert runtime.db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    runtime.db.close()
    migrated.close()
    print('PASS: 7 tables, 2 explicit indexes; exact runtime schema match; no rows; repeatable; integrity ok')
PY
```

2026-11-06에 위 명령을 실제 실행해 통과했다. 별도의 현재 인스턴스 DB 읽기 전용 점검 결과는 인수인계 보고에 기록하며 사용자 행 내용·암호 해시·세션 값은 남기지 않는다.
