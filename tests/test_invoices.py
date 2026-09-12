import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from pydantic import ValidationError

from hometax_login.errors import LoginError
from hometax_login.invoices import CounterpartyQuery, InvoiceFilters, InvoiceQuery
from hometax_login.protocol import HometaxClient

USER_ID = "user-2025"
TIN = "1234567890"
CONVERTED_TIN = "9876543210"
COMPANY = "예시공급사"
APPROVAL_1 = "202501011234567890123456"
APPROVAL_2 = "202501021234567890123456"
APPROVAL_3 = "202501031234567890123456"
APPROVAL_ALPHA = "20250101ABCDEFGH1234567Z"


def approval(index: int) -> str:
    return f"202501{index:02d}1234567890123456"


def json_body(request: httpx.Request) -> dict:
    return json.loads(request.content.decode() or "{}")


def row(
    approval: str = APPROVAL_1,
    *,
    written: str = "20250101",
    issued: str = "20250101",
    transmitted: str | None = "20250101",
    supply=1_000,
    tax=100,
    total=1_100,
    name="(주)예시거래처",
):
    return {
        "etan": approval,
        "wrtDt": written,
        "isnDtm": issued,
        "tmsnDt": transmitted or "",
        "tnmNm": name,
        "lsatNm": "테스트 용역",
        "sumSplCft": supply,
        "sumTxamt": tax,
        "totaAmt": total,
    }


def counterparty_row(
    *,
    name="(주)예시거래처",
    number="123-45-67890",
    representative="홍길동",
    primary="Y",
):
    return {
        "tnmNm": name,
        "txprDscmNoEncCntnView": number,
        "txprDscmNoEncCntn": "ciphertext-must-not-leak",
        "rprsFnm": representative,
        "pfbAdr": "서울",
        "bcNm": "서비스",
        "itmNm": "교육",
        "dmnrMpbNo": "0001",
        "frsRgtDtm": "20250101101010",
        "prcpClplcYn": primary,
        "txprDscmNo": "8001011234567",
    }


def verify_response(user_id=USER_ID):
    return {
        "resultMsg": {
            "sessionMap": {
                "userId": user_id,
                "userNm": "테스트",
            }
        }
    }


def token_response(**overrides):
    return {"txppSessionId": "token-1", **overrides}


def permission_response(
    *,
    user_id=USER_ID,
    tin=TIN,
    cnvr_tin=CONVERTED_TIN,
    txfr="Y",
    habo=None,
    user_class="02",
    mpb_no=None,
):
    session = {
        "userId": user_id,
        "tnmNm": COMPANY,
        "tin": tin,
        "cnvrTin": cnvr_tin,
        "txfrBmanLgnYn": txfr,
        "userClsfCd": user_class,
    }
    if habo is not None:
        session["haboInqrStat"] = habo
    if mpb_no is not None:
        session["mpbNo"] = mpb_no
    return {"resultMsg": {"sessionMap": session}}


def page_response(*, payload: dict, rows: list[dict], total: int | None = None, **overrides):
    page = payload["pageInfoVO"]
    invoice_vo = payload["etxivIsnBrkdTermDVOPrmt"]
    data = {
        "resultMsg": {"result": "S", "errorCd": "", "errorMsg": ""},
        "etxivIsnBrkdTermDVOPrmt": dict(invoice_vo),
        "pageInfoVO": {
            "pageNum": page["pageNum"],
            "pageSize": page["pageSize"],
            "totalCount": len(rows) if total is None else total,
        },
        "etxivIsnBrkdTermDVOList": rows,
    }
    data.update(overrides)
    return data


def detail_response(*, number=APPROVAL_1, supply=1_000, tax=100, total=1_100, **overrides):
    data = {
        "resultMsg": {"result": "S", "errorCd": "", "errorMsg": ""},
        "etxivIsnBrkdTermDVO": {
            "etan": number,
            "sumSplCft": supply,
            "sumTxamt": tax,
            "totaAmt": total,
            "splrTnmNm": COMPANY,
            "dmnrTnmNm": "(주)예시거래처",
            "splrTxprDscmNo": "123-45-67890",
            "dmnrTxprDscmNo": "800101-1234567",
            "privateKey": "raw-private-key-must-not-leak",
        },
        "lsatInfrBizSVOList": [
            {
                "lsatNm": "테스트 용역",
                "lsatSplDt": "20250101",
                "lsatRszeNm": "1식",
                "lsatQty": "1",
                "lsatUtprc": "1000",
                "lsatSplCft": supply,
                "lsatTxamt": tax,
                "lsatRmrkCntn": "비고",
            }
        ],
    }
    data.update(overrides)
    return data


