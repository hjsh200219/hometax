# HomeTax 인수인계

- 생성: 2026-09-11. 사용자 요청: Agent2 인증서 처리 로직을 재사용한 별도 Python 로그인 API.
- 실행 대상: 우선 Mac 로컬. 세금계산서 발행은 범위 밖.
- 위치: `ProjectTeoul/hometax`.
- 서버: `uv run --env-file .env uvicorn hometax_login.api:create_app --factory --host 127.0.0.1 --port 8787 --no-access-log`.
- API 키는 `.env`에 로컬 생성됨. 값 출력·커밋 금지. 인증서와 비밀번호는 저장하지 않음.
- Mac arm64에서 인증서 암복호화·서명 및 세션/HTTP 테스트 검증.
- 실제 본인 인증서를 이용한 홈택스 로그인은 수행하지 않음. 테스트 통과를 실인증 성공으로 보고하지 말 것.
- 공개 챌린지는 실응답 200 확인. 홈택스 로그인 후 permission 응답 계약은 아직 실인증 미검증.
- PFX는 검증 전용. VID 난수 누락 때문에 로그인은 NPKI DER+KEY 대상.
- 프로토콜 근거와 남은 확인 사항: `docs/PROTOCOL.md`.
- 코드 커밋·원격 생성·push·운영 배포는 하지 않음.
