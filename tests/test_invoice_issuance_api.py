import asyncio
import base64
import hashlib
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from hometax_login.api import Settings, create_app
from hometax_login.certificates import CertificateError
from hometax_login.errors import LoginError

KEY_A = "a" * 40
KEY_B = "b" * 40
CLIENT_REFERENCE = "11111111-1111-4111-8111-111111111111"
APPROVAL_NUMBER = "20260903ABCdef1234567890"
OPERATION_ID = "InvoiceOp_123456"
CONTENT_DIGEST = "a" * 64


class FakeInvoiceOperations:
    def __init__(self):
        self.calls = []

    async def preview_issue(self, payload):
        self.calls.append(("preview_issue", payload))
        return operation_preview("issue", payload.client_reference)

    async def preview_correct(self, approval_number, payload):
        self.calls.append(("preview_correct", approval_number, payload))
        return operation_preview("correct", payload.client_reference)

    async def preview_cancel(self, approval_number, payload):
        self.calls.append(("preview_cancel", approval_number, payload))
        return operation_preview("cancel", payload.client_reference)

    async def submit(self, operation_id, material, journal, content_digest):
        self.calls.append(("submit", operation_id, material, journal, content_digest))
        return {
            "operation_id": operation_id,
            "client_reference": CLIENT_REFERENCE,
            "operation": "issue",
            "status": "issued",
            "approval_numbers": [APPROVAL_NUMBER],
            "email_delivery": "not_requested",
        }


class FailingInvoiceOperations(FakeInvoiceOperations):
    async def preview_issue(self, payload):
        self.calls.append(("preview_issue", payload))
        raise LoginError("INVOICE_PREVIEW_FAILED", "세금계산서 미리보기에 실패했습니다.")


class SlowInvoiceOperations(FakeInvoiceOperations):
    async def preview_issue(self, payload):
        await asyncio.sleep(120)


class FakeClient:
    def __init__(self, operations=None):
        self.invoice_operations = operations or FakeInvoiceOperations()
        self.invoice_wire_encoding = None
        self.closed = False

    async def login(self, material, login_type):
        return {"user_id": "u"}

    async def close(self):
        self.closed = True


def owner_for(key):
    return hashlib.sha256(key.encode()).hexdigest()


async def add_session(app, key=KEY_A, client=None):
    return await app.state.sessions.add(owner_for(key), client or FakeClient(), {"user_id": "u"})


def issue_payload(**overrides):
    payload = {
        "client_reference": CLIENT_REFERENCE,
        "recipient_business_number": "1234567890",
        "written_date": "2026-09-11",
        "purpose": "claim",
        "items": [
            {
                "supply_date": "2026-09-11",
                "name": "테스트 용역",
                "supply_amount": 100000,
                "tax_amount": 10000,
            }
        ],
        "remarks": "",
    }
    payload.update(overrides)
    return payload


def correction_payload(**overrides):
    payload = issue_payload(reason="clerical_error")
    payload.update(overrides)
    return payload


def cancel_payload(**overrides):
    payload = {
        "client_reference": CLIENT_REFERENCE,
        "reason": "contract_cancellation",
        "written_date": "2026-09-11",
        "remarks": "",
    }
    payload.update(overrides)
    return payload


def submit_payload(**overrides):
    payload = {
        "confirm": True,
        "content_digest": CONTENT_DIGEST,
        "cert_type": "der",
        "cert_file": base64.b64encode(b"cert").decode(),
        "key_file": base64.b64encode(b"key").decode(),
        "password": "PRIVATE-PASSWORD",
    }
    payload.update(overrides)
    return payload


def operation_preview(operation, client_reference):
    return {
        "operation_id": OPERATION_ID,
        "client_reference": client_reference,
        "operation": operation,
        "expires_at": datetime.now(UTC) + timedelta(minutes=5),
        "documents": [
            {
                "supplier": {"business_number": "9876543210", "name": "예시공급사"},
                "recipient": {"business_number": "1234567890", "name": "(주)예시거래처"},
                "written_date": "2026-09-11",
                "purpose": "claim",
                "items": [
                    {
                        "supply_date": "2026-09-11",
                        "name": "테스트 용역",
                        "supply_amount": 100000,
                        "tax_amount": 10000,
                    }
                ],
                "supply_amount": 100000,
                "tax_amount": 10000,
                "total_amount": 110000,
            }
        ],
        "content_digest": CONTENT_DIGEST,
        "requires_confirmation": True,
        "email_delivery": "not_requested",
    }


def assert_no_store(response):
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Pragma"] == "no-cache"


