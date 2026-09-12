# HomeTax 프로젝트 메모리

- [test-dates-derive-from-asia-seoul](test-dates-derive-from-asia-seoul.md) — 시각 의존 테스트 날짜는 Asia/Seoul 기준 유도. 리터럴·date.today() 금지
- [test-date-literals-inventory](test-date-literals-inventory.md) — 날짜 리터럴 58개 중 조치 대상은 미래 날짜 1곳
- [invoice-api-bad-date-assertion-is-masked](invoice-api-bad-date-assertion-is-masked.md) — test_invoice_api.py:206 422는 기간 규칙이 아님. 고치면 뒤집힘
- [parser-strictness-fails-the-whole-page](parser-strictness-fails-the-whole-page.md) — 빈 값 한 행이 페이지 전체를 502로 만들었다. 값 부재는 None, 타입 변경만 거절
- [macos-path-compare-needs-nfc-normalization](macos-path-compare-needs-nfc-normalization.md) — 한글 경로는 NFD 저장·NFC 인자. samefile로 비교
- [cert-selection-needs-fingerprint-not-path](cert-selection-needs-fingerprint-not-path.md) — 인증서 기억은 경로+지문. 갱신이면 다시 묻는다
- [cli-write-path-two-preconditions](cli-write-path-two-preconditions.md) — 쓰기는 --wire 필수, 미리보기는 같은 실행 안에서만 유효
- [financial-lists-have-different-schemas](financial-lists-have-different-schemas.md) — 카드매출 목록 셋은 스키마가 다르다. 하나만 읽으면 금액 0
- [identifier-fields-need-the-business-number-gate](identifier-fields-need-the-business-number-gate.md) — *EncCntn은 사업자번호 정규식 통과분만 노출
