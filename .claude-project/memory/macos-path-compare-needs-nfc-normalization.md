---
name: macos-path-compare-needs-nfc-normalization
description: macOS 한글 경로는 NFD로 저장돼 문자열 비교가 어긋난다. samefile로 판정할 것
type: feedback
created: 2026-09-12
---

macOS는 파일명을 NFD(자모 분해)로 저장하고 셸 인자와 파이썬 리터럴은 NFC(완성형)입니다. 한글이 든
인증서 폴더 경로를 `str(a) == str(b)`로 비교하면 같은 파일인데 다르다고 나옵니다. `cli.py:same_path`는
두 경로가 실존하면 `os.path.samefile`(inode)로 판정하고, 없는 경로만 NFC 정규화해 비교합니다.

**Why:** APFS는 준 이름을 그대로 저장합니다. 그래서 테스트가 `tmp_path / "cn=예시상호"`처럼 NFC로
폴더를 만들면 이 상황이 재현되지 않고 테스트가 그냥 통과합니다. 인증서 폴더명은 `cn=<상호>` 형태라
한글이 사실상 항상 들어갑니다. 공동인증서를 다루는 코드에서 반복해 밟을 자리입니다.

**How to apply:** 사용자가 넘긴 경로와 디스크에서 찾은 경로를 비교할 때는 `same_path`를 쓰고 `==`를
새로 쓰지 않습니다. 회귀 테스트는 `unicodedata.normalize("NFD", ...)`로 디렉터리를 만들고 NFC 인자로
조회하는 쌍으로 작성합니다(`tests/test_cli.py`의 composed unicode 테스트). 관련
[[cert-selection-needs-fingerprint-not-path]]
