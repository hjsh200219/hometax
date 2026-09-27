# hometax

파일 기반 공동인증서(NPKI)로 홈택스에 로그인해 **전자세금계산서, 사업용카드, 카드매출,
현금영수증, 사업용계좌와 등록 거래처**를 조회·관리하는 도구입니다. 명령줄 도구(`hometax`),
Python 라이브러리, 로컬 HTTP API 세 가지 경로를 제공하며 셋 다 같은 구현을 씁니다.

> **공식 API가 아닙니다.** 국세청이 제공하는 개발자 API가 아니라 홈택스 웹 화면이 쓰는 내부
> 요청을 재현한 것입니다. 포털 규격이 바뀌면 응답 검증에서 멈춥니다. 본인 인증서와 본인
> 사업자에 대해서만, 본인 책임으로 쓰세요. 발행·정정·취소는 실제 세금 문서를 만듭니다.
>
> **타인 자료를 대신 조회하는 용도라면 국세청과 먼저 협의하세요.** 국세청 개인정보처리방침
> 제5조 ⑩은 정보주체 본인의 화면 내려받기는 제공 기능으로 두면서, 대리인의 자동화된
> 도구(스크래핑 등)를 통한 본인정보 전송에 대해서는 안전한 전송 조치를 준비 중이라며
> 사전협의 창구를 안내합니다. 본인 사용과 대행은 이 선에서 갈립니다.
>
> **국세청이 자동화 접근을 기술적으로 차단할 수 있습니다.** 신고 성수기 부하 관리나 보안
> 강화로 차단되면 이 도구는 동작하지 않습니다. 업무 흐름이 이 도구에만 의존하지 않게 두세요.

**검증 상태**

| 범위 | 상태 |
|---|---|
| NPKI 디스크 인증서(`04`) 로그인 | 실계정 확인(2026-09-11) |
| 매출·매입 목록·합계·상세, 거래처 조회 | 실계정 확인(2026-09-12, 매출 17건·매입 38건) |
| 사업용카드·카드매출·현금영수증·사업용계좌 조회 | 승인된 본인 계정으로 응답 계약 확인(2026-09-12) |
| 거래처 등록·수정·삭제 | 미리보기만 실계정 확인. 실제 반영은 미검증 |
| 발행·정정·취소 | 모의 HTTP와 실제 XML 서명까지만. **실제 전송 호환성 미검증** |
| 여러 사업자(`list`·`--company`·별칭·인증서별 세션) | 사업자 인증서 1건·개인 인증서 1건으로 확인(2026-09-27). 사업자 인증서 2건 동시는 미검증 |
| 플랫폼 | macOS Apple Silicon에서 검증. 그 외는 미검증 |
| 인증서 | 위 1건으로만 확인. 다른 인증서 호환성은 보장하지 않음 |

## 설치

Python 3.12 이상이 필요합니다.

```bash
# Claude Code 플러그인으로
/plugin marketplace add hjsh200219/hometax
/plugin install hometax@hometax

# 명령줄 도구만
uv tool install git+https://github.com/hjsh200219/hometax

# 저장소에서 직접
uv sync --locked && uv run hometax certs
```

## 빠른 시작

```bash
export HOMETAX_PW='<인증서 비밀번호>'      # 생략하면 실행할 때 입력받습니다
hometax list                               # 조회할 수 있는 사업자(인증서) 목록
hometax login                              # 인증서 선택 → 로그인 → 세션 10분
hometax summary --ytd                      # 올해 매출 합계
hometax summary --ytd --direction purchases
hometax revenue 2026                       # 매입매출 월별 + 누계
hometax revenue 2026 --by counterparty     # 거래처별 월 표
hometax vat 2026-2                         # 예상 부가세
hometax invoices --from 2026-07-01 --to 2026-09-12 --json
hometax counterparties --name 휴맥스
hometax cards --ytd --deduction deductible
hometax registered-cards
hometax card-sales --year 2026 --quarter-from 1 --quarter-to 3
hometax cash-purchases --from 2026-07-01 --to 2026-09-12
hometax cash-sales --year 2026
hometax business-accounts
hometax revenue -ytd 2026 --company 2     # 다른 사업자로(목록 번호·별칭·상호 일부)
hometax status / hometax logout
```

