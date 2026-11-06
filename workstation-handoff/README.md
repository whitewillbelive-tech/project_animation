# Site–워크스테이션 인수인계 · 계약 1.0

구현 빌드: `animation-site-mvp-v1`. 이 디렉터리에는 사용자 원본 미디어, 비밀키, 개인 자료가 없다. 워크스테이션은 아직 없으며 Site 화면은 **미연결**을 표시한다. 유료 API, 로컬 모델, 원격 실행, 실시간 동기화는 구현하지 않는다.

## 파일

- `package.schema.json`: JSON Schema Draft 2020-12. `work_request`/`work_result` 공통 봉투와 필수 관계·형식.
- `fixtures/work_request.valid.json`: 승인된 지시의 요청 예제.
- `fixtures/work_result.valid.json`: 샘플 1픽셀 PNG를 포함한 성공 결과.
- `fixtures/work_result.metadata-only.json`: 미디어가 없는 메타데이터 결과.
- `fixtures/work_result.invalid-*.json`: 잘못된 스키마, 경로 이탈, 체크섬 불일치, 누락 파일, 중복 ID, 비밀키 필드의 실패 예제.
- `../app/contracts.py`: 스키마 + 의미·파일·비밀 정보 검사.
- `../app/packages.py`: Site 내보내기, 미리보기, 중복·충돌 판정, 원자적 가져오기, 인증된 미디어 응답.
- `../tests/test_packages.py`: 계약·왕복·승인 분리·복원 테스트.

## 소유권과 상태

Site는 기획, 문서, 작업 배정, 기준 revision, 사람의 검토 결정을 소유한다. 워크스테이션은 실행 도구·모델·설정, 파일, 실행 시각, 기술 결과를 소유한다. Site 결과 수신은 생성 기록과 에셋만 추가하며 원래 작업의 검토 상태·revision을 변경하지 않는다.

`job_state`: `queued/running/succeeded/failed/cancelled/interrupted`.

`review_state`: `not_submitted/pending/changes_requested/approved`.

두 상태는 서로 독립적이다. `succeeded`는 사람의 승인도, 도구 채택도, 작업 완료도 아니다. 가져온 생성 기록·에셋은 `not_submitted`로 시작한다. 체크리스트 완료도 승인과 별개다. 승인·반려에는 사용자 동작·이유·시각이 필요하다.

현재 MVP는 소유자만 접근할 수 있다. 협업 초대나 계정별 공유를 제공하지 않는다. 기록의 담당자/검토자는 문서상 배정이며 로그인 계정을 생성하지 않는다.

## 봉투와 필수 참조

모든 패키지에는 `schema_version`, `package_id`, `package_type`, `project_id`, `created_at`, `producer`, `target`이 있다. `schema_version`은 정확히 문자열 `1.0`이며, UTC 시각은 `Z`로 끝나는 ISO 8601이다. UI는 프로젝트 설정 시간대로 표시한다.

ID는 이름·순번과 분리한 안정적인 문자열이다. 첫 문자는 영숫자, 나머지는 영숫자·`_`·`-`, 최대 120자다. `target.task_id`는 필수이며 `episode_id/scene_id/shot_id`는 해당 관계가 없을 때 null/생략할 수 있다. 요청과 결과의 target은 정확히 일치해야 한다.

요청에는 다음을 둔다.

- `source_revision`: Site 작업의 내보낸 revision.
- `input_assets`: 각 `asset_id/revision/sha256/title/mime_type`. 미디어 첨부가 없더라도 해시가 필요하다.
- `approved_instruction`: 승인된 지시문·설정·승인자·승인 시각.
- `output_spec`, `acceptance`: 출력 규격·완료 조건.

Site 내보내기는 현재 revision에 대해 별도 **지시 승인**을 받은 활성 작업만 허용한다. `POST /api/records/{id}/instructions/approve`는 revision과 이유를 받고 서명(actor/시각/이유/source_revision)을 서버에서 생성한다. 이 동작은 작업의 상태·결과 검토 상태를 바꾸지 않는다. 최종 결과 승인으로 지시 승인을 대신할 수 없다. 내용·입력 버전·검토자·출력 규격·완료 조건이 없으면 거절한다. 입력 에셋은 작업에 연결한 활성 에셋이며 SHA-256이 필요하다. 실질적 내용 수정으로 지시 서명이 해제되거나 revision이 바뀌면 다시 지시 승인해야 한다. 공개 기록 폼에서 지시 승인 서명을 임의 작성할 수 없다.

결과에는 대응 `request_package_id`, `run_id`, `attempt_id`, `used_inputs`, `job_state`, `execution`, `output_assets`, `errors`, `warnings`, `cost`가 필수다. `execution`은 모듈·도구·모델 버전, 프롬프트·설정, 시작·종료 시각이다. 종료 상태에는 종료 시각이 필요하다. 비용 `kind`는 `actual/estimated/unknown`이며 unknown은 금액·통화가 null이어야 한다. 추정 금액은 실결제로 취급하지 않는다.

## 파일과 안전 규칙

초기 전송 형식은 **UTF-8 JSON 하나**다. ZIP·압축파일·디렉터리 업로드는 받지 않으므로 압축 해제나 압축 폭탄 경로가 없다. 브라우저가 선택한 JSON만 읽으며 PC의 다른 파일을 탐색하지 않는다.

