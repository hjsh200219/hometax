---
created: 2026-09-27T09:30:00+09:00
project: hometax
summary: 0.9.2 Windows 지원(tzdata·저널 권한 검사·탐색 경로·출력 인코딩), Mac 389·Windows 387+2skip. 세무대리인·은행 fast 는 보류/결정 대기
---

## Session Digest

0.9.6(2026-11-05): **첫 실발행 성공** — `issue --wire signedinfo --trust-signed-info`(웹 동등 옵트인)로 1건 발급,
매출 목록·상세 재조회 일치(수신 이메일 포함). 엄격 다이제스트 대조는 미해결: `trnsXmlCntn` 은 `xmlCntn` 의 Base64 라
후보가 안 되고, 서버 다이제스트 규칙은 발급된 원본 XML(R02 `fileDwnYn=Y` 다운로드, 아직 미구현)과 대조해야 찾는다.
도구는 C08(이메일 발송)을 부르지 않지만, 발급 시각에 메일발송목록(UTEETBAB16 R01→R03)에 수신 메일이 등록됐고 웹 발행 건과
상태가 같았다(둘 다 「발송중」). 재발송 액션은 ATEETBAF001U01. 다음: 엄격 다이제스트 규칙(R02 원본 XML 다운로드로 대조).

0.9.5(2026-11-05): `--wire signedinfo` 추가. 홈택스 웹 HTML5 발급은 C04 응답 `szSignedInfo` 문자열에
RSA-SHA256(PKCS#1 v1.5) 서명 Base64 를 `xmlCntn`·`sSignData` 로, 원본 XML 을 `ognXML` 로, `brwerFg=Y` 로 C05 에 보낸다
(공개 스크립트 UTEETBAA01·common_teet_sgnt·ML4Web_API 확인). SignedInfo DigestValue 가 대조 끝난 XML 로 계산한 값과
같을 때만 서명. 단건 신규 발행만(정정 2장은 거절). 실발행 2회는 `xmlCntn` 다이제스트 불일치로 C05 전 중단 —
실 SignedInfo: C14N 정준화, 변환 C14N→XPath(HOMETAX_XPATH 동일), rsa-sha256/sha256. 다음 후보는 `trnsXmlCntn`.
홈택스 웹은 szSignedInfo 를 대조 없이 서명한다(웹 동등 모드는 사용자 결정 대기). 보안 리뷰 반영: XPath·변환 조합 고정.

0.9.4(2026-11-05): 실계정 신규 발행 첫 시도 4회, 발급 0건. (1) 원인 예외가 `from None` 으로 버려져 미확정 오류에
멈춘 단계를 붙임 (2) C04 XML 대조가 사업자 정보 주소 끝 공백으로 어긋남 → 양쪽 앞뒤 공백 제거·다른 항목 이름 표시
(3) C05 거절 응답은 `errorCd` 가 아니라 `code` 로 옴 → `code` 도 표시. C05 결과는 `ETICMZ0008` 전자서명 검증 실패
(base64; raw 코드는 미수집). 다음: 발급된 계산서 XML(상세 R02 `downloadView=Y`, 아직 src 미노출)의 ds:Signature 구조를
`invoice_signing.sign_invoice_xml` 결과와 대조. 저널에 같은 거래 4건이 `unknown` 으로 남아 있음(해제 기능 없음).

0.9.3(2026-11-04): 로그인 거부 시 홈택스 오류 코드를 보여 준다(원문은 넣지 않고 코드+자체 안내문).
EDB 갱신 인증서(2027-10-06 만료) 실로그인이 `ETINFZ0109` 홈택스 미등록 인증서로 거부된 것을 계기로 추가.
EDB 는 홈택스 웹에서 인증서 등록 뒤 `hometax login --company edb`(별칭 edb, `HOMETAX_PW_EDB`)로 재확인.

0.9.2(2026-11-04): Windows 지원. QA VM `vm-develop`(Windows 10 x64 한국어판, Python 3.12)에서 실측한 결함 4가지를 고쳤다.
1) `ZoneInfo("Asia/Seoul")` 이 tzdata 없이 import 시점에 죽음 → `tzdata; sys_platform == 'win32'` 의존성.
2) 쓰기 저널이 부모 폴더 `st_mode & 0o077` 검사로 Windows(늘 0o40777)에서 항상 WRITE_JOURNAL_UNAVAILABLE
   → `write_journal.POSIX_MODES` 로 Windows 는 권한 비트 검사·chmod 생략(NTFS 프로필 ACL 에 맡김, tech-debt 기록).