def counterparty_response(
    *, payload: dict, rows: list[dict], total: int | None = None, **overrides
):
    page = payload["pageInfoVO"]
    data = {
        "resultMsg": {"result": "S", "errorCd": "", "errorMsg": ""},
        "myClplcListDVO": rows,
        "pageInfoVO": {
            "pageNum": page["pageNum"],
            "pageSize": page["pageSize"],
            "totalCount": len(rows) if total is None else total,
        },
    }
    data.update(overrides)
    return data


class InvoiceTransport:
    def __init__(
        self,
        *,
        verify=None,
        token=None,
        permission=None,
        pages=None,
        details=None,
        counterparties=None,
        mutate_page=None,
        mutate_detail=None,
        mutate_counterparties=None,
    ):
        self.verify = verify if verify is not None else verify_response()
        self.token = token if token is not None else token_response()
        self.permission = permission if permission is not None else permission_response()
        self.pages = pages or {}
        self.details = details or {}
        self.counterparties = counterparties or {}
        self.mutate_page = mutate_page
        self.mutate_detail = mutate_detail
        self.mutate_counterparties = mutate_counterparties
        self.calls = []
        self.page_payloads = []
        self.detail_payloads = []
        self.counterparty_payloads = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, str(request.url), request.url.path))
        if (
            request.url.path == "/permission.do"
            and request.url.params.get("screenId") == "index_pp"
        ):
            return httpx.Response(200, json=self.verify)
        if request.url.path == "/token.do":
            return httpx.Response(200, json=self.token)
        if request.url.path == "/permission.do" and "teet.hometax.go.kr" in str(request.url):
            return httpx.Response(200, json=self.permission)
        if request.url.path == "/wqAction.do":
            payload = json_body(request)
            action_id = request.url.params.get("actionId")
            if action_id == "ATEETBDA001R01":
                self.page_payloads.append(payload)
                page_num = payload["pageInfoVO"]["pageNum"]
                rows, total = self.pages.get(page_num, ([], 0))
                data = page_response(payload=payload, rows=rows, total=total)
                if self.mutate_page:
                    data = self.mutate_page(data, payload, page_num)
            elif action_id == "ATEETBDA001R02":
                self.detail_payloads.append(payload)
                number = payload["etxivIsnBrkdTermDVOPrmt"]["etan"]
                data = self.details.get(number, detail_response(number=number))
                if self.mutate_detail:
                    data = self.mutate_detail(data, payload)
            elif action_id == "ATEETBAE001R06":
                self.counterparty_payloads.append(payload)
                page_num = payload["pageInfoVO"]["pageNum"]
                rows, total = self.counterparties.get(page_num, ([], 0))
                data = counterparty_response(payload=payload, rows=rows, total=total)
                if self.mutate_counterparties:
                    data = self.mutate_counterparties(data, payload, page_num)
            else:
                raise AssertionError(f"unexpected actionId: {action_id}")
            return httpx.Response(200, json=data)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")


async def run_list(transport: InvoiceTransport, **overrides):
    client = HometaxClient(transport=httpx.MockTransport(transport))
    try:
        query = InvoiceQuery(
            start_date=date(2025, 1, 1),
            end_date=date(2025, 1, 31),
            **overrides,
        )
        return await client.invoices.list(query), client
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_business_uses_verify_token_then_et_permission_and_keeps_current_tin():
    transport = InvoiceTransport(pages={1: ([row()], 1)})
    page, _ = await run_list(transport)

    assert page.company_name == COMPANY
    assert [path for _, _, path in transport.calls[:3]] == [
        "/permission.do",
        "/token.do",
        "/permission.do",
    ]
    invoice_vo = transport.page_payloads[0]["etxivIsnBrkdTermDVOPrmt"]
    assert invoice_vo["splrTin"] == TIN
    assert invoice_vo["dmnrTin"] == ""
    assert invoice_vo["prhSlsClCd"] == "01"


