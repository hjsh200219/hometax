---
name: cli-write-path-two-preconditions
description: 쓰기는 --wire 없이 거부되고, 미리보기는 그 실행 안에서만 산다
type: reference
created: 2026-09-12
---

CLI 쓰기 명령의 전제 두 가지입니다.

1. `--yes`를 주고 `--wire raw|base64`가 없으면 전송 전에 `CommandError`로 막습니다. 이 값은
   전송 시점에 `client.invoice_wire_encoding`으로 들어갑니다. 형식을 추측하지 않습니다.
2. 미리보기는 `InvoiceOperations.plans` 인메모리 dict에 `operation_id`로 담기고 TTL 300초,
   최대 100건입니다(초과 시 429 `INVOICE_PREVIEW_CAPACITY`). 거래처 변경 미리보기도 같은 성질입니다.

**Why:** 저널(`~/.hometax/writes.sqlite3`)은 디스크에 남지만 미리보기는 남지 않습니다. 둘을 혼동하면
"저널에 있는데 왜 없는 미리보기라고 하지"로 헤매고, 저널을 지워 해결하려는 유혹이 생깁니다. 저널
삭제는 미확정 전송을 다시 보내게 만드는 가장 위험한 조작입니다.

**How to apply:** 확인 전송은 `hometax <명령> ... --yes --wire raw`처럼 한 번의 실행에서 미리보기와
함께 수행합니다. 사람 승인이 5분을 넘는 흐름을 설계하지 마세요. 429가 뜨면 한도가 아니라 만료 대기
문제이므로 TTL을 기다린 뒤 다시 미리보기부터 합니다.
