import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from hometax_login.api import Settings, create_app
from hometax_login.errors import LoginError

KEY_A = "a" * 40
KEY_B = "b" * 40
CHANGE_ID = "ChangePlan_123456"


class FakeCounterpartyChanges:
    def __init__(self):
        self.calls = []

    async def preview_create(self, payload):
        self.calls.append(("preview_create", payload))
        return preview("create", payload.business_number, payload.branch_number, after=payload)

    async def preview_update(self, business_number, branch_number, payload):
        self.calls.append(("preview_update", business_number, branch_number, payload))
        return preview(
            "update",
            business_number,
            branch_number,
            before=counterparty_data(),
            after=counterparty_data(**payload.model_dump(exclude_unset=True)),
        )

    async def preview_delete(self, business_number, branch_number):
        self.calls.append(("preview_delete", business_number, branch_number))
        return preview(
            "delete",
            business_number,
            branch_number,
            before=counterparty_data(),
            after=None,
        )

    async def apply(self, change_id, journal):
        self.calls.append(("apply", change_id, journal))
        return {"change_id": change_id, "operation": "create", "status": "applied"}


class FailingCounterpartyChanges(FakeCounterpartyChanges):
    async def preview_create(self, payload):
        self.calls.append(("preview_create", payload))
        raise LoginError("COUNTERPARTY_PREVIEW_FAILED", "거래처 변경 미리보기에 실패했습니다.")


class SlowCounterpartyChanges(FakeCounterpartyChanges):
    async def preview_create(self, payload):
        await asyncio.sleep(120)


class FakeClient:
    def __init__(self, changes=None):
        self.counterparty_changes = changes or FakeCounterpartyChanges()
        self.closed = False

    async def close(self):
        self.closed = True


def owner_for(key):
    return hashlib.sha256(key.encode()).hexdigest()


async def add_session(app, key=KEY_A, client=None):
    return await app.state.sessions.add(owner_for(key), client or FakeClient(), {"user_id": "u"})


def counterparty_data(**overrides):
    data = {
        "name": "(주)예시거래처",
        "representative_name": "홍길동",
        "address": "서울",
        "business_type": "",
        "business_item": "",
        "primary_contact": {},
        "secondary_contact": {},
    }
    data.update(overrides)
    return data


def create_payload(**overrides):
    data = {
        "business_number": "1234567890",
        "branch_number": "",
        **counterparty_data(),
    }
    data.update(overrides)
    return data


def preview(operation, business_number, branch_number, *, before=None, after=None):
    return {
        "change_id": CHANGE_ID,
        "operation": operation,
        "expires_at": datetime.now(UTC) + timedelta(minutes=5),
        "company_name": "예시공급사",
        "business_number": business_number,
        "branch_number": branch_number,
        "before": before,
        "after": after,
        "requires_confirmation": True,
    }


def assert_no_store(response):
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Pragma"] == "no-cache"


def test_counterparty_writes_are_disabled_by_default():
    app = create_app(Settings(api_keys=(KEY_A,)))
    assert app.state.counterparty_journal is None


def test_counterparty_write_flag_from_env_is_strict(monkeypatch):
    monkeypatch.setenv("HOMETAX_API_KEYS", KEY_A)
    monkeypatch.setenv("HOMETAX_COUNTERPARTY_WRITES_ENABLED", "yes")

    with pytest.raises(ValueError, match="HOMETAX_COUNTERPARTY_WRITES_ENABLED"):
        Settings.from_env()


def test_counterparty_write_journal_is_created_only_when_enabled(tmp_path):
    journal_path = tmp_path / "writes.sqlite3"
    app = create_app(
        Settings(
            api_keys=(KEY_A,),
            counterparty_writes_enabled=True,
            write_journal_path=str(journal_path),
        )
    )

    assert app.state.counterparty_journal is not None
    assert journal_path.exists()


