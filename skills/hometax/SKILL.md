---
name: hometax
description: 홈택스 전자세금계산서·사업용카드·카드매출·현금영수증·사업용계좌를 공동인증서로 조회하고 세금계산서를 발행한다. 트리거 — "/hometax", "세금계산서 조회", "매출 세금계산서", "매입 세금계산서", "사업용카드", "카드 매출", "현금영수증", "사업용계좌", "부가세 자료", "거래처 등록", "세금계산서 발행", "세금계산서 정정", "세금계산서 취소", "홈택스 로그인". 조회는 바로, 쓰기는 미리보기 뒤 사용자 확인을 받고 실행한다.
argument-hint: "[조회 요청 또는 발행 요청]"
---

# /hometax — 홈택스 전자세금계산서

파일 기반 공동인증서(NPKI)로 홈택스에 로그인해 전자세금계산서와 등록 거래처를 다룬다.
국세청 공식 API가 아니라 홈택스 웹 화면의 내부 요청을 재현한 것이다. 포털 규격이 바뀌면
응답 검증에서 멈춘다(임의로 우회하지 않는다).

## 준비

```bash
uv tool install git+https://github.com/hjsh200219/hometax   # 또는 uv sync 후 uv run
export HOMETAX_PW='<인증서 비밀번호>'                        # 생략하면 실행 시 입력받음
hometax certs                                               # 인증서가 보이는지 먼저 확인
```

인증서는 `HOMETAX_NPKI_PATH`(지정 시 그것만), 없으면 `./NPKI`·`~/NPKI`·macOS 표준 경로·
`/Volumes/*/NPKI`에서 찾는다. 만료된 인증서는 후보에서 빠지고, 여러 개면 사람에게 묻는다.
한 번 고르면 `~/.hometax/config.toml`에 경로와 지문으로 기억하고, 같은 경로의 인증서가
갱신되면 다시 묻는다.

## 조회

```bash
hometax login                                   # 세션 10분, 이후 명령은 재로그인 없음
hometax summary --ytd                           # 올해 매출 합계
hometax summary --ytd --direction purchases     # 매입
hometax invoices --from 2026-07-01 --to 2026-09-12 --json
hometax counterparties --name 휴맥스
hometax cards --ytd --deduction deductible
hometax registered-cards
hometax card-sales --year 2026 --quarter-from 1 --quarter-to 3
hometax cash-purchases --from 2026-07-01 --to 2026-09-12
hometax cash-sales --year 2026
hometax business-accounts
hometax status / hometax logout
```

- 홈택스는 한 번에 3개월까지만 조회한다. `--ytd`나 긴 기간은 자동으로 나눠 부르고 합산한다.
- `--basis issued|written|transmitted`로 기준일을 바꾼다(기본 발급일).
- `--json`을 붙이면 집계·가공용 출력이 된다.
- 일반 은행 입출금 거래내역과 PG 경유 카드매출은 홈택스 조회 범위 밖이므로 별도 연동한다.

## 쓰기 — 반드시 두 단계

**모든 쓰기 명령은 기본이 미리보기다.** `--yes`를 붙인 실행만 홈택스로 전송한다.
사람이 미리보기를 확인하기 전에 `--yes`를 붙이지 말 것.

```bash
hometax add-counterparty --business-number 1234567890 --name 예시상사      # 미리보기
hometax add-counterparty --business-number 1234567890 --name 예시상사 --yes # 전송
hometax edit-counterparty --business-number 1234567890 --file patch.json
hometax remove-counterparty --business-number 1234567890
hometax issue  --file draft.json  [--wire raw|base64] [--yes]
hometax correct --approval <승인번호> --file draft.json [--yes]
hometax cancel  --approval <승인번호> --reason contract_cancellation|duplicate_issue [--yes]
```

- 미리보기와 전송은 한 번의 실행 안에서만 이어진다. 미리보기 id는 프로세스 밖으로 나가지 않는다.
- 발행 계열 전송은 `--wire`가 필수다. 형식을 추측하지 않는다.
- 중복 전송은 `~/.hometax/writes.sqlite3` 저널이 막는다. 결과가 불확실하면 같은 대상을 차단하며,
  **저널을 지워서 우회하지 말 것**. 먼저 홈택스에서 실제 반영 여부를 확인한다.
- 발행·정정·취소의 실제 홈택스 전송 호환성은 아직 검증되지 않았다. 처음 쓰는 계정에서는
  소액 1건으로 확인한 뒤 사용한다.

## 특이한 집계는 라이브러리로

정형 명령으로 표현하기 어려운 분석은 패키지를 직접 부른다.

```python
import asyncio
from hometax_login.cert_discovery import discover, usable
from hometax_login.certificates import load_certificate
from hometax_login.invoices import InvoiceQuery
from hometax_login.protocol import HometaxClient
```

## 알아둘 것

- 매입 목록에는 상호가 비어 오는 행이 있다. `counterparty_name`이 `null`일 수 있다.
- 종사업장이 있는 거래처는 자동 선택하지 않고 `COUNTERPARTY_BRANCH_UNSUPPORTED`로 멈춘다.
- 상세 조회는 같은 세션에서 목록으로 확인한 승인번호만 허용한다.
- 비밀번호는 명령 인자로 받지 않는다. `HOMETAX_PW` 또는 프롬프트만 쓴다.
- 세션 쿠키는 `~/.hometax/session.json`(0600)에 10분간 보관한다. `hometax logout`으로 지운다.