@pytest.mark.asyncio
async def test_business_selects_converted_tin_when_session_requires_conversion():
    transport = InvoiceTransport(
        permission=permission_response(txfr="N", habo="N"),
        pages={1: ([row()], 1)},
    )
    page, _ = await run_list(transport)

    assert page.total_count == 1
    invoice_vo = transport.page_payloads[0]["etxivIsnBrkdTermDVOPrmt"]
    assert invoice_vo["splrTin"] == CONVERTED_TIN


@pytest.mark.asyncio
async def test_business_rejects_identity_change_between_portal_and_et_permission():
    transport = InvoiceTransport(permission=permission_response(user_id="other-user"))
    client = HometaxClient(transport=httpx.MockTransport(transport))
    try:
        with pytest.raises(LoginError) as caught:
            await client.invoices.list(
                InvoiceQuery(start_date=date(2025, 1, 1), end_date=date(2025, 1, 31))
            )
    finally:
        await client.close()
    assert caught.value.code == "SESSION_IDENTITY_CHANGED"


@pytest.mark.asyncio
async def test_page_payload_uses_all_filters_and_distinguishes_sales_purchases_date_basis():
    sales_transport = InvoiceTransport(pages={1: ([], 0)})
    await run_list(sales_transport, direction="sales", date_basis="issued")
    sales_vo = sales_transport.page_payloads[0]["etxivIsnBrkdTermDVOPrmt"]

    purchase_transport = InvoiceTransport(pages={1: ([], 0)})
    await run_list(purchase_transport, direction="purchases", date_basis="transmitted")
    purchase_vo = purchase_transport.page_payloads[0]["etxivIsnBrkdTermDVOPrmt"]

    assert sales_vo["etxivClsfCd"] == "all"
    assert sales_vo["etxivKndCd"] == "all"
    assert sales_vo["isnTypeCd"] == "all"
    assert sales_vo["splrTin"] == TIN
    assert sales_vo["dmnrTin"] == ""
    assert sales_vo["prhSlsClCd"] == "01"
    assert sales_vo["dtCl"] == "02"
    assert purchase_vo["splrTin"] == ""
    assert purchase_vo["dmnrTin"] == TIN
    assert purchase_vo["prhSlsClCd"] == "02"
    assert purchase_vo["dtCl"] == "03"


@pytest.mark.asyncio
async def test_page_forwards_branch_number_by_direction_when_business_has_mpb_no():
    sales_transport = InvoiceTransport(
        permission=permission_response(mpb_no="4"),
        pages={1: ([], 0)},
    )
    await run_list(sales_transport, direction="sales")
    sales_vo = sales_transport.page_payloads[0]["etxivIsnBrkdTermDVOPrmt"]

    purchase_transport = InvoiceTransport(
        permission=permission_response(mpb_no="4"),
        pages={1: ([], 0)},
    )
    await run_list(purchase_transport, direction="purchases")
    purchase_vo = purchase_transport.page_payloads[0]["etxivIsnBrkdTermDVOPrmt"]

    assert sales_vo["splrMpbNo"] == "4"
    assert sales_vo["dmnrMpbNo"] == ""
    assert purchase_vo["splrMpbNo"] == ""
    assert purchase_vo["dmnrMpbNo"] == "4"


@pytest.mark.asyncio
async def test_page_normalizes_hyphenated_approval_number_to_api_safe_digits():
    hyphenated = "20250101-12345678-90123456"
    page, _ = await run_list(InvoiceTransport(pages={1: ([row(hyphenated)], 1)}))

    assert page.items[0].approval_number == APPROVAL_1


@pytest.mark.asyncio
async def test_page_normalizes_real_alnum_hyphenated_approval_number():
    page, _ = await run_list(InvoiceTransport(pages={1: ([row("20250101-ABCDEFGH-1234567Z")], 1)}))

    assert page.items[0].approval_number == APPROVAL_ALPHA


@pytest.mark.asyncio
async def test_page_accepts_normal_zero_result():
    page, _ = await run_list(InvoiceTransport(pages={1: ([], 0)}))

    assert page.total_count == 0
    assert page.items == []
    assert page.has_next is False


