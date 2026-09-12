"""Read-only HomeTax card, cash-receipt, and registered-account data."""

from __future__ import annotations

import calendar
import json
import re
from datetime import date, datetime
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .errors import LoginError

if TYPE_CHECKING:
    from .protocol import HometaxClient

MOBILE_CARD_BASE = "https://mob.tbcr.hometax.go.kr"
MOBILE_TAX_BASE = "https://mob.tbht.hometax.go.kr"


def changed() -> LoginError:
    return LoginError("FINANCIAL_RESPONSE_CHANGED", "홈택스 금융자료 응답을 검증하지 못했습니다.")


def integer(value, *, default: int | None = None) -> int:
    if value in (None, "") and default is not None:
        return default
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch(r"-?(?:\d+|\d{1,3}(?:,\d{3})+)", value):
        return int(value.replace(",", ""))
    raise changed()


def optional_text(value) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise changed()
    return value.strip() or None


def first_text(row: dict, *keys: str) -> str | None:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if value is not None and not isinstance(value, str):
            raise changed()
    return None


def normalized_day(value) -> date:
    if not isinstance(value, str):
        raise changed()
    compact = re.sub(r"[^0-9]", "", value)
    if len(compact) != 8:
        raise changed()
    try:
        return date.fromisoformat(f"{compact[:4]}-{compact[4:6]}-{compact[6:]}")
    except ValueError:
        raise changed() from None


def normalized_month(value) -> str:
    if not isinstance(value, str):
        raise changed()
    compact = re.sub(r"[^0-9]", "", value)
    if len(compact) != 6 or not 1 <= int(compact[4:]) <= 12:
        raise changed()
    return f"{compact[:4]}-{compact[4:]}"


def masked_number(value, *, prefix: int = 0) -> str | None:
    text = optional_text(value)
    if text is None:
        return None
    if "*" in text:
        return text
    digits = re.sub(r"\D", "", text)
    if len(digits) < 4:
        return "****"
    visible_prefix = digits[:prefix] if prefix else ""
    separator = "-" if visible_prefix else ""
    return f"{visible_prefix}{separator}****-{digits[-4:]}"


class PeriodQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def valid_period(self):
        today = datetime.now(ZoneInfo("Asia/Seoul")).date()
        month = self.start_date.month - 1 + 3
        year = self.start_date.year + month // 12
        month = month % 12 + 1
        limit = date(year, month, min(self.start_date.day, calendar.monthrange(year, month)[1]))
        if not self.start_date <= self.end_date <= min(today, limit):
            raise ValueError(
                "Use an ordered period of at most three months ending today or earlier"
            )
        return self


class PagedPeriodQuery(PeriodQuery):
    page: int = Field(default=1, ge=1, le=10000)
    page_size: Literal[10, 20, 30, 50] = 50


class BusinessCardQuery(PagedPeriodQuery):
    deduction: Literal["all", "deductible", "non_deductible"] = "all"


class CashReceiptPurchaseQuery(PagedPeriodQuery):
    deduction: Literal["all", "deductible", "non_deductible"] = "all"


class YearQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    year: int = Field(ge=2000, le=2100)

    @model_validator(mode="after")
    def not_future(self):
        if self.year > datetime.now(ZoneInfo("Asia/Seoul")).year:
            raise ValueError("Future years are not supported")
        return self


class CardSalesQuery(YearQuery):
    quarter_from: int = Field(default=1, ge=1, le=4)
    quarter_to: int = Field(default=4, ge=1, le=4)

    @model_validator(mode="after")
    def ordered_quarters(self):
        if self.quarter_from > self.quarter_to:
            raise ValueError("quarter_from must not exceed quarter_to")
        return self


class BusinessAccountQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page: int = Field(default=1, ge=1, le=10000)
    page_size: Literal[10, 20, 30, 50] = 50


class BusinessCardTransaction(BaseModel):
    transaction_date: date
    merchant_name: str | None = None
    merchant_business_number: str | None = None
    card_company: str | None = None
    card_number: str | None = None
    transaction_type: str | None = None
    supply_amount: int
    tax_amount: int
    tax_exempt_amount: int
    total_amount: int
    deduction_code: str | None = None
    deduction_name: str | None = None


