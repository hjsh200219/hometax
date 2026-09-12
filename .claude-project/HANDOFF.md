---
created: 2026-09-12T22:52:00+09:00
project: hometax
summary: F01-F05와 cryptography 보안 수정 d330dc0 push, 343개 테스트 통과, 문서 맵·메모리 정리
---

## Session Digest

외부 기능·보안 보고서가 지적한 F01-F05를 수정하고 `d330dc0`을 origin/main에 push했습니다.
잘못된 조회 날짜를 세션 생성 전에 거절하고, --json 옵션을 명령 전후 모두 지원합니다.
CLI 발행·정정·취소는 선택 인증서로 새 로그인한 뒤 미리보기/전송하며,
신규 발행 ID와 저널 v2 내용 가드가 미확정 요청의 새 UUID 재전송을 막습니다.
cryptography는 50.0.1로 갱신했습니다. sh-git-push의 Pack 5개 역할과 문서·구조·품질 감사를 완료했습니다.

## Progress

- [x] F01/F05: 잘못된 날짜·역전 기간·미래 날짜/연도를 로그인 전에 사용 오류(exit 2)로 처리
- [x] F04: --json을 하위 명령 전후 모두 지원; 파서 monkeypatch/type-ignore 없이 구현
- [x] F02: CLI --yes 발행·정정·취소에서 새 인증 후 미리보기/전송; 인증서 일치 검사 유지
- [x] F03: 정규화된 요청의 안정적인 UUID; 사업자 범위+내용 기준 미확정 가드
- [x] v1 기록을 보존하는 원자적 저널 v2 확장과 동시 요청 차단
- [x] cryptography 50.0.1 잠금 및 합성 NPKI·PFX·XML 서명 검증
- [x] 343개 테스트, Ruff, format, compileall, wheel/sdist 통과
- [x] 문서 링크·런타임 import 점검, 루트 문서 맵과 CLAUDE 연결
- [x] 코드 커밋 d330dc0 push
- [ ] 실제 세금계산서 발행·정정·취소의 홈택스 전송 호환성 검증

## Next Steps

1. 새 PC에서는 git pull 후 uv sync --locked. 검증 명령은 AGENTS.md / docs/QUALITY.md 참조.
2. 실제 발행 검증이 필요하면 별도 사용자 지시에 따라 한정된 거래와 전송 프로필을 검증한다.
3. 기존 미확정 저널 기록은 실제 반영 여부를 먼저 대조한다. 자동 재전송·삭제로 풀지 않는다.
4. 추가 검토 대상은 docs/exec-plans/tech-debt-tracker.md 및 이전 금융자료 리뷰 이력을 참고한다.

## Blockers

- 코드 검증·push 차단 문제 없음.
- 실제 발행 호환성은 모의 테스트만으로 확정할 수 없다.

## Watch Out

- 법적 경계 확인 결과는 .claude-project/memory/no-automation-ban-but-delegation-needs-consultation.md 참조.
  기존 조사를 반복하지 말고 본인 사용/대행 경계와 차단 시 중단 지침을 유지한다.
- CLI 조회 쿠키 캐시는 서명 인증 근거가 아니다. 쓰기 직전 선택 인증서로 재인증한다.
- v1 미확정 행에는 내용 해시가 없어 신규 발행이 보수적으로 차단된다. 저널을 지우지 않는다.
- CI·정적 타입 검사기는 이번 작업에서 추가하지 않았다.
- tests/conftest.py가 외부 socket 연결을 차단한다. 합성 fixture/모의 HTTP 검증을 유지한다.
- 이번 수정에서는 실발행을 수행하지 않았다. 기존 로그인·조회 실검증 이력과 구분한다.
- 같은 패키지의 API와 CLI는 저장 위치가 다를 수 있다. 쓰기 저널 경로를 바꿔 이전 기록을 잃지 않는다.
- 카드매출은 카드사분과 홈택스 제공 PG 대행분을 합산하며 분기 요약을 중복 합산하지 않는다.
- 저장소에 실제 인증서·비밀번호·쿠키·거래 원문을 넣지 않는다. 로컬 백업 HANDOFF.md.prev*는 ignored.

## Files Touched

- src/hometax_login/{cli,invoice_operations,issuance_models,write_journal}.py
- tests/{conftest,test_cli,test_cli_invoice_safety,test_invoice_operations,test_write_journal}.py
- pyproject.toml / uv.lock
- README.md / docs/{ISSUANCE,INVOICES,COUNTERPARTIES,PROTOCOL,QUALITY}.md
- AGENTS.md / CLAUDE.md / ARCHITECTURE.md / docs/harness/
- skills/hometax/SKILL.md / .claude/settings.json / .gitignore
- .claude-project/memory/invoice-cli-reauth-and-content-guard.md