3) 파이프·파일 출력이 cp949 라 그 밖 문자에서 UnicodeEncodeError → Windows 에서 stdout/stderr `errors="replace"`.
4) 테스트 conftest 가 Windows asyncio 의 루프백 socketpair 까지 막음 → 루프백만 허용.
탐색 경로는 `%USERPROFILE%\AppData\LocalLow\NPKI`·이동식 디스크 `X:\NPKI` 추가, 인증서 없음 오류에 찾아본 곳 표시.
VM 확인: pip·`uv sync --locked` 양쪽 387 passed·2 skipped(POSIX 권한 비트), ruff·format 통과, 합성 인증서를
LocalLow 에 두고 `hometax certs` 탐색 성공(확인 뒤 삭제). Windows 실계정 로그인은 미검증(VM 에 실인증서 없음).
VM 으로 복사할 때 macOS tar 는 `COPYFILE_DISABLE=1` 을 줘야 `._*` 파일이 안 생긴다(ruff E902 원인이었음).

0.9.0: `hometax list`(번호·인증서·종류 개인/사업자·별칭·만료·세션), 모든 조회에 `--company <번호|별칭|상호 일부>`,
`hometax alias <이름> <번호>`, 비밀번호 `HOMETAX_PW_<별칭>` → `HOMETAX_PW`, 세션 `~/.hometax/sessions/<지문16>.json`.
0.8.1 에서 --cert 로 사업자를 바꿔도 캐시 세션이 직전 사업자로 조회되던 버그를 고쳤다.
독립 리뷰 HIGH 2건(기본값 없는 비대화형 세션 재사용 회귀, TOML 제어문자 이스케이프) 수정·뮤테이션 확인.
개인 인증서(`이름()…`)는 로그인만 되고 사업자 자료는 BUSINESS_REQUIRED. 세무대리인 로그인은 계정·국세청 사전협의가
없어 보류(참고: myjung/hometax-agent-client 도 고객별 세션 방식, 세무대리 로그인 없음).


0.8.0: `hometax vat [YYYY-1|YYYY-2]`(예정·확정 분기별 매출세액−공제 매입세액)와
`hometax revenue [YYYY] --by counterparty`(세금계산서 거래처×월, 3개월 이상 반복 `*`)를 추가했습니다.
실계정 2026-1기 예상 납부세액 1,374,347, 2기 예정분 −79,421(8월 매출 세금계산서 0건).
거래처별 표에서 8월 매입 급증=에이투케이인터랙티브 1,980,000, 토스페이먼츠 매입(PG 가맹)이 확인됐습니다.
은행 거래내역은 새 repo 로 "빠른조회(fast)" 경로를 검토 중입니다 — 아래 Next Steps.


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

0. 은행 연동(사용자 결정 대기): fast 경로(계좌번호+빠른조회 비밀번호+생년월일/사업자번호, ID/PW 불필요)를
   hometax 와 분리된 로컬 도구로. 은행별 실계좌 1개로 검증해야 하고 가상 키패드 때문에 Playwright 필요.
   카카오뱅크 등 인터넷은행은 빠른조회가 없어 파일 가져오기. clobe 는 CODEF(codefCert) 기반.
   참고 OSS: Beomi/simple_bank_korea(KB), jungyeup/bank_api(19개 은행 목록, 코드 일부·무라이선스).

1. 새 PC에서는 git pull 후 uv sync --locked.
2. 플러그인 설치본은 버전 캐시다. 스킬 문서를 바꾸면 버전을 올리고 marketplace update → uninstall/install.
3. 실제 발행 검증이 필요하면 별도 사용자 지시에 따라 한정된 거래와 전송 프로필로 검증한다.

## Watch Out

- vat 카드매출 세액은 달별 합계를 한 번에 ÷11 환산. 봉사료가 totaStlAmt 에 포함인지 미확인이라 빼지 않는다.
  2026-11-05 실계정 재조회: 2025·2026 카드매출은 판매(결제)대행 1행뿐이고 그 금액 필드는 `sumTipExclAmt`(봉사료 제외)라
  현재 계정에는 영향이 없다. totaStlAmt 의미는 카드사 제출 행(`crdcTrsBrkdMateAdmDVOList`)에 tip 이 0 아닌 자료가 생겨야 확인된다.
- vat 는 세금계산서 불공제·개인사업자 신용카드매출 세액공제·예정고지를 반영하지 않는 참고 추정이다.

- 카드매출은 공급가액·세액을 나눠 주지 않는다. revenue 를 공급가액 기준으로 바꾸려면 추정이 들어간다.
- revenue 의 사업용카드 매입은 공제·불공제 전체 합이다. 공제 판단은 `cards --deduction`.
- 이전 세션 Watch Out(쓰기 재인증·저널 삭제 금지·네트워크 차단 테스트·실데이터 금지)은 그대로 유효하다.
  `.claude-project/HANDOFF.md.prev-20260927`에 직전 인계서가 있다.

## Files Touched

- src/hometax_login/{revenue,cli}.py, tests/test_revenue.py
- README.md, docs/FINANCIALS.md, skills/hometax/SKILL.md, pyproject.toml, uv.lock, .claude-plugin/plugin.json
