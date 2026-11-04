import httpx
import pytest

from hometax_login.errors import LoginError
from hometax_login.protocol import (
    HometaxClient,
    login_rejection_message,
    parse_login_error_code,
    parse_login_result,
)


def test_callback_requires_both_success_markers():
    assert parse_login_result("nts_loginSystemCallback('TXPP', {'code':'S','lgnRsltCd':'01'});")
    assert not parse_login_result("nts_loginSystemCallback('TXPP', {'code':'S'});")
    assert not parse_login_result('{"code":"F", "lgnRsltCd":"01"}')
    assert not parse_login_result("<html>login success</html>")
    assert not parse_login_result('{"code":"F","code":"S","lgnRsltCd":"01"}')


@pytest.mark.asyncio
async def test_anonymous_permission_is_not_authenticated():
    def handler(request):
        return httpx.Response(200, json={"resultMsg": {"errorCd": "", "errorMsg": ""}})

    client = HometaxClient(transport=httpx.MockTransport(handler))
    with pytest.raises(LoginError) as caught:
        await client.verify()
    assert caught.value.code == "SESSION_NOT_AUTHENTICATED"
    await client.close()


@pytest.mark.asyncio
async def test_permission_returns_only_minimal_identity():
    def handler(request):
        assert request.url.path == "/permission.do"
        return httpx.Response(
            200,
            json={
                "resultMsg": {
                    "sessionMap": {
                        "userId": "test-account",
                        "userNm": "테스트",
                        "txprDscmNo": "never-return",
                    }
                }
            },
        )

    client = HometaxClient(transport=httpx.MockTransport(handler))
    assert await client.verify() == {"user_id": "test-account", "user_name": "테스트"}
    await client.close()


@pytest.mark.asyncio
async def test_redirect_and_protection_page_do_not_leak_response():
    def handler(request):
        return httpx.Response(
            302, headers={"Location": "https://example.org"}, text="upstream-secret"
        )

    client = HometaxClient(transport=httpx.MockTransport(handler))
    with pytest.raises(LoginError) as caught:
        await client.verify()
    assert "upstream-secret" not in str(caught.value)
    assert caught.value.code == "UPSTREAM_RESPONSE_CHANGED"
    await client.close()


REJECTED_UNREGISTERED = (
    "nts_loginSystemCallback('TXPP', { 'code' : 'F', 'errCode' : null, 'errMsg' : "
    "decodeURIComponent('%5BETINFZ0109%5D%ED%99%88%ED%83%9D%EC%8A%A4%EC%97%90+"
    "%EB%93%B1%EB%A1%9D%EB%90%9C+%EC%9D%B8%EC%A6%9D%EC%84%9C%EA%B0%80+"
    "%EC%95%84%EB%8B%99%EB%8B%88%EB%8B%A4.%0A%ED%99%95%EC%9D%B8')"
    ".replace(/\\+/g,' ').replace(/\\\\n/g,'\\n'), 'lgnRsltCd' : null});"
)


def test_rejection_uses_error_code_and_local_guidance_not_hometax_text():
    assert parse_login_error_code(REJECTED_UNREGISTERED) == "ETINFZ0109"
    message = login_rejection_message(REJECTED_UNREGISTERED)
    assert "ETINFZ0109" in message
    assert "등록되지 않은 인증서" in message
    assert "%5B" not in message and "확인" not in message.split("ETINFZ0109")[0]


def test_unknown_code_is_reported_without_hometax_text():
    text = "{'errMsg':'%5BETXYZ1234%5D+%EC%9B%90%EB%AC%B8'}"
    assert parse_login_error_code(text) == "ETXYZ1234"
    message = login_rejection_message(text)
    assert "ETXYZ1234" in message
    assert "원문" not in message


def test_missing_duplicate_oversized_or_codeless_error_falls_back():
    fallback = login_rejection_message("nts_loginSystemCallback('TXPP', {'code':'F'});")
    assert "인증서 등록 상태" in fallback
    assert parse_login_error_code("{'errMsg':'[ETA11]a','errMsg':'[ETB22]b'}") is None
    assert parse_login_error_code("x" * 70000) is None
    assert parse_login_error_code("{'errMsg':'코드 없는 안내'}") is None
    assert parse_login_error_code("{'errMsg':null}") is None