@pytest.mark.asyncio
async def test_preview_issue_requires_auth():
    app = create_app(Settings(api_keys=(KEY_A,)))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as c:
        res = await c.post("/v1/hometax/sessions/missing/tax-invoices/drafts", json=issue_payload())

    assert res.status_code == 401
    assert_no_store(res)


@pytest.mark.asyncio
async def test_preview_issue_path_does_not_match_invoice_detail():
    app = create_app(Settings(api_keys=(KEY_A,)))
    operations = FakeInvoiceOperations()
    item = await add_session(app, client=FakeClient(operations))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {KEY_A}"},
    ) as c:
        res = await c.post(
            f"/v1/hometax/sessions/{item.id}/tax-invoices/drafts", json=issue_payload()
        )

    assert res.status_code == 200
    assert_no_store(res)
    assert res.json()["operation"] == "issue"
    assert operations.calls[-1][0] == "preview_issue"


@pytest.mark.asyncio
async def test_preview_issue_is_bound_to_session_owner():
    app = create_app(Settings(api_keys=(KEY_A, KEY_B)))
    item = await add_session(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {KEY_B}"},
    ) as c:
        res = await c.post(
            f"/v1/hometax/sessions/{item.id}/tax-invoices/drafts",
            json=issue_payload(),
        )

    assert res.status_code == 404
    assert_no_store(res)


@pytest.mark.asyncio
async def test_preview_issue_validates_input_dates():
    app = create_app(Settings(api_keys=(KEY_A,)))
    item = await add_session(app)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {KEY_A}"},
    ) as c:
        future = await c.post(
            f"/v1/hometax/sessions/{item.id}/tax-invoices/drafts",
            json=issue_payload(written_date="2026-09-12"),
        )
        mismatched_month = await c.post(
            f"/v1/hometax/sessions/{item.id}/tax-invoices/drafts",
            json=issue_payload(
                items=[
                    {
                        "supply_date": "2026-08-31",
                        "name": "테스트 용역",
                        "supply_amount": 100000,
                        "tax_amount": 10000,
                    }
                ]
            ),
        )

    assert future.status_code == 422
    assert_no_store(future)
    assert mismatched_month.status_code == 422
    assert_no_store(mismatched_month)


@pytest.mark.asyncio
async def test_preview_correct_and_cancel_forward_approval_number_and_reason():
    app = create_app(Settings(api_keys=(KEY_A,)))
    operations = FakeInvoiceOperations()
    item = await add_session(app, client=FakeClient(operations))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {KEY_A}"},
    ) as c:
        correction = await c.post(
            f"/v1/hometax/sessions/{item.id}/tax-invoices/{APPROVAL_NUMBER}/corrections",
            json=correction_payload(),
        )
        invalid_reason = await c.post(
            f"/v1/hometax/sessions/{item.id}/tax-invoices/{APPROVAL_NUMBER}/corrections",
            json=correction_payload(reason="wrong"),
        )
        cancellation = await c.post(
            f"/v1/hometax/sessions/{item.id}/tax-invoices/{APPROVAL_NUMBER}/cancellations",
            json=cancel_payload(reason="duplicate_issue"),
        )
        invalid_approval = await c.post(
            f"/v1/hometax/sessions/{item.id}/tax-invoices/not-a-number/cancellations",
            json=cancel_payload(),
        )

    assert correction.status_code == 200
    assert_no_store(correction)
    assert invalid_reason.status_code == 422
    assert_no_store(invalid_reason)
    assert cancellation.status_code == 200
    assert_no_store(cancellation)
    assert invalid_approval.status_code == 422
    assert_no_store(invalid_approval)
    assert operations.calls[0][0:2] == ("preview_correct", APPROVAL_NUMBER)
    assert operations.calls[0][2].reason == "clerical_error"
    assert operations.calls[1][0:2] == ("preview_cancel", APPROVAL_NUMBER)
    assert operations.calls[1][2].reason == "duplicate_issue"


@pytest.mark.asyncio
async def test_preview_failure_is_safe():
    app = create_app(Settings(api_keys=(KEY_A,)))
    item = await add_session(app, client=FakeClient(FailingInvoiceOperations()))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {KEY_A}"},
    ) as c:
        res = await c.post(
            f"/v1/hometax/sessions/{item.id}/tax-invoices/drafts",
            json=issue_payload(),
        )

    assert res.status_code == 502
    assert_no_store(res)
    assert res.json()["error"]["code"] == "INVOICE_PREVIEW_FAILED"
    assert "Traceback" not in res.text