@pytest.mark.parametrize(
    ("mutate", "error_code"),
    [
        (
            lambda data, payload, page: {k: v for k, v in data.items() if k != "resultMsg"},
            "INVOICE_QUERY_FAILED",
        ),
        (
            lambda data, payload, page: {
                **data,
                "resultMsg": {"result": "F", "errorCd": "E", "errorMsg": "denied"},
            },
            "INVOICE_QUERY_FAILED",
        ),
        (
            lambda data, payload, page: {
                **data,
                "etxivIsnBrkdTermDVOPrmt": {
                    **data["etxivIsnBrkdTermDVOPrmt"],
                    "etxivClsfCd": "",
                },
            },
            "INVOICE_RESPONSE_CHANGED",
        ),
        (
            lambda data, payload, page: {
                **data,
                "pageInfoVO": {**data["pageInfoVO"], "pageNum": page + 1},
            },
            "INVOICE_RESPONSE_CHANGED",
        ),
        (
            lambda data, payload, page: {
                **data,
                "etxivIsnBrkdTermDVOList": [
                    {k: v for k, v in data["etxivIsnBrkdTermDVOList"][0].items() if k != "sumTxamt"}
                ],
            },
            "INVOICE_RESPONSE_CHANGED",
        ),
        (
            lambda data, payload, page: {
                **data,
                "etxivIsnBrkdTermDVOList": [
                    {**data["etxivIsnBrkdTermDVOList"][0], "totaAmt": "999"}
                ],
            },
            "INVOICE_RESPONSE_CHANGED",
        ),
        (
            lambda data, payload, page: {
                **data,
                "etxivIsnBrkdTermDVOList": [
                    {**data["etxivIsnBrkdTermDVOList"][0], "isnDtm": "20250201"}
                ],
            },
            "INVOICE_RESPONSE_CHANGED",
        ),
        (
            lambda data, payload, page: {
                **data,
                "etxivIsnBrkdTermDVOList": [
                    {**data["etxivIsnBrkdTermDVOList"][0], "etan": "bad-approval"}
                ],
            },
            "INVOICE_RESPONSE_CHANGED",
        ),
        (
            lambda data, payload, page: {
                **data,
                "etxivIsnBrkdTermDVOList": [
                    data["etxivIsnBrkdTermDVOList"][0],
                    data["etxivIsnBrkdTermDVOList"][0],
                ],
                "pageInfoVO": {**data["pageInfoVO"], "totalCount": 2},
            },
            "INVOICE_RESPONSE_CHANGED",
        ),
    ],
)
async def test_page_rejects_changed_or_inconsistent_upstream_responses(mutate, error_code):
    transport = InvoiceTransport(pages={1: ([row()], 1)}, mutate_page=mutate)
    client = HometaxClient(transport=httpx.MockTransport(transport))
    try:
        with pytest.raises(LoginError) as caught:
            await client.invoices.list(
                InvoiceQuery(start_date=date(2025, 1, 1), end_date=date(2025, 1, 31))
            )
    finally:
        await client.close()
    assert caught.value.code == error_code


@pytest.mark.asyncio
async def test_summary_fetches_all_pages_and_includes_negative_corrections(monkeypatch):
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("hometax_login.invoices.asyncio.sleep", no_sleep)
    rows_1 = [
        row(approval(i), issued=f"202501{min(i, 31):02d}", supply=10, tax=1, total=11)
        for i in range(1, 51)
    ]
    rows_1[1] = row(APPROVAL_2, issued="20250102", supply=-300, tax=-30, total=-330)
    rows_2 = [row(approval(51), issued="20250103", supply=2_000, tax=200, total=2_200)]
    transport = InvoiceTransport(pages={1: (rows_1, 51), 2: (rows_2, 51)})
    client = HometaxClient(transport=httpx.MockTransport(transport))
    try:
        summary = await client.invoices.summary(
            InvoiceFilters(start_date=date(2025, 1, 1), end_date=date(2025, 1, 31))
        )
    finally:
        await client.close()

    assert [p["pageInfoVO"]["pageNum"] for p in transport.page_payloads] == [1, 2]
    assert summary.total_count == 51
    assert summary.supply_amount == 2_190
    assert summary.tax_amount == 219
    assert summary.total_amount == 2_409


