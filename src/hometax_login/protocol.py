"""HomeTax certificate protocol. No HIRA endpoints or HIRA CMS format are reused."""

import base64
import json
import re
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import httpx
from cryptography.hazmat.primitives.serialization import Encoding

from .errors import LoginError

if TYPE_CHECKING:
    from .certificates import CertificateMaterial

BASE = "https://www.hometax.go.kr"
LOGIN_PAGE = BASE + "/websquare/websquare.wq?w2xPath=/ui/comm/a/b/UTXPPABA01.xml"
MAX_RESPONSE_BYTES = 1024 * 1024


def parse_login_result(text: str) -> bool:
    """Inspect markers without executing JavaScript; reject missing/duplicate markers."""
    if len(text) > 65536:
        return False
    if not text.lstrip().startswith("{") and "nts_loginSystemCallback(" not in text:
        return False
    markers = {}
    for key in ("code", "lgnRsltCd"):
        markers[key] = re.findall(rf"['\"]{key}['\"]\s*:\s*['\"]([^'\"]*)['\"]", text)
    return markers == {"code": ["S"], "lgnRsltCd": ["01"]}


class HometaxClient:
    """One HTTP client/cookie jar per login session; no shared global cookie jar."""

    def __init__(self, *, transport=None, timeout: float = 20):
        self.http = httpx.AsyncClient(
            base_url=BASE,
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json, text/plain, */*",
                "Origin": BASE,
                "Referer": LOGIN_PAGE,
            },
        )

    async def close(self):
        await self.http.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> str:
        # Every path is an internal constant; callers cannot proxy arbitrary URLs.
        try:
            async with self.http.stream(method, path, **kwargs) as response:
                if response.status_code != 200:
                    raise LoginError(
                        "UPSTREAM_RESPONSE_CHANGED",
                        "홈택스가 요청을 거부했거나 로그인 절차가 변경됐습니다.",
                    )
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(chunks) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise LoginError(
                            "UPSTREAM_RESPONSE_CHANGED", "응답 크기가 예상과 다릅니다."
                        )
                    chunks.extend(chunk)
                return chunks.decode("utf-8", errors="replace")
        except httpx.TimeoutException:
            raise LoginError("UPSTREAM_TIMEOUT", "홈택스 응답 시간이 초과됐습니다.", 504) from None
        except httpx.HTTPError:
            raise LoginError("UPSTREAM_UNAVAILABLE", "홈택스에 연결하지 못했습니다.") from None

    @staticmethod
    def _json(text: str) -> dict:
        try:
            result = json.loads(text)
        except (ValueError, TypeError):
            raise LoginError(
                "UPSTREAM_RESPONSE_CHANGED", "홈택스 응답 형식을 확인할 수 없습니다."
            ) from None
        if not isinstance(result, dict):
            raise LoginError("UPSTREAM_RESPONSE_CHANGED", "홈택스 응답 형식이 변경됐습니다.")
        return result

    async def login(self, material: "CertificateMaterial", login_type: str) -> dict:
        if not material.random_number:
            raise LoginError(
                "CERT_RANDOM_MISSING",
                "홈택스 서명용 randomNum이 없습니다. NPKI der/key 인증서를 사용하세요.",
                422,
            )
        if login_type not in {"03", "04"}:
            raise LoginError("INVALID_LOGIN_TYPE", "지원하지 않는 로그인 구분입니다.", 422)
        await self._request("GET", "/")
        challenge_response = self._json(
            await self._request(
                "POST",
                "/wqAction.do",
                params={"actionId": "ATXPPZXA001R01", "screenId": "UTXPPABA01"},
                json={
                    "pageNo": "",
                    "pageRef": "",
                    "pageOrder": "",
                    "pageCl": "",
                    "pageExcelYn": "",
                    "map": {},
                },
            )
        )
        challenge = challenge_response.get("pkcEncSsn")
        if not isinstance(challenge, str) or not 1 <= len(challenge) <= 8192 or "$" in challenge:
            raise LoginError("UPSTREAM_RESPONSE_CHANGED", "홈택스 인증 챌린지를 받지 못했습니다.")
        signed = base64.b64encode(material.sign(challenge.encode("utf-8"))).decode("ascii")
        serial_number = material.certificate.serial_number
        serial = serial_number.to_bytes((serial_number.bit_length() + 7) // 8, "big").hex()
        timestamp = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d%H%M%S")
        envelope = base64.b64encode(f"{challenge}${serial}${timestamp}${signed}".encode()).decode(
            "ascii"
        )
        result = await self._request(
            "POST",
            "/pubcLogin.do",
            params={"domain": "hometax.go.kr", "mainSys": "Y"},
            data={
                "logSgnt": envelope,
                "cert": material.certificate.public_bytes(Encoding.PEM).decode("ascii"),
                "randomEnc": base64.b64encode(material.random_number).decode("ascii"),
                "pkcLoginYnImpv": "Y",
                "pkcLgnClCd": login_type,
                "ssoStatus": "",
                "portalStatus": "",
                "scrnId": "UTXPPABA01",
                "userScrnRslnXcCnt": "1920",
                "userScrnRslnYcCnt": "1080",
            },
        )
        if not parse_login_result(result):
            # Do not replay authentication attempts or try to bypass protective responses.
            raise LoginError(
                "LOGIN_REJECTED",
                "홈택스 로그인이 확인되지 않았습니다. "
                "인증서 등록 상태 또는 추가 인증을 확인하세요.",
                401,
            )
        return await self.verify()

    async def verify(self) -> dict[str, str]:
        text = await self._request(
            "POST",
            "/permission.do",
            params={"screenId": "index_pp"},
            content=b'<map id="postParam"><popupYn>false</popupYn></map>',
            headers={"Content-Type": "application/xml; charset=UTF-8"},
        )
        data = self._json(text)
        result = data.get("resultMsg")
        if not isinstance(result, dict):
            raise LoginError("SESSION_NOT_AUTHENTICATED", "인증된 홈택스 세션이 아닙니다.", 401)
        session = result.get("sessionMap")
        if not isinstance(session, dict) or result.get("errorMsg"):
            raise LoginError(
                "SESSION_NOT_AUTHENTICATED", "홈택스 세션이 만료됐거나 미인증 상태입니다.", 401
            )
        user_id = session.get("userId") or session.get("pubcUserNo")
        if not isinstance(user_id, str) or not user_id.strip():
            raise LoginError(
                "SESSION_NOT_AUTHENTICATED", "로그인 사용자 정보를 확인하지 못했습니다.", 401
            )
        name = session.get("userNm")
        return {"user_id": user_id, "user_name": name if isinstance(name, str) else ""}