`files`는 선택적인 검토용 미디어 배열이다. 각 항목은 `path`, `sha256`, `mime_type`, `content_base64`를 갖는다. 연결 에셋의 `file_path`가 있으면 파일이 반드시 포함되어야 하며 유형·해시가 일치해야 한다. `file_path`를 생략하면 메타데이터만 저장하고 미리보기가 없는 이유를 UI에 표시한다. 연결되지 않은 첨부 파일은 거절한다.

- 전체 JSON: 10 MiB 이하.
- 파일 개별: 디코딩 후 3 MiB 이하, 합계 8 MiB 이하, 최대 10개.
- 미디어: PNG/JPEG/WebP/WAV/MP3/MP4만 허용하며 파일 시그니처를 확인한다. HTML/SVG/실행 파일은 허용하지 않는다.
- 상대 경로만 허용한다. 절대·Windows 드라이브·역슬래시·`..`·숨김·빈 구간·퍼센트 인코딩 경로를 거절한다.
- Base64를 엄격히 디코딩하고 SHA-256을 검증한다. 경로는 디스크에 추출하지 않는다.
- 알려진 비밀키 필드·인증 토큰 패턴·로컬 절대 경로를 거절한다. 이 검사는 완전한 비밀정보 탐지기를 뜻하지 않는다. 워크스테이션은 내보내기 전에 원문 로그·인증 정보·개인정보를 제거해야 한다. 민감 원문 로그용 필드는 계약에 없다.

선택한 검토용 미디어만 서버 SQLite에 포함하며 인증된 `/api/media/{asset_id}`로 제공한다. 공개 정적 번들에 넣지 않는다. 대용량 제작 원본은 워크스테이션에 남긴다.

## 미리보기·충돌·중복 정책

1. Site에서 승인된 작업을 JSON으로 내려받는다. 내보낸 요청 스냅샷은 서버에 남는다.
2. 워크스테이션은 해당 요청 ID·입력 revision·해시를 그대로 결과에 기록한다.
3. `POST /api/packages/preview`에 `{ "package": result }`를 보낸다. 스키마/파일 검증 실패는 422, 다른 프로젝트는 403이다. 미리보기는 DB를 변경하지 않는다.
4. 결과는 `ready/duplicate/conflict/stale`이며 이유·경고와 `fingerprint`(정규화 JSON SHA-256)를 제공한다. ready에서만 가져온다.
5. `POST /api/packages/import`에 `{ "package": result, "fingerprint": preview.fingerprint }`를 보낸다. 가져오기 시 현재 revision과 내용을 다시 검증한다. 복수 기록·수신 이력·감사 이벤트는 한 트랜잭션으로 저장한다.

동일 package_id + 동일 내용은 중복으로 아무것도 추가하지 않는다. 같은 ID의 다른 내용, 이미 쓰인 run_id/attempt_id, 기존 에셋 ID는 충돌이다. 자동 덮어쓰기는 없다. 다른 시도는 새 attempt_id와 새 출력 에셋 ID를 사용한다. 생성 기록의 Site ID는 run_id/attempt_id 쌍에서 안정적으로 파생하며 원래 두 ID는 fields에 보존한다.

알 수 없는 요청, 변경된 target/입력 해시는 충돌이다. 작업 또는 입력 에셋 revision이 바뀌면 오래된 입력(stale)이며 가져오기를 중단한다. 새 요청으로 재실행하거나 별도 수동 검토 절차를 설계해야 한다. 결과로 Site의 기획·승인 기록을 덮어쓰는 기능은 없다.

## fixture 사용과 테스트

첨부 PNG는 계약 테스트용 합성 1픽셀 이미지다. 사용자 원본·실제 제작물이 아니며 자유롭게 테스트에 사용할 수 있다. fixture는 `producer.is_fixture: true`를 반드시 유지한다. 가져오면 생성 기록·에셋 모두 샘플 표시되며 실데이터 집계에서 제외한다. 저장소 fixture의 project/task/request ID는 예시이므로 실제 프로젝트에 그대로 가져오면 미등록 요청/프로젝트 검증으로 거절된다.

```bash
cd /workspace/project_animation
python3 -m unittest discover -s tests -p test_packages.py -v
```

왕복 테스트는 임시 SQLite에 소유자·샘플 작업을 만들고 별도 지시 승인→실제 내보내기를 수행한다. 반환 요청의 ID·revision으로 fixture 결과를 연결해 미리보기→가져오기→재가져오기→백업/복원을 검증한다. 성공 결과를 가져와도 작업 원본이 바뀌지 않고 신규 생성 기록이 미승인인지 확인한다. 실패 결과, 메타데이터만 있는 결과, 변조·중복·오래된 입력도 검증한다. 테스트용 계정·자료는 임시 디렉터리에만 만든다.

## 호환 정책과 다음 앱

1.0은 정확한 스키마 검증으로 미지원 버전·추가 필드를 거절한다. 필드·상태·ID 의미를 바꿀 때는 새 버전을 선언하고 양쪽 호환 정책·변환기·왕복 테스트를 먼저 추가한다. 버전 문자열만 올려 기존 의미를 변경하지 않는다.

다음 워크스테이션 개발자는 이 디렉터리 전체와 `app/contracts.py`, `tests/test_packages.py`를 참고한다. 실행 어댑터, 로컬 파일 색인, 대용량 원본 저장, 도구 설치·API 인증·요금 처리는 다음 앱의 별도 범위다. 실제 구독·API 권한이나 도구 성능이 확인됐다고 가정하지 않는다.