@pytest.mark.parametrize(
    "second_page",
    [
        ([row(approval(1), issued="20250103")], 51),
        (
            [
                row(approval(51), issued="20250103"),
                row(approval(52), issued="20250104"),
            ],
            52,
        ),
    ],
)
@pytest.mark.asyncio
async def test_summary_rejects_duplicate_or_total_count_change_while_paging(
    second_page, monkeypatch
):
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("hometax_login.invoices.asyncio.sleep", no_sleep)
    first_page = [row(approval(i), issued=f"202501{min(i, 31):02d}") for i in range(1, 51)]
    transport = InvoiceTransport(
        pages={
            1: (first_page, 51),
            2: second_page,
        }
    )
    client = HometaxClient(transport=httpx.MockTransport(transport))
    try:
        with pytest.raises(LoginError) as caught:
            await client.invoices.summary(
                InvoiceFilters(start_date=date(2025, 1, 1), end_date=date(2025, 1, 31))
            )
    finally:
        await client.close()

    assert caught.value.code == "INVOICE_LIST_CHANGED"
    assert caught.value.status == 409


@pytest.mark.asyncio
async def test_summary_blocks_over_5000_rows_before_fetching_next_page():
    rows = [row(approval(i), issued=f"202501{min(i, 31):02d}") for i in range(1, 51)]
    transport = InvoiceTransport(pages={1: (rows, 5_001)})
    client = HometaxClient(transport=httpx.MockTransport(transport))
    try:
        with pytest.raises(LoginError) as caught:
            await client.invoices.summary(
                InvoiceFilters(start_date=date(2025, 1, 1), end_date=date(2025, 1, 31))
            )
    finally:
        await client.close()

    assert caught.value.code == "INVOICE_SUMMARY_TOO_LARGE"
    assert caught.value.status == 422
    assert len(transport.page_payloads) == 1


@pytest.mark.asyncio
async def test_detail_requires_cached_list_reference_without_network_call():
    transport = InvoiceTransport()
    client = HometaxClient(transport=httpx.MockTransport(transport))
    try:
        with pytest.raises(LoginError) as caught:
            await client.invoices.detail(APPROVAL_1)
    finally:
        await client.close()

    assert caught.value.code == "INVOICE_NOT_LISTED"
    assert caught.value.status == 404
    assert transport.calls == []


@pytest.mark.asyncio
async def test_detail_clears_cached_reference_when_business_changes():
    transport = InvoiceTransport(pages={1: ([row()], 1)})
    client = HometaxClient(transport=httpx.MockTransport(transport))
    try:
        await client.invoices.list(
            InvoiceQuery(start_date=date(2025, 1, 1), end_date=date(2025, 1, 31))
        )
        transport.permission = permission_response(tin="2222222222", cnvr_tin="2222222222")
        with pytest.raises(LoginError) as caught:
            await client.invoices.detail(APPROVAL_1)
    finally:
        await client.close()

    assert caught.value.code == "INVOICE_NOT_LISTED"
    assert transport.detail_payloads == []


@pytest.mark.asyncio
async def test_detail_clears_cached_reference_when_branch_changes():
    transport = InvoiceTransport(pages={1: ([row()], 1)})
    client = HometaxClient(transport=httpx.MockTransport(transport))
    try:
        await client.invoices.list(InvoiceQuery(start_date="2025-01-01", end_date="2025-01-31"))
        transport.permission = permission_response(mpb_no="0001")
        with pytest.raises(LoginError) as caught:
            await client.invoices.detail(APPROVAL_1)
        assert caught.value.code == "INVOICE_NOT_LISTED"
        assert transport.detail_payloads == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_detail_rejects_nested_data_inside_scalar_item_fields():
    data = detail_response()
    data["lsatInfrBizSVOList"][0]["lsatRmrkCntn"] = {"secret": "never-return-this"}
    transport = InvoiceTransport(pages={1: ([row()], 1)}, details={APPROVAL_1: data})
    client = HometaxClient(transport=httpx.MockTransport(transport))
    try:
        await client.invoices.list(InvoiceQuery(start_date="2025-01-01", end_date="2025-01-31"))
        with pytest.raises(LoginError) as caught:
            await client.invoices.detail(APPROVAL_1)
        assert caught.value.code == "INVOICE_RESPONSE_CHANGED"
        assert "never-return-this" not in str(caught.value)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_detail_payload_and_response_return_only_allowlisted_fields():
    transport = InvoiceTransport(pages={1: ([row("20250101-12345678-90123456")], 1)})
    client = HometaxClient(transport=httpx.MockTransport(transport))
    try:
        await client.invoices.list(
            InvoiceQuery(start_date=date(2025, 1, 1), end_date=date(2025, 1, 31))
        )
        detail = await client.invoices.detail(APPROVAL_1)
    finally:
        await client.close()

    payload = transport.detail_payloads[0]["etxivIsnBrkdTermDVOPrmt"]
    assert payload == {
        "etan": APPROVAL_1,
        "screenId": "UTEETBDA01",
        "pageNum": "1",
        "slsPrhClCd": "01",
        "etxivTin": TIN,
        "etxivClCd": "",
        "etxivClsfCd": "",
        "etxivMpbNo": "",
    }
    dumped = detail.model_dump()
    assert dumped["supplier_business_number"] == "1234567890"
    assert dumped["customer_business_number"] is None
    assert dumped["items"][0]["name"] == "테스트 용역"
    assert "privateKey" not in dumped
    assert "8001011234567" not in str(dumped)


