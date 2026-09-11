from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from hometax_login.issuance_models import (
    InvoiceCancelRequest,
    InvoiceCorrectionRequest,
    InvoiceIssueRequest,
)

REFERENCE = UUID("11111111-1111-4111-8111-111111111111")


def line(**overrides):
    data = {
        "supply_date": date(2025, 1, 10),
        "name": "테스트 품목",
        "specification": "규격",
        "quantity": Decimal("1"),
        "unit_price": Decimal("1000"),
        "supply_amount": 1000,
        "tax_amount": 100,
        "remarks": "",
    }
    data.update(overrides)
    return data


def issue_request(**overrides):
    data = {
        "client_reference": REFERENCE,
        "recipient_business_number": "1234567890",
        "written_date": date(2025, 1, 10),
        "purpose": "claim",
        "items": [line()],
        "remarks": "",
    }
    data.update(overrides)
    return data


def test_issue_request_accepts_minimal_valid_payload():
    request = InvoiceIssueRequest(**issue_request())

    assert request.client_reference == REFERENCE
    assert request.items[0].supply_amount == 1000


@pytest.mark.parametrize(
    "payload",
    [
        issue_request(written_date=date.today() + timedelta(days=1)),
        issue_request(items=[line(supply_date=date(2025, 2, 1))]),
        issue_request(items=[line(supply_date=date(2025, 1, 11))]),
        issue_request(items=[line(name="가" * 101)]),
        issue_request(items=[line(name="bad\x00name")]),
        issue_request(items=[line(quantity=Decimal("Infinity"))]),
        issue_request(items=[line(unit_price=Decimal("NaN"))]),
        issue_request(items=[line(supply_amount="1000")]),
        issue_request(items=[line(tax_amount="100")]),
        issue_request(items=[line(supply_amount=-1)]),
        issue_request(items=[line(supply_amount=0, tax_amount=100)]),
        issue_request(items=[line(supply_amount=0, tax_amount=0)]),
        issue_request(client_reference="not-a-uuid"),
        issue_request(recipient_business_number="123"),
    ],
)
def test_issue_request_validations_reject_invalid_dates_text_numbers_and_ids(payload):
    with pytest.raises(ValidationError):
        InvoiceIssueRequest(**payload)


@pytest.mark.parametrize(
    "request_cls,payload",
    [
        (InvoiceCorrectionRequest, {**issue_request(), "reason": "contract_cancellation"}),
        (
            InvoiceCancelRequest,
            {
                "client_reference": REFERENCE,
                "reason": "clerical_error",
                "written_date": date(2025, 1, 10),
            },
        ),
    ],
)
def test_unsupported_correction_or_cancel_reasons_are_rejected(request_cls, payload):
    with pytest.raises(ValidationError):
        request_cls(**payload)


def test_cancel_request_rejects_future_date_and_control_text():
    with pytest.raises(ValidationError):
        InvoiceCancelRequest(
            client_reference=REFERENCE,
            reason="duplicate_issue",
            written_date=date.today() + timedelta(days=1),
        )

    with pytest.raises(ValidationError):
        InvoiceCancelRequest(
            client_reference=REFERENCE,
            reason="duplicate_issue",
            written_date=date(2025, 1, 10),
            remarks="bad\x7fremarks",
        )
