# 단건 발행·정정·취소 API

이 API는 홈택스의 공개 WebSquare 화면 요청을 재현하는 연동 구현입니다.
**국세청 공식 개발자 API 또는 상용 연동 인증을 받은 SDK가 아닙니다.**
실제 세금계산서 발행·정정·취소는 이 개발 작업에서 실행하지 않습니다.
서버 기본 설정은 전송 비활성이며, 모의 HTTP 통합 검증과 실제 읽기 전용 확인을 구분합니다.

## 지원 범위

- 본점 사업자, 사업자등록번호 10자리, 주소록에 등록된 거래처의 일반 전자세금계산서.
- 한 장당 품목 1~4개. 원화 공급가액·세액은 정수로 명시합니다.
- 신규 발행: 양수 공급가액의 일반 건별 발행.
- 정정: `clerical_error`(기재사항 착오정정, 화면 사유 코드 01) — 원본 취소분과 교체분 2장.
- 전액 취소: `contract_cancellation`(계약 해제, 04), `duplicate_issue`(착오 중복발급, 06).
- 공급가액 변동, 일부 환입, 영세율, 내국신용장, 위수탁, 면세 계산서, 종사업장, 일괄 발행은 미지원.
- 이미 수정발급된 문서의 재정정은 이 버전에서 지원하지 않습니다.
- 별도 이메일 발송 action은 호출하지 않습니다. `email_delivery`는 `not_requested`로 표시합니다.

정정·취소는 기존 문서를 삭제하거나 덮어쓰지 않습니다. 원본 승인번호와 사유를 연결한
수정 세금계산서를 발행합니다. 사유의 세무상 적합성은 호출자가 확인해야 하며 API가 대신 선택하지 않습니다.

## 1. 미리보기

공통 경로: `/v1/hometax/sessions/{session_id}`. 기존 로그인 세션과 같은 Bearer API 키가 필요합니다.

| 메서드·경로 | 내용 |
|---|---|
| `POST /tax-invoices/drafts` | 신규 단건 발행 미리보기 |
| `POST /tax-invoices/{approval_number}/corrections` | 원본 취소분·교체분 2장 미리보기 |
| `POST /tax-invoices/{approval_number}/cancellations` | 원본 전액 취소분 1장 미리보기 |

정정·취소 대상은 **같은 세션에서 매출 목록으로 확인한 승인번호**만 허용합니다.
승인번호는 하이픈 없는 24자리이며 뒤 16자리에 영문이 포함될 수 있습니다.

신규 미리보기 요청 예시 — 합성 사업자번호·금액이며 실제 발행에 사용하면 안 됩니다:

```json
{
  "client_reference": "00000000-0000-4000-8000-000000000001",
  "recipient_business_number": "1234567890",
  "written_date": "2026-09-01",
  "purpose": "claim",
  "items": [{
    "supply_date": "2026-09-01",
    "name": "예시 용역",
    "specification": "1식",
    "quantity": "1",
    "unit_price": "1000",
    "supply_amount": 1000,
    "tax_amount": 100,
    "remarks": ""
  }],
  "remarks": ""
}
```

`purpose`: `claim`(청구), `receipt`(영수). 미래 작성일은 미지원이며 품목 일자는 작성일과 같은 월,
작성일 이하로 제한합니다. 수량·단가는 유한 소수로 받으며 공급가액·세액은 자동 추정하지 않습니다.

정정 요청은 위 형식에 `"reason":"clerical_error"`를 추가하고 교체할 내용을 모두 지정합니다.
최초 버전에서는 당초 작성일을 유지합니다. 취소 요청은 아래 필드를 사용합니다.

```json
{
  "client_reference": "00000000-0000-4000-8000-000000000002",
  "reason": "contract_cancellation",
  "written_date": "2026-09-02",
  "remarks": ""
}
```

중복발급 취소는 원본 작성일을 사용하며, 계약 해제일은 원본 작성일보다 앞설 수 없습니다.
미리보기 응답의 **각 문서**에서 공급자·공급받는자·작성일·금액 부호·품목·사유·원본 연결을 확인하세요.

미리보기는 세션당 최대 100개, 유효시간 5분입니다. `operation_id`, `content_digest`,
`documents`, `expires_at`, `requires_confirmation:true`를 반환합니다.
미리보기에서는 C04/C05 등 발행 상태 변경 action을 호출하지 않습니다.

## 2. 전송 설정

```dotenv
HOMETAX_INVOICE_WRITES_ENABLED=false
# 아래 값은 호환성을 검증한 프로필만 명시적으로 선택합니다.
# HOMETAX_INVOICE_WIRE_ENCODING=raw
```

