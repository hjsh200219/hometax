import json
from datetime import date

import pytest
from pydantic import ValidationError

from hometax_login.errors import LoginError
from hometax_login.financials import (
    BusinessAccountQuery,
    BusinessCardQuery,
    CardSalesQuery,
    CashReceiptPurchaseQuery,
    FinancialDataClient,
    YearQuery,
)


class FakeInvoices:
    def __init__(self):
        self.business_profile = {"txprDscmNo": "1234567890"}

    async def _business(self):
        return "business-tin", "예시컨설팅"


class FakeClient:
    def __init__(self, responses, *, token="sso-token"):
        self.invoices = FakeInvoices()
        self.responses = responses
        self.token = token
        self.calls = []

    @staticmethod
    def _json(text):
        return json.loads(text)

    async def _request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if path == "/token.do":
            return json.dumps({"ssoToken": self.token})
        if path.endswith("/token.do"):
            return json.dumps({"RESULT": {"code": "S", "msg": "pubcPermission"}})
        action = kwargs["params"]["actionId"]
        return json.dumps(self.responses[action])


def result(**payload):
    return {"RESULT": {"result": "S", "code": ""}, **payload}


def card_response():
    return result(
        pageInfoVO={"pageNum": 1, "pageSize": 50, "totalCount": 1},
        sumTotaTrsAmt="11,000",
        busnCrdcTrsBrkdAdmDVOList=[
            {
                "trsDt": "20260901",
                "crdcBmanTxprNm": "예시상점",
                "crdcTxprDscmNoEncCntn": "123-45-67890",
                "crccTxprNm": "예시카드",
                "busnCrdCardNoEncCntn": "1111222233334444",
                "trsClNm": "승인",
                "splCft": "10,000",
                "vaTxamt": "1,000",
                "tip": 0,
                "totaTrsAmt": "11,000",
                "vatDdcClCd": "001",
                "vatDdcClNm": "공제",
            }
        ],
    )


def cash_purchase_response():
    totals = {}
    for prefix, count in (("ddcTrgt", 3), ("ddc", 2), ("chce", 1), ("rsnb", 1)):
        totals.update(
            {
                f"{prefix}CshEdeCmttScnt": count,
                f"{prefix}SplSumCft": count * 100,
                f"{prefix}VatSumTxamt": count * 10,
                f"{prefix}TipPblSumAmt": 0,
                f"{prefix}TotaSumAmt": count * 110,
            }
        )
    return result(
        pageInfoVO={"pageNum": 1, "pageSize": 50, "totalCount": 1},
        txamtDdcBrkdInqrDVO=totals,
        cshTrsBrkdInqrDVOList=[
            {
                "rcprTxprNm": "예시사용자",
                "mrntTxprNm": "예시가맹점",
                "mrntTxprDscmNoEncCntn": "123-45-67890",
                "bmanClNm": "일반",
                "trsScnt": 1,
                "splCft": 100,
                "vaTxamt": 10,
                "tip": 0,
                "totaTrsAmt": 110,
                "prhTxamtDdcClCd": "001",
            }
        ],
    )


@pytest.mark.parametrize(
    "factory",
    [
        lambda: BusinessCardQuery(start_date="2026-01-01", end_date="2026-04-02"),
        lambda: CashReceiptPurchaseQuery(start_date="2026-03-01", end_date="2026-02-01"),
        lambda: CardSalesQuery(year=2026, quarter_from=3, quarter_to=2),
        lambda: YearQuery(year=2100),
    ],
)
def test_financial_queries_reject_invalid_periods(factory):
    with pytest.raises(ValidationError):
        factory()


@pytest.mark.asyncio
async def test_business_card_purchase_parses_and_masks_card_number():
    client = FakeClient({"ATECRCCA001R06": card_response()})
    service = FinancialDataClient(client)

    page = await service.business_cards(
        BusinessCardQuery(start_date="2026-09-01", end_date="2026-09-12")
    )

    assert page.total_count == 1
    assert page.total_amount == 11_000
    assert page.items[0].transaction_date == date(2026, 9, 1)
    assert page.items[0].card_number == "1111-****-4444"
    action_call = client.calls[-1]
    sent = json.loads(action_call[2]["data"]["datas"])["BusnCrdcTrsBrkdAdmSVO"]
    assert sent["tin"] == "business-tin"
    assert sent["prhTxamtDdcYn"] == ""


