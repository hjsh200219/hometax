# 카드·현금영수증·사업용계좌 조회

공동인증서 로그인 세션으로 홈택스가 제공하는 다음 자료를 읽기 전용으로 조회합니다.

- 사업용 신용카드 매입내역과 매입세액 공제 분류
- 홈택스에 등록한 사업용 신용카드 목록과 처리상태
- 신용카드 매출 월별 합계
- 현금영수증 매입 공제금액과 사용자·가맹점별 합계
- 홈택스에서 발급한 현금영수증 매출 월별 합계
- 홈택스에 신고한 사업용계좌 목록

일반 은행 계좌의 입출금 거래내역은 홈택스가 제공하지 않으므로 지원하지 않습니다. 은행 거래는
각 은행 API나 거래내역 파일을 별도로 연동해야 합니다.

신용카드 매출 응답에는 목록이 셋 들어 있고 스키마가 서로 다릅니다. 카드사 제출분
(`crdcTrsBrkdMateAdmDVOList`)과 판매·결제대행분(`sleVcexSlsMateInqrDVOList`)을 각각의 필드로
읽어 합산하고, 분기 요약(`crdcZrpSleStlVcexMateAdmDVOList`)은 중복이므로 합산하지 않고
대행분과 어긋나는지 대조만 합니다. 즉 PG 경유분도 홈택스가 주는 범위까지는 포함됩니다.
다만 카드사·결제대행사 제출 시점에 따라 최근 자료가 늦게 반영될 수 있어 정산자료와 금액이
다를 수 있습니다.

## CLI

```bash
hometax cards --from 2026-07-01 --to 2026-09-12
hometax cards --ytd --deduction deductible
hometax registered-cards
hometax cash-purchases --from 2026-07-01 --to 2026-09-12
hometax cash-sales --year 2026
hometax card-sales --year 2026 --quarter-from 1 --quarter-to 3
hometax business-accounts
```

`cards`와 `cash-purchases`는 홈택스의 3개월 조회 제한에 맞춰 긴 기간을 자동 분할합니다.
`--deduction`은 `all`, `deductible`, `non-deductible` 중 하나입니다. 모든 명령은 전역
`--json` 옵션을 지원합니다.

사업용카드 자료는 카드사가 국세청에 제출한 시점에 따라 직전 월 자료가 늦게 반영될 수 있습니다.
공제 분류는 신고 보조자료이며, 최종 공제 여부는 거래의 실제 사업 관련성을 확인해야 합니다.

## 매입매출 리포트

`hometax revenue [YYYY] [-ytd [YYYY]] [--basis written|issued|transmitted]`는 한 해의 매입매출을
월별로 모아 누계와 함께 냅니다. 올해면 1월 1일부터 한국 시간 오늘까지, 지난해면 1~12월이고 연도를
생략하면 올해입니다. `revenue 2026`은 월별 행과 누계를, `revenue -ytd 2026`은 누계만 보여 줍니다
(`-ytd`에 `-m`을 더하면 월별 행이 다시 붙습니다). 연도를 두 자리에 서로 다르게 쓰면 거절합니다.
`--json`은 형태와 무관하게 `months`와 `ytd`를 모두 담습니다.

| 구분 | 출처 | 조회 방식 | 월 귀속 |
|---|---|---|---|
| 매출 | 전자세금계산서 | 3개월 단위 목록 | `--basis` 날짜(기본 작성일) |
| 매출 | 신용카드 매출 | 연 단위 월별 합계(1분기~현재 분기) | 결제월 |
| 매출 | 현금영수증 매출 | 연 단위 월별 합계 | 거래월 |
| 매입 | 전자세금계산서 | 3개월 단위 목록 | `--basis` 날짜(기본 작성일) |
| 매입 | 사업용카드 | 3개월 단위 목록(공제 분류 전체) | 거래일 |
| 매입 | 현금영수증 매입 | 한 달씩 조회한 가맹점별 합계 | 조회한 달 |

- 금액은 전부 합계금액(부가세 포함)입니다. 카드매출은 공급가액·세액을 나눠 주지 않습니다.
- 현금영수증 매입 응답은 기간 전체의 가맹점별 합계뿐이라 월별 값을 얻으려고 달마다 따로 조회합니다.
- 연 단위 응답에 조회 범위 밖의 달이 섞여 오면 버립니다.
- 사업용카드 매입은 공제·불공제를 모두 더합니다. 공제 판단은 `cards --deduction`으로 따로 봅니다.

## HTTP API

공통 경로는 `/v1/hometax/sessions/{session_id}`이고 기존 Bearer API 키와 로그인 세션을 사용합니다.

| 메서드·경로 | 내용 |
|---|---|
| `GET /business-card-purchases` | 사업용카드 거래내역·공제 분류·금액 합계 |
| `GET /registered-business-cards` | 등록된 사업용카드와 처리상태 |
| `GET /cash-receipt-purchases` | 현금영수증 매입 공제합계·가맹점별 합계 |
| `GET /cash-receipt-sales` | 홈택스 발급 현금영수증 매출 월별 합계 |
| `GET /card-sales` | 신용카드 매출 월별 합계 |
| `GET /business-accounts` | 신고된 사업용계좌 목록 |

사업용카드와 현금영수증 매입은 `start_date`, `end_date`, `page`, `page_size`를 받습니다.
기간은 최대 3개월이고 `page_size`는 10·20·30·50입니다. `cash-receipt-sales`는 `year`,
`card-sales`는 `year`, `quarter_from`, `quarter_to`를 받습니다.

계좌번호와 카드번호는 응답 전에 마스킹합니다. 홈택스 세션 쿠키·통합인증 토큰·원문 응답은 API
응답이나 로그에 포함하지 않습니다. 상류 응답 필드와 금액 합계가 계약과 다르면 추측하지 않고
`FINANCIAL_RESPONSE_CHANGED`로 중단합니다.

## 검증 범위

- 공개된 홈택스 모바일 화면의 고정 조회 action과 입력·출력 VO를 확인했습니다.
- 승인된 본인 계정에서 여섯 조회 action의 세션 전환과 응답 계약을 읽기 전용으로 확인했습니다.
- 2026-09-27 `revenue` 올해 누계 실계정 결과가 `summary --basis written`(매출·매입), `cash-purchases`,
  `cards`의 같은 기간 합계와 일치함을 읽기 전용으로 확인했습니다.
- 테스트 픽스처는 합성 상호·사업자번호·카드번호·계좌번호만 사용합니다.
- 공제여부 변경, 카드 등록, 계좌 등록·해지, 현금영수증 발급·취소는 구현하지 않습니다.
