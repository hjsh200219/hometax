# 로그인 프로토콜 근거와 검증 범위

## 기존 코드에서 가져온 부분

사용자 소유 `internal-csharp`, `origin/internal-branch`의
`internal-commit` 기준
`internal Npki loader`의 인증서 로딩과 SEED 개인키 복호화 흐름을 Python으로 옮겼습니다.
심평원 `HiraApiClient`의 URL·SSO 흐름과 공백 한 글자에 대한 CMS 서명은 재사용하지 않았습니다.

## 홈택스 참고 자료

- [jc-lab/korea-pki의 직접 인증 예제](https://github.com/jc-lab/korea-pki/tree/e98a86d3eede6df8f847fffe141214258e9c7864/examples/hometax)
  — 챌린지, SHA256 RSA 서명, Base64 `logSgnt`, `permission.do` 확인 흐름 참고.
- [2026-08-25 공개 PoC](https://github.com/zisu17/zisu17.github.io/blob/4ba83a1794ed1216a298c75b5c457d18d54ffd40/_posts/2026-08-25-%5BDev%5D-%EB%B8%8C%EB%9D%BC%EC%9A%B0%EC%A0%80-%EC%97%86%EC%9D%B4-%ED%99%88%ED%83%9D%EC%8A%A4-%EA%B3%B5%EB%8F%99%EC%9D%B8%EC%A6%9D%EC%84%9C-%EB%A1%9C%EA%B7%B8%EC%9D%B8-%EC%9E%90%EB%8F%99%ED%99%94%ED%95%98%EA%B8%B0.md)
  — 현재 JSON 챌린지 요청과 `lgnRsltCd=01` 확인 근거.
- [hometax-agent-client 0.1.0a1](https://pypi.org/project/hometax-agent-client/0.1.0a1/)
  — 인증 규격 변경과 미구현 인증서 경로의 한계 참고. 이 라이브러리를 의존성으로 설치하지 않았습니다.

위 자료는 국세청 공식 API 계약이 아닌 공개 참고 구현입니다.

## 구현 흐름

1. 메인 페이지 GET으로 세션 쿠키 수신.
2. `ATXPPZXA001R01` 액션에 JSON POST해 `pkcEncSsn` 수신.
3. NPKI 개인키로 챌린지를 RSA PKCS#1 v1.5 + SHA256 서명.
4. 챌린지·인증서 시리얼(hex bytes)·KST 시각·서명(Base64)을 `$`로 결합한 뒤 Base64 인코딩.
5. `pubcLogin.do`에 PEM 인증서, 서명 결과, NPKI VID 난수와 로그인 구분을 form POST.
6. 응답 코드 `S`, 로그인 결과 `01`을 모두 요구.
7. 같은 cookie jar로 `permission.do`를 호출해 `resultMsg.sessionMap` 사용자 식별자를 확인.

## 2026-09-11 직접 확인한 내용

- macOS arm64 / uv Python 3.12.13에서 실행.
- 홈택스 메인 페이지 HTTP 200.
- 공개 챌린지 POST HTTP 200, JSON `pkcEncSsn` 존재 확인. 값과 쿠키는 기록하지 않음.
- 비로그인 `permission.do`는 `resultMsg`만 있고 인증 사용자 `sessionMap`이 없음.
- 로컬 서버 상태 조회 200 / 인증 없는 검증 요청 401 / 합성 PFX 검증 200.
- 이후 사용자 승인 실인증: NPKI `04` 로그인과 `sessionMap` 사용자 식별자, 후속 재확인 성공.
- 공식 `UTXPPABA01.xml`의 `fn_lgnClCdFromHtml5`에서 `03=browser`, `04=hdd` 확인.
- 로그인 구분 `03`과 다른 인증서의 호환성은 미검증.

## 전자세금계산서 조회 (2026-09-11)

- 공식 `/websquare/app.js`의 운영 ET 호스트는 `https://teet.hometax.go.kr`.
- 포털 로그인만으로 ET 세션이 생기지 않는다. 포털 `/token.do` JSON을 메모리에서 받아
  ET `/permission.do?screenId=UTEETBDA01&domain=hometax.go.kr`에 `popupYn:false`와 함께 전송한다.
- `sessionMap` 사용자와 사업자 TIN을 확인한다. 사업자 선택은 `common_teet.xml`의
  `fn_haboTitleChgFortxfrBmanLgn`/`fn_getSessionTin`에 따라 `tin`/`cnvrTin`을 사용한다.
- 목록 화면 `/ui/et/b/d/a/UTEETBDA01.xml`: 조회 action `ATEETBDA001R01`,
  입력 `etxivIsnBrkdTermDVOPrmt`, 출력 `etxivIsnBrkdTermDVOList`와 `pageInfoVO`.
- **거짓 0건 회귀:** `/websquare/config.js`의 `allValue`는 `all`이다.
  `etxivClsfCd`, `etxivKndCd`, `isnTypeCd`를 빈 문자열로 보내면 정상 응답이어도
  실제 내역이 누락된다. 세 값을 `all`로 바꿔 사용자가 알려준 실제 건의 조회를 확인했다.
- `prhSlsClCd`: `01=매출`, `02=매입`. `dtCl`: `01=작성일`, `02=발급일`, `03=전송일`.
- 상세 팝업 `/ui/et/b/d/a/UTEETBDA38.xml`: 조회 action `ATEETBDA001R02`,
  본문 `etxivIsnBrkdTermDVO`, 품목 `lsatInfrBizSVOList`.
- 목록의 승인번호는 `8자리-8자리-8자리`; API와 상세 요청에서는 하이픈을 제거한다.
  앞 8자리는 숫자, 뒤 16자리는 영문·숫자다. 매입 실응답에서 영문 포함 확인, 대소문자 보존.
- 실검증: 동일 사업자의 9월 매출 1건에 대한 목록·합계·상세의 건수/금액 일치.
  다량 페이지 수집과 다른 사업자는 합성 응답 테스트 대상이며 실운영 검증을 뜻하지 않는다.
- 매입 목록·상세도 동일 인증서로 실검증. 등록 거래처 전체 11건, 거래처명 검색 1건 확인.
- 등록 거래처 화면 `/ui/et/b/a/e/UTEETBAB02.xml`, 읽기 action `ATEETBAE001R06`,
  응답 `myClplcListDVO`/`pageInfoVO`. 검색은 `txprNm`, `txprDscmNoEncCntn`, `rprsFnm`;
  빈 검색값은 이 화면에서는 전체를 의미한다. 정렬 `srtClCd=1`, `srtOpt=01`은 이름 오름차순.
- 거래처 삭제 `ATEETBAE001D03`, 주거래처 변경 `ATEETBAB002U01`, 수정 화면은 호출하지 않는다.
- 발행/수정/취소/신고 요청은 수행하지 않는다. 거래 내용·인증서·비밀번호·토큰·쿠키를 문서에 기록하지 않는다.

PFX/P12 로딩은 가능하지만 원본 개인키의 VID 부가 속성 복원은 지원하지 않으므로 홈택스
로그인 요청은 `CERT_RANDOM_MISSING`으로 거부합니다. NPKI 종류·홈택스 등록 상태·추가 인증
필요 여부는 실제 본인 인증서로 한 번의 로그인 절차를 검증해야 합니다.
