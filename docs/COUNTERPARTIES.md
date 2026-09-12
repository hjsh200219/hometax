# 등록 거래처 관리 API

전자세금계산서 **거래처 주소록**의 등록·수정·삭제 기능입니다.
세금계산서 발행·정정·취소는 별도 [발행 API](ISSUANCE.md)에서 다룹니다. 주소록은 사업자등록번호 10자리 대상만 지원하며
주민등록번호·외국인번호 거래처는 관리 대상에서 제외합니다.

## 기본은 미리보기

| 요청 | 동작 |
|---|---|
| `POST /v1/hometax/sessions/{session_id}/counterparties` | 신규 등록 미리보기 |
| `PATCH /v1/hometax/sessions/{session_id}/counterparties/{business_number}` | 기존 거래처 변경 미리보기 |
| `DELETE /v1/hometax/sessions/{session_id}/counterparties/{business_number}` | 삭제 대상·기존 정보 미리보기 |
| `POST /v1/hometax/sessions/{session_id}/counterparty-changes/{change_id}/apply` | 확인한 미리보기 실제 반영 |

모든 요청에 기존 세션을 생성한 것과 같은 Bearer API 키를 사용합니다.
PATCH/DELETE의 `branch_number` 쿼리는 종사업장번호 4자리이며 기본값은 빈 문자열입니다.
사업자번호와 종사업장번호는 수정할 수 없습니다. 다른 번호로 바꾸려면 별도 등록/삭제 검토가 필요합니다.
**신규 종사업장 등록은 미지원**이며 자동 선택하지 않고 `COUNTERPARTY_BRANCH_UNSUPPORTED`(409)로 중단합니다.
기존 거래처의 종사업장번호를 지정한 수정·삭제 미리보기는 지원합니다.

등록·변경·삭제 미리보기는 **조회만** 합니다. 5분 동안 유효한 `change_id`,
대상 사업자·거래처, `before`/`after`, 만료시각, `requires_confirmation:true`를 반환합니다.
실제 반영은 같은 세션에서 해당 ID와 `{"confirm":true}`를 보내야 합니다.
`confirm` 생략·false·문자열·숫자는 허용하지 않습니다. 세션 만료 시 미리보기도 사용할 수 없습니다.

## 쓰기 활성화

서버의 `HOMETAX_COUNTERPARTY_WRITES_ENABLED` 기본값은 `false`입니다.
미리보기만 사용할 때는 설정을 바꾸지 않습니다.
실제 거래처 변경을 운영자가 허용할 때만 서버 실행 환경에 다음을 지정합니다.

```dotenv
HOMETAX_COUNTERPARTY_WRITES_ENABLED=true
HOMETAX_WRITE_JOURNAL_PATH=/절대경로/전용-비공개-디렉터리/counterparty-writes.sqlite3
```

허용값은 정확히 `true` 또는 `false`입니다. `yes` 같은 오타는 기동 시 거절합니다.
기본 저널 경로는 실행 디렉터리의 `.state/counterparty-writes.sqlite3`입니다.
기존 부모 디렉터리는 사용자 전용 권한(0700)이어야 하며, 프로그램은 기존 디렉터리 권한을 바꾸지 않습니다.
새 저널 파일은 0600으로 생성합니다. 여러 프로세스/세션이 쓰는 경우 같은 영속 저널을 사용해야 합니다.

## 등록 예시 — 합성 데이터

```json
{
  "business_number": "1234567890",
  "branch_number": "",
  "name": "예시거래처",
  "representative_name": "홍길동",
  "address": "서울시 예시 주소",
  "business_type": "서비스업",
  "business_item": "교육",
  "primary_contact": {
    "name": "담당자",
    "department": "경영지원",
    "telephone": "02-1234-5678",
    "email": "billing@example.com"
  }
}
```

예시 사업자번호는 실제 등록에 사용하면 안 됩니다. 등록 전 홈택스의 사업자 식별과 중복 여부를 조회합니다.
상호 또는 대표자 중 하나는 필수입니다. 이름/주소/업태/종목은 각각 UTF-8 바이트 길이 제한을 검증합니다.
주담당자(`primary_contact`), 부담당자(`secondary_contact`)를 지원합니다.
각 담당자 필드: `department`, `name`, `telephone`, `mobile`, `fax`, `email`, `remarks`.
기존 거래처에 주담당자만 있거나 담당자가 없으면, 없는 역할은 빈 담당자로 표현합니다.
수정 요청은 홈택스 폼처럼 두 역할을 전송하되 기존 담당자 식별자를 보존합니다.
전화번호의 하이픈은 제거해서 전송합니다. 제어문자, 잘못된 전화·이메일, 미지원 필드는 거절합니다.

