# hometax

파일 기반 공동인증서(NPKI)로 홈택스에 로그인해 **전자세금계산서 매출·매입 조회와 등록 거래처
관리**를 하는 도구입니다. 명령줄 도구(`hometax`), Python 라이브러리, 로컬 HTTP API 세 가지
경로를 제공하며 셋 다 같은 구현을 씁니다.

> **공식 API가 아닙니다.** 국세청이 제공하는 개발자 API가 아니라 홈택스 웹 화면이 쓰는 내부
> 요청을 재현한 것입니다. 포털 규격이 바뀌면 응답 검증에서 멈춥니다. 본인 인증서와 본인
> 사업자에 대해서만, 본인 책임으로 쓰세요. 발행·정정·취소는 실제 세금 문서를 만듭니다.

**검증 상태**

| 범위 | 상태 |
|---|---|
| NPKI 디스크 인증서(`04`) 로그인 | 실계정 확인(2026-09-11) |
| 매출·매입 목록·합계·상세, 거래처 조회 | 실계정 확인(2026-09-12, 매출 17건·매입 38건) |
| 거래처 등록·수정·삭제 | 미리보기만 실계정 확인. 실제 반영은 미검증 |
| 발행·정정·취소 | 모의 HTTP와 실제 XML 서명까지만. **실제 전송 호환성 미검증** |
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
hometax certs                              # 인증서 목록(만료분 표시)
hometax login                              # 인증서 선택 → 로그인 → 세션 10분
hometax summary --ytd                      # 올해 매출 합계
hometax summary --ytd --direction purchases
hometax invoices --from 2026-07-01 --to 2026-09-12 --json
hometax counterparties --name 휴맥스
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
| GET · POST · PATCH · DELETE | `.../counterparties[/{사업자번호}]` | 거래처 조회와 변경 미리보기 |
| POST | `.../tax-invoices/drafts`, `/{승인번호}/corrections`, `/cancellations` | 발행·정정·취소 미리보기 |
| POST | `.../counterparty-changes/{id}/apply`, `.../tax-invoice-operations/{id}/submit` | 확인 후 전송 |

상세: [조회](docs/INVOICES.md) · [거래처](docs/COUNTERPARTIES.md) · [발행](docs/ISSUANCE.md) ·
[프로토콜 근거](docs/PROTOCOL.md).

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