발행 전송을 활성화하려면 `HOMETAX_INVOICE_WRITES_ENABLED=true`와
`HOMETAX_INVOICE_WIRE_ENCODING=raw` 또는 `base64`를 함께 지정해야 합니다.
**raw/base64 중 어느 값이 실제 홈택스 MagicLine 반환값과 완전히 호환되는지는 아직 실발행 검증하지 않았습니다.**
값을 추측해 운영 전송을 켜지 마세요. 다른 프로필을 자동 재시도하지 않습니다.
거래처 관리 쓰기 설정과 세금계산서 전송 설정은 서로 독립입니다.

영속 저널은 기존 `HOMETAX_WRITE_JOURNAL_PATH`를 공유합니다. 기본 파일명은 이전 버전 호환을 위해
`.state/counterparty-writes.sqlite3`를 유지합니다. 파일을 삭제하거나 새 경로로 바꾸면 기존 기록을 보호할 수 없습니다.

## 3. 명시적 확인·인증서 전자서명

```http
POST /v1/hometax/sessions/{session_id}/tax-invoice-operations/{operation_id}/submit
Authorization: Bearer <로컬 API 키>
Content-Type: application/json
```

```json
{
  "confirm": true,
  "content_digest": "<미리보기의 SHA-256 digest>",
  "cert_type": "der",
  "cert_file": "<signCert.der Base64>",
  "key_file": "<signPri.key Base64>",
  "password": "<인증서 비밀번호>"
}
```

로그인 시 사용한 것과 같은 인증서를 다시 제공합니다. 서버는 공개 인증서 지문만 세션에 보관하고,
개인키·비밀번호를 다음 요청용으로 저장하지 않습니다. NPKI VID 난수가 필요하며 PFX 제출은 지원하지 않습니다.
요청·응답·인증서·서명 XML을 로그나 디스크에 남기지 마세요.

서명은 `lxml`·`xmlsec`를 사용합니다. 외부 참조 URI, DTD/엔티티, 임의 XPath/XSLT,
잘못된 namespace·중복 Signature·예상하지 않은 템플릿은 거절합니다.
서버가 생성한 XML이 미리보기와 일치하는지 확인한 후에만 서명합니다.
정정 두 장은 각각 서명하며, 결과 수가 맞지 않거나 일부 결과만 확인되면 완료로 처리하지 않습니다.

## 4. 결과와 재시도

정상 응답: `operation_id`, `client_reference`, `operation`, `status:issued`, `approval_numbers`.
같은 작업 재호출은 `already_issued`이며 원격 발행을 반복하지 않습니다.

- 신규 발행의 `client_reference`는 **같은 발행 의도에 대해 항상 같은 UUID**를 재사용해야 합니다.
  새로운 UUID로 바꾸면 다른 발행 의도로 취급되므로, 불명확한 결과를 우회하려고 바꾸면 안 됩니다.
- 정정·취소는 원본 승인번호당 한 번만 허용합니다. 추가 정정이 필요하면 홈택스에서 별도 검토해야 합니다.
- C04/C03 원본 XML 생성이 시작되기 전 영속 저널에 `in_flight`를 기록합니다.
- 전송·서명·반영 후 조회 중 결과가 불확실하면 `unknown`을 유지하고 재시도를 차단합니다.
- 일부만 처리된 정정 두 장을 처음부터 재전송하지 않습니다. 홈택스에서 결과를 먼저 확인해야 합니다.
- 결과 복구를 위해 저널 행/파일을 삭제하는 기능은 제공하지 않습니다.

자주 보는 오류: `INVOICE_WRITES_DISABLED`(403), `INVOICE_CERTIFICATE_MISMATCH`(403),
`INVOICE_PREVIEW_NOT_FOUND`(404), `INVOICE_CONFIRMATION_MISMATCH`/`INVOICE_PREVIEW_STALE`(409),
`WRITE_TARGET_COMPLETE`/`WRITE_OUTCOME_UNKNOWN`(409), `INVOICE_OUTCOME_UNKNOWN`(502).

## 검증 한계

발행용 XML 생성·최종 발급·수정발급·메일 발송 action은 실제 실행하지 않았습니다.
모의 HTTP, 합성 인증서, 서명 검증, 기존 발행 내역의 읽기 전용 XML 구조 확인을 검증 근거로 사용합니다.
실제 홈택스의 서명 전송 형식, IPInside 선택 필드, 시기별 업무 검증·경고까지 호환된다는 보장은 아닙니다.
실운영 활성화 전에 사용자가 지정한 합법적인 실제 거래 건으로 별도 검증이 필요합니다.

읽기 전용 가드로 실계정 신규 발행 1장·기재사항 정정 2장·중복발급 취소 1장 미리보기를 확인했습니다.
같은 검사에서 모든 발행 C/A action을 차단했고, 쓰기 시도는 0건이었습니다.