## 부분 수정

```json
{
  "business_item": "교육 및 컨설팅",
  "primary_contact": {"email": "accounts@example.com"}
}
```

지정하지 않은 사업자·담당자 정보는 기존 상세를 읽어 보존합니다.
필드를 비우려면 빈 문자열을 지정하며, `null`은 허용하지 않습니다.
주담당자 설정 변경(주소록의 주거래처 표시), 거래처 번호 변경, 일괄 삭제는 포함하지 않습니다.

삭제는 등록 거래처 상세를 읽어 대상을 확정한 뒤 미리보기만 반환합니다.
삭제 후 원격 복구 가능성은 검증되지 않았으므로 반영 전에 `before` 정보를 확인하세요.

## 실제 반영 및 중복 방지

```http
POST /v1/hometax/sessions/{session_id}/counterparty-changes/{change_id}/apply
Authorization: Bearer <로컬 API 키>
Content-Type: application/json

{"confirm":true}
```

1. 영속 SQLite 저널에 대상·작업 지문과 `in_flight`를 기록합니다.
2. 사업자/지점 및 기존 거래처 정보가 미리보기 시점과 같은지 다시 확인합니다.
3. 일치할 때만 한 번 전송합니다. 성공 응답 뒤 다시 조회해 반영 결과를 검증합니다.
4. 완료한 작업을 재호출하면 `already_applied`를 반환하며 다시 전송하지 않습니다.

원격 API에 원자적 버전 비교 기능이 확인된 것은 아닙니다. 재조회와 쓰기 사이에 홈택스 화면 등
외부 경로에서 동시에 수정하는 모든 경합까지 방지한다고 보장하지 않습니다.

외부 전송 후 타임아웃·연결 유실·취소·재조회 불일치는 `WRITE_OUTCOME_UNKNOWN`으로 처리합니다.
프로세스가 종료되어 `in_flight`가 남은 경우도 같은 대상을 차단합니다.
새 미리보기를 만들거나 서버를 재시작해도 미확정 대상을 자동 재시도하지 않습니다.
원격 상태를 확인하기 전 저널 파일/행을 삭제해서 우회하지 마세요.
현재 원격 결과를 판정해 저널을 해제하는 복구 API는 제공하지 않습니다.

저널에는 작업 ID·해시 지문·상태·시각만 저장하고, 거래처/담당자 원문·인증서·비밀번호·쿠키는 저장하지 않습니다.
미리보기와 원격 식별값은 세션 메모리에만 존재합니다. 로그에 요청·응답·세션 URL을 남기지 마세요.
미리보기는 세션당 최대 100개이며 만료된 항목은 정리합니다.

## 오류와 검증 범위

- `403 COUNTERPARTY_WRITES_DISABLED`: 서버 쓰기 설정이 꺼짐.
- `404 CHANGE_NOT_FOUND`: 다른 세션, 없거나 만료된 미리보기.
- `409 CHANGE_STALE`: 사업자·지점·기존 정보가 달라짐. 새 미리보기가 필요함.
- `409 WRITE_OUTCOME_UNKNOWN`: 앞선 작업의 결과가 불확실해 같은 대상이 차단됨.
- `502 WRITE_OUTCOME_UNKNOWN`: 전송 후 결과를 확정하지 못함. 자동 재전송 금지.
- `503 WRITE_JOURNAL_UNAVAILABLE`: 저널 손상·권한·경합 등으로 기록 불가. 쓰기 중단.

전체 요청은 세션 잠금 대기 포함 60초 제한입니다. 기본 단일 worker 구성을 유지하세요.
이번 구현에서는 **실제 등록·수정·삭제를 실행하지 않았습니다**.
공개 홈택스 화면의 요청 규격과 합성 응답 테스트를 사용하며, 원격 실제 반영 호환성은 별도 검증이 필요합니다.
실제 클라이언트와 모의 HTTP를 연결한 통합 테스트로 등록→수정→삭제 전체를 검증합니다.
실인증은 쓰기 액션을 차단한 상태에서 기존 거래처 수정·삭제 미리보기, 중복 등록 거절,
등록 전 납세자 확인·중복 건수 조회에만 사용했습니다. 원본 거래처 정보 불변과 쓰기 시도 0건을 확인했습니다.
