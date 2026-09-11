# HomeTax

Mac/Linux/Windows에서 파일 기반 공동인증서를 읽어 홈택스 로그인을 시도하고,
검증된 세션을 서버 메모리에서 관리하는 독립 Python API입니다.
전자세금계산서 매출·매입 목록, 기간 합계, 상세 및 등록 거래처 조회를 지원하며 **발행 기능은 없습니다**.

**검증 상태:** macOS Apple Silicon에서 합성 인증서 암복호화·서명, HTTP API, 단위 테스트를
확인했습니다. 2026-09-11 사용자 승인 하에 NPKI 디스크 인증서(`04`) 로그인과
전자세금계산서 목록·합계·상세 조회를 실검증했습니다. 다른 인증서의 호환성을 보장하지 않습니다.
홈택스가 공식 제공하는 개발자 로그인 API가
아니므로 포털 규격 변경 시 응답 계약을 재확인해야 합니다.

## Mac에서 실행

Python 3.12 이상과 `uv`를 사용합니다. Windows 레지스트리, 인증서 저장소, 보안 모듈,
브라우저 자동화에 의존하지 않습니다. macOS Intel은 별도 기기에서 검증하지 않았습니다.

```bash
uv sync --locked
uv run python scripts/init_local.py
uv run --env-file .env uvicorn hometax_login.api:create_app \
  --factory --host 127.0.0.1 --port 8787 --no-access-log
```

- 문서: http://127.0.0.1:8787/docs
- 상태: http://127.0.0.1:8787/healthz
- `.env`는 로컬 API 키만 보관하며 처음 생성 시 파일 권한은 `0600`입니다.
- 인증서와 인증서 비밀번호는 파일·DB·환경변수로 저장하지 않습니다.
- 서버는 기본적으로 로컬 주소에만 바인딩합니다. 외부 공개를 위한 TLS·운영 설정은 포함하지 않습니다.

## 지원하는 인증서

| 형식 | 파일 검증 | 홈택스 로그인 |
|---|---|---|
| NPKI `signCert.der` + `signPri.key` | 지원 | 개인키의 VID `randomNum` 필요, 디스크 인증서 1건 실인증 확인 |
| PFX/P12 | 지원 | 현재 변환 경로에서 VID 난수가 보존되지 않아 명시적으로 거부 |

NPKI는 기존 Agent API에서 사용한 PBKDF1-SHA1/SEED-CBC 및 PBES2/PBKDF2/SEED-CBC를
지원합니다. 지원하지 않는 암호화 규격·RSA 이외 키·비ASCII NPKI 비밀번호는 오류로 반환합니다.
PFX 비밀번호는 UTF-8을 지원합니다. 만료·유효기간 시작 전·인증서와 키 불일치를 검사합니다.

## API

`/healthz`와 문서 외에는 `Authorization: Bearer <로컬 API 키>`가 필요합니다.
키는 `.env`의 `HOMETAX_API_KEYS`로 지정하며, 여러 호출자 키를 쉼표로 구분할 수 있습니다.
세션은 생성한 API 키에 귀속됩니다. 같은 키를 공유하는 호출자는 같은 소유자로 취급합니다.

| 메서드 | 경로 | 동작 |
|---|---|---|
| POST | `/v1/certificates/validate` | 파일·암호·유효기간·키 일치 검사. 외부 로그인 없음 |
| POST | `/v1/hometax/sessions` | 인증서 서명 로그인 후 서버 세션 확인. 성공 시 201 |
| GET | `/v1/hometax/sessions/{session_id}` | 보관 중인 쿠키로 홈택스 인증 상태를 다시 확인 |
| DELETE | `/v1/hometax/sessions/{session_id}` | 이 API의 메모리 세션과 HTTP client 폐기. 홈택스 원격 로그아웃은 아님 |
| GET | `/v1/hometax/sessions/{session_id}/tax-invoices` | 전자세금계산서 매출·매입 목록, 날짜 조건과 페이지 지정 |
| GET | `/v1/hometax/sessions/{session_id}/tax-invoices/summary` | 기간 전체 건수·공급가액·세액·합계(최대 5,000건) |
| GET | `/v1/hometax/sessions/{session_id}/tax-invoices/{approval_number}` | 같은 세션에서 목록으로 확인한 승인번호의 상세·품목 |
| GET | `/v1/hometax/sessions/{session_id}/counterparties` | 등록 거래처 목록 및 거래처명·사업자번호·대표자명 검색 |

조회 예제·응답·제한: [전자세금계산서 조회 API](docs/INVOICES.md).

인증서 검증 요청:

```json
{
  "cert_type": "der",
  "cert_file": "<인증서 파일의 Base64>",
  "key_file": "<암호화된 개인키 파일의 Base64>",
  "password": "<인증서 비밀번호>"
}
```

로그인 요청에는 `login_type`을 추가합니다. 홈택스 화면 소스 기준 `03`은 브라우저,
`04`는 디스크 인증서입니다. `04`는 실제 인증서로 확인했고 `03`은 미검증입니다. 기본값이나
실패 후 다른 값으로 재시도하는 동작을 넣지 않았습니다. 이 값만으로 사업자 발행 권한을
판정하지 않습니다.

비밀번호를 명령행 인수나 JSON 파일에 남기지 않고 로컬에서 입력하려면:

```bash
uv run --env-file .env python scripts/local_client.py \
  --cert /path/to/signCert.der --key /path/to/signPri.key --validate-only

# 본인 인증서와 로그인 구분을 확인한 뒤 직접 실행할 통합 검증 예시
uv run --env-file .env python scripts/local_client.py \
  --cert /path/to/signCert.der --key /path/to/signPri.key --login-type 04
```

API 응답에는 불투명한 `session_id`, 만료시각, 최소 사용자 식별 정보만 포함됩니다.
개인키·인증서 비밀번호·홈택스 쿠키는 반환하지 않습니다. 세션 기본 TTL은 10분이며,
프로세스 재시작 시 모두 사라집니다. 현재 저장소는 프로세스 내부 메모리이므로 **단일 worker**로 실행합니다.

## 실패 처리

- 인증 실패나 보호 페이지를 자동으로 반복 요청하거나 우회하지 않습니다.
- 콜백의 성공 코드만으로 세션을 발급하지 않고 `permission.do`의 사용자 정보를 추가 확인합니다.
- HTTP 200이어도 익명/불완전 세션은 거부합니다.
- 요청 본문 2MiB, 인증서/키 각각 256KiB, 동시 인증 요청 4개, 세션 128개로 제한합니다.
- 상류 요청당 20초, 전체 로그인은 60초 제한입니다. 실패 시 HTTP client/cookie jar를 폐기합니다.
- 오류 응답에 입력 데이터와 홈택스 원문 응답을 포함하지 않습니다.
- 인증서·비밀번호는 요청 중 메모리에 존재합니다. Python 객체가 해제될 때 메모리 전체가
  즉시 0으로 덮어써지는 것을 보장하지는 않습니다.

## 검증

```bash
uv run pytest -q
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv build
```

합성 인증서로 실제 암복호화와 RSA 서명을 검사하며, 로그인 프로토콜은 MockTransport로
성공/실패·쿠키 유지·VID 누락·성공 후 세션 확인을 검증합니다. 이러한 테스트는 실제 홈택스
계정 로그인의 대체 증거가 아닙니다.

연동 근거와 남은 확인 사항은 [docs/PROTOCOL.md](docs/PROTOCOL.md)를 참고하세요.
