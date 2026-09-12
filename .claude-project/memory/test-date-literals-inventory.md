---
name: test-date-literals-inventory
description: tests/의 날짜 리터럴 58개 중 위험한 것은 미래 날짜 1곳뿐. 전수 수정은 낭비
type: reference
created: 2026-09-12
---

2026-09-12 실측. `tests/` 안의 날짜 리터럴은 문자열 15개(`"20xx-xx-xx"`)와 `date(YYYY, M, D)` 43개로
합계 58개입니다. 센 방법은 `grep -rhoE '"20[0-9]{2}-[01][0-9]-[0-3][0-9]"' tests/`와
`grep -rhoE 'date\(20[0-9]{2}, *[0-9]+, *[0-9]+\)' tests/`입니다.

**Why:** "하드코딩 날짜가 많다"는 이유로 전수 교체에 들어가면 낭비입니다. 검증 규칙 3개가 모두
`<= today` 형태라 시간이 흐를수록 조건이 느슨해지고, 과거 날짜 픽스처는 영구적으로 통과합니다.
위험한 것은 지금 기준 미래인 리터럴뿐입니다.

**How to apply:** 오늘보다 과거면 방치하고, 미래면 `Asia/Seoul` 기준 유도식으로 교체합니다
([[test-dates-derive-from-asia-seoul]]). 2026-09-12 기준 미래 리터럴은
`tests/test_invoice_api.py:206`의 `"2026-09-30"` 한 곳이며, 그 줄은 별도 함정이 있습니다
([[invoice-api-bad-date-assertion-is-masked]]).
