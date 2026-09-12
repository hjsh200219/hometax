---
name: financial-lists-have-different-schemas
description: 카드매출 응답의 목록 셋은 필드명이 서로 다르다. 하나만 골라 읽으면 금액이 0이 된다
type: reference
created: 2026-09-12
---

`ATESFAAA014R02`(신용카드 매출) 응답에는 목록이 셋 들어 있고 스키마가 서로 다릅니다.

| 목록 | 내용 | 금액 필드 |
|---|---|---|
| `crdcTrsBrkdMateAdmDVOList` | 카드사 제출 월별 | `stlScnt`·`totaStlAmt`·`etcSls`·`purcEuCardSls`·`tip` |
| `sleVcexSlsMateInqrDVOList` | 판매(결제)대행 월별 | `sumStlScnt`·`crdcAmt`·`etcAmt`·`sumTipExclAmt` |
| `crdcZrpSleStlVcexMateAdmDVOList` | 분기 요약(중복) | `stlScnt`·`totaStlAmt`·`stlQrt`·`mateKndNm` |

**Why:** 처음 구현은 "비어 있지 않은 첫 목록"을 고른 뒤 첫 번째 스키마의 필드명으로 읽었습니다.
실계정에서는 대행 목록만 채워져 있었고, 그 행에는 `totaStlAmt`가 없어서 **9,900원 매출이 0원으로
보고**됐습니다. 월(`stlYm`)만 공통이라 "0원 한 줄"이 남아 빈 결과처럼 보였습니다. 이런 오집계는
예외가 없어서 테스트로도 화면으로도 드러나지 않습니다.

**How to apply:** 목록마다 필드 맵을 따로 두고, 카드사분과 대행분은 합산하되 분기 요약은 같은
매출을 다시 담은 것이라 합산하지 말고 대행분과 대조만 하세요(어긋나면 `changed()`). 새 액션을
붙일 때도 "목록이 여러 개면 스키마가 다를 수 있다"를 먼저 의심하고, 업스트림 원본을 떠서 필드
이름을 확인한 뒤 매핑하세요. 관련 [[parser-strictness-fails-the-whole-page]]
