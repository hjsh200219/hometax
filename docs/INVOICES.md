# 전자세금계산서 조회 API

기존 인증 API로 세션을 생성한 뒤 **같은 Bearer API 키**와 `session_id`를 사용합니다.
홈택스 자체의 공식 개발자 API가 아니라, 홈페이지의 조회 요청을 재현하는 로컬 연동입니다.
세금계산서 발행·수정·취소, 면세 전자계산서, PDF/엑셀 다운로드는 포함하지 않습니다.

## 목록

```http
GET /v1/hometax/sessions/{session_id}/tax-invoices?start_date=2026-09-01&end_date=2026-09-11&direction=sales&date_basis=issued&page=1&page_size=50
Authorization: Bearer <로컬 API 키>
```

| 파라미터 | 값 |
|---|---|
| `start_date`, `end_date` | 필수, `YYYY-MM-DD`, 시작·종료일 포함 |
| `direction` | `sales`(매출, 기본), `purchases`(매입) |
| `date_basis` | `issued`(발급일, 기본), `written`(작성일), `transmitted`(전송일) |
| `page` | 1부터, 최대 10,000 |
| `page_size` | 10·20·30·50, 기본 50 |

종료일은 한국 시간 오늘을 넘을 수 없으며, 조회 범위는 최대 3개월입니다.
페이지는 승인번호 내림차순입니다. 전체 분류·유형·발급 방식으로 조회합니다.
아래는 **합성 응답 예시**입니다.

```json
{
  "company_name": "예시회사",
  "filters": {"start_date": "2026-09-01", "end_date": "2026-09-11", "direction": "sales", "date_basis": "issued"},
  "page": 1,
  "page_size": 50,
  "total_count": 1,
  "has_next": false,
  "items": [{
    "approval_number": "202609030000000000000001",
    "written_date": "2026-09-03",
    "issued_date": "2026-09-03",
    "transmitted_date": "2026-09-03",
    "counterparty_name": "예시거래처",
    "item_name": "교육",
    "supply_amount": 100000,
    "tax_amount": 10000,
    "total_amount": 110000
  }]
}
```

금액 단위는 원(KRW)이며 정수입니다. 음수 수정 세금계산서 금액을 임의로 제외하지 않습니다.
매출의 거래처는 공급받는자, 매입의 거래처는 공급자입니다.

## 기간 합계

```http
GET /v1/hometax/sessions/{session_id}/tax-invoices/summary?start_date=2026-09-01&end_date=2026-09-11&direction=sales&date_basis=issued
```

목록과 같은 필터를 사용하지만 `page`·`page_size`는 받지 않습니다.
응답은 `company_name`, `filters`, `total_count`, `supply_amount`, `tax_amount`, `total_amount`입니다.
페이지 전체를 수집해서 합산합니다. 최대 5,000건이며 초과 시 기간을 줄여야 합니다.
조회 중 건수 변경·승인번호 중복·누락이 감지되면 부분 합계를 반환하지 않고 오류를 반환합니다.
홈택스가 스냅샷을 제공하는 것은 아니므로 동일 건수로 구성만 바뀌는 모든 경우를 검출한다고 보장하지 않습니다.

## 상세

```http
GET /v1/hometax/sessions/{session_id}/tax-invoices/{approval_number}
```

먼저 같은 세션에서 목록을 조회하고 응답의 `approval_number`를 전달합니다.
승인번호는 하이픈 없이 24자리(앞 8자리 숫자, 뒤 16자리 영문·숫자)이며 대소문자는 보존합니다.
임의 승인번호 탐색은 허용하지 않습니다. 목록에서 확인된 최근 최대 500개 승인번호만 메모리에 보관하므로,
오래된 번호는 해당 목록 페이지를 다시 조회해야 합니다. 세션 만료·사업자 변경 시 재조회가 필요합니다.

응답: `invoice`(목록과 동일 정보), `supplier_name`, `customer_name`,
`supplier_business_number`, `customer_business_number`, `items`.
사업자등록번호는 10자리로 확인된 경우만 반환하며 주민등록번호나 마스킹된 식별자는 반환하지 않습니다.
품목에는 이름, 공급일 표기, 규격, 수량, 단가, 공급가액, 세액, 비고가 포함됩니다.
원문 응답·인증 토큰·쿠키·인증서·개인키는 반환하지 않습니다.

## 등록 거래처 목록·검색

```http
GET /v1/hometax/sessions/{session_id}/counterparties?name=휴맥스&page=1&page_size=50
```

홈택스의 **거래처 정보에 등록한 목록**입니다. 세금계산서에서 추출한 거래처 목록과 다를 수 있습니다.
거래처 등록·수정·삭제는 별도의 [미리보기·확인 API](COUNTERPARTIES.md)로 제공합니다.

| 파라미터 | 값 |
|---|---|
| `name` | 거래처명 검색, 기본 빈 값(전체), 최대 100자 |
| `business_number` | 사업자등록번호 숫자 10자리, 기본 빈 값. 주민번호 검색은 지원하지 않음 |
| `representative_name` | 대표자명 검색, 기본 빈 값(전체), 최대 100자 |
| `page`, `page_size` | 목록 조회와 동일, 기본 1·50 |

응답은 `company_name`, `query`, `total_count`, `has_next`, `items`입니다.
항목은 `name`, `business_number`, `representative_name`, `address`, `business_type`,
`business_item`, `branch_number`, `registered_at`, `is_primary`를 포함합니다.
사업자등록번호가 아닌 식별번호·마스킹 값은 `null`로 반환합니다. 거래처명 오름차순으로 조회합니다.

## 오류와 운용

- `401/403`: 로그인 또는 ET 서비스 인증/권한 확인 실패. 0건으로 처리하면 안 됩니다.
- `404 SESSION_NOT_FOUND`: 다른 API 키의 세션, 없거나 만료된 세션.
- `404 INVOICE_NOT_LISTED`: 해당 세션의 목록에서 확인되지 않은 승인번호.
- `409 BUSINESS_REQUIRED/INVOICE_LIST_CHANGED`: 사업자 선택 또는 목록 변경 상태 확인.
- `401 SESSION_IDENTITY_CHANGED`: 세션의 로그인 사용자 정보가 변경됨.
- `422 INVALID_REQUEST`: 날짜·페이지·승인번호·추가 필드 오류.
- `422 INVOICE_SUMMARY_TOO_LARGE`: 5,000건 초과, 기간 축소 필요.
- `502 INVOICE_QUERY_FAILED/INVOICE_RESPONSE_CHANGED`: 상류 실패/응답 형식·필터·금액 불일치.
- `502 COUNTERPARTY_QUERY_FAILED`: 등록 거래처 조회 실패.
- `504 INVOICE_TIMEOUT`: 전체 조회 60초 초과. 상류 요청당 제한은 20초입니다.

세션당 요청은 직렬화하며 API는 단일 worker로 실행합니다. 조회 결과는 HTTP 캐시를 금지합니다.
로그에 URL 세션 ID·응답·거래 내역을 남기지 않도록 기본 실행의 `--no-access-log`를 유지하세요.
증명서 파일·비밀번호는 요청 중 메모리에서만 처리합니다. 테스트에도 실제 거래 원문을 저장하지 않습니다.