def test_invoice_write_flag_from_env_is_strict(monkeypatch):
    monkeypatch.setenv("HOMETAX_API_KEYS", KEY_A)
    monkeypatch.setenv("HOMETAX_INVOICE_WRITES_ENABLED", "yes")

    with pytest.raises(ValueError, match="HOMETAX_INVOICE_WRITES_ENABLED"):
        Settings.from_env()


def test_invoice_writes_require_wire_encoding_when_enabled():
    with pytest.raises(ValueError, match="invoice_wire_encoding"):
        Settings(api_keys=(KEY_A,), invoice_writes_enabled=True)


def test_invoice_wire_encoding_from_env_is_strict(monkeypatch):
    monkeypatch.setenv("HOMETAX_API_KEYS", KEY_A)
    monkeypatch.setenv("HOMETAX_INVOICE_WIRE_ENCODING", "xml")

    with pytest.raises(ValueError, match="invoice_wire_encoding"):
        Settings.from_env()


def test_invoice_and_counterparty_write_flags_do_not_enable_each_other(tmp_path):
    invoice_path = tmp_path / "invoice.sqlite3"
    invoice_app = create_app(
        Settings(
            api_keys=(KEY_A,),
            invoice_writes_enabled=True,
            invoice_wire_encoding="raw",
            write_journal_path=str(invoice_path),
        )
    )
    assert invoice_app.state.invoice_journal is not None
    assert invoice_app.state.counterparty_journal is None

    counterparty_path = tmp_path / "counterparty.sqlite3"
    counterparty_app = create_app(
        Settings(
            api_keys=(KEY_A,),
            counterparty_writes_enabled=True,
            write_journal_path=str(counterparty_path),
        )
    )
    assert counterparty_app.state.counterparty_journal is not None
    assert counterparty_app.state.invoice_journal is None


@pytest.mark.asyncio
async def test_login_sets_invoice_wire_encoding(monkeypatch, tmp_path):
    import hometax_login.api as api

    app = create_app(
        Settings(
            api_keys=(KEY_A,),
            invoice_writes_enabled=True,
            invoice_wire_encoding="raw",
            write_journal_path=str(tmp_path / "writes.sqlite3"),
        )
    )
    client = FakeClient()
    app.state.client_factory = lambda: client
    monkeypatch.setattr(api, "load_certificate", lambda *args: object())

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
    assert client.invoice_wire_encoding == "raw"


@pytest.mark.asyncio
async def test_submit_requires_strict_confirm_true_and_digest():
    app = create_app(Settings(api_keys=(KEY_A,)))
    item = await add_session(app)
    path = f"/v1/hometax/sessions/{item.id}/tax-invoice-operations/{OPERATION_ID}/submit"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {KEY_A}"},
    ) as c:
        omitted = await c.post(
            path,
            json={k: v for k, v in submit_payload().items() if k != "confirm"},
        )
        false = await c.post(path, json=submit_payload(confirm=False))
        string = await c.post(path, json=submit_payload(confirm="true"))
        digest = await c.post(path, json=submit_payload(content_digest="bad"))

    assert omitted.status_code == 422
    assert_no_store(omitted)
    assert false.status_code == 422
    assert_no_store(false)
    assert string.status_code == 422
    assert_no_store(string)
    assert digest.status_code == 422
    assert_no_store(digest)


@pytest.mark.asyncio
async def test_submit_requires_der_cert_type_and_key_file():
    app = create_app(Settings(api_keys=(KEY_A,)))
    item = await add_session(app)
    path = f"/v1/hometax/sessions/{item.id}/tax-invoice-operations/{OPERATION_ID}/submit"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {KEY_A}"},
    ) as c:
        pfx = await c.post(path, json=submit_payload(cert_type="pfx"))
        missing_key = await c.post(
            path,
            json={k: v for k, v in submit_payload().items() if k != "key_file"},
        )

    assert pfx.status_code == 422
    assert_no_store(pfx)
    assert missing_key.status_code == 422
    assert_no_store(missing_key)


@pytest.mark.asyncio
async def test_submit_disabled_does_not_load_material_or_call_manager(monkeypatch):
    import hometax_login.api as api

    app = create_app(Settings(api_keys=(KEY_A,)))
    operations = FakeInvoiceOperations()
    item = await add_session(app, client=FakeClient(operations))
    material_calls = 0

    def load_certificate(*args):
        nonlocal material_calls
        material_calls += 1
        return object()

    monkeypatch.setattr(api, "load_certificate", load_certificate)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {KEY_A}"},
    ) as c:
        res = await c.post(
            f"/v1/hometax/sessions/{item.id}/tax-invoice-operations/{OPERATION_ID}/submit",
            json=submit_payload(),
        )

    assert res.status_code == 403
    assert_no_store(res)
    assert res.json()["error"]["code"] == "INVOICE_WRITES_DISABLED"
    assert material_calls == 0
    assert operations.calls == []


