"""Local validation for a single ordinary business-to-business invoice draft."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .counterparty_changes import validate_text


class InvoiceIssueLine(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    supply_date: date
    name: str = Field(min_length=1)
    specification: str = ""
    quantity: Decimal | None = Field(
        default=None, ge=0, le=999_999_999_999, max_digits=18, decimal_places=6, allow_inf_nan=False
    )
    unit_price: Decimal | None = Field(
        default=None, ge=0, le=999_999_999_999, max_digits=18, decimal_places=6, allow_inf_nan=False
    )
    supply_amount: int = Field(strict=True, ge=0, le=999_999_999_999)
    tax_amount: int = Field(strict=True, ge=0, le=999_999_999_999)
    remarks: str = ""

    @model_validator(mode="after")
    def text_and_amounts(self):
        validate_text(self.model_dump(), {"name": 100, "specification": 60, "remarks": 100})
        if self.supply_amount == 0 and self.tax_amount:
            raise ValueError("A zero supply amount cannot carry a positive tax amount")
        return self


class InvoiceIssueRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    client_reference: UUID
    recipient_business_number: str = Field(pattern=r"^[0-9]{10}$")
    written_date: date
    purpose: Literal["claim", "receipt"] = "claim"
    items: list[InvoiceIssueLine] = Field(min_length=1, max_length=4)
    remarks: str = ""

    @model_validator(mode="after")
    def dates_and_totals(self):
        if self.written_date > datetime.now(ZoneInfo("Asia/Seoul")).date():
            raise ValueError("Future written dates are not supported")
        if any(
            (line.supply_date.year, line.supply_date.month)
            != (
                self.written_date.year,
                self.written_date.month,
            )
            or line.supply_date > self.written_date
            for line in self.items
        ):
            raise ValueError(
                "Supply dates must be in the written month and not after the written date"
            )
        validate_text(self.model_dump(), {"remarks": 300})
        if sum(line.supply_amount for line in self.items) <= 0:
            raise ValueError("Ordinary invoices require a positive supply total")
        return self


class InvoiceCorrectionRequest(InvoiceIssueRequest):
    reason: Literal["clerical_error"]


class InvoiceCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    client_reference: UUID
    reason: Literal["contract_cancellation", "duplicate_issue"]
    written_date: date
    remarks: str = ""

    @model_validator(mode="after")
    def valid_date(self):
        if self.written_date > datetime.now(ZoneInfo("Asia/Seoul")).date():
            raise ValueError("Future written dates are not supported")
        validate_text(self.model_dump(), {"remarks": 300})
        return self


class InvoiceParty(BaseModel):
    business_number: str = Field(pattern=r"^[0-9]{10}$")
    name: str
    representative_name: str = ""
    address: str = ""
    business_type: str = ""
    business_item: str = ""
    email: str = ""
    secondary_email: str = ""


class InvoiceDocumentLine(BaseModel):
    supply_date: date
    name: str
    specification: str = ""
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    supply_amount: int
    tax_amount: int
    remarks: str = ""


class InvoiceDocumentPreview(BaseModel):
    supplier: InvoiceParty
    recipient: InvoiceParty
    written_date: date
    purpose: Literal["claim", "receipt"]
    items: list[InvoiceDocumentLine]
    remarks: str = ""
    supply_amount: int
    tax_amount: int
    total_amount: int
    original_approval_number: str | None = None
    correction_reason_code: Literal["01", "04", "06"] | None = None


class InvoiceOperationPreview(BaseModel):
    operation_id: str
    client_reference: UUID
    operation: Literal["issue", "correct", "cancel"]
    expires_at: datetime
    documents: list[InvoiceDocumentPreview]
    content_digest: str
    requires_confirmation: Literal[True] = True
    email_delivery: Literal["not_requested"] = "not_requested"


class InvoiceOperationResult(BaseModel):
    operation_id: str
    client_reference: UUID
    operation: Literal["issue", "correct", "cancel"]
    status: Literal["issued", "already_issued"]
    approval_numbers: list[str]
    email_delivery: Literal["not_requested"] = "not_requested"


@dataclass(repr=False)
class PreparedInvoiceOperation:
    operation: Literal["issue", "correct", "cancel"]
    client_reference: UUID
    scope: tuple[str, str]
    documents: list[InvoiceDocumentPreview]
    payload: dict
    request: InvoiceIssueRequest | InvoiceCorrectionRequest | InvoiceCancelRequest
    original_approval_number: str | None = None

    def comparable(self) -> dict:
        return {
            "operation": self.operation,
            "scope": self.scope,
            "documents": [d.model_dump(mode="json") for d in self.documents],
            "payload": self.payload,
            "original_approval_number": self.original_approval_number,
        }
