# 데이터 모델과 소유권

## 저장 경계

동일 출처 Python 서버와 SQLite WAL을 사용한다. users(암호 해시), sessions(세션 토큰 해시와 CSRF 토큰), projects(소유자), records, stages, events, imports를 저장한다. 인증 쿠키는 HttpOnly/SameSite Strict다. UI 번들에는 사용자 기록이나 비밀값이 없다. 서버가 세션과 프로젝트 소유권을 검증한다. 허용 역할은 owner 하나뿐이며 협업 초대나 다른 권한 부여는 아직 제공하지 않는다.

## 엔터티

문서 `document` 및 반복 유형 15종: 팀원·역할 `member`, 일정·작업 `task`, 회의록 `meeting`, 의사결정 `decision`, 예산·구독 `budget`, 인물 `character`, 회차 `episode`, 장면/숏 `scene`/`shot`, AI 작업자·도구 `tool`, 작업 매뉴얼 `manual`, 기술 테스트 `technical_test`, 에셋·결과물 `asset`, 생성 기록 `run`, 검토·수정 `review`, 자료 목록 `reference`다. 장면과 숏은 분리하되 원본 ID와 관계를 보존한다.

공통 필드: 안정적 문자열 `id`, 단조 증가 `revision`, `type`, `title`, `category`(A~F), 여러 `stage_ids`, `status`, `owner`, `reviewer`, `due_date`, `content`, `purpose`, `input_version`, `input_revisions`, `prerequisites`, `acceptance`, `output_spec`, `related_ids`, 독립 `checklist`, 확장 `fields`, `review_state`, `archived`, `is_sample`, 출처 `source`, UTC 생성·수정 시각. 유형별 메타데이터는 fields에 저장한다. 비용의 실제/추정/미상은 구분하며 결제를 의미하지 않는다.

관계는 related_ids로 저장하고 UI는 정방향과 역방향을 함께 표시한다. 회차→장면→숏은 해당 유형의 관계를 통해 탐색한다. 다른 프로젝트/존재하지 않는 ID, 순환 선행 작업을 거부한다. 보관은 삭제가 아니며 참조를 유지한다.

카테고리는 문서 분류, stage는 공정이다. 작업 없는 stage는 미등록이다. 실제 집계는 보관/샘플을 제외하고 승인 완료 건수와 전체 작업 수를 표시한다. 공정과 제안 템플릿은 확정 작품 내용이 아니다. 복제한 템플릿은 독립 스냅샷이며 기존 인스턴스에 변경을 소급하지 않는다.

## 상태와 충돌

기록 상태는 draft/todo/in_progress/blocked/pending/done, 검토 상태는 not_submitted/pending/changes_requested/approved다. 기술 실행은 별도 queued/running/succeeded/failed/cancelled/interrupted다. 결과 수신만으로 승인/완료를 바꾸지 않는다. 명시적 승인에 수행자·이유·시각을 남기고 완료 조건, 체크리스트, 선행 작업과 입력 revision을 확인한다. 승인된 내용·기준이 변경되면 재검토한다.

수정은 현재 revision이 필요하며 오래된 수정은 HTTP 409로 거부한다. imports가 이관/패키지 ID와 해시를 보존한다. 백업은 프로젝트/기록/공정/이력/이관 수신 내역과 SHA-256 무결성을 포함한다. 복원 전 안전 사본을 저장하고 트랜잭션으로 교체한다. 암호·세션은 백업하지 않는다.

Site는 기획·배정·기준·검토 결정의 원본, 워크스테이션은 로컬 파일·실행·기술 결과의 원본이다. 실시간 동기화나 원격 실행은 없다. 상세 계약은 `workstation-handoff/`에 고정한다.
