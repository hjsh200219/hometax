# HomeTax 인수인계

- 생성: 2026-09-11. 사용자 요청: Agent2 인증서 처리 로직을 재사용한 별도 Python 로그인 API.
- 실행 대상: 우선 Mac 로컬. 세금계산서 발행은 범위 밖.
- 위치: `ProjectTeoul/hometax`.
- 서버: `uv run --env-file .env uvicorn hometax_login.api:create_app --factory --host 127.0.0.1 --port 8787 --no-access-log`.
- API 키는 `.env`에 로컬 생성됨. 값 출력·커밋 금지. 인증서와 비밀번호는 저장하지 않음.
- Mac arm64에서 인증서 암복호화·서명 및 세션/HTTP 테스트 검증.
- 2026-09-11 사용자 승인 실인증: NPKI 디스크(`04`) 로그인 및 permission 세션 재확인 성공.
- 전자세금계산서 조회 API 추가: 목록(매출/매입), 기간 합계, 목록에서 확인된 승인번호 상세.
- 등록 거래처 조회 API도 추가: 거래처명/사업자번호/대표자명 검색, 페이지 이동. 관리 쓰기 없음.
- 승인번호 뒤 16자리에 영문 포함 가능. 숫자 전용 정규식 사용 금지, 대소문자 보존.
- ET SSO는 포털 `/token.do` → teet `/permission.do`. 토큰/쿠키는 메모리만 사용.
- 중요 회귀: 전체 필터 3개는 빈 문자열이 아닌 `all`. 빈값은 정상 응답이어도 거짓 0건을 만들었다.
- 실검증: 9월 매출 목록·합계·상세 일치, 매입 목록·상세 성공, 등록 거래처 전체/이름 검색 성공.
  다량 페이지·다른 인증서는 별도 실검증하지 않음.
- 상세 계약/제한: `docs/INVOICES.md`, 근거: `docs/PROTOCOL.md`.
- PFX는 검증 전용. VID 난수 누락 때문에 로그인은 NPKI DER+KEY 대상.
- 프로토콜 근거와 남은 확인 사항: `docs/PROTOCOL.md`.
- 초기 구현은 private GitHub에 push됨(`8d5d837`). 이번 조회 기능은 push·배포 승인 없음.
