import asyncio
import hashlib
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from hometax_login.api import Settings, create_app
from hometax_login.errors import LoginError

KEY_A = "a" * 40
KEY_B = "b" * 40


class FakeInvoices:
    def __init__(self):
        self.calls = []

    async def list(self, query):
        self.calls.append(("list", query))
        return {
            "company_name": "예시공급사",
            "filters": filters_from(query),
            "items": [],
            "page": query.page,
            "page_size": query.page_size,
            "total_count": 0,
            "has_next": False,
        }

    async def summary(self, filters):
        self.calls.append(("summary", filters))
        return {
            "company_name": "예시공급사",
            "filters": filters,
            "total_count": 0,
            "supply_amount": 0,
            "tax_amount": 0,
            "total_amount": 0,
        }

    async def detail(self, approval_number):
        self.calls.append(("detail", approval_number))
        return {
            "invoice": {
                "approval_number": approval_number,
                "written_date": "2026-09-03",
                "issued_date": "2026-09-03",
                "transmitted_date": None,
                "counterparty_name": "(주)예시거래처",
                "item_name": "테스트 용역",
                "supply_amount": 100000,
                "tax_amount": 10000,
                "total_amount": 110000,
            },
            "supplier_name": "예시공급사",
            "customer_name": "(주)예시거래처",
            "items": [],
        }

    async def counterparties(self, query):
        self.calls.append(("counterparties", query))
        return {
            "company_name": "예시공급사",
            "query": query,
            "total_count": 1,
            "has_next": query.page * query.page_size < 1,
            "items": [
                {
                    "name": "(주)예시거래처",
                    "business_number": "1234567890",
                    "representative_name": "홍길동",
                    "address": "서울",
                    "business_type": None,
                    "business_item": None,
                    "branch_number": None,
                    "registered_at": None,
                    "is_primary": True,
                }
            ],
        }


class SlowInvoices(FakeInvoices):
    async def list(self, query):
        await asyncio.sleep(120)


class FailingInvoices(FakeInvoices):
    async def counterparties(self, query):
        self.calls.append(("counterparties", query))
        raise LoginError("COUNTERPARTY_QUERY_FAILED", "홈택스 등록 거래처 조회에 실패했습니다.")


@pytest.mark.asyncio
async def test_query_timeout_includes_wait_for_session_lock(app, monkeypatch):
    item = await add_session(app)
    original_timeout = asyncio.timeout
    monkeypatch.setattr(asyncio, "timeout", lambda _seconds: original_timeout(0.02))
    await item.lock.acquire()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://test",
            headers={"Authorization": f"Bearer {KEY_A}"},
        ) as client:
            response = await client.get(
                f"/v1/hometax/sessions/{item.id}/tax-invoices", params=invoice_params()
            )
        assert response.status_code == 504
        assert response.json()["error"]["code"] == "INVOICE_TIMEOUT"
        assert response.headers["cache-control"] == "no-store"
    finally:
        item.lock.release()


class FakeClient:
    def __init__(self, invoices=None):
        self.invoices = invoices or FakeInvoices()
        self.closed = False

    async def close(self):
        self.closed = True


@pytest.fixture
def app():
    return create_app(Settings(api_keys=(KEY_A, KEY_B), session_ttl=60))


def owner_for(key):
    return hashlib.sha256(key.encode()).hexdigest()


def invoice_params(**overrides):
    params = {"start_date": "2026-09-01", "end_date": "2026-09-11"}
    params.update(overrides)
    return params


def filters_from(query):
    return {
        "start_date": query.start_date,
        "end_date": query.end_date,
        "direction": query.direction,
        "date_basis": query.date_basis,
    }


async def add_session(app, key=KEY_A, client=None):
    return await app.state.sessions.add(owner_for(key), client or FakeClient(), {"user_id": "u"})


@pytest.mark.asyncio
async def test_invoice_list_requires_auth(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as c:
        res = await c.get("/v1/hometax/sessions/missing/tax-invoices")
    assert res.status_code == 401
    assert res.headers["Cache-Control"] == "no-store"
    assert res.headers["Pragma"] == "no-cache"


@pytest.mark.asyncio
async def test_invoice_list_is_bound_to_session_owner(app):
    item = await add_session(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {KEY_B}"},
    ) as c:
        res = await c.get(f"/v1/hometax/sessions/{item.id}/tax-invoices", params=invoice_params())
    assert res.status_code == 404
    assert res.headers["Cache-Control"] == "no-store"
    assert res.headers["Pragma"] == "no-cache"


@pytest.mark.asyncio
async def test_invoice_list_rejects_expired_session():
    app = create_app(Settings(api_keys=(KEY_A,), session_ttl=30))
    item = await app.state.sessions.add(owner_for(KEY_A), FakeClient(), {"user_id": "u"})
    item.expires_at = 0

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {KEY_A}"},
    ) as c:
        res = await c.get(f"/v1/hometax/sessions/{item.id}/tax-invoices", params=invoice_params())
    assert res.status_code == 404
    assert res.headers["Cache-Control"] == "no-store"
    assert res.headers["Pragma"] == "no-cache"


