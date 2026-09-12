# 로그인 프로토콜 근거와 검증 범위

## 기존 코드에서 가져온 부분

저자가 소유한 비공개 C# 구현의 NPKI 인증서 로딩과 SEED 개인키 복호화 흐름을 Python으로
옮겼습니다. 그 구현의 다른 기관 연동(URL·SSO 흐름·CMS 서명)은 재사용하지 않았습니다.

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
- 목록 화면의 **일괄 삭제** `ATEETBAE001D03`와 주거래처 표시 변경 `ATEETBAB002U01`은 미지원이다.
  0.3.0의 **단건 삭제**는 아래 상세 화면 계약 `ATEETBAE001D04`만 사용한다.
- 위 조회 검증에서는 발행/수정/취소/신고 요청을 수행하지 않았다. 거래 내용·인증서·비밀번호·토큰·쿠키를 문서에 기록하지 않는다.

## 거래처 관리 확장 (0.3.0)

- 0.3.0에서 주소록 등록/수정/단건 삭제를 미리보기→명시적 확인으로 추가했다. 세금계산서 발행은 이후 0.4.0에서 추가했으며 [발행 API](ISSUANCE.md)의 검증 한계를 적용한다.
- 등록 화면 `UTEETBAB04`: 단위과세 조회 `ATTABZAA001R05`, 납세자 확인 `ATTABZAA001R01`,
  중복 확인 `ATEETBAA001R04`, 등록 `ATEETBAA001C01`.
- R05는 공개 코드의 `mpbCtlDVO.count`와 실응답의 `mpbCtlDVOList` 형식을 구분한다.
  목록형의 빈 목록을 수용하려면 페이지 총 건수도 0이어야 한다. 미지정/불일치 건수는 오류다.
- R01의 `bmanCrpNtplDVO.tin/txprClsfCd/txprDscmNoEncCntn`을 실조회로 확인했다.
  R04 실응답의 중복 건수 `clplcCnt`는 JSON 루트에 있다. 기존 `response.clplcCnt`도 지원하지만
  건수가 없으면 0으로 간주하지 않는다.
- 상세 화면 `UTEETBAB03`: `ATEETBAE001R07`로 담당자/식별자를 읽고,
  `ATEETBAE001U02` 수정 또는 `ATEETBAE001D04` 단건 삭제 요청을 구성한다.
- 실조회에서 담당자 1행(주담당자)도 확인했다. 없는 역할은 폼과 동일하게 빈 담당자/식별자로 표현하고,
  기존 담당자의 `chrgSn`은 보존한다. 미지정 API 필드는 기존 값으로 병합한다.
- 실제 쓰기 액션은 미실행. 서버 쓰기 기본 false, confirm true, 5분 만료 미리보기,
  영속 SQLite 저널과 대상/사업자 스코프 재확인, 전송 후 재조회 검증을 적용했다.
- 신규 종사업장 등록은 팝업 선택 규격의 추가 검증이 필요하여 409로 거절한다.

## 발행·정정·취소 참조 구현 (0.4.0)

- 사용자 승인으로 `lxml`/`xmlsec` 추가. API·로컬 서명·모의 전송을 구현했지만 실발행은 하지 않았다.
- 일반 화면 `UTEETBAA01`: `ATEETBAA002R04`로 작성일 기준 발급 유형 검사,
  `ATEETBAA002C04` XML 생성, `ATEETBAA002C05` 최종 발급.
- 기재사항 정정(01) 화면 `UTEETBAA44`, 계약 해제(04) `UTEETBAA48`, 중복발급 취소(06) `UTEETBAA42`.
  수정 XML 생성/최종 발급은 `ATEETBAA003C03`/`ATEETBAA003C04`이다.
- `isnScrnClCd=10`은 공동인증서 건별 발급 구분이다. 화면 번호 42/44/48과 혼동하지 않는다.
  2번째 공급자 입력 키는 공개 `UTEETBAA44.xml` indatalist의 철자 **`splrInfrBIzSVO2`**를 따른다.
