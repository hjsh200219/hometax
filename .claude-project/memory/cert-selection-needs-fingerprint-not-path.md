---
name: cert-selection-needs-fingerprint-not-path
description: 인증서 선택 기억은 경로만으로 부족하다. 갱신 대비로 지문을 함께 본다
type: reference
created: 2026-09-12
---

`cert_discovery.resolve_selection`은 저장된 선택을 경로와 지문(DER 원본의 SHA-256) 두 값으로
판정하고 사유를 `saved`·`changed`·`missing`·`expired`·`single`·`choose`·`empty`로 돌려줍니다.
`discover`의 중복 제거 키도 경로가 아니라 지문입니다. 저장 위치는 `$HOMETAX_HOME/config.toml`
(기본 `~/.hometax/`, 0600)이며 비밀번호는 저장하지 않습니다.

**Why:** 공동인증서는 갱신하면 같은 폴더 경로에 내용이 다른 인증서가 덮여 들어옵니다. 경로만
기억하면 갱신 후에도 "기억한 선택"으로 조용히 통과해 엉뚱하거나 만료된 인증서로 로그인을 시도하고,
실패 원인이 인증서 교체라는 사실이 드러나지 않습니다. 지문이 있어야 `changed`를 `missing`·`expired`와
구분해 사용자에게 다시 묻습니다.

**How to apply:** 선택 기억 로직을 손댈 때 지문 검사를 빼지 마세요. 인증서 관련 문제 보고를 받으면
`~/.hometax/config.toml`의 `fingerprint`와 디스크 인증서의 지문을 먼저 대조합니다. 같은 패턴(경로만
기억)이 다른 곳에 있으면 내용 해시를 함께 기록하세요.
