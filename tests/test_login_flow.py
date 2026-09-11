import base64
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from hometax_login.errors import LoginError
from hometax_login.protocol import HometaxClient


def signing_material():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic-test")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1234)
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return SimpleNamespace(
        certificate=cert,
        random_number=b"\x00\x00test-vid-random",
        sign=lambda data: key.sign(data, padding.PKCS1v15(), hashes.SHA256()),
    )


@pytest.mark.parametrize("login_type", ["03", "04"])
async def test_complete_login_requires_signature_and_authenticated_permission(login_type):
    material = signing_material()
    visited = []

    def handler(request):
        visited.append(request.url.path)
        if request.url.path == "/":
            return httpx.Response(
                200, text="landing", headers={"set-cookie": "bootstrap=one; Path=/"}
            )
        assert "bootstrap=one" in request.headers["cookie"]
        if request.url.path == "/wqAction.do":
            return httpx.Response(200, json={"pkcEncSsn": "challenge"})
        if request.url.path == "/pubcLogin.do":
            body = parse_qs(request.content.decode())
            envelope = base64.b64decode(body["logSgnt"][0]).decode().split("$")
            assert envelope[0] == "challenge"
            assert envelope[1] == "04d2"
            assert len(envelope[2]) == 14
            material.certificate.public_key().verify(
                base64.b64decode(envelope[3]), b"challenge", padding.PKCS1v15(), hashes.SHA256()
            )
            assert body["pkcLgnClCd"] == [login_type]
            assert base64.b64decode(body["randomEnc"][0]) == material.random_number
            assert "password" not in body and "key_file" not in body
            return httpx.Response(
                200, text="nts_loginSystemCallback('TXPP', {'code':'S','lgnRsltCd':'01'});"
            )
        assert request.url.path == "/permission.do"
        return httpx.Response(200, json={"resultMsg": {"sessionMap": {"userId": "verified"}}})

    client = HometaxClient(transport=httpx.MockTransport(handler))
    assert (await client.login(material, login_type))["user_id"] == "verified"
    assert visited == ["/", "/wqAction.do", "/pubcLogin.do", "/permission.do"]
    await client.close()


async def test_missing_random_stops_before_any_network_request():
    def handler(request):
        pytest.fail("Missing VID must not trigger an upstream login")

    material = signing_material()
    material.random_number = None
    client = HometaxClient(transport=httpx.MockTransport(handler))
    with pytest.raises(LoginError) as caught:
        await client.login(material, "04")
    assert caught.value.code == "CERT_RANDOM_MISSING"
    await client.close()


async def test_invalid_password_response_never_retries():
    attempts = []

    def handler(request):
        attempts.append(request.url.path)
        if request.url.path == "/":
            return httpx.Response(200)
        if request.url.path == "/wqAction.do":
            return httpx.Response(200, json={"pkcEncSsn": "challenge"})
        return httpx.Response(200, text="nts_loginSystemCallback('TXPP', {'code':'F'});")

    client = HometaxClient(transport=httpx.MockTransport(handler))
    with pytest.raises(LoginError) as caught:
        await client.login(signing_material(), "04")
    assert caught.value.code == "LOGIN_REJECTED"
    assert attempts.count("/pubcLogin.do") == 1
    assert "/permission.do" not in attempts
    await client.close()
