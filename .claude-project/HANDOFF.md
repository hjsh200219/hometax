---
created: 2026-09-12T14:41:18+09:00
project: hometax
summary: 0.6.0에서 사업용카드·카드매출·현금영수증·사업용계좌 읽기 조회를 CLI와 HTTP API에 추가
---

## Session Digest

홈택스가 제공하는 카드·현금영수증·계좌 관련 읽기 기능을 공개 모바일 화면 계약에서 발굴하고 기존 공동인증서 세션에 연결했다. 포털 SSO 토큰을 고정된 `mob.tbcr`·`mob.tbht` 업무 도메인으로 전달한 뒤 읽기 action만 호출한다.

0.6.0은 등록된 사업용 신용카드와 처리상태, 사업용카드 매입내역, 신용카드 매출 월별 합계, 현금영수증 매입 공제합계, 홈택스 발급 현금영수증 매출 월별 합계, 신고된 사업용계좌 목록을 CLI와 HTTP API로 제공한다. 일반 은행 입출금 거래내역과 PG 경유 카드매출은 홈택스 자료에 없거나 제외되므로 지원하지 않는다.

## Progress

- [x] `FinancialDataClient`와 6종 응답 모델·검증·마스킹 구현
- [x] CLI: `registered-cards`, `cards`, `card-sales`, `cash-purchases`, `cash-sales`, `business-accounts`
- [x] HTTP API: `/registered-business-cards`, `/business-card-purchases`, `/card-sales`, `/cash-receipt-purchases`, `/cash-receipt-sales`, `/business-accounts`
- [x] 사업용카드·현금영수증 3개월 제한과 CLI 장기기간 자동 분할
- [x] 카드번호·계좌번호 마스킹, 고정 action만 허용, 토큰·쿠키·원문 미노출
- [x] 승인된 본인 계정으로 6개 읽기 action과 실제 CLI 종단간 검증
- [x] 합성 단위·API·CLI 테스트 포함 전체 320개 통과
- [x] Ruff lint·format, compileall, 0.6.0 wheel/sdist, Claude 플러그인 검증 통과
- [ ] 원격 push — 사용자 승인 전 로컬 커밋만 유지

## Next Steps

1. 사용자가 push를 지시하면 0.6.0 커밋을 `origin/main`에 푸시한다.
2. PG 정산은 각 결제대행사 API 또는 파일을 별도로 연결한다.
3. 은행 입출금은 은행 API·오픈뱅킹·CSV 수입 중 하나로 별도 모듈화한다.

## Blockers

- 없음.

## Watch Out

- 홈택스 공식 개발자 API가 아니라 공개 웹 클라이언트의 고정 요청을 재현한다. 화면 계약 변경 시 추측하지 않고 `FINANCIAL_RESPONSE_CHANGED`로 멈춘다.
- 사업용카드 자료는 카드사 제출 지연으로 직전 월 자료가 늦게 나타날 수 있다.
- `card-sales`의 홈택스 자료에는 판매·결제대행사 경유분이 제외될 수 있다.
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