홈택스는 한 번에 3개월까지만 조회합니다. 긴 기간과 `--ytd`는 자동으로 나눠 부르고 합칩니다.

### 인증서 선택

- 찾는 곳: `HOMETAX_NPKI_PATH`(지정하면 그곳만), 없으면 `./NPKI`·`~/NPKI`·macOS 표준 경로·
  `/Volumes/*/NPKI`.
- 만료된 인증서는 후보에서 빼고, 여러 개면 묻습니다. 대화형이 아니면 임의로 고르지 않고
  `--cert`를 요구합니다.
- 고른 인증서는 `~/.hometax/config.toml`(0600)에 **경로와 지문**으로 기억합니다. 같은 경로의
  인증서가 갱신되면 지문이 달라 다시 묻습니다. `--choose`로 언제든 다시 고릅니다.
- 비밀번호는 저장하지 않고 명령 인자로도 받지 않습니다. `HOMETAX_PW` 또는 프롬프트뿐입니다.

### 여러 사업자

사업자를 여럿 운영하면 사업자마다 인증서를 NPKI 폴더에 두고 `--company`로 고릅니다.

```bash
hometax list                          # 번호·인증서·종류(사업자/개인)·별칭·만료일·상태
hometax alias shc 3                   # 3번에 별칭 shc
hometax vat 2026-2 --company shc      # 별칭으로
hometax revenue 2026 --company 2      # 목록 번호로
hometax summary --ytd --company 컨설팅 # 상호 일부(한 곳에만 맞을 때)
hometax alias shc --remove
```

- `--company`는 `--cert`와 함께 쓸 수 없고, 이번 실행만 바꿉니다. 인자 없이 쓰는 기본 사업자는
  `hometax login --company <번호|별칭> --remember`로 바꿉니다(`hometax list`의 "기본"). 목록 번호는 상호순이라 인증서를 더하거나 빼면 바뀝니다. 오래 쓸 사업자는 별칭을 붙이세요.
- 조회 세션은 인증서마다 따로 10분 보관합니다(`~/.hometax/sessions/`). 사업자를 오가도 서로의
  세션을 덮지 않고, 다른 사업자 자료가 섞이지 않습니다. `hometax status`가 사업자별 남은 시간을,
  `hometax logout`이 전부를(`--company`면 그 사업자만) 지웁니다.
  사업자를 지정하지 않으면 기본 사업자, 기본이 없으면 살아 있는 세션이 하나뿐일 때 그 사업자를 씁니다.
- 사업자마다 인증서 비밀번호가 다르면 별칭별 환경변수를 씁니다. 별칭 `shc`면 `HOMETAX_PW_SHC`를
  먼저 보고, 없으면 `HOMETAX_PW`, 그다음 프롬프트입니다.
- **개인 공동인증서**(`이름()…`)는 로그인은 되지만 사업자 자료는 조회되지 않습니다
  (`조회할 사업자로 로그인하거나 전환하세요`). 사업자용 인증서를 쓰세요. 홈택스의 사업자 전환은
  지원하지 않습니다.
- **세무대리인 로그인(수임납세자 조회)은 지원하지 않습니다.** 세무사가 고객 인증서를 받아 이 도구로
  조회하는 것은 기술적으로는 위와 같지만, 대리인의 자동화 조회는 국세청 사전협의 대상입니다(맨 위 안내).

## 0.6.0 금융자료 조회

공동인증서로 로그인한 본인 사업자의 금융 증빙자료를 읽기 전용으로 조회합니다.

