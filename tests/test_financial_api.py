import hashlib

import httpx
import pytest

from hometax_login.api import Settings, create_app

KEY = "a" * 40


class FakeFinancials:
    def __init__(self):
        self.calls = []

    async def business_cards(self, query):
        self.calls.append(("business_cards", query))
        return {
            "company_name": "예시컨설팅",
            "query": query,
            "page": query.page,
            "page_size": query.page_size,
            "total_count": 0,
            "has_next": False,
            "total_amount": 0,
            "items": [],
        }

    async def cash_receipt_purchases(self, query):
        self.calls.append(("cash_receipt_purchases", query))
        empty = {
            "count": 0,
            "supply_amount": 0,
            "tax_amount": 0,
            "tax_exempt_amount": 0,
            "total_amount": 0,
        }
        return {
            "company_name": "예시컨설팅",
            "query": query,
            "page": query.page,
            "page_size": query.page_size,
            "total_count": 0,
            "has_next": False,
            "eligible_total": empty,
            "deductible": empty,
            "optional_non_deductible": empty,
            "mandatory_non_deductible": empty,
            "items": [],
        }

    async def registered_business_cards(self, query):
        self.calls.append(("registered_business_cards", query))
        return {
            "company_name": "예시컨설팅",
            "query": query,
            "page": query.page,
            "page_size": query.page_size,
            "total_count": 0,
            "has_next": False,
            "items": [],
        }

    async def cash_receipt_sales(self, query):
        self.calls.append(("cash_receipt_sales", query))
        return {
            "company_name": "예시컨설팅",
            "query": query,
            "count": 0,
            "supply_amount": 0,
            "tax_amount": 0,
            "service_charge_amount": 0,
            "total_amount": 0,
            "items": [],
        }

    async def card_sales(self, query):
        self.calls.append(("card_sales", query))
        return {
            "company_name": "예시컨설팅",
            "query": query,
            "count": 0,
            "total_sales_amount": 0,
            "credit_card_amount": 0,
            "purchase_card_amount": 0,
            "service_charge_amount": 0,
            "items": [],
        }

    async def business_accounts(self, query):
        self.calls.append(("business_accounts", query))
        return {
            "company_name": "예시컨설팅",
            "query": query,
            "page": query.page,
            "page_size": query.page_size,
            "total_count": 0,
            "has_next": False,
            "items": [],
        }


class FakeClient:
    def __init__(self, financials):
        self.financials = financials
        self.closed = False

    async def close(self):
        self.closed = True


async def add_session(app, client):
    owner = hashlib.sha256(KEY.encode()).hexdigest()
    return await app.state.sessions.add(owner, client, {"user_id": "u"})


@pytest.fixture
def app():
    return create_app(Settings(api_keys=(KEY,), session_ttl=60))


@pytest.mark.asyncio
async def test_financial_routes_require_auth_and_never_cache(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as c:
        response = await c.get("/v1/hometax/sessions/missing/business-accounts")

    assert response.status_code == 401
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Pragma"] == "no-cache"


@pytest.mark.asyncio
async def test_financial_routes_validate_and_forward_read_only_queries(app):
    financials = FakeFinancials()
    item = await add_session(app, FakeClient(financials))
    headers = {"Authorization": f"Bearer {KEY}"}
    period = {"start_date": "2026-09-01", "end_date": "2026-09-12"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test", headers=headers
    ) as c:
        cards = await c.get(
            f"/v1/hometax/sessions/{item.id}/business-card-purchases",
            params={**period, "deduction": "deductible"},
        )
        registered_cards = await c.get(f"/v1/hometax/sessions/{item.id}/registered-business-cards")
        cash_purchases = await c.get(
            f"/v1/hometax/sessions/{item.id}/cash-receipt-purchases", params=period
        )
        cash_sales = await c.get(
            f"/v1/hometax/sessions/{item.id}/cash-receipt-sales", params={"year": 2026}
        )
        card_sales = await c.get(
            f"/v1/hometax/sessions/{item.id}/card-sales",
            params={"year": 2026, "quarter_from": 1, "quarter_to": 3},
        )
        accounts = await c.get(f"/v1/hometax/sessions/{item.id}/business-accounts")
        invalid = await c.get(
            f"/v1/hometax/sessions/{item.id}/business-card-purchases",
            params={"start_date": "2026-01-01", "end_date": "2026-04-02"},
        )

    assert [
        response.status_code
        for response in (
            cards,
            registered_cards,
            cash_purchases,
            cash_sales,
            card_sales,
            accounts,
        )
    ] == [
        200,
        200,
        200,
        200,
        200,
        200,
    ]
    assert invalid.status_code == 422
    assert cards.json()["query"]["deduction"] == "deductible"
    assert card_sales.json()["query"]["quarter_to"] == 3
    assert [name for name, _query in financials.calls] == [
        "business_cards",
        "registered_business_cards",
        "cash_receipt_purchases",
        "cash_receipt_sales",
        "card_sales",
        "business_accounts",
    ]
    assert all(response.headers["Cache-Control"] == "no-store" for response in (cards, accounts))
