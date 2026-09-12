---
name: test-dates-derive-from-asia-seoul
description: 시각 의존 테스트 날짜는 Asia/Seoul 기준으로 유도할 것. 리터럴도 date.today()도 안 됨
type: feedback
created: 2026-09-12
---

날짜 검증 게이트 3곳이 모두 `datetime.now(ZoneInfo("Asia/Seoul")).date()`와 직접 비교합니다.

- `src/hometax_login/issuance_models.py:49` (발행 작성일)
- `src/hometax_login/issuance_models.py:82` (취소 작성일)
- `src/hometax_login/invoices.py:36` (조회 기간, `end_date <= min(today, limit)`)

**Why:** 고정 날짜를 쓰면 작성 다음 날 깨집니다. 실제로 `test_preview_issue_validates_input_dates`가
`written_date="2026-09-12"`를 하드코딩해 2026-09-12에 422 대신 200을 받아 실패했고, `af8de29`에서
고쳤습니다. `date.today()`로 바꾸는 것도 답이 아닙니다. 이 Mac에서는 같지만 UTC CI 러너에서는
KST 자정 전후 9시간이 어긋나 같은 버그가 재발합니다. 일반론이 아니라 이 저장소가 타임존을
고정해 둔 데서 나오는 제약입니다.

**How to apply:** 테스트에서 오늘·미래 경계를 다룰 때 `datetime.now(ZoneInfo("Asia/Seoul")).date()`에서
`timedelta`로 유도합니다(`tests/test_invoice_issuance_api.py:229`가 정답 형태). tz 없는 `datetime.now()`,
`date.today()`, 고정 리터럴은 쓰지 않습니다. 검증 로직에 새 시계 의존을 넣을 때도 `ZoneInfo("Asia/Seoul")`를
유지합니다. `protocol.py:136`(요청 타임스탬프)과 `certificates.py:295`(인증서 만료, UTC)는 검증 게이트가
아니므로 이 규칙 대상이 아닙니다.
