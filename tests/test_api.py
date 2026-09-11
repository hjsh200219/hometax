import base64

import httpx
import pytest

from hometax_login.api import Settings, create_app

KEY_A = "a" * 40
KEY_B = "b" * 40


@pytest.fixture
def app():
    return create_app(Settings(api_keys=(KEY_A, KEY_B)))


@pytest.mark.asyncio
async def test_auth_is_required(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as c:
        res = await c.post("/v1/hometax/sessions", json={})
    assert res.status_code == 401


@pytest.mark.asyncio
async def test_validation_never_echoes_password_or_cert(app):
    secret = "not-a-valid-base64-PRIVATE-CERT"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {KEY_A}"},
    ) as c:
        res = await c.post(
            "/v1/hometax/sessions",
            json={
                "cert_type": "bad",
                "cert_file": secret,
                "password": "SECRET-PASSWORD",
                "login_type": "invalid",
            },
        )
    assert res.status_code == 422
    assert secret not in res.text
    assert "SECRET-PASSWORD" not in res.text


@pytest.mark.asyncio
async def test_success_is_opaque_and_bound_to_caller(app, monkeypatch):
    import hometax_login.api as api

    class FakeMaterial:
        pass

    class FakeClient:
        closed = False

        async def login(self, material, login_type):
            return {"user_id": "test-user", "user_name": "테스트"}

        async def verify(self):
            return {"user_id": "test-user", "user_name": "테스트"}

        async def close(self):
            self.closed = True

    backend = FakeClient()
    monkeypatch.setattr(api, "load_certificate", lambda *args: FakeMaterial())
    app.state.client_factory = lambda: backend
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {KEY_A}"},
    ) as c:
        res = await c.post(
            "/v1/hometax/sessions",
            json={
                "cert_type": "der",
                "cert_file": base64.b64encode(b"cert").decode(),
                "key_file": base64.b64encode(b"key").decode(),
                "password": "PRIVATE-PASSWORD",
                "login_type": "04",
            },
        )
        assert res.status_code == 201
        body = res.json()
        assert set(body) == {"session_id", "expires_at", "identity"}
        assert "PRIVATE-PASSWORD" not in res.text
        sid = body["session_id"]
        assert (
            await c.get(f"/v1/hometax/sessions/{sid}", headers={"Authorization": f"Bearer {KEY_B}"})
        ).status_code == 404
        assert (await c.get(f"/v1/hometax/sessions/{sid}")).status_code == 200
        assert (await c.delete(f"/v1/hometax/sessions/{sid}")).status_code == 204
        assert backend.closed


@pytest.mark.asyncio
async def test_request_body_limit(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {KEY_A}"},
    ) as c:
        res = await c.post("/v1/hometax/sessions", content=b"x" * (2 * 1024 * 1024 + 1))
    assert res.status_code == 413
