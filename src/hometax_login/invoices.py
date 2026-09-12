"""Read-only electronic tax invoices; upstream URLs/actions are never caller-controlled."""

from __future__ import annotations

import asyncio
import calendar
import re
from collections import OrderedDict
from datetime import date, datetime
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .errors import LoginError

if TYPE_CHECKING:
    from .protocol import HometaxClient

ET_BASE = "https://teet.hometax.go.kr"
LIST_SCREEN = "UTEETBDA01"
LIST_ACTION = "ATEETBDA001R01"
MAX_SUMMARY_ROWS = 5000
MAX_DETAIL_REFERENCES = 500


class InvoiceFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_date: date
    end_date: date
    direction: Literal["sales", "purchases"] = "sales"
    date_basis: Literal["issued", "written", "transmitted"] = "issued"

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


class InvoiceQuery(InvoiceFilters):
    page: int = Field(default=1, ge=1, le=10000)
    page_size: int = Field(default=50, ge=10, le=50)

    @field_validator("page_size")
    @classmethod
    def supported_page_size(cls, value):
        if value not in {10, 20, 30, 50}:
            raise ValueError("page_size must be 10, 20, 30, or 50")
        return value


class TaxInvoice(BaseModel):
    approval_number: str
    written_date: date
    issued_date: date
    transmitted_date: date | None
    counterparty_name: str | None
    item_name: str | None
    supply_amount: int
    tax_amount: int
    total_amount: int


class TaxInvoicePage(BaseModel):
    company_name: str
    filters: InvoiceFilters
    page: int
    page_size: int
    total_count: int
    has_next: bool
    items: list[TaxInvoice]


class TaxInvoiceSummary(BaseModel):
    company_name: str
    filters: InvoiceFilters
    total_count: int
    supply_amount: int
    tax_amount: int
    total_amount: int


class CounterpartyQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(default="", max_length=100)
    business_number: str = Field(default="", pattern=r"^(?:[0-9]{10})?$")
    representative_name: str = Field(default="", max_length=100)
    page: int = Field(default=1, ge=1, le=10000)
    page_size: int = Field(default=50, ge=10, le=50)

    @field_validator("page_size")
    @classmethod
    def supported_page_size(cls, value):
        return InvoiceQuery.supported_page_size(value)


class Counterparty(BaseModel):
    name: str
    business_number: str | None = None
    representative_name: str | None = None
    address: str | None = None
    business_type: str | None = None
    business_item: str | None = None
    branch_number: str | None = None
    registered_at: str | None = None
    is_primary: bool | None = None


class CounterpartyPage(BaseModel):
    company_name: str
    query: CounterpartyQuery
    total_count: int
    has_next: bool
    items: list[Counterparty]


class TaxInvoiceLine(BaseModel):
    name: str
    supplied_date: str | None = None
    specification: str | None = None
    quantity: str | None = None
    unit_price: str | None = None
    supply_amount: int
    tax_amount: int
    remarks: str | None = None


class TaxInvoiceDetail(BaseModel):
    invoice: TaxInvoice
    # Additional detail fields are explicitly mapped; no upstream raw object is returned.
    supplier_name: str
    customer_name: str
    supplier_business_number: str | None = None
    customer_business_number: str | None = None
    items: list[TaxInvoiceLine]


def changed():
    return LoginError("INVOICE_RESPONSE_CHANGED", "홈택스 조회 응답을 검증하지 못했습니다.")


def integer(value) -> int:
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch(r"-?(?:\d+|\d{1,3}(?:,\d{3})+)", value):
        return int(value.replace(",", ""))
    raise changed()


def upstream_date(value, *, optional=False) -> date | None:
    if optional and value in (None, ""):
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-?\d{2}-?\d{2}", value):
        raise changed()
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise changed() from None


def approval_number(value) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"(?:[0-9]{8}[A-Za-z0-9]{16}|[0-9]{8}-[A-Za-z0-9]{8}-[A-Za-z0-9]{8})", value
    ):
        raise changed()
    return value.replace("-", "")