@pytest.mark.asyncio
async def test_cash_receipt_purchase_returns_breakdowns_and_merchant_totals():
    client = FakeClient({"ATECRCBA001R09": cash_purchase_response()})
    service = FinancialDataClient(client)

    page = await service.cash_receipt_purchases(
        CashReceiptPurchaseQuery(
            start_date="2026-09-01", end_date="2026-09-12", deduction="deductible"
        )
    )

    assert page.eligible_total.count == 3
    assert page.deductible.total_amount == 220
    assert page.optional_non_deductible.count == 1
    assert page.items[0].merchant_name == "예시가맹점"
    sent = json.loads(client.calls[-1][2]["data"]["datas"])["CshTrsBrkdInqrSVO"]
    assert sent["prhTxamtDdcYn"] == "Y"


@pytest.mark.asyncio
async def test_sales_summaries_and_business_accounts_use_fixed_read_actions():
    responses = {
        "ATECRCBA003R05": result(
            cshptIsfIsnPubcDVOList=[
                {
                    "sttsYm": "202609",
                    "cshSlsCmttScnt": 2,
                    "cshSlsSplCftCmttAmt": 1000,
                    "cshSlsVatCmttAmt": 100,
                    "cshSlsTipCmttAmt": 0,
                    "cshSlsCmttAmt": 1100,
                }
            ]
        ),
        "ATESFAAA014R02": result(
            crdcTrsBrkdMateAdmDVOList=[],
            sleVcexSlsMateInqrDVOList=[
                {
                    "stlYm": "202609",
                    "mateKndNm": "신용카드",
                    "stlScnt": 3,
                    "totaStlAmt": 3300,
                    "etcSls": 3300,
                    "purcEuCardSls": 0,
                    "tip": 0,
                }
            ],
            crdcZrpSleStlVcexMateAdmDVOList=[],
        ),
        "ATTCMCDA001R02": result(
            pageInfoVO={"pageNum": 1, "pageSize": 50, "totalCount": 1},
            accAdmDVOList=[
                {
                    "txprDscmNoEncCntn": "123-45-67890",
                    "txprAccClCdNm": "사업용계좌",
                    "bankNm": "예시은행",
                    "accnoEncCntn": "123456789012",
                    "accRgtDt": "20260901",
                    "accUseStrtDt": "20260901",
                    "accUseEndDt": "",
                    "accStatClCdNm": "사용",
                }
            ],
        ),
        "ATECREAA002R02": result(
            pageInfoVO={"pageNum": 1, "pageSize": 50, "totalCount": 1},
            busnCrdcAdmDVOList=[
                {
                    "busnCrdcCrcmClCdNm": "신용카드",
                    "busnCrdCardNoEncCntn": "1111222233334444",
                    "busnCrdcCrtnDt": "20260901",
                    "busnCrdcRgtStatCdNm": "등록완료",
                    "cardCnfrDt": "20260902",
                }
            ],
        ),
    }
    client = FakeClient(responses)
    service = FinancialDataClient(client)

    cash = await service.cash_receipt_sales(YearQuery(year=2026))
    cards = await service.card_sales(CardSalesQuery(year=2026, quarter_from=1, quarter_to=3))
    accounts = await service.business_accounts(BusinessAccountQuery())
    registered_cards = await service.registered_business_cards(BusinessAccountQuery())

    assert cash.total_amount == 1100
    assert cards.credit_card_amount == 3300
    assert accounts.items[0].account_number == "****-9012"
    assert accounts.items[0].registered_date == date(2026, 9, 1)
    assert registered_cards.items[0].card_number == "1111-****-4444"
    assert registered_cards.items[0].status == "등록완료"
    assert [
        call[2]["params"]["actionId"]
        for call in client.calls
        if "actionId" in call[2].get("params", {})
    ] == [
        "ATECRCBA003R05",
        "ATESFAAA014R02",
        "ATTCMCDA001R02",
        "ATECREAA002R02",
    ]


@pytest.mark.asyncio
async def test_mobile_sso_rejects_missing_token_before_financial_action():
    client = FakeClient({"ATECRCCA001R06": card_response()}, token="")

    with pytest.raises(LoginError) as caught:
        await FinancialDataClient(client).business_cards(
            BusinessCardQuery(start_date="2026-09-01", end_date="2026-09-12")
        )

    assert caught.value.code == "SESSION_NOT_AUTHENTICATED"
    assert all("jsonAction.do" not in path for _method, path, _kwargs in client.calls)
