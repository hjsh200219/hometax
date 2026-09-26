---
created: 2026-09-27T09:30:00+09:00
project: hometax
summary: 0.7.0 매입매출 리포트(hometax revenue) 추가, 실계정 대조 일치, 360개 테스트 통과
---

## Session Digest

`hometax revenue [YYYY]`(월별+누계)와 `hometax revenue -ytd [YYYY]`(누계만)를 추가했습니다.
매출은 세금계산서·카드매출·현금영수증 매출, 매입은 세금계산서·사업용카드·현금영수증 매입을
월별로 모으고 금액은 합계금액(부가세 포함), 세금계산서 월 귀속은 작성일 기본(`--basis`)입니다.
실계정 2026 YTD 결과가 `summary --basis written`·`cash-purchases`·`cards` 합계와 일치했습니다.

## Progress

- [x] `revenue.py`(월 집계·표 렌더링 순수 함수) + `cli.py` `cmd_revenue`·`all_pages`·`revenue_mode`
- [x] 현금영수증 매입은 가맹점별 기간 합계뿐이라 달마다 따로 조회
- [x] 연도 두 번 지정(서로 다름)·미래 연도·2000년 미만은 로그인 전에 exit 2
- [x] 360개 테스트, Ruff, format; 뮤테이션 2종(기준일 무시·월 분할 제거)을 테스트가 잡음
- [x] README·docs/FINANCIALS.md·SKILL.md·버전 0.7.0
- [ ] 실제 세금계산서 발행·정정·취소의 홈택스 전송 호환성 검증(이전부터 미결)

## Next Steps

1. 새 PC에서는 git pull 후 uv sync --locked.
2. 플러그인 설치본은 버전 캐시다. 스킬 문서를 바꾸면 버전을 올리고 marketplace update → uninstall/install.
3. 실제 발행 검증이 필요하면 별도 사용자 지시에 따라 한정된 거래와 전송 프로필로 검증한다.

## Watch Out

- 카드매출은 공급가액·세액을 나눠 주지 않는다. revenue 를 공급가액 기준으로 바꾸려면 추정이 들어간다.
- revenue 의 사업용카드 매입은 공제·불공제 전체 합이다. 공제 판단은 `cards --deduction`.
- 이전 세션 Watch Out(쓰기 재인증·저널 삭제 금지·네트워크 차단 테스트·실데이터 금지)은 그대로 유효하다.
  `.claude-project/HANDOFF.md.prev-20260927`에 직전 인계서가 있다.

## Files Touched

- src/hometax_login/{revenue,cli}.py, tests/test_revenue.py
- README.md, docs/FINANCIALS.md, skills/hometax/SKILL.md, pyproject.toml, uv.lock, .claude-plugin/plugin.json