| CLI | 조회 자료 | 주요 결과 |
|---|---|---|
| `hometax registered-cards` | 홈택스에 등록한 사업용 신용카드 | 카드 구분·마스킹 번호·신청일·처리상태 |
| `hometax cards --ytd` | 올해 사업용카드 매입내역 | 거래일·가맹점·공제 분류·공급가액·세액·합계 |
| `hometax card-sales --year 2026` | 신용카드 매출 월별 합계 | 건수·신용카드·직불카드·기타 매출액 |
| `hometax cash-purchases --ytd` | 현금영수증 매입 공제내역 | 공제·불공제 금액과 가맹점별 합계 |
| `hometax cash-sales --year 2026` | 홈택스 발급 현금영수증 매출 | 월별 건수·공급가액·세액·합계 |
| `hometax business-accounts` | 홈택스에 신고한 사업용계좌 | 은행·마스킹 계좌번호·신고일·상태 |

`--ytd`는 한국 시간 기준 올해 1월 1일부터 오늘까지 조회합니다. `cards`는 개인카드 전체
이용내역이 아니라 홈택스에 등록되고 카드사가 국세청에 제출한 **사업용카드 매입자료**입니다.
`registered-cards`가 0장이면 `cards` 결과도 0건일 수 있습니다. 카드사 제출 시점에 따라 최근
자료가 늦게 반영될 수도 있습니다.

`business-accounts`는 신고된 계좌의 등록 정보만 반환하며 입출금 거래내역은 포함하지 않습니다.
상세한 매개변수와 API 응답은 [카드·현금영수증·사업용계좌 조회](docs/FINANCIALS.md)를 보세요.

## 0.7.0~0.8.0 매입매출·거래처별·예상 부가세

```bash
hometax revenue 2026             # 2026년 월별 행 + 누계(연도 생략 시 올해)
hometax revenue -ytd 2026        # 월별 행 없이 누계만(연도 생략 시 올해)
hometax revenue 2025             # 지난해 1~12월
hometax revenue 2026 --basis issued   # 세금계산서를 발급일 기준으로 월에 귀속
hometax revenue 2026 --by counterparty   # 세금계산서 거래처 × 월, * = 3개월 이상 반복
hometax vat 2026-2               # 2026년 2기 예상 부가세(생략 시 현재 과세기간)
```

