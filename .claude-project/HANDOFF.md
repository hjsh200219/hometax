---
created: 2026-09-12T15:40:00+09:00
project: hometax
summary: 0.6.0 금융자료 조회를 추가하고(다른 세션), 독립 리뷰로 찾은 P0 2건과 카드매출 오집계를 고쳐 push했습니다
---

## Session Digest

**두 세션이 이어서 작업했습니다.** 앞 세션(Codex)이 0.6.0 금융자료 조회를 구현했고, 이어진 세션이
독립 코드 리뷰와 실계정 대조로 결함 3종을 찾아 고친 뒤 둘을 함께 push했습니다.

### A. 기능 추가 (앞 세션)

홈택스가 제공하는 카드·현금영수증·계좌 관련 읽기 기능을 공개 모바일 화면 계약에서 발굴하고 기존 공동인증서 세션에 연결했다. 포털 SSO 토큰을 고정된 `mob.tbcr`·`mob.tbht` 업무 도메인으로 전달한 뒤 읽기 action만 호출한다.

0.6.0은 등록된 사업용 신용카드와 처리상태, 사업용카드 매입내역, 신용카드 매출 월별 합계, 현금영수증 매입 공제합계, 홈택스 발급 현금영수증 매출 월별 합계, 신고된 사업용계좌 목록을 CLI와 HTTP API로 제공한다. 일반 은행 입출금 거래내역과 PG 경유 카드매출은 홈택스 자료에 없거나 제외되므로 지원하지 않는다.

### B. 리뷰와 수정 (이어진 세션)

리뷰가 P0 2건을 짚었고, 실계정 응답을 원본과 대조해 오집계 1건을 더 찾았습니다.

- 금액 필드가 사라지면 `integer(..., default=0)`이 "0원"으로 통과시켰습니다. 기본값을 없애고
  항목마다 `공급가액+세액+봉사료=합계` 교차검산을 넣었습니다.
- `*EncCntn` 식별번호가 검증 없이 응답에 실렸습니다. `invoices.py`와 같은 사업자번호 정규식
  게이트를 3곳에 적용해, 주민번호 형태나 암호문은 `None`이 됩니다.
- `card_sales`가 스키마가 다른 세 목록 중 하나만 골라 같은 필드명으로 읽어, **실계정 9,900원
  매출이 0원으로 보고**됐습니다. 목록별 필드 맵으로 나눠 카드사분과 대행분을 합산하고 분기
  요약은 대조 전용으로 바꿨습니다.
- 페이징은 `pageNum`·`pageSize`·`totalCount` echo와 행 수를 검증하고, CLI 루프에 0.2초 간격과
  200쪽 상한을 넣었습니다.
- 새 가드를 무는 테스트 7건을 추가했고, 수정 전 코드에서 7건 모두 실패하는 것을 확인했습니다.

## Progress

- [x] `FinancialDataClient`와 6종 응답 모델·검증·마스킹 구현
- [x] CLI: `registered-cards`, `cards`, `card-sales`, `cash-purchases`, `cash-sales`, `business-accounts`
- [x] HTTP API: `/registered-business-cards`, `/business-card-purchases`, `/card-sales`, `/cash-receipt-purchases`, `/cash-receipt-sales`, `/business-accounts`
- [x] 사업용카드·현금영수증 3개월 제한과 CLI 장기기간 자동 분할
- [x] 카드번호·계좌번호 마스킹, 고정 action만 허용, 토큰·쿠키·원문 미노출
- [x] 승인된 본인 계정으로 6개 읽기 action과 실제 CLI 종단간 검증
- [x] 합성 단위·API·CLI 테스트 포함 전체 320개 통과
- [x] Ruff lint·format, compileall, 0.6.0 wheel/sdist, Claude 플러그인 검증 통과
- [x] 독립 코드 리뷰(P0 2·P1 5·P2 4) 수행과 P0·P1 조치
- [x] 실계정 6개 명령 재확인 — card-sales 9,900원 정상, 나머지 5개 오탐 거절 없음
- [x] 전체 327개 테스트·ruff·wheel 통과 후 `origin/main` push 완료
- [x] 법적 경계 원문 확인 — 행정규칙 71개 조문에 자동화 금지 없음, 별도 이용약관 페이지 없음,
  개인정보처리방침 제5조 ⑩만 대리인 자동화 조회를 사전협의 대상으로 안내. README 면책과
  SKILL.md에 본인 사용/대행 경계와 차단 가능성을 명시

## Next Steps

1. 리뷰 P2 4건을 처리할지 정한다 — 권한 오류 코드 오도(`_business()`가 세금계산서 화면을 먼저 탐),
   상류 마스킹 문자열 무검증 통과, KST 헬퍼 3곳 중복, `business_accounts`의 `totalCount` 누락.
2. 은행 입출금은 은행 API·오픈뱅킹·CSV 수입 중 하나로 별도 모듈화한다.
3. 발행 실전송 검증은 본인 계정 소액 1건으로 `--wire` 형식을 확정한 뒤 진행한다.

## Blockers

- 없음.

## Watch Out

- 법적 근거를 다시 조사하지 말 것. 확인 결과는
  `.claude-project/memory/no-automation-ban-but-delegation-needs-consultation.md`에 있다.
- 차단을 만나면 우회하지 말고 사용자에게 알린다(SKILL.md에 같은 지시가 있다).

- 홈택스 공식 개발자 API가 아니라 공개 웹 클라이언트의 고정 요청을 재현한다. 화면 계약 변경 시 추측하지 않고 `FINANCIAL_RESPONSE_CHANGED`로 멈춘다.
- 사업용카드 자료는 카드사 제출 지연으로 직전 월 자료가 늦게 나타날 수 있다.
- `card-sales` 응답에는 스키마가 다른 목록이 셋 들어 있다. 카드사 제출분과 판매대행분을 각각의
  필드로 읽어 합산하고, 분기 요약은 중복이라 합산하지 않고 대조만 한다. 한 목록만 골라 같은
  필드명으로 읽으면 금액이 통째로 0이 된다(실제로 그렇게 새 나갔다).
- 금액 파싱에 `default=0`을 다시 넣지 말 것. 필드가 사라져도 조용히 0원이 보고된다.
- `cash-sales`는 홈택스 발급 시스템을 통한 현금영수증 월별 현황이다.
- `business-accounts`는 신고된 계좌 메타데이터만 반환하며 은행 입출금은 제공하지 않는다.
- 저장소는 public이다. 실제 상호·사업자번호·카드·계좌·금액을 테스트나 문서에 넣지 않는다.
- 발행·정정·취소의 실제 홈택스 전송 호환성은 여전히 미검증이다.

## Files Touched

- src/hometax_login/financials.py
- src/hometax_login/protocol.py
- src/hometax_login/api.py
- src/hometax_login/cli.py
- tests/test_financials.py
- tests/test_financial_api.py
- tests/test_cli.py
- docs/FINANCIALS.md
- docs/PROTOCOL.md
- README.md
- skills/hometax/SKILL.md
- .claude-plugin/plugin.json
- .claude-plugin/marketplace.json
- pyproject.toml
- uv.lock