@pytest.mark.asyncio
async def test_preview_create_requires_auth():
    app = create_app(Settings(api_keys=(KEY_A, KEY_B)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as c:
        res = await c.post("/v1/hometax/sessions/missing/counterparties", json=create_payload())
    assert res.status_code == 401
    assert_no_store(res)


@pytest.mark.asyncio
async def test_preview_create_is_bound_to_session_owner():
    app = create_app(Settings(api_keys=(KEY_A, KEY_B)))
    item = await add_session(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {KEY_B}"},
    ) as c:
        res = await c.post(f"/v1/hometax/sessions/{item.id}/counterparties", json=create_payload())
    assert res.status_code == 404
    assert_no_store(res)


@pytest.mark.asyncio
async def test_preview_create_accepts_body_and_forwards_call_when_writes_disabled():
    app = create_app(Settings(api_keys=(KEY_A,)))
    changes = FakeCounterpartyChanges()
    item = await add_session(app, client=FakeClient(changes))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {KEY_A}"},
    ) as c:
        res = await c.post(f"/v1/hometax/sessions/{item.id}/counterparties", json=create_payload())

    assert res.status_code == 200
    assert_no_store(res)
    assert res.json()["operation"] == "create"
    kind, payload = changes.calls[-1]
    assert kind == "preview_create"
    assert payload.business_number == "1234567890"
    assert payload.name == "(주)예시거래처"


@pytest.mark.asyncio
async def test_preview_create_validates_payload():
    app = create_app(Settings(api_keys=(KEY_A,)))
    item = await add_session(app)
    payload = create_payload(business_number="123-45-67890")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {KEY_A}"},
    ) as c:
        res = await c.post(f"/v1/hometax/sessions/{item.id}/counterparties", json=payload)

    assert res.status_code == 422
    assert_no_store(res)


@pytest.mark.asyncio
async def test_preview_update_validates_path_query_and_forwards_call():
    app = create_app(Settings(api_keys=(KEY_A,)))
    changes = FakeCounterpartyChanges()
    item = await add_session(app, client=FakeClient(changes))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {KEY_A}"},
    ) as c:
        bad_number = await c.patch(
            f"/v1/hometax/sessions/{item.id}/counterparties/123-45-67890",
            json={"name": "새이름"},
        )
        bad_branch = await c.patch(
            f"/v1/hometax/sessions/{item.id}/counterparties/1234567890",
            params={"branch_number": "12345"},
            json={"name": "새이름"},
        )
        empty_patch = await c.patch(
            f"/v1/hometax/sessions/{item.id}/counterparties/1234567890",
            json={},
        )
        res = await c.patch(
            f"/v1/hometax/sessions/{item.id}/counterparties/1234567890",
            params={"branch_number": "0001"},
            json={"name": " 새이름 "},
        )

    assert bad_number.status_code == 422
    assert_no_store(bad_number)
    assert bad_branch.status_code == 422
    assert_no_store(bad_branch)
    assert empty_patch.status_code == 422
    assert_no_store(empty_patch)
    assert res.status_code == 200
    assert_no_store(res)
    kind, business_number, branch_number, payload = changes.calls[-1]
    assert kind == "preview_update"
    assert business_number == "1234567890"
    assert branch_number == "0001"
    assert payload.name == "새이름"


@pytest.mark.asyncio
async def test_preview_delete_validates_path_query_and_forwards_call():
    app = create_app(Settings(api_keys=(KEY_A,)))
    changes = FakeCounterpartyChanges()
    item = await add_session(app, client=FakeClient(changes))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {KEY_A}"},
    ) as c:
        bad_number = await c.delete(f"/v1/hometax/sessions/{item.id}/counterparties/123456789")
        bad_branch = await c.delete(
            f"/v1/hometax/sessions/{item.id}/counterparties/1234567890",
            params={"branch_number": "abcd"},
        )
        res = await c.delete(
            f"/v1/hometax/sessions/{item.id}/counterparties/1234567890",
            params={"branch_number": "0001"},
        )

    assert bad_number.status_code == 422
    assert_no_store(bad_number)
    assert bad_branch.status_code == 422
    assert_no_store(bad_branch)
    assert res.status_code == 200
    assert_no_store(res)
    assert changes.calls[-1] == ("preview_delete", "1234567890", "0001")