매출은 세금계산서·신용카드 매출·현금영수증 매출, 매입은 세금계산서·사업용카드·현금영수증
매입을 월별로 모아 합계와 차액(매출−매입)을 냅니다. 금액은 모두 **합계금액(부가세 포함)** 입니다.
카드매출이 공급가액과 세액을 나눠 주지 않아 출처를 한 기준으로 더할 수 있는 값이 합계금액뿐이기
때문입니다. 세금계산서는 기본으로 부가세 귀속 기준인 **작성일**의 달에 넣습니다. 은행 입출금·
PG 정산·종이 증빙은 홈택스에 없으므로 빠집니다. 자세한 기준은 [FINANCIALS](docs/FINANCIALS.md#매입매출-리포트)를
보세요.

`--by counterparty`는 전자세금계산서를 거래처별로 묶어 월별 금액·건수·거래 개월 수를 보여 줍니다.
3개월 이상 거래가 있는 거래처에 `*`를 붙여 임대료·관리비 같은 반복 거래와 일회성 거래를 가릅니다.
카드·현금영수증 매출은 거래처 없이 월 합계만 오므로 이 표에는 들어가지 않습니다.

`vat`는 과세기간(1기 1~6월, 2기 7~12월)의 매출세액과 공제 매입세액을 예정·확정 분기로 나눠
예상 납부세액을 냅니다. 참고용 추정이며 세금계산서 불공제 매입, 개인사업자 신용카드매출 세액공제,
예정고지·기납부세액, 가산세는 반영하지 않습니다.

## 쓰기 — 미리보기가 기본입니다

```bash
hometax add-counterparty --business-number 1234567890 --name 예시상사        # 미리보기
hometax add-counterparty --business-number 1234567890 --name 예시상사 --yes  # 실제 등록
hometax edit-counterparty --business-number 1234567890 --file patch.json
hometax remove-counterparty --business-number 1234567890
hometax issue   --file draft.json --wire raw --yes
hometax correct --approval <승인번호> --file draft.json --wire raw --yes
hometax cancel  --approval <승인번호> --reason contract_cancellation --wire raw --yes
```

- `--yes` 없이는 **홈택스로 아무것도 보내지 않습니다.** 미리보기와 전송은 한 실행 안에서만
  이어지고, 미리보기 식별자는 프로세스 밖으로 나가지 않습니다.
- 발행 계열 전송에는 `--wire raw|base64`가 필요합니다. 전송 형식을 추측하지 않습니다.
- 중복 전송은 `~/.hometax/writes.sqlite3` 저널이 막습니다. 결과가 불확실하면 같은 대상을
  차단합니다. **저널을 지워 우회하지 마세요.** 홈택스에서 실제 반영 여부를 먼저 확인하세요.
- 발행·정정·취소의 `--yes` 실행은 선택한 인증서로 새로 로그인합니다. 저장된 조회 세션의
  인증서 지문을 복원하거나 일치 검사를 생략하지 않습니다.
- 신규 발행에서 `client_reference`를 생략하면 같은 정규화 입력에 같은 UUID를 사용합니다.
  다른 UUID로 다시 요청해도 동일 사업자·동일 계산서 내용의 미확정 전송은 차단합니다.
  기존 v1 저널의 미확정 기록이 있으면 신규 발행도 차단하므로 결과를 먼저 확인해야 합니다.
- 요청 본문 형식은 [발행](docs/ISSUANCE.md)과 [거래처](docs/COUNTERPARTIES.md) 문서를 보세요.

## 라이브러리로 직접 쓰기

정형 명령으로 표현하기 어려운 집계는 패키지를 직접 부릅니다.

```python
import asyncio, os
from hometax_login.cert_discovery import discover, usable
from hometax_login.certificates import load_certificate
from hometax_login.invoices import InvoiceQuery
from hometax_login.protocol import HometaxClient


async def main():
    entry = usable(discover())[0]
    material = load_certificate(
        entry.cert_path.read_bytes(), entry.key_path.read_bytes(), os.environ["HOMETAX_PW"], "der"
    )
    client = HometaxClient()
    try:
        await client.login(material, "04")
        page = await client.invoices.list(
            InvoiceQuery(start_date="2026-07-01", end_date="2026-09-12", direction="purchases")
        )
        print(page.total_count)
    finally:
        await client.close()


asyncio.run(main())
```

## 로컬 HTTP API (선택)

여러 호출자가 붙거나 다른 언어에서 쓸 때만 필요합니다. 혼자 쓸 때는 CLI가 더 간단합니다.

```bash
uv run python scripts/init_local.py
uv run --env-file .env uvicorn hometax_login.api:create_app \
  --factory --host 127.0.0.1 --port 8787 --no-access-log
```

`/healthz`와 `/docs` 외에는 `Authorization: Bearer <로컬 API 키>`가 필요합니다. 키는 `.env`의
`HOMETAX_API_KEYS`이며 세션은 키 소유자에게 귀속됩니다. 서버 경로에서는 쓰기가 기본 비활성이라
`HOMETAX_COUNTERPARTY_WRITES_ENABLED`·`HOMETAX_INVOICE_WRITES_ENABLED`가 필요합니다.
CLI에는 이 환경변수 게이트가 없고 `--yes`가 그 역할을 합니다.

| 메서드 | 경로 | 동작 |
|---|---|---|
| POST | `/v1/certificates/validate` | 파일·암호·유효기간·키 일치 검사 |
| POST · GET · DELETE | `/v1/hometax/sessions[/{id}]` | 로그인·상태 확인·세션 폐기 |
| GET | `.../tax-invoices`, `/summary`, `/{승인번호}` | 목록·기간 합계·상세 |
| GET | `.../business-card-purchases`, `.../card-sales` | 사업용카드 매입·신용카드 매출 합계 |
| GET | `.../registered-business-cards` | 등록된 사업용카드와 처리상태 |
| GET | `.../cash-receipt-purchases`, `.../cash-receipt-sales` | 현금영수증 매입·매출 합계 |
| GET | `.../business-accounts` | 홈택스에 신고된 사업용계좌 목록 |
| GET · POST · PATCH · DELETE | `.../counterparties[/{사업자번호}]` | 거래처 조회와 변경 미리보기 |
| POST | `.../tax-invoices/drafts`, `/{승인번호}/corrections`, `/cancellations` | 발행·정정·취소 미리보기 |
| POST | `.../counterparty-changes/{id}/apply`, `.../tax-invoice-operations/{id}/submit` | 확인 후 전송 |

상세: [세금계산서 조회](docs/INVOICES.md) · [카드·현금영수증·계좌](docs/FINANCIALS.md) ·
[거래처](docs/COUNTERPARTIES.md) · [발행](docs/ISSUANCE.md) · [프로토콜 근거](docs/PROTOCOL.md).

일반 은행 계좌의 입출금 거래내역은 홈택스가 제공하지 않아 지원하지 않습니다. 카드매출은 카드사
제출분과 판매·결제대행(PG) 경유분을 각각 읽어 합산합니다. 제출 시점에 따라 최근 자료가 늦게
반영될 수 있어 정산자료와 금액이 다를 수 있습니다. `cash-sales`는 홈택스 발급 시스템을 통한
현금영수증의 월별 현황입니다.

## 지원하는 인증서

| 형식 | 파일 검증 | 홈택스 로그인 |
|---|---|---|
| NPKI `signCert.der` + `signPri.key` | 지원 | 개인키의 VID `randomNum` 필요. 디스크 인증서(`04`) 1건 실인증 |
| PFX/P12 | 지원 | 변환 경로에서 VID 난수가 보존되지 않아 거부 |

PBKDF1-SHA1/SEED-CBC와 PBES2/PBKDF2/SEED-CBC를 지원합니다. 지원하지 않는 암호화 규격, RSA가
아닌 키, 비ASCII NPKI 비밀번호는 오류로 돌려줍니다. 만료·유효기간 시작 전·인증서와 키 불일치를
검사합니다. 홈택스 화면 소스 기준 `03`은 브라우저, `04`는 디스크 인증서이며 `03`은 미검증입니다.

## 무엇을 어디에 두는가

| 파일 | 내용 | 권한 |
|---|---|---|
| `~/.hometax/config.toml` | 고른 인증서의 경로와 지문 | 0600 |
| `~/.hometax/session.json` | 홈택스 세션 쿠키(기본 10분) | 0600 |
| `~/.hometax/writes.sqlite3` | 쓰기 저널(작업 id·해시·상태·시각) | 0600 |
| `.env` | 로컬 API 키, 선택적으로 `HOMETAX_PW` | 0600, gitignore |

인증서 개인키와 비밀번호는 어디에도 저장하지 않습니다. 저널에는 거래처 원문·쿠키·비밀번호를
남기지 않습니다. HTTP API 경로는 쿠키를 프로세스 메모리에만 둡니다.

## 실패 처리

- 인증 실패나 보호 페이지를 자동으로 반복 요청하거나 우회하지 않습니다.
- 콜백 성공 코드만으로 세션을 인정하지 않고 `permission.do`의 사용자 정보를 추가 확인합니다.
- HTTP 200이어도 익명·불완전 세션은 거부합니다.
- 응답이 예상과 다르면 `INVOICE_RESPONSE_CHANGED`로 멈춥니다. 추측해서 진행하지 않습니다.
- 요청 본문 2MiB, 인증서·키 각 256KiB, 동시 인증 4건, 세션 128개 제한. 상류 요청 20초,
  로그인 전체 60초 제한입니다.
- 오류 응답에 입력 데이터와 홈택스 원문을 넣지 않습니다.
- 인증서·비밀번호는 처리 중 메모리에 존재합니다. 해제 시 즉시 0으로 덮어쓰는 것을 보장하지는
  않습니다.

## 검증

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv build
```

합성 인증서로 실제 암복호화와 RSA 서명을 검사하고, 프로토콜은 MockTransport로 성공·실패·쿠키
유지·VID 누락·세션 확인을 검증합니다. 이 테스트는 실제 홈택스 계정 동작의 대체 증거가 아닙니다.

## 라이선스

MIT. [LICENSE](LICENSE)를 보세요.