@pytest.mark.asyncio
async def test_invoice_list_validates_query_and_forwards_call(app):
    invoices = FakeInvoices()
    item = await add_session(app, client=FakeClient(invoices))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {KEY_A}"},
    ) as c:
        # 하이픈 없는 날짜는 기간 규칙 이전에 타입 변환에서 걸린다.
        # 기간 규칙 자체는 아래 future_period가 검사한다.
        malformed_date = await c.get(
            f"/v1/hometax/sessions/{item.id}/tax-invoices",
            params={"start_date": "20260901", "end_date": "2026-09-30"},
        )
        today = datetime.now(ZoneInfo("Asia/Seoul")).date()
        future_period = await c.get(
            f"/v1/hometax/sessions/{item.id}/tax-invoices",
            params={
                "start_date": today.isoformat(),
                "end_date": (today + timedelta(days=1)).isoformat(),
            },
        )
        bad_page = await c.get(
            f"/v1/hometax/sessions/{item.id}/tax-invoices",
            params=invoice_params(page=0),
        )
        res = await c.get(
            f"/v1/hometax/sessions/{item.id}/tax-invoices",
            params=invoice_params(
                direction="sales",
                date_basis="issued",
                page=2,
            ),
        )

    assert malformed_date.status_code == 422
    assert malformed_date.headers["Cache-Control"] == "no-store"
    assert malformed_date.headers["Pragma"] == "no-cache"
    assert future_period.status_code == 422
    assert future_period.headers["Cache-Control"] == "no-store"
    assert future_period.headers["Pragma"] == "no-cache"
    assert len(invoices.calls) == 1
    assert bad_page.status_code == 422
    assert bad_page.headers["Cache-Control"] == "no-store"
    assert bad_page.headers["Pragma"] == "no-cache"
    assert res.status_code == 200
    assert res.headers["Cache-Control"] == "no-store"
    assert res.headers["Pragma"] == "no-cache"
    kind, query = invoices.calls[-1]
    assert kind == "list"
    assert query.start_date == date(2026, 9, 1)
    assert query.end_date == date(2026, 9, 11)
    assert query.direction == "sales"
    assert query.date_basis == "issued"
    assert query.page == 2
    assert query.page_size == 50


@pytest.mark.asyncio
async def test_invoice_list_accepts_supported_page_sizes_from_query_string(app):
    invoices = FakeInvoices()
    item = await add_session(app, client=FakeClient(invoices))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {KEY_A}"},
    ) as c:
        for page_size in (10, 20, 30, 50):
            res = await c.get(
                f"/v1/hometax/sessions/{item.id}/tax-invoices",
                params=invoice_params(page_size=page_size),
            )
            assert res.status_code == 200
            assert res.headers["Cache-Control"] == "no-store"
            assert res.headers["Pragma"] == "no-cache"
            assert invoices.calls[-1][1].page_size == page_size


@pytest.mark.asyncio
async def test_invoice_summary_route_precedes_detail_route(app):
    invoices = FakeInvoices()
    item = await add_session(app, client=FakeClient(invoices))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {KEY_A}"},
    ) as c:
        res = await c.get(
            f"/v1/hometax/sessions/{item.id}/tax-invoices/summary",
            params=invoice_params(direction="sales"),
        )

    assert res.status_code == 200
    assert res.headers["Cache-Control"] == "no-store"
    assert res.headers["Pragma"] == "no-cache"
    kind, filters = invoices.calls[-1]
    assert kind == "summary"
    assert filters.start_date == date(2026, 9, 1)
    assert filters.end_date == date(2026, 9, 11)
    assert filters.direction == "sales"


@pytest.mark.asyncio
async def test_invoice_detail_validates_approval_number_and_forwards_call(app):
    invoices = FakeInvoices()
    item = await add_session(app, client=FakeClient(invoices))
    approval_number = "20260903ABCdef1234567890"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {KEY_A}"},
    ) as c:
        res = await c.get(f"/v1/hometax/sessions/{item.id}/tax-invoices/{approval_number}")

    assert res.status_code == 200
    assert res.headers["Cache-Control"] == "no-store"
    assert res.headers["Pragma"] == "no-cache"
    assert invoices.calls[-1] == ("detail", approval_number)


