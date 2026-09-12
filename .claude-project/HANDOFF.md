---
created: 2026-09-12T09:35:00+09:00
project: hometax
summary: 하드코딩 날짜로 하루 만에 깨진 발행 미리보기 테스트를 고치고 미push 커밋 3개와 함께 origin/main에 올렸습니다.
---

## Session Digest
저장소 점검 요청으로 시작해 pytest 268개 중 1개 실패를 발견했습니다.
`tests/test_invoice_issuance_api.py::test_preview_issue_validates_input_dates`가
`written_date="2026-09-12"`를 하드코딩해 미래 날짜 422를 기대했는데, 2026-09-12가 되자
유효한 날짜가 되어 200을 반환했습니다. 검증 코드는 Asia/Seoul 기준 오늘과 비교합니다
(`src/hometax_login/issuance_models.py:49,82`). 같은 기준으로 오늘+1일을 계산하도록 고치고
`.gitignore`에 `.omc/`와 인계서 백업 패턴을 추가했습니다.

## Progress
- 완료: 날짜 하드코딩 테스트 수정(`af8de29`). pytest 268개 전부 통과.
- 완료: ruff check·format, compileall, `uv build --wheel`(hometax-0.4.0) 통과.
- 완료: origin/main push. 이전 세션 미push 커밋 3개(`fa4582e`, `233e6ec`, `e684589`)도 함께 반영.
- 미완료: 발행·정정·취소의 실제 홈택스 전송 호환성 검증(0.4.0부터 계속 미검증).

## Next Steps
1. 발행 API 실전송 검증 여부 결정. 지금은 `HOMETAX_INVOICE_WRITES_ENABLED=false`가 기본입니다.
2. 거래처 쓰기(`HOMETAX_COUNTERPARTY_WRITES_ENABLED`) 실반영 검증도 같은 상태입니다.
3. 인증서 `03`(브라우저) 로그인과 PFX 로그인 경로는 여전히 미검증입니다.

## Blockers
- 없음. 이번 세션에서 하드 블록은 없었습니다.

## Watch Out
- 테스트에 미래 날짜를 넣을 때는 리터럴을 쓰지 말고 Asia/Seoul 기준으로 계산하세요.
  검증 코드가 실시간 오늘과 비교하므로 리터럴은 다음 날 바로 깨집니다.
- `tests/`에 남은 날짜 리터럴은 전부 과거 날짜라 시간이 지나도 깨지지 않습니다. 판정 규칙은
  "오늘보다 과거면 방치, 미래면 유도식으로 교체"입니다.
- `tests/test_invoice_api.py:206`의 `bad_date` 단언은 기간 규칙이 아니라 `start_date="20260901"`의
  타입 변환 실패(`date_from_datetime_inexact`)로 422가 납니다. 즉 `InvoiceFilters.valid_period`는
  이 단언에서 실행되지 않습니다(이번 세션 실측). 하이픈을 넣어 "고치면" 2026-09-30부터
  `end_date <= today`가 성립해 200이 되어 단언이 뒤집힙니다. 손볼 때는 파싱 실패 케이스를 그대로 두고
  기간 규칙 검증은 `today + timedelta(days=N)`으로 유도한 별도 단언을 추가하세요.
- mypy·pyright 설정이 없어 정적 타입 검사 단계는 실행할 수 없습니다. ruff만 있습니다.
- 이 저장소에는 AGENTS.md 하네스가 없어 harness-gc 점검은 건너뛰었습니다.

## Files Touched
- `tests/test_invoice_issuance_api.py`
- `.gitignore`

---

# 이전 세션 기록 (2026-09-11)