@pytest.mark.asyncio
async def test_detail_accepts_cached_alnum_approval_number_with_letter_suffix():
    transport = InvoiceTransport(
        pages={1: ([row("20250101-ABCDEFGH-1234567Z")], 1)},
        details={APPROVAL_ALPHA: detail_response(number="20250101-ABCDEFGH-1234567Z")},
    )
    client = HometaxClient(transport=httpx.MockTransport(transport))
    try:
        await client.invoices.list(
            InvoiceQuery(start_date=date(2025, 1, 1), end_date=date(2025, 1, 31))
        )
        detail = await client.invoices.detail(APPROVAL_ALPHA)
    finally:
        await client.close()

    assert detail.invoice.approval_number == APPROVAL_ALPHA
    assert transport.detail_payloads[0]["etxivIsnBrkdTermDVOPrmt"]["etan"] == APPROVAL_ALPHA


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data, payload: {
            **data,
            "etxivIsnBrkdTermDVO": {**data["etxivIsnBrkdTermDVO"], "etan": APPROVAL_2},
        },
        lambda data, payload: {
            **data,
            "etxivIsnBrkdTermDVO": {**data["etxivIsnBrkdTermDVO"], "sumSplCft": "999"},
        },
        lambda data, payload: {
            **data,
            "lsatInfrBizSVOList": [{**data["lsatInfrBizSVOList"][0], "lsatSplCft": "999"}],
        },
    ],
)
@pytest.mark.asyncio
async def test_detail_rejects_mismatched_approval_amounts_or_line_totals(mutate):
    transport = InvoiceTransport(pages={1: ([row()], 1)}, mutate_detail=mutate)
    client = HometaxClient(transport=httpx.MockTransport(transport))
    try:
        await client.invoices.list(
            InvoiceQuery(start_date=date(2025, 1, 1), end_date=date(2025, 1, 31))
        )
        with pytest.raises(LoginError) as caught:
            await client.invoices.detail(APPROVAL_1)
    finally:
        await client.close()

    assert caught.value.code == "INVOICE_RESPONSE_CHANGED"


@pytest.mark.asyncio
async def test_session_rejection_is_safe_and_does_not_return_upstream_body():
    transport = InvoiceTransport(
        verify={"resultMsg": {"errorMsg": "raw-upstream-secret", "sessionMap": {}}},
    )
    client = HometaxClient(transport=httpx.MockTransport(transport))
    try:
        with pytest.raises(LoginError) as caught:
            await client.invoices.list(
                InvoiceQuery(start_date=date(2025, 1, 1), end_date=date(2025, 1, 31))
            )
    finally:
        await client.close()

    assert caught.value.code == "SESSION_NOT_AUTHENTICATED"
    assert caught.value.status == 401
    assert "raw-upstream-secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_counterparty_query_payload_and_zero_result_are_forwarded():
    transport = InvoiceTransport(
        permission=permission_response(mpb_no="7"),
        counterparties={2: ([], 0)},
    )
    client = HometaxClient(transport=httpx.MockTransport(transport))
    try:
        page = await client.invoices.counterparties(
            CounterpartyQuery(
                name="예시거래처",
                business_number="1234567890",
                representative_name="홍길동",
                page=2,
                page_size=20,
            )
        )
    finally:
        await client.close()

    assert page.company_name == COMPANY
    assert page.total_count == 0
    assert page.has_next is False
    payload = transport.counterparty_payloads[0]
    assert payload["splrTin"] == TIN
    assert payload["splrMpbNo"] == "7"
    assert payload["txprDscmNoEncCntn"] == "1234567890"
    assert payload["txprNm"] == "예시거래처"
    assert payload["rprsFnm"] == "홍길동"
    assert payload["pageInfoVO"] == {"pageNum": 2, "pageSize": 20, "totalCount": 0}
    action_ids = [
        url.split("actionId=", 1)[1].split("&", 1)[0]
        for _, url, path in transport.calls
        if path == "/wqAction.do"
    ]
    assert action_ids == ["ATEETBAE001R06"]