class BusinessCardPage(BaseModel):
    company_name: str
    query: BusinessCardQuery
    page: int
    page_size: int
    total_count: int
    has_next: bool
    total_amount: int
    items: list[BusinessCardTransaction]


class BusinessCardRegistration(BaseModel):
    card_type: str | None = None
    card_number: str | None = None
    requested_date: date | None = None
    status: str | None = None
    confirmed_date: date | None = None


class BusinessCardRegistrationPage(BaseModel):
    company_name: str
    query: BusinessAccountQuery
    page: int
    page_size: int
    total_count: int
    has_next: bool
    items: list[BusinessCardRegistration]


class AmountBreakdown(BaseModel):
    count: int
    supply_amount: int
    tax_amount: int
    tax_exempt_amount: int
    total_amount: int


class CashReceiptMerchantTotal(BaseModel):
    user_name: str | None = None
    merchant_name: str | None = None
    merchant_business_number: str | None = None
    merchant_type: str | None = None
    count: int
    supply_amount: int
    tax_amount: int
    tax_exempt_amount: int
    total_amount: int
    deduction_code: str | None = None


class CashReceiptPurchasePage(BaseModel):
    company_name: str
    query: CashReceiptPurchaseQuery
    page: int
    page_size: int
    total_count: int
    has_next: bool
    eligible_total: AmountBreakdown
    deductible: AmountBreakdown
    optional_non_deductible: AmountBreakdown
    mandatory_non_deductible: AmountBreakdown
    items: list[CashReceiptMerchantTotal]


class CashReceiptSalesMonth(BaseModel):
    month: str
    count: int
    supply_amount: int
    tax_amount: int
    service_charge_amount: int
    total_amount: int


class CashReceiptSalesSummary(BaseModel):
    company_name: str
    query: YearQuery
    count: int
    supply_amount: int
    tax_amount: int
    service_charge_amount: int
    total_amount: int
    items: list[CashReceiptSalesMonth]


class CardSalesMonth(BaseModel):
    month: str
    data_type: str | None = None
    count: int
    total_sales_amount: int
    credit_card_amount: int
    purchase_card_amount: int
    service_charge_amount: int


class CardSalesSummary(BaseModel):
    company_name: str
    query: CardSalesQuery
    count: int
    total_sales_amount: int
    credit_card_amount: int
    purchase_card_amount: int
    service_charge_amount: int
    items: list[CardSalesMonth]


class BusinessAccount(BaseModel):
    business_number: str | None = None
    account_type: str | None = None
    bank_name: str | None = None
    account_number: str | None = None
    registered_date: date | None = None
    use_start_date: date | None = None
    use_end_date: date | None = None
    status: str | None = None


class BusinessAccountPage(BaseModel):
    company_name: str
    query: BusinessAccountQuery
    page: int
    page_size: int
    total_count: int
    has_next: bool
    items: list[BusinessAccount]