- 생성: 2026-09-11. 사용자 요청: Agent2 인증서 처리 로직을 재사용한 별도 Python 로그인 API.
- 실행 대상: 우선 Mac 로컬. 발행 API는 0.4.0에서 참조 구현을 추가했으며 기본 전송 비활성.
- 위치: `ProjectTeoul/hometax`.
- 서버: `uv run --env-file .env uvicorn hometax_login.api:create_app --factory --host 127.0.0.1 --port 8787 --no-access-log`.
- API 키는 `.env`에 로컬 생성됨. 값 출력·커밋 금지. 인증서와 비밀번호는 저장하지 않음.
- 0.4.0: 단건 일반 발행, 기재사항 정정(2장), 계약 해제/중복발급 취소 미리보기·확인 전송.
  `docs/ISSUANCE.md` 필독. 사용자 승인으로 lxml/xmlsec 설치, 입력 개인키/암호는 요청 메모리만 사용.
  `HOMETAX_INVOICE_WRITES_ENABLED=false` 기본. 전송 활성화에는 별도 raw/base64 프로필 명시가 필요하나
  실제 MagicLine 전송 호환성·실발행은 미검증이다. 라이브 검사에서는 모든 C/A action을 차단했다.
- 고유 client_reference는 재시도마다 바꾸지 않는다. 정정/취소는 원본 승인번호를 중복 방지 키로 사용.
  C04/C03 시작 전에 저널에 기록하고 부분 발급/응답 유실은 미확정으로 차단. 조회 없이 재발행 금지.
- 실제 클라이언트 + 실제 XML 서명 + 모의 HTTP로 발행 raw/base64, 정정 2장, 취소 2사유 및
  재조회 변조 후 unknown/retry 차단을 검증. 실계정 미리보기 1/2/1장 200, 쓰기 시도 0 확인.
- 0.4.0 검증: pytest 268개, ruff/format/compileall/build 통과. 독립 보안 검토 HIGH/CRITICAL 0.
  배포 패키지에 NPKI/.env/.state 등 비공개 런타임 파일이 포함되지 않는지도 확인.
- Mac arm64에서 인증서 암복호화·서명 및 세션/HTTP 테스트 검증.
- 2026-09-11 사용자 승인 실인증: NPKI 디스크(`04`) 로그인 및 permission 세션 재확인 성공.
- 전자세금계산서 조회 API 추가: 목록(매출/매입), 기간 합계, 목록에서 확인된 승인번호 상세.
- 등록 거래처 조회 API도 추가: 거래처명/사업자번호/대표자명 검색, 페이지 이동.
- 0.3.0 거래처 등록·수정·삭제 미리보기/확인 API 추가(`docs/COUNTERPARTIES.md`).
  실제 반영은 `HOMETAX_COUNTERPARTY_WRITES_ENABLED=true`와 confirm:true가 필요. 기본은 false.
  이번 작업에서는 실제 쓰기 미실행, 모의 HTTP 통합 검증과 실제 읽기 전용 미리보기만 수행.
- 0.3.0 검증: 테스트 155개, ruff/format/compileall/build 통과. 실제 HometaxClient+모의 HTTP로
  등록→수정→삭제/중복 반영 차단 검증. 실제 읽기 전용 가드로 수정·삭제 미리보기 200,
  중복 등록 409, 등록 전 납세자 확인·중복 검사, 원본 불변/쓰기 시도 0 확인.
- 영속 쓰기 저널 기본 `.state/counterparty-writes.sqlite3`. 미확정 결과를 재시도하려고 저널을 지우지 말 것.
  신규 종사업장 등록은 409 미지원, 기존 담당자 0/1/2행은 미지정 필드 보존하여 처리.
- 승인번호 뒤 16자리에 영문 포함 가능. 숫자 전용 정규식 사용 금지, 대소문자 보존.
- ET SSO는 포털 `/token.do` → teet `/permission.do`. 토큰/쿠키는 메모리만 사용.
- 중요 회귀: 전체 필터 3개는 빈 문자열이 아닌 `all`. 빈값은 정상 응답이어도 거짓 0건을 만들었다.
- 실검증: 9월 매출 목록·합계·상세 일치, 매입 목록·상세 성공, 등록 거래처 전체/이름 검색 성공.
  다량 페이지·다른 인증서는 별도 실검증하지 않음.
- 상세 계약/제한: `docs/INVOICES.md`, 근거: `docs/PROTOCOL.md`.
- PFX는 검증 전용. VID 난수 누락 때문에 로그인은 NPKI DER+KEY 대상.
- 프로토콜 근거와 남은 확인 사항: `docs/PROTOCOL.md`.
- 초기 구현은 private GitHub에 push됨(`8d5d837`). 이번 조회 기능은 push·배포 승인 없음.