- 정정 2장의 `xmlCntn/xmlCntn2`, `etan/etan2`, `trnsXmlCntn/trnsXmlCntn2`를 각각 검증·서명한다.
  C04/C03 응답의 `tteet*` VO/list 및 `...2` 세트를 최종 요청에 승계한다.
- 서명 전후 승인번호와 발급 후 재조회의 전체 문서 내용이 맞아야 완료 처리한다.
  부분 처리·서명/통신/재조회 실패는 미확정 상태로 남기며 재전송하지 않는다.
- 원본 XML 다운로드는 상세 R02의 form `downloadParam` JSON과 `downloadView=Y`로 읽을 수 있었다.
  기존 문서의 표준 태그 구조만 확인했으며 원문 거래자료를 저장하지 않았다.
- 표준 QName은 `{urn:kr:or:kec:standard:Tax:ReusableAggregateBusinessInformationEntitySchemaModule:1:0}TaxInvoice`.
  종사업장번호는 `SpecifiedOrganization/TaxRegistrationID`이며 사업자번호(ID)와 다르다.
  당사자의 `TypeCode`/`ClassificationCode`는 업태/종목, 품목 `InformationText`/`DescriptionText`는 규격/비고다.
  정정 원본 연결은 `TaxInvoiceDocument/OriginalIssueID`, 사유는 `AmendmentStatusCode`다.
- 표준 참고: [KEC 전자세금계산서 표준](https://www.entax.co.kr/images/etax.pdf),
  [홈택스 단건 발급 화면](https://teet.hometax.go.kr/ui/et/b/a/a/UTEETBAA01.xml).
- MagicLine 반환값의 실제 `raw`/`base64` 전송 호환성은 미검증이다. 기본 전송 비활성,
  활성화 시 프로필 명시·확인 digest·동일 로그인 인증서·영속 저널을 요구한다.
- 자동 이메일 C08/C13 호출은 제외. 재정정·부분 환입 등 다른 사유는 미지원.

PFX/P12 로딩은 가능하지만 원본 개인키의 VID 부가 속성 복원은 지원하지 않으므로 홈택스
로그인 요청은 `CERT_RANDOM_MISSING`으로 거부합니다. NPKI 종류·홈택스 등록 상태·추가 인증
필요 여부는 실제 본인 인증서로 한 번의 로그인 절차를 검증해야 합니다.

## 카드·현금영수증·사업용계좌 조회 (2026-09-12)

포털 로그인 세션에서 `/token.do?idx=getToken2SC`로 통합인증 토큰을 받고, 고정된 모바일 업무
도메인의 `/token.do?idx=verify`로 세션을 연 뒤 `jsonAction.do`의 읽기 action만 호출합니다.
토큰과 쿠키는 메모리에만 두고 응답·로그에 포함하지 않습니다.

| 자료 | 화면 | 읽기 action |
|---|---|---|
| 사업용 신용카드 매입 | `UTBCRCB023F001` | `ATECRCCA001R06` |
| 등록 사업용 신용카드 | `UTBCRJD001F001` | `ATECREAA002R02` |
| 현금영수증 매입 공제 | `UTBCRCB007F001` | `ATECRCBA001R09` |
| 현금영수증 매출 합계 | `UTBCRCB044F001` | `ATECRCBA003R05` |
| 신용카드 매출 합계 | `UTBSFABG35F001` | `ATESFAAA014R02` |
| 사업용계좌 신고현황 | `UTBCMCDA02F001` | `ATTCMCDA001R02` |

공개 화면 HTML의 mapper와 입력·출력 VO를 기준으로 요청을 구성했고, 승인된 본인 계정에서 다섯
action의 성공 응답 계약을 읽기 전용으로 확인했습니다. 카드·현금영수증·계좌 등록이나 공제분류
변경 action은 호출하지 않습니다.