class FinancialDataClient:
    """Fixed read-only mobile HomeTax actions; callers cannot supply endpoints or action IDs."""

    def __init__(self, client: HometaxClient):
        self.client = client

    async def _business(self) -> tuple[str, str, str]:
        tin, company = await self.client.invoices._business()
        number = self.client.invoices.business_profile.get("txprDscmNo")
        if not isinstance(number, str) or not re.fullmatch(r"\d{10}", number):
            raise LoginError("BUSINESS_REQUIRED", "조회할 사업자로 로그인하거나 전환하세요.", 409)
        return tin, company, number

    async def _mobile_session(self, base: str, screen: str) -> None:
        token_data = self.client._json(
            await self.client._request("GET", "/token.do", params={"idx": "getToken2SC"})
        )
        token = token_data.get("ssoToken")
        if not isinstance(token, str) or not 1 <= len(token) <= 8192:
            raise LoginError("SESSION_NOT_AUTHENTICATED", "홈택스 통합인증 토큰이 없습니다.", 401)
        data = self.client._json(
            await self.client._request(
                "GET",
                base + "/token.do",
                params={
                    "idx": "verify",
                    "ssoToken": token,
                    "jspName": screen,
                    "txaaAdmNo": token_data.get("txaaAdmNo", ""),
                },
                headers={"Referer": f"{base}/jsonAction.do?actionId={screen}"},
            )
        )
        result = data.get("RESULT")
        if (
            not isinstance(result, dict)
            or result.get("code") != "S"
            or result.get("msg") not in {"pubcPermission", "tokenOK"}
        ):
            raise LoginError(
                "FINANCIAL_ACCESS_DENIED", "홈택스 금융자료 조회 권한을 확인하세요.", 403
            )

    async def _action(self, base: str, screen: str, action: str, payload: dict) -> dict:
        await self._mobile_session(base, screen)
        data = self.client._json(
            await self.client._request(
                "POST",
                base + "/jsonAction.do",
                params={
                    "actionId": action,
                    "jspName": screen,
                    "popupYn": "N",
                    "realJspName": screen,
                },
                data={"datas": json.dumps(payload, separators=(",", ":")), "m": ""},
                headers={
                    "Origin": base,
                    "Referer": f"{base}/jsonAction.do?actionId={screen}",
                    "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                },
            )
        )
        result = data.get("RESULT")
        if not isinstance(result, dict) or result.get("result") != "S":
            code = result.get("code") if isinstance(result, dict) else None
            if code == "loginRequest":
                raise LoginError("SESSION_NOT_AUTHENTICATED", "홈택스 세션이 만료됐습니다.", 401)
            raise LoginError("FINANCIAL_QUERY_FAILED", "홈택스 금융자료 조회에 실패했습니다.")
        return data

    async def business_cards(self, query: BusinessCardQuery) -> BusinessCardPage:
        tin, company, _number = await self._business()
        deduction = {"all": "", "deductible": "Y", "non_deductible": "N"}[query.deduction]
        data = await self._action(
            MOBILE_CARD_BASE,
            "UTBCRCB023F001",
            "ATECRCCA001R06",
            {
                "BusnCrdcTrsBrkdAdmSVO": {
                    "resultCd": "",
                    "dwldTrsBrkdScnt": "",
                    "txprDclsCd": "",
                    "tin": tin,
                    "trsDtRngStrt": query.start_date.strftime("%Y%m%d"),
                    "trsDtRngEnd": query.end_date.strftime("%Y%m%d"),
                    "prhTxamtDdcYn": deduction,
                    "sumTotaTrsAmt": "",
                },
                "pageInfoVO": {
                    "pageNum": query.page,
                    "pageSize": query.page_size,
                    "totalCount": 0,
                },
            },
        )
        rows, page = data.get("busnCrdcTrsBrkdAdmDVOList"), data.get("pageInfoVO")
        if not isinstance(rows, list) or not isinstance(page, dict):
            raise changed()
        items = []
        for row in rows:
            if not isinstance(row, dict):
                raise changed()
            supply = integer(row.get("splCft"))
            tax = integer(row.get("vaTxamt"), default=0)
            tax_exempt = integer(row.get("tip"), default=0)
            total = integer(row.get("totaTrsAmt"))
            if supply + tax + tax_exempt != total:
                raise changed()
            items.append(
                BusinessCardTransaction(
                    transaction_date=normalized_day(row.get("trsDt") or row.get("prhDt")),
                    merchant_name=first_text(row, "crdcBmanTxprNm", "mrntTxprNm"),
                    merchant_business_number=first_text(
                        row, "crdcTxprDscmNoEncCntn", "mrntTxprDscmNoEncCntn"
                    ),
                    card_company=first_text(row, "crccTxprNm", "tfbNm"),
                    card_number=masked_number(
                        row.get("busnCrdCardNoEncCntn") or row.get("wlfCardNoEncCntn"),
                        prefix=4,
                    ),
                    transaction_type=optional_text(row.get("trsClNm")),
                    supply_amount=supply,
                    tax_amount=tax,
                    tax_exempt_amount=tax_exempt,
                    total_amount=total,
                    deduction_code=optional_text(row.get("vatDdcClCd")),
                    deduction_name=first_text(row, "vatDdcClNm", "ddcYnNm"),
                )
            )
        total_count = integer(page.get("totalCount"), default=0)
        page_number = integer(page.get("pageNum"), default=query.page)
        page_size = integer(page.get("pageSize"), default=query.page_size)
        return BusinessCardPage(
            company_name=company,
            query=query,
            page=page_number,
            page_size=page_size,
            total_count=total_count,
            has_next=page_number * page_size < total_count,
            total_amount=integer(data.get("sumTotaTrsAmt"), default=0),
            items=items,
        )

    async def registered_business_cards(
        self, query: BusinessAccountQuery
    ) -> BusinessCardRegistrationPage:
        tin, company, _number = await self._business()
        data = await self._action(
            MOBILE_CARD_BASE,
            "UTBCRJD001F001",
            "ATECREAA002R02",
            {
                "BusnCrdcAdmSVO": {
                    "cmttYr": "",
                    "reqCd": "mb",
                    "tin": "",
                    "bmanTin": tin,
                },
                "pageInfoVO": {
                    "pageNum": query.page,
                    "pageSize": query.page_size,
                    "totalCount": 0,
                },
            },
        )
        rows, page = data.get("busnCrdcAdmDVOList"), data.get("pageInfoVO")
        if not isinstance(rows, list) or not isinstance(page, dict):
            raise changed()

        def optional_day(value) -> date | None:
            return None if value in (None, "") else normalized_day(value)

        items = []
        for row in rows:
            if not isinstance(row, dict):
                raise changed()
            items.append(
                BusinessCardRegistration(
                    card_type=optional_text(row.get("busnCrdcCrcmClCdNm")),
                    card_number=masked_number(row.get("busnCrdCardNoEncCntn"), prefix=4),
                    requested_date=optional_day(row.get("busnCrdcCrtnDt")),
                    status=optional_text(row.get("busnCrdcRgtStatCdNm")),
                    confirmed_date=optional_day(row.get("cardCnfrDt")),
                )
            )
        total_count = integer(page.get("totalCount"), default=len(items))
        page_number = integer(page.get("pageNum"), default=query.page)
        page_size = integer(page.get("pageSize"), default=query.page_size)
        return BusinessCardRegistrationPage(
            company_name=company,
            query=query,
            page=page_number,
            page_size=page_size,
            total_count=total_count,
            has_next=page_number * page_size < total_count,
            items=items,
        )

    @staticmethod
    def _breakdown(row: dict, prefix: str) -> AmountBreakdown:
        fields = {
            "ddcTrgt": (
                "ddcTrgtCshEdeCmttScnt",
                "ddcTrgtSplSumCft",
                "ddcTrgtVatSumTxamt",
                "ddcTrgtTipPblSumAmt",
                "ddcTrgtTotaSumAmt",
            ),
            "ddc": (
                "ddcCshEdeCmttScnt",
                "ddcSplSumCft",
                "ddcVatSumTxamt",
                "ddcTipPblSumAmt",
                "ddcTotaSumAmt",
            ),
            "chce": (
                "chceCshEdeCmttScnt",
                "chceSplSumCft",
                "chceVatSumTxamt",
                "chceTipPblSumAmt",
                "chceTotaSumAmt",
            ),
            "rsnb": (
                "rsnbCshEdeCmttScnt",
                "rsnbSplSumCft",
                "rsnbVatSumTxamt",
                "rsnbTipPblSumAmt",
                "rsnbTotaSumAmt",
            ),
        }[prefix]
        return AmountBreakdown(
            count=integer(row.get(fields[0]), default=0),
            supply_amount=integer(row.get(fields[1]), default=0),
            tax_amount=integer(row.get(fields[2]), default=0),
            tax_exempt_amount=integer(row.get(fields[3]), default=0),
            total_amount=integer(row.get(fields[4]), default=0),
        )

    async def cash_receipt_purchases(
        self, query: CashReceiptPurchaseQuery
    ) -> CashReceiptPurchasePage:
        tin, company, _number = await self._business()
        deduction = {"all": "", "deductible": "Y", "non_deductible": "N"}[query.deduction]
        data = await self._action(
            MOBILE_CARD_BASE,
            "UTBCRCB007F001",
            "ATECRCBA001R09",
            {
                "CshTrsBrkdInqrSVO": {
                    "tin": tin,
                    "trsDtRngStrt": query.start_date.strftime("%Y%m%d"),
                    "trsDtRngEnd": query.end_date.strftime("%Y%m%d"),
                    "prhTxamtDdcYn": deduction,
                },
                "pageInfoVO": {
                    "pageNum": query.page,
                    "pageSize": query.page_size,
                    "totalCount": 0,
                },
            },
        )
        rows, page, totals = (
            data.get("cshTrsBrkdInqrDVOList"),
            data.get("pageInfoVO"),
            data.get("txamtDdcBrkdInqrDVO"),
        )
        if not isinstance(rows, list) or not isinstance(page, dict) or not isinstance(totals, dict):
            raise changed()
        items = []
        for row in rows:
            if not isinstance(row, dict):
                raise changed()
            items.append(
                CashReceiptMerchantTotal(
                    user_name=optional_text(row.get("rcprTxprNm")),
                    merchant_name=optional_text(row.get("mrntTxprNm")),
                    merchant_business_number=optional_text(row.get("mrntTxprDscmNoEncCntn")),
                    merchant_type=optional_text(row.get("bmanClNm")),
                    count=integer(row.get("trsScnt"), default=0),
                    supply_amount=integer(row.get("splCft"), default=0),
                    tax_amount=integer(row.get("vaTxamt"), default=0),
                    tax_exempt_amount=integer(row.get("tip"), default=0),
                    total_amount=integer(row.get("totaTrsAmt"), default=0),
                    deduction_code=optional_text(row.get("prhTxamtDdcClCd")),
                )
            )
        total_count = integer(page.get("totalCount"), default=0)
        page_number = integer(page.get("pageNum"), default=query.page)
        page_size = integer(page.get("pageSize"), default=query.page_size)
        return CashReceiptPurchasePage(
            company_name=company,
            query=query,
            page=page_number,
            page_size=page_size,
            total_count=total_count,
            has_next=page_number * page_size < total_count,
            eligible_total=self._breakdown(totals, "ddcTrgt"),
            deductible=self._breakdown(totals, "ddc"),
            optional_non_deductible=self._breakdown(totals, "chce"),
            mandatory_non_deductible=self._breakdown(totals, "rsnb"),
            items=items,
        )

    async def cash_receipt_sales(self, query: YearQuery) -> CashReceiptSalesSummary:
        tin, company, _number = await self._business()
        data = await self._action(
            MOBILE_CARD_BASE,
            "UTBCRCB044F001",
            "ATECRCBA003R05",
            {"CshptIsfIsnPubcSVO": {"tin": tin, "trsDt": str(query.year)}},
        )
        rows = data.get("cshptIsfIsnPubcDVOList")
        if not isinstance(rows, list):
            raise changed()
        items = []
        for row in rows:
            if not isinstance(row, dict):
                raise changed()
            items.append(
                CashReceiptSalesMonth(
                    month=normalized_month(row.get("sttsYm")),
                    count=integer(row.get("cshSlsCmttScnt"), default=0),
                    supply_amount=integer(row.get("cshSlsSplCftCmttAmt"), default=0),
                    tax_amount=integer(row.get("cshSlsVatCmttAmt"), default=0),
                    service_charge_amount=integer(row.get("cshSlsTipCmttAmt"), default=0),
                    total_amount=integer(row.get("cshSlsCmttAmt"), default=0),
                )
            )
        return CashReceiptSalesSummary(
            company_name=company,
            query=query,
            count=sum(item.count for item in items),
            supply_amount=sum(item.supply_amount for item in items),
            tax_amount=sum(item.tax_amount for item in items),
            service_charge_amount=sum(item.service_charge_amount for item in items),
            total_amount=sum(item.total_amount for item in items),
            items=items,
        )

    async def card_sales(self, query: CardSalesQuery) -> CardSalesSummary:
        _tin, company, number = await self._business()
        data = await self._action(
            MOBILE_TAX_BASE,
            "UTBSFABG35F001",
            "ATESFAAA014R02",
            {
                "CrdcTrsBrkdMateAdmSVO": {
                    "bsno": number,
                    "stlYr": str(query.year),
                    "qrtFrom": str(query.quarter_from),
                    "qrtTo": str(query.quarter_to),
                    "dwldYn": "N",
                }
            },
        )
        candidates = (
            data.get("crdcTrsBrkdMateAdmDVOList"),
            data.get("sleVcexSlsMateInqrDVOList"),
            data.get("crdcZrpSleStlVcexMateAdmDVOList"),
        )
        rows = next((value for value in candidates if isinstance(value, list) and value), [])
        if any(value is not None and not isinstance(value, list) for value in candidates):
            raise changed()
        items = []
        for row in rows:
            if not isinstance(row, dict):
                raise changed()
            items.append(
                CardSalesMonth(
                    month=normalized_month(row.get("stlYm")),
                    data_type=optional_text(row.get("mateKndNm")),
                    count=integer(row.get("stlScnt"), default=0),
                    total_sales_amount=integer(row.get("totaStlAmt"), default=0),
                    credit_card_amount=integer(row.get("etcSls"), default=0),
                    purchase_card_amount=integer(row.get("purcEuCardSls"), default=0),
                    service_charge_amount=integer(row.get("tip"), default=0),
                )
            )
        return CardSalesSummary(
            company_name=company,
            query=query,
            count=sum(item.count for item in items),
            total_sales_amount=sum(item.total_sales_amount for item in items),
            credit_card_amount=sum(item.credit_card_amount for item in items),
            purchase_card_amount=sum(item.purchase_card_amount for item in items),
            service_charge_amount=sum(item.service_charge_amount for item in items),
            items=items,
        )

    async def business_accounts(self, query: BusinessAccountQuery) -> BusinessAccountPage:
        tin, company, _number = await self._business()
        data = await self._action(
            MOBILE_TAX_BASE,
            "UTBCMCDA02F001",
            "ATTCMCDA001R02",
            {
                "AccAdmSVO": {
                    "txaaYn": "N",
                    "tin": tin,
                    "txprAccClCd": "Z14",
                    "pfbIcldYn": "Y",
                    "ntplInfpYn": "N",
                },
                "pageInfoVO": {"pageNum": query.page, "pageSize": query.page_size},
            },
        )
        rows, page = data.get("accAdmDVOList"), data.get("pageInfoVO")
        if not isinstance(rows, list) or not isinstance(page, dict):
            raise changed()

        def optional_day(value) -> date | None:
            return None if value in (None, "") else normalized_day(value)

        items = []
        for row in rows:
            if not isinstance(row, dict):
                raise changed()
            items.append(
                BusinessAccount(
                    business_number=optional_text(
                        row.get("txprDscmNoEncCntn") or row.get("accTxprDscmNoEncCntn")
                    ),
                    account_type=optional_text(row.get("txprAccClCdNm")),
                    bank_name=first_text(row, "bankNm", "bankCdNm"),
                    account_number=masked_number(row.get("accnoEncCntn") or row.get("accno")),
                    registered_date=optional_day(row.get("accRgtDt")),
                    use_start_date=optional_day(row.get("accUseStrtDt")),
                    use_end_date=optional_day(row.get("accUseEndDt")),
                    status=first_text(row, "accStatClCdNm", "sncStatCdNm"),
                )
            )
        total_count = integer(page.get("totalCount"), default=len(items))
        page_number = integer(page.get("pageNum"), default=query.page)
        page_size = integer(page.get("pageSize"), default=query.page_size)
        return BusinessAccountPage(
            company_name=company,
            query=query,
            page=page_number,
            page_size=page_size,
            total_count=total_count,
            has_next=page_number * page_size < total_count,
            items=items,
        )