@pytest.mark.asyncio
async def test_preview_failure_is_safe():
    app = create_app(Settings(api_keys=(KEY_A,)))
    item = await add_session(app, client=FakeClient(FailingCounterpartyChanges()))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {KEY_A}"},
    ) as c:
        res = await c.post(f"/v1/hometax/sessions/{item.id}/counterparties", json=create_payload())

    assert res.status_code == 502
    assert_no_store(res)
    assert res.json()["error"]["code"] == "COUNTERPARTY_PREVIEW_FAILED"
    assert "Traceback" not in res.text


@pytest.mark.asyncio
async def test_apply_requires_strict_confirm_true():
    app = create_app(Settings(api_keys=(KEY_A,)))
    item = await add_session(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {KEY_A}"},
    ) as c:
        omitted = await c.post(
            f"/v1/hometax/sessions/{item.id}/counterparty-changes/{CHANGE_ID}/apply",
            json={},
        )
        false = await c.post(
            f"/v1/hometax/sessions/{item.id}/counterparty-changes/{CHANGE_ID}/apply",
            json={"confirm": False},
        )
        string = await c.post(
            f"/v1/hometax/sessions/{item.id}/counterparty-changes/{CHANGE_ID}/apply",
            json={"confirm": "true"},
        )

    assert omitted.status_code == 422
    assert_no_store(omitted)
    assert false.status_code == 422
    assert_no_store(false)
    assert string.status_code == 422
    assert_no_store(string)


@pytest.mark.asyncio
async def test_apply_rejects_invalid_change_id():
    app = create_app(Settings(api_keys=(KEY_A,)))
    item = await add_session(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {KEY_A}"},
    ) as c:
        res = await c.post(
            f"/v1/hometax/sessions/{item.id}/counterparty-changes/bad/apply",
            json={"confirm": True},
        )

    assert res.status_code == 422
    assert_no_store(res)


@pytest.mark.asyncio
async def test_apply_is_disabled_without_calling_manager():
    app = create_app(Settings(api_keys=(KEY_A,)))
    changes = FakeCounterpartyChanges()
    item = await add_session(app, client=FakeClient(changes))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {KEY_A}"},
    ) as c:
        res = await c.post(
            f"/v1/hometax/sessions/{item.id}/counterparty-changes/{CHANGE_ID}/apply",
            json={"confirm": True},
        )

    assert res.status_code == 403
    assert_no_store(res)
    assert res.json()["error"]["code"] == "COUNTERPARTY_WRITES_DISABLED"
    assert changes.calls == []


@pytest.mark.asyncio
async def test_apply_forwards_to_manager_with_journal_when_enabled(tmp_path):
    journal_path = tmp_path / "writes.sqlite3"
    app = create_app(
        Settings(
            api_keys=(KEY_A,),
            counterparty_writes_enabled=True,
            write_journal_path=str(journal_path),
        )
    )
    changes = FakeCounterpartyChanges()
    item = await add_session(app, client=FakeClient(changes))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {KEY_A}"},
    ) as c:
        res = await c.post(
            f"/v1/hometax/sessions/{item.id}/counterparty-changes/{CHANGE_ID}/apply",
            json={"confirm": True},
        )

    assert res.status_code == 200
    assert_no_store(res)
    assert res.json() == {"change_id": CHANGE_ID, "operation": "create", "status": "applied"}
    assert changes.calls == [("apply", CHANGE_ID, app.state.counterparty_journal)]


@pytest.mark.asyncio
async def test_management_timeout_includes_wait_for_session_lock(monkeypatch):
    app = create_app(Settings(api_keys=(KEY_A,)))
    item = await add_session(app, client=FakeClient(SlowCounterpartyChanges()))
    original_timeout = asyncio.timeout
    monkeypatch.setattr(asyncio, "timeout", lambda _seconds: original_timeout(0.02))
    await item.lock.acquire()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {KEY_A}"},
        ) as client:
            response = await client.post(
                f"/v1/hometax/sessions/{item.id}/counterparties", json=create_payload()
            )
        assert response.status_code == 504
        assert response.json()["error"]["code"] == "INVOICE_TIMEOUT"
        assert_no_store(response)
    finally:
        item.lock.release()
