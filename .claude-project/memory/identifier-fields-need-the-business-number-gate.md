---
name: identifier-fields-need-the-business-number-gate
description: *EncCntn 필드는 주민번호·암호문을 담을 수 있다. 사업자번호 정규식을 통과한 값만 내보낸다
type: feedback
created: 2026-09-12
---

홈택스 응답의 `*EncCntn` 계열 식별번호 필드(`crdcTxprDscmNoEncCntn`·`mrntTxprDscmNoEncCntn`·
`txprDscmNoEncCntn` 등)는 사업자등록번호만 담는다는 보장이 없습니다. `invoices.py`는 이 필드에
`(?:[0-9]{10}|[0-9]{3}-[0-9]{2}-[0-9]{5})` 정규식을 걸고 통과하지 못하면 `None`으로 버립니다.

**Why:** 0.6.0 금융자료 모듈이 이 규칙만 빠뜨리고 `optional_text`로 그대로 실었습니다. 카드번호는
마스킹하면서 바로 옆 필드로 주민번호 형태 값이 응답 모델·HTTP API·CLI `--json`에 나갈 수 있는
상태였습니다. 공개 저장소이고 다른 사람이 쓰는 도구라 그대로 배포됐다면 회수가 어렵습니다.

**How to apply:** 새 파서에서 식별번호를 꺼낼 때 `business_number()`/`business_number_from()`을
쓰세요(`financials.py`). 마스킹은 카드·계좌번호에만 걸려 있으니 식별번호는 별도 게이트가 필요합니다.
테스트는 주민번호 형태와 암호문을 픽스처에 넣고 `model_dump_json()` 결과에 원본이 없다고
단언하세요. 사업자번호 형태만 넣은 픽스처는 이 결함을 못 잡습니다.