@pytest.mark.asyncio
async def test_submit_enabled_owner_check_happens_before_material_load(monkeypatch, tmp_path):
    import hometax_login.api as api

    app = create_app(
        Settings(
            api_keys=(KEY_A, KEY_B),
            invoice_writes_enabled=True,
            invoice_wire_encoding="raw",
            write_journal_path=str(tmp_path / "writes.sqlite3"),
        )
    )
    item = await add_session(app)
    material_calls = 0

    def load_certificate(*args):
        nonlocal material_calls
        material_calls += 1
        return object()

    monkeypatch.setattr(api, "load_certificate", load_certificate)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {KEY_B}"},
    ) as c:
        res = await c.post(
            f"/v1/hometax/sessions/{item.id}/tax-invoice-operations/{OPERATION_ID}/submit",
            json=submit_payload(),
        )

    assert res.status_code == 404
    assert_no_store(res)
    assert material_calls == 0


@pytest.mark.asyncio
async def test_submit_certificate_error_is_safe(monkeypatch, tmp_path):
    import hometax_login.api as api

    app = create_app(
        Settings(
            api_keys=(KEY_A,),
            invoice_writes_enabled=True,
            invoice_wire_encoding="raw",
            write_journal_path=str(tmp_path / "writes.sqlite3"),
        )
    )
    operations = FakeInvoiceOperations()
    item = await add_session(app, client=FakeClient(operations))

    def fail_certificate(*args):
        raise CertificateError("BAD_CERT", "인증서 오류")

    monkeypatch.setattr(api, "load_certificate", fail_certificate)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {KEY_A}"},
    ) as c:
        res = await c.post(
            f"/v1/hometax/sessions/{item.id}/tax-invoice-operations/{OPERATION_ID}/submit",
            json=submit_payload(password="PRIVATE-PASSWORD-SECRET"),
        )

    assert res.status_code == 422
    assert_no_store(res)
    assert res.json()["error"]["code"] == "BAD_CERT"
    assert "PRIVATE-PASSWORD-SECRET" not in res.text
    assert operations.calls == []


@pytest.mark.asyncio
async def test_submit_enabled_forwards_material_journal_and_digest(monkeypatch, tmp_path):
    import hometax_login.api as api

    app = create_app(
        Settings(
            api_keys=(KEY_A,),
            invoice_writes_enabled=True,
            invoice_wire_encoding="base64",
            write_journal_path=str(tmp_path / "writes.sqlite3"),
        )
    )
    operations = FakeInvoiceOperations()
    item = await add_session(app, client=FakeClient(operations))
    material = object()
    monkeypatch.setattr(api, "load_certificate", lambda *args: material)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {KEY_A}"},
    ) as c:
        res = await c.post(
            f"/v1/hometax/sessions/{item.id}/tax-invoice-operations/{OPERATION_ID}/submit",
            json=submit_payload(),
        )

    assert res.status_code == 200
    assert_no_store(res)
    assert res.json()["approval_numbers"] == [APPROVAL_NUMBER]
    assert operations.calls == [
        ("submit", OPERATION_ID, material, app.state.invoice_journal, CONTENT_DIGEST)
    ]


@pytest.mark.asyncio
async def test_invoice_operation_timeout_includes_lock_and_material(monkeypatch, tmp_path):
    import hometax_login.api as api

    app = create_app(
        Settings(
            api_keys=(KEY_A,),
            invoice_writes_enabled=True,
            invoice_wire_encoding="raw",
            write_journal_path=str(tmp_path / "writes.sqlite3"),
        )
    )
    item = await add_session(app, client=FakeClient(SlowInvoiceOperations()))
    original_timeout = asyncio.timeout
    material_calls = 0

    def load_certificate(*args):
        nonlocal material_calls
        material_calls += 1
        return object()

    monkeypatch.setattr(api, "load_certificate", load_certificate)
    monkeypatch.setattr(asyncio, "timeout", lambda _seconds: original_timeout(0.02))
    await item.lock.acquire()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {KEY_A}"},
        ) as c:
            res = await c.post(
                f"/v1/hometax/sessions/{item.id}/tax-invoice-operations/{OPERATION_ID}/submit",
                json=submit_payload(),
            )

        assert res.status_code == 504
        assert_no_store(res)
        assert res.json()["error"]["code"] == "INVOICE_TIMEOUT"
        assert material_calls == 0
    finally:
        item.lock.release()
