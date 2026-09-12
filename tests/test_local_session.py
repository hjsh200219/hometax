from __future__ import annotations

import json
import time

import pytest

from hometax_login.local_session import (
    clear_session,
    client_from_session,
    load_session,
    save_session,
    session_path,
)
from hometax_login.protocol import HometaxClient


@pytest.fixture(autouse=True)
def local_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOMETAX_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


def make_client() -> HometaxClient:
    client = HometaxClient()
    client.http.cookies.set("TXPPsessionID", "cookie-value", domain="hometax.go.kr")
    return client


@pytest.mark.asyncio
async def test_save_and_load_round_trip_keeps_identity_and_cookies(local_home):
    client = make_client()
    try:
        save_session(
            client, {"user_id": "example-user", "user_name": "예시컨설팅"}, ttl_seconds=600
        )
    finally:
        await client.close()

    saved = load_session()

    assert saved is not None
    assert saved["identity"]["user_name"] == "예시컨설팅"
    assert saved["cookies"]["TXPPsessionID"] == "cookie-value"
    assert session_path().stat().st_mode & 0o777 == 0o600
    assert local_home.stat().st_mode & 0o777 == 0o700


@pytest.mark.asyncio
async def test_expired_session_is_not_returned(monkeypatch):
    client = make_client()
    try:
        save_session(client, {"user_id": "example-user"}, ttl_seconds=-1)
    finally:
        await client.close()

    assert load_session() is None


@pytest.mark.asyncio
async def test_corrupt_session_file_is_ignored():
    client = make_client()
    try:
        save_session(client, {"user_id": "example-user"}, ttl_seconds=600)
    finally:
        await client.close()
    session_path().write_text("{not json", encoding="utf-8")

    assert load_session() is None


@pytest.mark.asyncio
async def test_saved_session_never_contains_password_or_certificate():
    client = make_client()
    try:
        save_session(client, {"user_id": "example-user"}, ttl_seconds=600)
    finally:
        await client.close()

    body = json.loads(session_path().read_text(encoding="utf-8"))

    assert set(body) == {"cookies", "identity", "expires_at", "saved_at"}
    assert body["expires_at"] > time.time()


@pytest.mark.asyncio
async def test_client_from_session_restores_the_cookie_jar():
    client = make_client()
    try:
        save_session(client, {"user_id": "example-user"}, ttl_seconds=600)
    finally:
        await client.close()

    restored = client_from_session(load_session())
    try:
        assert restored.http.cookies.get("TXPPsessionID") == "cookie-value"
    finally:
        await restored.close()


@pytest.mark.asyncio
async def test_clear_session_removes_the_file():
    client = make_client()
    try:
        save_session(client, {"user_id": "example-user"}, ttl_seconds=600)
    finally:
        await client.close()

    clear_session()

    assert not session_path().exists()
    assert load_session() is None