def invoice_from_row(row: dict) -> TaxInvoice:
    approval = approval_number(row.get("etan"))
    name = row.get("tnmNm")
    if not isinstance(name, str):
        raise changed()
    # 매입 목록은 일부 행의 상호를 공백으로 보내고 대체 상호 필드도 비워 둔다.
    # 그 행 하나 때문에 페이지 전체를 버리지 않고, 상호만 비운 채로 통과시킨다.
    name = name.strip() or None
    supply, tax, total = (integer(row.get(k)) for k in ("sumSplCft", "sumTxamt", "totaAmt"))
    if supply + tax != total:
        raise changed()
    return TaxInvoice(
        approval_number=approval,
        written_date=upstream_date(row.get("wrtDt")),
        issued_date=upstream_date(row.get("isnDtm")),
        transmitted_date=upstream_date(row.get("tmsnDt"), optional=True),
        counterparty_name=name,
        item_name=row.get("lsatNm") if isinstance(row.get("lsatNm"), str) else None,
        supply_amount=supply,
        tax_amount=tax,
        total_amount=total,
    )


class TaxInvoiceClient:
    def __init__(self, client: HometaxClient):
        self.client = client
        self.business_tin: str | None = None
        self.business_mpb_no = ""
        self.business_profile: dict = {}
        self.references: OrderedDict[str, tuple[str, TaxInvoice]] = OrderedDict()

    async def _business(self) -> tuple[str, str]:
        identity = await self.client.verify()
        token = self.client._json(await self.client._request("POST", "/token.do"))
        if not token:
            raise LoginError("SESSION_NOT_AUTHENTICATED", "홈택스 인증 세션이 없습니다.", 401)
        data = self.client._json(
            await self.client._request(
                "POST",
                ET_BASE + "/permission.do",
                params={"screenId": LIST_SCREEN, "domain": "hometax.go.kr"},
                json={**token, "popupYn": False},
            )
        )
        result = data.get("resultMsg")
        if not isinstance(result, dict) or result.get("errorMsg") or result.get("errorCd"):
            raise LoginError("INVOICE_ACCESS_DENIED", "세금계산서 조회 권한을 확인하세요.", 403)
        session = result.get("sessionMap")
        if not isinstance(session, dict):
            raise LoginError("SESSION_NOT_AUTHENTICATED", "전자세금계산서 인증이 필요합니다.", 401)
        user_id = session.get("userId") or session.get("pubcUserNo")
        if user_id != identity["user_id"]:
            raise LoginError("SESSION_IDENTITY_CHANGED", "로그인 사용자 정보가 변경됐습니다.", 401)
        tin = session.get("tin")
        if session.get("txfrBmanLgnYn") != "Y" and (
            session.get("haboInqrStat") == "N" or tin != session.get("cnvrTin")
        ):
            tin = session.get("cnvrTin")
        if (
            not isinstance(tin, str)
            or not tin
            or (session.get("userClsfCd") == "01" and tin == session.get("tin"))
        ):
            raise LoginError("BUSINESS_REQUIRED", "조회할 사업자로 로그인하거나 전환하세요.", 409)
        mpb = session.get("mpbNo")
        branch = str(mpb) if mpb is not None and str(mpb).isdigit() and int(mpb) else ""
        if tin != self.business_tin or branch != self.business_mpb_no:
            self.references.clear()
            self.business_tin = tin
        self.business_mpb_no = branch
        self.business_profile = {
            key: session.get(key)
            for key in (
                "txprDscmNo",
                "tnmNm",
                "rprsFnm",
                "adr",
                "bcNm",
                "itmNm",
                "pubcUserNo",
                "crtfUqno",
                "etxivPkcYn",
                "lgnCertCd",
                "emlAdr",
                "email",
            )
        }
        name = session.get("tnmNm")
        return tin, name if isinstance(name, str) else ""

    async def _page(self, query: InvoiceQuery, tin: str, company: str) -> TaxInvoicePage:
        filters = InvoiceFilters(**query.model_dump(exclude={"page", "page_size"}))
        vo = {
            "splrTin": tin if query.direction == "sales" else "",
            "dmnrTin": tin if query.direction == "purchases" else "",
            "prhSlsClCd": "01" if query.direction == "sales" else "02",
            "dtCl": {"written": "01", "issued": "02", "transmitted": "03"}[query.date_basis],
            "etxivClCd": "01",
            "bmanCd": "00",
            "pageSize": str(query.page_size),
            "inqrDtStrt": query.start_date.strftime("%Y%m%d"),
            "inqrDtEnd": query.end_date.strftime("%Y%m%d"),
            # WebSquare config allValue is "all", NOT an empty string (false-zero regression).
            "etxivClsfCd": "all",
            "etxivKndCd": "all",
            "isnTypeCd": "all",
            "splrMpbNo": self.business_mpb_no if query.direction == "sales" else "",
            "dmnrMpbNo": self.business_mpb_no if query.direction == "purchases" else "",
            "splrTxprDscmNo": "",
            "dmnrTxprDscmNo": "",
        }
        page_info = {"pageNum": query.page, "pageSize": query.page_size, "totalCount": 0}
        data = self.client._json(
            await self.client._request(
                "POST",
                ET_BASE + "/wqAction.do",
                params={
                    "actionId": LIST_ACTION,
                    "screenId": LIST_SCREEN,
                    "popupYn": "false",
                    "realScreenId": "",
                },
                json={
                    "resnoSecYn": "Y",
                    "icldLsatInfr": "N",
                    "fleDwldYn": "N",
                    "srtClCd": "2",
                    "srtOpt": "01",
                    "pageInfoVO": page_info,
                    "excelPageInfoVO": page_info,
                    "etxivIsnBrkdTermDVOPrmt": vo,
                },
            )
        )
        result, echo, page = (
            data.get(k)
            for k in (
                "resultMsg",
                "etxivIsnBrkdTermDVOPrmt",
                "pageInfoVO",
            )
        )
        rows = data.get("etxivIsnBrkdTermDVOList")
        if (
            not isinstance(result, dict)
            or result.get("result") != "S"
            or (result.get("errorMsg") or result.get("errorCd"))
        ):
            raise LoginError("INVOICE_QUERY_FAILED", "홈택스 세금계산서 조회에 실패했습니다.")
        if not isinstance(echo, dict) or any(
            echo.get(k) != vo[k]
            for k in (
                "splrTin",
                "dmnrTin",
                "prhSlsClCd",
                "dtCl",
                "inqrDtStrt",
                "inqrDtEnd",
                "etxivClsfCd",
                "etxivKndCd",
                "isnTypeCd",
            )
        ):
            raise changed()
        if not isinstance(page, dict) or not isinstance(rows, list):
            raise changed()
        total = integer(page.get("totalCount"))
        if (
            total < 0
            or integer(page.get("pageNum")) != query.page
            or (integer(page.get("pageSize")) != query.page_size)
        ):
            raise changed()
        expected = min(query.page_size, max(0, total - (query.page - 1) * query.page_size))
        if len(rows) != expected or any(not isinstance(row, dict) for row in rows):
            raise changed()
        items = [invoice_from_row(row) for row in rows]
        if len({item.approval_number for item in items}) != len(items):
            raise changed()
        for item in items:
            selected_date = getattr(item, query.date_basis + "_date")
            if selected_date is None or not query.start_date <= selected_date <= query.end_date:
                raise changed()
        for item in items:
            self.references[item.approval_number] = (query.direction, item)
            self.references.move_to_end(item.approval_number)
            if len(self.references) > MAX_DETAIL_REFERENCES:
                self.references.popitem(last=False)
        return TaxInvoicePage(
            company_name=company,
            filters=filters,
            page=query.page,
            page_size=query.page_size,
            total_count=total,
            has_next=query.page * query.page_size < total,
            items=items,
        )

    async def list(self, query: InvoiceQuery) -> TaxInvoicePage:
        tin, company = await self._business()
        return await self._page(query, tin, company)

    async def summary(self, filters: InvoiceFilters) -> TaxInvoiceSummary:
        tin, company = await self._business()
        query = InvoiceQuery(**filters.model_dump(), page_size=50)
        page = await self._page(query, tin, company)
        total = page.total_count
        if total > MAX_SUMMARY_ROWS:
            raise LoginError(
                "INVOICE_SUMMARY_TOO_LARGE", "5,000건 이하로 조회 기간을 줄이세요.", 422
            )
        items = list(page.items)
        seen = {item.approval_number for item in items}
        while page.has_next:
            await asyncio.sleep(0.2)
            query = query.model_copy(update={"page": query.page + 1})
            page = await self._page(query, tin, company)
            ids = {item.approval_number for item in page.items}
            if page.total_count != total or seen.intersection(ids):
                raise LoginError(
                    "INVOICE_LIST_CHANGED", "조회 중 목록이 변경됐습니다. 다시 조회하세요.", 409
                )
            seen.update(ids)
            items.extend(page.items)
        if len(items) != total:
            raise changed()
        return TaxInvoiceSummary(
            company_name=company,
            filters=filters,
            total_count=total,
            supply_amount=sum(item.supply_amount for item in items),
            tax_amount=sum(item.tax_amount for item in items),
            total_amount=sum(item.total_amount for item in items),
        )

    async def detail(self, number: str) -> TaxInvoiceDetail:
        if not re.fullmatch(r"[0-9]{8}[A-Za-z0-9]{16}", number):
            raise LoginError("INVALID_APPROVAL_NUMBER", "승인번호 24자리 형식을 확인하세요.", 422)
        if number not in self.references:
            raise LoginError("INVOICE_NOT_LISTED", "같은 세션에서 목록을 먼저 조회하세요.", 404)
        tin, _company = await self._business()
        reference = self.references.get(number)
        if reference is None:
            raise LoginError(
                "INVOICE_NOT_LISTED", "사업자가 변경됐습니다. 목록을 다시 조회하세요.", 404
            )
        direction, invoice = reference
        data = self.client._json(
            await self.client._request(
                "POST",
                ET_BASE + "/wqAction.do",
                params={
                    "actionId": "ATEETBDA001R02",
                    "screenId": "UTEETBDA38",
                    "popupYn": "true",
                    "realScreenId": "",
                },
                json={
                    "etxivIsnBrkdTermDVOPrmt": {
                        "etan": number,
                        "screenId": LIST_SCREEN,
                        "pageNum": "1",
                        "slsPrhClCd": "01" if direction == "sales" else "02",
                        "etxivTin": tin,
                        "etxivClCd": "",
                        "etxivClsfCd": "",
                        "etxivMpbNo": self.business_mpb_no,
                    }
                },
            )
        )
        result, body, lines = (
            data.get(k)
            for k in (
                "resultMsg",
                "etxivIsnBrkdTermDVO",
                "lsatInfrBizSVOList",
            )
        )
        if (
            not isinstance(result, dict)
            or result.get("result") != "S"
            or (result.get("errorMsg") or result.get("errorCd"))
        ):
            raise LoginError("INVOICE_QUERY_FAILED", "홈택스 세금계산서 상세 조회에 실패했습니다.")
        if not isinstance(body, dict) or not isinstance(lines, list):
            raise changed()
        if approval_number(body.get("etan")) != number or any(
            integer(body.get(key)) != getattr(invoice, attr)
            for key, attr in (
                ("sumSplCft", "supply_amount"),
                ("sumTxamt", "tax_amount"),
                ("totaAmt", "total_amount"),
            )
        ):
            raise changed()
        supplier, customer = body.get("splrTnmNm"), body.get("dmnrTnmNm")
        if not all(isinstance(name, str) and name.strip() for name in (supplier, customer)):
            raise changed()
        items = []
        for line in lines:
            if not isinstance(line, dict) or not isinstance(line.get("lsatNm"), str):
                raise changed()
            if any(
                line.get(key) is not None and type(line[key]) not in (str, int, float)
                for key in ("lsatSplDt", "lsatRszeNm", "lsatQty", "lsatUtprc", "lsatRmrkCntn")
            ):
                raise changed()
            items.append(
                TaxInvoiceLine(
                    name=line["lsatNm"],
                    supply_amount=integer(line.get("lsatSplCft")),
                    tax_amount=integer(line.get("lsatTxamt")),
                    **{
                        out: str(line[key]) if line.get(key) is not None else None
                        for key, out in (
                            ("lsatSplDt", "supplied_date"),
                            ("lsatRszeNm", "specification"),
                            ("lsatQty", "quantity"),
                            ("lsatUtprc", "unit_price"),
                            ("lsatRmrkCntn", "remarks"),
                        )
                    },
                )
            )
        if sum(item.supply_amount for item in items) != invoice.supply_amount or (
            sum(item.tax_amount for item in items) != invoice.tax_amount
        ):
            raise changed()

        def business_number(value):
            if isinstance(value, str) and re.fullmatch(
                r"(?:[0-9]{10}|[0-9]{3}-[0-9]{2}-[0-9]{5})", value
            ):
                return value.replace("-", "")
            return None  # Do not expose resident IDs or masked identifiers.

        return TaxInvoiceDetail(
            invoice=invoice,
            supplier_name=supplier,
            customer_name=customer,
            supplier_business_number=business_number(body.get("splrTxprDscmNo")),
            customer_business_number=business_number(body.get("dmnrTxprDscmNo")),
            items=items,
        )

    async def _counterparty_rows(self, query: CounterpartyQuery) -> tuple[str, list[dict], int]:
        tin, company = await self._business()
        page_info = {"pageNum": query.page, "pageSize": query.page_size, "totalCount": 0}
        data = self.client._json(
            await self.client._request(
                "POST",
                ET_BASE + "/wqAction.do",
                params={"actionId": "ATEETBAE001R06", "screenId": "UTEETBAB02", "popupYn": "false"},
                json={
                    "splrTin": tin,
                    "splrMpbNo": self.business_mpb_no,
                    "txprDscmNoEncCntn": query.business_number,
                    "txprNm": query.name,
                    "rprsFnm": query.representative_name,
                    "srtClCd": "1",
                    "srtOpt": "01",
                    "fleDwldNbInqrYn": "Y",
                    "fleDwldYn": "N",
                    "fleTp": "",
                    "resnoSecYn": "Y",
                    "pageInfoVO": page_info,
                    "excelPageInfoVO": page_info,
                },
            )
        )
        result, rows, page = (data.get(k) for k in ("resultMsg", "myClplcListDVO", "pageInfoVO"))
        if (
            not isinstance(result, dict)
            or result.get("result") != "S"
            or (result.get("errorMsg") or result.get("errorCd"))
        ):
            raise LoginError("COUNTERPARTY_QUERY_FAILED", "홈택스 등록 거래처 조회에 실패했습니다.")
        if not isinstance(rows, list) or not isinstance(page, dict):
            raise changed()
        total = integer(page.get("totalCount"))
        if (
            total < 0
            or integer(page.get("pageNum")) != query.page
            or (integer(page.get("pageSize")) != query.page_size)
            or len(rows) != min(query.page_size, max(0, total - (query.page - 1) * query.page_size))
        ):
            raise changed()
        return company, rows, total

    async def counterparties(self, query: CounterpartyQuery) -> CounterpartyPage:
        company, rows, total = await self._counterparty_rows(query)
        items = []
        for row in rows:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("tnmNm"), str)
                or not row["tnmNm"].strip()
            ):
                raise changed()
            number = row.get("txprDscmNoEncCntnView") or row.get("txprDscmNoEncCntn")
            # Only business IDs; never include resident IDs, ciphertext, TINs or raw rows.
            if isinstance(number, str) and re.fullmatch(
                r"(?:[0-9]{10}|[0-9]{3}-[0-9]{2}-[0-9]{5})", number
            ):
                number = number.replace("-", "")
            else:
                number = None
            items.append(
                Counterparty(
                    name=row["tnmNm"],
                    business_number=number,
                    is_primary={"Y": True, "N": False}.get(row.get("prcpClplcYn")),
                    **{
                        out: row[key] if isinstance(row.get(key), str) else None
                        for key, out in (
                            ("rprsFnm", "representative_name"),
                            ("pfbAdr", "address"),
                            ("bcNm", "business_type"),
                            ("itmNm", "business_item"),
                            ("dmnrMpbNo", "branch_number"),
                            ("frsRgtDtm", "registered_at"),
                        )
                    },
                )
            )
        return CounterpartyPage(
            company_name=company,
            query=query,
            total_count=total,
            has_next=query.page * query.page_size < total,
            items=items,
        )
