---
created: 2026-09-12T11:20:00+09:00
project: hometax
summary: CLI와 Claude Code 플러그인으로 배포 형태를 갖추고, 매입 파서 결함을 고치고, 공개 전 실명·사내 참조를 정리했습니다.
---

## Session Digest

조회를 할 때마다 로컬 HTTP 서버를 띄워야 하던 것을 없앴습니다. `hometax` 명령줄 도구를 만들고
인증서 선택·세션 캐시·쓰기 명령을 붙인 뒤 Claude Code 플러그인으로 포장했습니다. 진행 중
실계정 조회에서 매입 목록이 전부 502로 실패하는 결함을 찾아 고쳤고, 공개 전환을 앞두고
히스토리 보안 감사를 돌려 실명 픽스처와 사내 저장소 참조를 정리했습니다.

## Progress

- 완료: 매입 목록 파서 수정. 홈택스가 일부 행의 `tnmNm`을 공백으로 보내 페이지 전체가
  `INVOICE_RESPONSE_CHANGED`로 죽던 것을 `counterparty_name` nullable로 해결(`af4cfc7`).
- 완료: 시각 의존 테스트 정리. 미래 날짜 리터럴 제거와 가려진 422 단언 보강(`af8de29`, `c82aa9f`).
- 완료: 인증서 탐색·선택 기억 모듈(`dcd1250`). 만료 제외, 여러 개면 질문, 경로+지문 저장.
- 완료: CLI 신설(`0048c18`)과 쓰기 명령(`eaf507d`). 조회 6종 + 쓰기 6종, 전역 `--json`.
- 완료: 플러그인 포장(`e6bada8`). `.claude-plugin/`, `skills/hometax/SKILL.md`, LICENSE, README 재작성.
- 완료: 공개 전 정리(`19523e1`). 테스트 픽스처 실명 → 합성, `docs/PROTOCOL.md` 사내 참조 축약.
- 완료: 로컬 플러그인 설치 검증(`claude plugin details hometax` → 스킬 1개).
- 미완료: 저장소 public 전환. 히스토리 재작성 여부를 사용자가 결정해야 합니다.
- 미완료: 발행·정정·취소의 실제 홈택스 전송. 한 건도 보낸 적이 없습니다.

## Next Steps

1. 공개 전환 결정. 실명이 이미 푸시된 커밋 3개(`dcd1250`·`0048c18`·`eaf507d`) diff에 남아 있어,
   `filter-repo`로 지우고 force-push한 뒤 공개할지 그대로 공개할지 정합니다.
2. 공개 후 `/plugin marketplace add hjsh200219/hometax`로 타인 설치 경로를 검증합니다.
3. 발행 실전송을 검증한다면 본인 계정에서 소액 1건으로 합니다. `--wire` 형식을 먼저 확정해야 합니다.
4. HTTP 계층 제거 판단. CLI가 17개 엔드포인트를 덮었는지, 소유자 검사·세션 TTL·요청 직렬화·
   오류 새니타이즈를 어디에 둘지 정한 뒤에 결정합니다.

## Blockers

- 없음. 공개 전환만 사용자 결정 대기입니다.

## Watch Out

- **미리보기와 전송은 같은 실행 안에 있어야 합니다.** preview id는 프로세스 메모리에만 있어
  다음 실행에서 못 씁니다. `--yes`는 "미리보기 후 바로 전송"을 뜻합니다.
- 발행 전송에는 `client.invoice_wire_encoding`(CLI `--wire`)이 필요합니다. 없으면 전송 단계에서 멈춥니다.
- macOS는 파일명을 NFD로 저장하고 셸 인자는 NFC로 들어옵니다. 한글 경로는 문자열이 아니라
  `os.path.samefile`로 비교해야 합니다. APFS는 준 그대로 저장하므로 테스트는 NFD로 만들어야
  재현됩니다.
- 인증서 기억은 경로만으로 부족합니다. 갱신하면 같은 경로에 다른 인증서가 들어오므로 지문을 함께 봅니다.
- CLI는 세션 쿠키를 `~/.hometax/session.json`(0600)에 저장합니다. HTTP API 경로는 여전히 메모리만 씁니다.
- 쓰기 저널(`~/.hometax/writes.sqlite3`)이 미확정 작업을 막습니다. 지워서 우회하지 말고 홈택스에서
  실제 반영 여부를 먼저 확인합니다.
- 테스트 픽스처에 실명·실제 사업자번호를 넣지 마세요. 이 저장소는 공개 예정입니다.

## Files Touched

- `src/hometax_login/cli.py`, `cert_discovery.py`, `local_session.py`, `invoices.py`
- `tests/test_cli.py`, `test_cert_discovery.py`, `test_local_session.py`, `test_invoices.py`, `test_invoice_api.py`
- `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `skills/hometax/SKILL.md`
- `README.md`, `LICENSE`, `docs/INVOICES.md`, `docs/PROTOCOL.md`, `pyproject.toml`
