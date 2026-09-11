import httpx
import pytest

from hometax_login.errors import LoginError
from hometax_login.protocol import HometaxClient, parse_login_result


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