@pytest.mark.asyncio
async def test_invoice_detail_rejects_bad_approval_number(app):
    item = await add_session(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {KEY_A}"},
    ) as c:
        res = await c.get(f"/v1/hometax/sessions/{item.id}/tax-invoices/not-a-number")
    assert res.status_code == 422
    assert res.headers["Cache-Control"] == "no-store"
    assert res.headers["Pragma"] == "no-cache"


@pytest.mark.asyncio
async def test_invoice_timeout_is_safe(app, monkeypatch):
    item = await add_session(app, client=FakeClient(SlowInvoices()))

    class InstantTimeout:
        async def __aenter__(self):
            raise TimeoutError

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(asyncio, "timeout", lambda seconds: InstantTimeout())

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {KEY_A}"},
    ) as c:
        res = await c.get(f"/v1/hometax/sessions/{item.id}/tax-invoices", params=invoice_params())

    assert res.status_code == 504
    assert res.headers["Cache-Control"] == "no-store"
    assert res.headers["Pragma"] == "no-cache"
    assert res.json()["error"]["code"] == "INVOICE_TIMEOUT"
    assert "Traceback" not in res.text


@pytest.mark.asyncio
async def test_counterparties_require_auth(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as c:
        res = await c.get("/v1/hometax/sessions/missing/counterparties")
    assert res.status_code == 401
    assert res.headers["Cache-Control"] == "no-store"
    assert res.headers["Pragma"] == "no-cache"


@pytest.mark.asyncio
async def test_counterparties_are_bound_to_session_owner(app):
    item = await add_session(app)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {KEY_B}"},
    ) as c:
        res = await c.get(f"/v1/hometax/sessions/{item.id}/counterparties")
    assert res.status_code == 404
    assert res.headers["Cache-Control"] == "no-store"
    assert res.headers["Pragma"] == "no-cache"


@pytest.mark.asyncio
async def test_counterparties_validate_query_limits_and_forward_call(app):
    invoices = FakeInvoices()
    item = await add_session(app, client=FakeClient(invoices))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {KEY_A}"},
    ) as c:
        bad_business_number = await c.get(
            f"/v1/hometax/sessions/{item.id}/counterparties",
            params={"business_number": "123-45-67890"},
        )
        bad_page = await c.get(
            f"/v1/hometax/sessions/{item.id}/counterparties",
            params={"page": 0},
        )
        bad_page_size = await c.get(
            f"/v1/hometax/sessions/{item.id}/counterparties",
            params={"page_size": 40},
        )
        res = await c.get(
            f"/v1/hometax/sessions/{item.id}/counterparties",
            params={
                "name": " 예시거래처 ",
                "business_number": "1234567890",
                "representative_name": " 홍길동 ",
                "page": 2,
                "page_size": 20,
            },
        )

    assert bad_business_number.status_code == 422
    assert bad_business_number.headers["Cache-Control"] == "no-store"
    assert bad_business_number.headers["Pragma"] == "no-cache"
    assert bad_page.status_code == 422
    assert bad_page.headers["Cache-Control"] == "no-store"
    assert bad_page.headers["Pragma"] == "no-cache"
    assert bad_page_size.status_code == 422
    assert bad_page_size.headers["Cache-Control"] == "no-store"
    assert bad_page_size.headers["Pragma"] == "no-cache"
    assert res.status_code == 200
    assert res.headers["Cache-Control"] == "no-store"
    assert res.headers["Pragma"] == "no-cache"
    kind, query = invoices.calls[-1]
    assert kind == "counterparties"
    assert query.name == "예시거래처"
    assert query.business_number == "1234567890"
    assert query.representative_name == "홍길동"
    assert query.page == 2
    assert query.page_size == 20


@pytest.mark.asyncio
async def test_counterparties_accept_supported_page_sizes_from_query_string(app):
    invoices = FakeInvoices()
    item = await add_session(app, client=FakeClient(invoices))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {KEY_A}"},
    ) as c:
        for page_size in (10, 20, 30, 50):
            res = await c.get(
                f"/v1/hometax/sessions/{item.id}/counterparties",
                params={"page_size": page_size},
            )
            assert res.status_code == 200
            assert res.headers["Cache-Control"] == "no-store"
            assert res.headers["Pragma"] == "no-cache"
            assert invoices.calls[-1][1].page_size == page_size


@pytest.mark.asyncio
async def test_counterparties_propagate_safe_service_failure(app):
    item = await add_session(app, client=FakeClient(FailingInvoices()))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {KEY_A}"},
    ) as c:
        res = await c.get(f"/v1/hometax/sessions/{item.id}/counterparties")

    assert res.status_code == 502
    assert res.headers["Cache-Control"] == "no-store"
    assert res.headers["Pragma"] == "no-cache"
    assert res.json()["error"]["code"] == "COUNTERPARTY_QUERY_FAILED"
    assert "Traceback" not in res.text
