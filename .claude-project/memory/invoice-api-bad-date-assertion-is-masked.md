---
name: invoice-api-bad-date-assertion-is-masked
description: test_invoice_api.py:206의 422는 기간 규칙이 아니라 타입 변환 실패. 하이픈 넣어 고치면 뒤집힘
type: reference
created: 2026-09-12
---

`tests/test_invoice_api.py:206`은 `{"start_date": "20260901", "end_date": "2026-09-30"}`로 422를
단언합니다. 실측 결과 422의 원인은 기간 규칙이 아니라 pydantic 타입 변환 실패
(`date_from_datetime_inexact`)입니다. 즉 `InvoiceFilters.valid_period`는 이 단언에서 한 번도
실행되지 않습니다.

확인 방법:

```python
from hometax_login.invoices import InvoiceFilters

InvoiceFilters(start_date="20260901", end_date="2026-09-30")  # date_from_datetime_inexact
InvoiceFilters(start_date="2026-09-01", end_date="2026-09-30")  # value_error (기간 규칙)
```

**Why:** 조용한 커버리지 구멍과 시한폭탄이 한 줄에 겹쳐 있습니다. 누군가 "하이픈 빠진 오타"로 보고
`"2026-09-01"`로 고치면 지금은 기간 규칙으로 여전히 422지만, 2026-09-30부터는 `end_date <= today`가
성립해 200이 되어 단언이 뒤집힙니다.

2026-09-12 조치 완료: 변수명을 `malformed_date`로 바꾸고 그 이유를 주석으로 고정했으며, 기간 규칙은
같은 테스트의 `future_period`(오늘+1일)와 `tests/test_invoices.py::test_invoice_filters_reject_periods_the_rule_owns`가
따로 검사합니다. `valid_period`를 무력화하면 두 단언이 실제로 실패하는 것을 확인했습니다.

**How to apply:** 이 줄을 건드릴 때는 둘을 분리한 채로 둡니다. 파싱 실패 케이스는 그대로 두고, 기간 규칙
검증은 `Asia/Seoul` 기준 `today + timedelta(days=N)`으로 유도한 단언으로 유지합니다. 넓게는,
검증 규칙을 겨냥한 422 단언을 볼 때 "정말 그 규칙이 거부했는가"를 먼저 확인하세요. 다른 필드의
변환 오류가 단언을 가릴 수 있습니다. 관련 [[test-date-literals-inventory]]