@pytest.mark.asyncio
async def test_counterparty_response_maps_pagination_and_filters_non_business_identifiers():
    rows = [
        counterparty_row(number="123-45-67890", primary="Y"),
        counterparty_row(name="개인거래처", number="800101-1234567", primary="N"),
        counterparty_row(name="암호문거래처", number="encrypted-value", primary=""),
    ]
    rows.extend(counterparty_row(name=f"거래처{i}", number=f"12345{i:05d}") for i in range(47))
    transport = InvoiceTransport(counterparties={1: (rows, 60)})
    client = HometaxClient(transport=httpx.MockTransport(transport))
    try:
        page = await client.invoices.counterparties(CounterpartyQuery(page=1, page_size=50))
    finally:
        await client.close()

    assert page.total_count == 60
    assert page.has_next is True
    assert page.items[0].business_number == "1234567890"
    assert page.items[0].representative_name == "홍길동"
    assert page.items[0].is_primary is True
    assert page.items[1].business_number is None
    assert page.items[1].is_primary is False
    assert page.items[2].business_number is None
    assert page.items[2].is_primary is None
    dumped = page.model_dump()
    assert "8001011234567" not in str(dumped)
    assert "ciphertext-must-not-leak" not in str(dumped)


@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    [
        (
            lambda data, payload, page: {
                **data,
                "resultMsg": {"result": "F", "errorCd": "E", "errorMsg": "denied"},
            },
            "COUNTERPARTY_QUERY_FAILED",
        ),
        (
            lambda data, payload, page: {k: v for k, v in data.items() if k != "myClplcListDVO"},
            "INVOICE_RESPONSE_CHANGED",
        ),
        (
            lambda data, payload, page: {
                **data,
                "pageInfoVO": {
                    **data["pageInfoVO"],
                    "pageSize": payload["pageInfoVO"]["pageSize"] + 1,
                },
            },
            "INVOICE_RESPONSE_CHANGED",
        ),
        (
            lambda data, payload, page: {
                **data,
                "myClplcListDVO": [{**data["myClplcListDVO"][0], "tnmNm": ""}],
            },
            "INVOICE_RESPONSE_CHANGED",
        ),
    ],
)
@pytest.mark.asyncio
async def test_counterparty_rejects_failed_or_changed_upstream_response(mutate, expected_code):
    transport = InvoiceTransport(
        counterparties={1: ([counterparty_row()], 1)},
        mutate_counterparties=mutate,
    )
    client = HometaxClient(transport=httpx.MockTransport(transport))
    try:
        with pytest.raises(LoginError) as caught:
            await client.invoices.counterparties(CounterpartyQuery(page_size=10))
    finally:
        await client.close()

    assert caught.value.code == expected_code


def test_invoice_filters_reject_periods_the_rule_owns():
    today = datetime.now(ZoneInfo("Asia/Seoul")).date()

    allowed = InvoiceFilters(start_date=today - timedelta(days=1), end_date=today)
    assert allowed.end_date == today

    rejected = (
        (today, today + timedelta(days=1)),
        (today - timedelta(days=1), today - timedelta(days=2)),
        (today - timedelta(days=200), today - timedelta(days=1)),
    )
    for start, end in rejected:
        with pytest.raises(ValidationError) as caught:
            InvoiceFilters(start_date=start, end_date=end)
        assert "ordered period of at most three months" in str(caught.value)
