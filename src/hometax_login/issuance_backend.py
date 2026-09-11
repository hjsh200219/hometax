"""Fail-closed HomeTax invoice issuance adapter.

This module only prepares and dispatches the public WebSquare action contract that
was observed from HomeTax screen XML.  It deliberately does not discover
credentials, retry write actions, or send the optional mail action.
"""

from __future__ import annotations

import base64
import copy
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal

from cryptography.hazmat.primitives.serialization import Encoding
from lxml import etree
from pydantic import ValidationError

from .counterparty_changes import CounterpartySnapshot, fingerprint
from .errors import LoginError
from .invoice_signing import InvoiceSigningError, sign_invoice_xml
from .invoices import ET_BASE, LIST_SCREEN, approval_number, changed, integer
from .issuance_models import (
    InvoiceCancelRequest,
    InvoiceCorrectionRequest,
    InvoiceDocumentLine,
    InvoiceDocumentPreview,
    InvoiceIssueLine,
    InvoiceIssueRequest,
    InvoiceOperationPreview,
    InvoiceParty,
    PreparedInvoiceOperation,
)

TAX_NS = "urn:kr:or:kec:standard:Tax:ReusableAggregateBusinessInformationEntitySchemaModule:1:0"
ISSUE_SCREEN = "UTEETBAA01"
DETAIL_SCREEN = "UTEETBDA38"
ISSUE_XML_ACTION = "ATEETBAA002C04"
ISSUE_COMMIT_ACTION = "ATEETBAA002C05"
CORRECTION_XML_ACTION = "ATEETBAA003C03"
CORRECTION_COMMIT_ACTION = "ATEETBAA003C04"

CORRECTION_REASON_CODES = {
    "clerical_error": "01",
    "contract_cancellation": "04",
    "duplicate_issue": "06",
}
CORRECTION_SCREEN_CODES = {
    "clerical_error": "44",
    "contract_cancellation": "48",
    "duplicate_issue": "42",
}
PURPOSE_CODES = {"receipt": "01", "claim": "02"}
ALLOWED_WIRE_ENCODINGS = {"raw", "base64"}


class InvoiceBackend:
    def __init__(self, client):
        self.client = client
        self.invoices = client.invoices

    async def preview_issue(self, request: InvoiceIssueRequest) -> PreparedInvoiceOperation:
        owner_tin, company = await self.invoices._business()
        if self._owner_branch():
            raise LoginError(
                "INVOICE_BRANCH_UNSUPPORTED",
                "본점 사업자 단건 발행만 자동 처리할 수 있습니다.",
                409,
            )
        supplier = self._supplier_party()
        recipient_snapshot = await self._registered_counterparty(request.recipient_business_number)
        recipient = self._recipient_party(recipient_snapshot)
        document = self._document(request, supplier, recipient)
        payload = self._issue_payload(document, company, recipient_snapshot)
        await self._check_issue_eligibility(payload["xml_payload"])
        return PreparedInvoiceOperation(
            operation="issue",
            client_reference=request.client_reference,
            scope=(owner_tin, ""),
            documents=[document],
            payload=payload,
            request=request,
        )

    async def _check_issue_eligibility(self, payload: dict) -> None:
        data = await self._action(
            "ATEETBAA002R04",
            ISSUE_SCREEN,
            {
                "splrInfrBizSVO": payload["splrInfrBizSVO"],
                "sncInfrBizSVO": payload["sncInfrBizSVO"],
            },
        )
        _ok(data, "INVOICE_ELIGIBILITY_FAILED", "작성일 기준 발급 가능 여부를 확인하지 못했습니다.")
        result = data.get("request", data)
        if not isinstance(result, dict) or "sptxpTxivIsnClCd" not in result:
            raise changed()
        if result.get("crpCntcOficYn") == "Y" or result.get("isnImpTxfrBmanYn") == "Y":
            raise LoginError(
                "INVOICE_INELIGIBLE", "해당 사업자의 일반 세금계산서 발급이 제한됩니다.", 403
            )
        category = result.get("sptxpTxivIsnClCd")
        if category in {"ZZ", "01"}:
            return
        conversion = result.get("typeCnvrTrtDt")
        if conversion and _date8(payload["sncInfrBizSVO"]["wrtDt"]) < _date8(conversion):
            return
        raise LoginError(
            "INVOICE_INELIGIBLE", "작성일 기준 발급 가능한 일반 과세 유형이 아닙니다.", 403
        )

    async def preview_correct(
        self, original: str, request: InvoiceCorrectionRequest
    ) -> PreparedInvoiceOperation:
        original_number = approval_number(original)
        owner_tin, _company = await self.invoices._business()
        if self._owner_branch():
            raise LoginError(
                "INVOICE_BRANCH_UNSUPPORTED",
                "본점 사업자 수정발행만 자동 처리할 수 있습니다.",
                409,
            )
        original_document, raw = await self._original_document(original_number)
        if request.written_date != original_document.written_date:
            raise LoginError(
                "INVOICE_CORRECTION_DATE_UNSUPPORTED",
                "기재사항 착오정정은 원본 작성일자 기준만 지원합니다.",
                422,
            )
        recipient_snapshot = await self._registered_counterparty(request.recipient_business_number)
        replacement = self._document(
            request,
            self._supplier_party(),
            self._recipient_party(recipient_snapshot),
            original_approval_number=original_number,
            correction_reason_code="01",
        )
        negative = self._negative_document(
            original_document,
            original_approval_number=original_number,
            correction_reason_code="01",
        )
        payload = self._correction_payload(
            [negative, replacement],
            raw,
            original_number,
            reason="clerical_error",
            recipient_snapshot=recipient_snapshot,
        )
        return PreparedInvoiceOperation(
            operation="correct",
            client_reference=request.client_reference,
            scope=(owner_tin, ""),
            documents=[negative, replacement],
            payload=payload,
            request=request,
            original_approval_number=original_number,
        )

    async def preview_cancel(
        self, original: str, request: InvoiceCancelRequest
    ) -> PreparedInvoiceOperation:
        original_number = approval_number(original)
        owner_tin, _company = await self.invoices._business()
        if self._owner_branch():
            raise LoginError(
                "INVOICE_BRANCH_UNSUPPORTED",
                "본점 사업자 취소발행만 자동 처리할 수 있습니다.",
                409,
            )
        original_document, raw = await self._original_document(original_number)
        if (
            request.reason == "contract_cancellation"
            and request.written_date < original_document.written_date
        ):
            raise LoginError(
                "INVOICE_CANCEL_DATE_INVALID",
                "계약해제 수정세금계산서 작성일은 원본 작성일보다 빠를 수 없습니다.",
                422,
            )
        written_date = (
            request.written_date
            if request.reason == "contract_cancellation"
            else original_document.written_date
        )
        if (
            request.reason == "duplicate_issue"
            and request.written_date != original_document.written_date
        ):
            raise LoginError(
                "INVOICE_CANCEL_DATE_UNSUPPORTED",
                "이중발급 취소는 원본 작성일자 기준만 지원합니다.",
                422,
            )
        reason_code = CORRECTION_REASON_CODES[request.reason]
        negative = self._negative_document(
            original_document.model_copy(
                update={
                    "written_date": written_date,
                    "items": [
                        line.model_copy(update={"supply_date": written_date})
                        for line in original_document.items
                    ]
                    if request.reason == "contract_cancellation"
                    else original_document.items,
                }
            ),
            original_approval_number=original_number,
            correction_reason_code=reason_code,
            remarks=request.remarks,
        )
        payload = self._correction_payload([negative], raw, original_number, reason=request.reason)
        return PreparedInvoiceOperation(
            operation="cancel",
            client_reference=request.client_reference,
            scope=(owner_tin, ""),
            documents=[negative],
            payload=payload,
            request=request,
            original_approval_number=original_number,
        )

    async def refresh(self, prepared: PreparedInvoiceOperation) -> PreparedInvoiceOperation:
        if prepared.operation == "issue":
            return await self.preview_issue(self._request_as(InvoiceIssueRequest, prepared.request))
        if prepared.operation == "correct":
            if prepared.original_approval_number is None:
                raise changed()
            return await self.preview_correct(
                prepared.original_approval_number,
                self._request_as(InvoiceCorrectionRequest, prepared.request),
            )
        if prepared.operation == "cancel":
            if prepared.original_approval_number is None:
                raise changed()
            return await self.preview_cancel(
                prepared.original_approval_number,
                self._request_as(InvoiceCancelRequest, prepared.request),
            )
        raise changed()

    async def issue(self, prepared: PreparedInvoiceOperation, material) -> list[str]:
        encoding = getattr(self.client, "invoice_wire_encoding", None)
        if encoding not in ALLOWED_WIRE_ENCODINGS:
            raise LoginError(
                "INVOICE_WIRE_ENCODING_REQUIRED",
                "홈택스 서명 XML 전송 형식(raw/base64)을 먼저 검증해야 합니다.",
                409,
            )
        current = await self.refresh(prepared)
        if current.comparable() != prepared.comparable():
            raise LoginError(
                "INVOICE_PREVIEW_STALE",
                "사업자·거래처·원본 계산서 정보가 변경됐습니다. 다시 미리보기 하세요.",
                409,
            )

        xml_payload = copy.deepcopy(prepared.payload["xml_payload"])
        xml_payload["request"].update(self._pre_xml_request(material))
        xml_response = await self._action(
            prepared.payload["xml_action"],
            prepared.payload["screen_id"],
            xml_payload,
        )
        _ok(xml_response, "INVOICE_XML_CREATE_FAILED", "홈택스 원본 XML 생성에 실패했습니다.")
        response = _response(xml_response)
        c04_documents = self._xml_response_documents(response, expected=len(prepared.documents))
        for c04_document, preview_document in zip(c04_documents, prepared.documents, strict=True):
            self._assert_xml_matches_documents(
                c04_document["xml"],
                [preview_document],
                expected_issue_id=c04_document["etan"],
            )
        signatures = [
            self._signed_document_request(c04_document, material, encoding)
            for c04_document in c04_documents
        ]

        commit_payload = self._commit_payload_from_xml_response(prepared, xml_response, signatures)
        commit_response = await self._action(
            prepared.payload["commit_action"],
            prepared.payload["screen_id"],
            commit_payload,
        )
        _ok(commit_response, "INVOICE_ISSUE_FAILED", "홈택스 전자세금계산서 발급에 실패했습니다.")
        approval_numbers = self._approval_numbers(commit_response, expected=len(prepared.documents))
        staged_numbers = [document["etan"] for document in c04_documents]
        if set(approval_numbers) != set(staged_numbers):
            raise LoginError(
                "INVOICE_APPROVAL_MISMATCH", "서명한 문서와 발급 승인번호가 다릅니다.", 409
            )
        approval_numbers = staged_numbers
        await self._verify_readback(prepared, approval_numbers)
        return approval_numbers

    def _owner_branch(self) -> str:
        branch = getattr(self.invoices, "business_mpb_no", "")
        return "" if branch in (None, "", "0", "0000") else str(branch)

    def _supplier_party(self) -> InvoiceParty:
        profile = getattr(self.invoices, "business_profile", {})
        if profile.get("etxivPkcYn") != "Y":
            raise LoginError(
                "SUPPLIER_CERTIFICATE_NOT_READY",
                "세금계산서 발급 가능한 인증서 세션을 확인하지 못했습니다.",
                409,
            )
        business_number = _business_number(profile.get("txprDscmNo"))
        if business_number is None:
            raise LoginError(
                "SUPPLIER_PROFILE_MISSING",
                "공급자 사업자 정보를 확인하지 못했습니다.",
            )
        try:
            return InvoiceParty(
                business_number=business_number,
                name=_text(profile.get("tnmNm")),
                representative_name=_text(profile.get("rprsFnm")),
                address=_text(profile.get("adr")),
                business_type=_text(profile.get("bcNm")),
                business_item=_text(profile.get("itmNm")),
                email=_text(profile.get("emlAdr") or profile.get("email")),
            )
        except ValidationError:
            raise changed() from None

    async def _registered_counterparty(self, number: str) -> CounterpartySnapshot:
        snapshot = await self.client.counterparty_changes.backend.current(number, "")
        if snapshot is None:
            raise LoginError(
                "COUNTERPARTY_NOT_FOUND",
                "등록된 거래처만 전자세금계산서를 자동 발행할 수 있습니다.",
                404,
            )
        if snapshot.owner_tin != self.invoices.business_tin or snapshot.owner_branch:
            raise LoginError(
                "INVOICE_PREVIEW_STALE",
                "사업자 정보가 변경됐습니다. 다시 미리보기 하세요.",
                409,
            )
        return snapshot

    @staticmethod
    def _recipient_party(snapshot: CounterpartySnapshot) -> InvoiceParty:
        data = snapshot.data
        primary = (
            data.get("primary_contact") if isinstance(data.get("primary_contact"), dict) else {}
        )
        secondary = (
            data.get("secondary_contact") if isinstance(data.get("secondary_contact"), dict) else {}
        )
        try:
            return InvoiceParty(
                business_number=snapshot.business_number,
                name=_text(data.get("name")),
                representative_name=_text(data.get("representative_name")),
                address=_text(data.get("address")),
                business_type=_text(data.get("business_type")),
                business_item=_text(data.get("business_item")),
                email=_text(primary.get("email")),
                secondary_email=_text(secondary.get("email")),
            )
        except ValidationError:
            raise changed() from None

    def _document(
        self,
        request: InvoiceIssueRequest | InvoiceCorrectionRequest,
        supplier: InvoiceParty,
        recipient: InvoiceParty,
        *,
        original_approval_number: str | None = None,
        correction_reason_code: Literal["01", "04", "06"] | None = None,
    ) -> InvoiceDocumentPreview:
        items = [_line_from_request(line) for line in request.items]
        supply = sum(line.supply_amount for line in items)
        tax = sum(line.tax_amount for line in items)
        return InvoiceDocumentPreview(
            supplier=supplier,
            recipient=recipient,
            written_date=request.written_date,
            purpose=request.purpose,
            items=items,
            remarks=request.remarks,
            supply_amount=supply,
            tax_amount=tax,
            total_amount=supply + tax,
            original_approval_number=original_approval_number,
            correction_reason_code=correction_reason_code,
        )

    @staticmethod
    def _negative_document(
        document: InvoiceDocumentPreview,
        *,
        original_approval_number: str,
        correction_reason_code: Literal["01", "04", "06"],
        remarks: str | None = None,
    ) -> InvoiceDocumentPreview:
        return document.model_copy(
            update={
                "items": [
                    line.model_copy(
                        update={
                            "unit_price": -abs(line.unit_price)
                            if line.unit_price is not None
                            else None,
                            "supply_amount": -abs(line.supply_amount),
                            "tax_amount": -abs(line.tax_amount),
                        }
                    )
                    for line in document.items
                ],
                "supply_amount": -abs(document.supply_amount),
                "tax_amount": -abs(document.tax_amount),
                "total_amount": -abs(document.total_amount),
                "remarks": document.remarks if remarks is None else remarks,
                "original_approval_number": original_approval_number,
                "correction_reason_code": correction_reason_code,
            }
        )

    async def _original_document(self, number: str) -> tuple[InvoiceDocumentPreview, dict]:
        reference = self.invoices.references.get(number)
        if reference is None:
            raise LoginError(
                "INVOICE_NOT_LISTED",
                "같은 세션에서 매출 세금계산서 목록을 먼저 조회하세요.",
                404,
            )
        direction, invoice = reference
        if direction != "sales":
            raise LoginError(
                "INVOICE_SALES_ONLY",
                "매출 세금계산서만 수정·취소 발행할 수 있습니다.",
                422,
            )
        tin = getattr(self.invoices, "business_tin", None)
        if not isinstance(tin, str) or not tin:
            raise changed()
        data = await self._detail_action(number, direction)
        _ok(data, "INVOICE_QUERY_FAILED", "홈택스 세금계산서 상세 조회에 실패했습니다.")
        body = data.get("etxivIsnBrkdTermDVO")
        rows = data.get("lsatInfrBizSVOList")
        payments = data.get("sncClInfrBizSVO")
        if (
            not isinstance(body, dict)
            or not isinstance(rows, list)
            or not isinstance(payments, dict)
        ):
            raise changed()
        if approval_number(body.get("etan")) != number:
            raise changed()
        document = self._document_from_detail(body, rows)
        if document.supplier.business_number != self._supplier_party().business_number:
            raise LoginError(
                "INVOICE_SUPPLIER_MISMATCH",
                "현재 사업자의 매출 세금계산서만 수정·취소 발행할 수 있습니다.",
                409,
            )
        if document.supply_amount <= 0 or document.tax_amount < 0 or document.total_amount <= 0:
            raise LoginError(
                "INVOICE_ORIGINAL_UNSUPPORTED",
                "양수 일반 원본 세금계산서만 수정·취소 발행할 수 있습니다.",
                422,
            )
        if document.correction_reason_code is not None:
            raise LoginError(
                "INVOICE_ORIGINAL_UNSUPPORTED",
                "이미 수정된 원본의 재정정은 지원하지 않습니다.",
                422,
            )
        if (
            document.supply_amount != invoice.supply_amount
            or document.tax_amount != invoice.tax_amount
            or document.total_amount != invoice.total_amount
        ):
            raise changed()
        return document, {"body": body, "lines": rows, "payment": payments}

    async def _detail_action(self, number: str, direction: str) -> dict:
        tin = self.invoices.business_tin
        return await self._action(
            "ATEETBDA001R02",
            DETAIL_SCREEN,
            {
                "etxivIsnBrkdTermDVOPrmt": {
                    "etan": number,
                    "screenId": LIST_SCREEN,
                    "pageNum": "1",
                    "slsPrhClCd": "01" if direction == "sales" else "02",
                    "etxivTin": tin,
                    "etxivClCd": "",
                    "etxivClsfCd": "",
                    "etxivMpbNo": self.invoices.business_mpb_no,
                }
            },
            popup="true",
        )

    def _document_from_detail(self, body: dict, rows: list[dict]) -> InvoiceDocumentPreview:
        supplier_number = _business_number(body.get("splrTxprDscmNo"))
        recipient_number = _business_number(body.get("dmnrTxprDscmNo"))
        if supplier_number is None or recipient_number is None:
            raise changed()
        if not 1 <= len(rows) <= 4:
            raise changed()
        items = []
        for row in rows:
            if not isinstance(row, dict):
                raise changed()
            supplied = _date8(row.get("lsatSplDt"))
            name = _text(row.get("lsatNm"))
            if not name:
                raise changed()
            items.append(
                InvoiceDocumentLine(
                    supply_date=supplied,
                    name=name,
                    specification=_text(row.get("lsatRszeNm")),
                    quantity=_decimal_or_none(row.get("lsatQty")),
                    unit_price=_decimal_or_none(row.get("lsatUtprc")),
                    supply_amount=integer(row.get("lsatSplCft")),
                    tax_amount=integer(row.get("lsatTxamt")),
                    remarks=_text(row.get("lsatRmrkCntn")),
                )
            )
        supply = integer(body.get("sumSplCft"))
        tax = integer(body.get("sumTxamt"))
        if (
            sum(item.supply_amount for item in items) != supply
            or sum(item.tax_amount for item in items) != tax
            or supply + tax != integer(body.get("totaAmt"))
        ):
            raise changed()
        purpose = body.get("recApeClCd")
        if purpose not in ("01", "02"):
            raise changed()
        reason = body.get("etxivMdfRsnCd")
        original = body.get("tfstEtan")
        if reason in (None, "", "ZZ"):
            reason = None
            if original not in (None, ""):
                raise changed()
            original = None
        elif reason in {"01", "04", "06"}:
            original = approval_number(original)
        else:
            raise changed()
        return InvoiceDocumentPreview(
            supplier=InvoiceParty(
                business_number=supplier_number,
                name=_text(body.get("splrTnmNm")),
                representative_name=_text(body.get("splrRprsFnm")),
                address=_text(body.get("splrPfbAdr")),
                business_type=_text(body.get("splrBcNm")),
                business_item=_text(body.get("splrItmNm")),
                email=_text(body.get("splrMchrgEmlAdr")),
            ),
            recipient=InvoiceParty(
                business_number=recipient_number,
                name=_text(body.get("dmnrTnmNm")),
                representative_name=_text(body.get("dmnrRprsFnm")),
                address=_text(body.get("dmnrPfbAdr")),
                business_type=_text(body.get("dmnrBcNm")),
                business_item=_text(body.get("dmnrItmNm")),
                email=_text(body.get("dmnrMchrgEmlAdr")),
                secondary_email=_text(body.get("dmnrSchrgEmlAdr")),
            ),
            written_date=_date8(body.get("wrtDt")),
            purpose="receipt" if purpose == "01" else "claim",
            items=items,
            remarks=_text(body.get("etxivSq1RmrkCntn")),
            supply_amount=supply,
            tax_amount=tax,
            total_amount=integer(body.get("totaAmt")),
            original_approval_number=original,
            correction_reason_code=reason,
        )

    def _issue_payload(
        self,
        document: InvoiceDocumentPreview,
        company: str,
        recipient_snapshot: CounterpartySnapshot,
    ) -> dict:
        xml_payload = {
            "request": {
                "etxivClsfCd": "01",
                "etxivKndCd": "01",
                "etxivDmnrClsfCd": "01",
                "etxivDmnrClsfCdBot": "",
                "inqrClCd": "01",
                "isnScrnClCd": "10",  # Certificate-based single issuance (screen's PKC path).
                "splrTin": self.invoices.business_tin,
                "splrMpbNo": "0",
                "brwerFg": "",
                "etan": "",
                "ognXML": "",
                "sSignData": "",
                "trnsXmlCntn": "",
                "xmlCntn": "",
                "pubcUserNo": _text(self.invoices.business_profile.get("pubcUserNo")),
                "crtfUqno": _text(self.invoices.business_profile.get("crtfUqno")),
            },
            "userReqInfoVO": {},
            "splrInfrBizSVO": self._supplier_vo(document.supplier),
            "dmnrInfrBizSVO": self._recipient_vo(document.recipient, recipient_snapshot),
            "trteInfrBizSVO": {},
            "lsatInfrBizSVOList": [self._line_vo(line) for line in document.items],
            "sncInfrBizSVO": self._summary_vo(document),
            "sncClInfrBizSVO": self._payment_vo(document),
        }
        return {
            "kind": "ordinary_issue",
            "company_name": company,
            "screen_id": ISSUE_SCREEN,
            "xml_action": ISSUE_XML_ACTION,
            "commit_action": ISSUE_COMMIT_ACTION,
            "xml_payload": xml_payload,
            "commit_payload": {
                "request": dict(xml_payload["request"]),
                "userReqInfoVO": {},
            },
        }

    def _correction_payload(
        self,
        documents: list[InvoiceDocumentPreview],
        raw: dict,
        original_number: str,
        *,
        reason: Literal["clerical_error", "contract_cancellation", "duplicate_issue"],
        recipient_snapshot: CounterpartySnapshot | None = None,
    ) -> dict:
        reason_code = CORRECTION_REASON_CODES[reason]
        screen_code = CORRECTION_SCREEN_CODES[reason]
        xml_payload = {
            "request": {
                "etxivClsfCd": "02",
                "etxivKndCd": "01",
                "etxivDmnrClsfCd": "01",
                "etxivDmnrClsfCdBot": "",
                "inqrClCd": "01",
                "isnScrnClCd": "10",
                "splrTin": self.invoices.business_tin,
                "splrMpbNo": "0",
                "oldAprvNo": original_number,
                "tfstEtan": original_number,
                "etxivKndCdBot": "01" if len(documents) == 2 else "",
                "lsatInptYn": "Y",
                "cfbDtInqrYn": "Y",
                "brwerFg": "",
                "etan": "",
                "ognXML": "",
                "sSignData": "",
                "trnsXmlCntn": "",
                "xmlCntn": "",
                "pubcUserNo": _text(self.invoices.business_profile.get("pubcUserNo")),
                "crtfUqno": _text(self.invoices.business_profile.get("crtfUqno")),
            },
            "userReqInfoVO": {},
            "splrInfrBizSVO": self._supplier_vo(documents[0].supplier),
            "dmnrInfrBizSVO": self._recipient_vo(documents[0].recipient, recipient_snapshot),
            "trteInfrBizSVO": {},
            "lsatInfrBizSVOList": [self._line_vo(line) for line in documents[0].items],
            "sncInfrBizSVO": self._summary_vo(documents[0]),
            "sncClInfrBizSVO": self._payment_vo(documents[0]),
            "etxivMdfRsnCd": reason_code,
        }
        original_tin = _text(raw["body"].get("dmnrTin"))
        original_branch = _text(raw["body"].get("dmnrMpbNo"))
        if not original_tin or original_branch not in {"", "0", "0000"}:
            raise changed()
        xml_payload["dmnrInfrBizSVO"]["dmnrTin"] = original_tin
        xml_payload["dmnrInfrBizSVO"]["dmnrMpbNo"] = "0"
        xml_payload["request"]["etxivDmnrClsfCdBot"] = "01" if len(documents) == 2 else ""
        if len(documents) == 2:
            xml_payload.update(
                {
                    # UTEETBAA44.xml C03 indatalist uses this exact unusual spelling.
                    "splrInfrBIzSVO2": self._supplier_vo(documents[1].supplier),
                    "dmnrInfrBizSVO2": self._recipient_vo(
                        documents[1].recipient, recipient_snapshot
                    ),
                    "trteInfrBizSVO2": {},
                    "lsatInfrBizSVOList2": [self._line_vo(line) for line in documents[1].items],
                    "sncInfrBizSVO2": self._summary_vo(documents[1]),
                    "sncClInfrBizSVO2": self._payment_vo(documents[1]),
                }
            )
        return {
            "kind": "correction",
            "screen_id": "UTEETBAA" + screen_code,
            "xml_action": CORRECTION_XML_ACTION,
            "commit_action": CORRECTION_COMMIT_ACTION,
            "xml_payload": xml_payload,
            "commit_payload": {
                "request": dict(xml_payload["request"]),
                "userReqInfoVO": {},
            },
        }

    def _supplier_vo(self, party: InvoiceParty) -> dict:
        return {
            "splrTin": self.invoices.business_tin,
            "splrMpbNo": "0",
            "splrTxprDscmNo": party.business_number,
            "txprDscmNoCnfr": party.business_number,
            "splrTnmNm": party.name,
            "splrRprsFnm": party.representative_name,
            "splrPfbAdr": party.address,
            "splrBcNm": party.business_type,
            "splrItmNm": party.business_item,
            "splrChrgEmlAdr": party.email,
            "splrChrgEmlId": _email_id(party.email),
            "splrChrgEmlDman": _email_domain(party.email),
        }

    @staticmethod
    def _recipient_vo(party: InvoiceParty, snapshot: CounterpartySnapshot | None = None) -> dict:
        recipient_tin = snapshot.recipient_tin if snapshot is not None else ""
        recipient_branch = snapshot.branch_number if snapshot is not None else ""
        return {
            "dmnrTin": recipient_tin,
            "dmnrMpbNo": "0" if recipient_branch in ("", "0", "0000") else recipient_branch,
            "dmnrTxprDscmNo": party.business_number,
            "txprDscmNoCnfr": party.business_number,
            "dmnrTnmNm": party.name,
            "dmnrRprsFnm": party.representative_name,
            "dmnrPfbAdr": party.address,
            "dmnrBcNm": party.business_type,
            "dmnrItmNm": party.business_item,
            "dmnrMchrgEmlAdr": party.email,
            "dmnrMchrgEmlId": _email_id(party.email),
            "dmnrMchrgEmlDman": _email_domain(party.email),
            "dmnrSchrgEmlAdr": party.secondary_email,
            "dmnrSchrgEmlId": _email_id(party.secondary_email),
            "dmnrSchrgEmlDman": _email_domain(party.secondary_email),
        }

    @staticmethod
    def _summary_vo(document: InvoiceDocumentPreview) -> dict:
        return {
            "wrtDt": _date_text(document.written_date),
            "rmrkCntn": document.remarks,
            "splCft": str(document.supply_amount),
            "txamt": str(document.tax_amount),
            "sumAmt": str(document.total_amount),
            "isnDt": "",
        }

    @staticmethod
    def _payment_vo(document: InvoiceDocumentPreview) -> dict:
        return {
            "csh": "",
            "chck": "",
            "note": "",
            "crit": "",
            "recApeClCd": PURPOSE_CODES[document.purpose],
        }

    @staticmethod
    def _line_vo(line: InvoiceDocumentLine) -> dict:
        return {
            "lsatSplMm": f"{line.supply_date.month:02d}",
            "lsatSplDd": f"{line.supply_date.day:02d}",
            "lsatSplDt": _date_text(line.supply_date),
            "lsatNm": line.name,
            "lsatRszeNm": line.specification,
            "lsatQty": _decimal_text(line.quantity),
            "lsatUtprc": _decimal_text(line.unit_price),
            "lsatSplCft": str(line.supply_amount),
            "lsatTxamt": str(line.tax_amount),
            "lsatRmrkCntn": line.remarks,
        }

    async def _action(
        self, action_id: str, screen_id: str, payload: dict, *, popup: str = "false"
    ) -> dict:
        return self.client._json(
            await self.client._request(
                "POST",
                ET_BASE + "/wqAction.do",
                params={
                    "actionId": action_id,
                    "screenId": screen_id,
                    "popupYn": popup,
                    "realScreenId": "",
                },
                json=self._wire_payload(payload),
            )
        )

    @staticmethod
    def _wire_payload(payload: dict) -> dict:
        body = copy.deepcopy(payload)
        request = body.pop("request", None)
        if request is None:
            return body
        if not isinstance(request, dict):
            raise changed()
        return {**request, **body}

    @staticmethod
    def _pre_xml_request(material) -> dict:
        return {
            "brwerFg": "N",
            "userDn": getattr(
                material,
                "certsubjectRFC",
                material.certificate.subject.rfc4514_string(),
            ),
        }

    def _xml_response_documents(self, response: dict, *, expected: int) -> list[dict]:
        documents = []
        for suffix in ("", "2"):
            xml = _text(response.get(f"xmlCntn{suffix}"))
            transfer = _text(response.get(f"trnsXmlCntn{suffix}"))
            etan = _text(response.get(f"etan{suffix}"))
            if xml or transfer or etan:
                if not xml or not transfer or not etan:
                    raise changed()
                documents.append(
                    {
                        "suffix": suffix,
                        "xml": xml,
                        "transfer": transfer,
                        "etan": approval_number(etan),
                    }
                )
        if len(documents) != expected:
            raise changed()
        return documents

    def _signed_document_request(self, document: dict, material, encoding: str) -> dict:
        try:
            signed_xml = sign_invoice_xml(document["xml"], material)
        except InvoiceSigningError as exc:
            raise LoginError(
                "INVOICE_SIGNING_FAILED",
                "전자세금계산서 XML 서명에 실패했습니다.",
            ) from exc
        wire_signed_xml = self._encode_signed_xml(signed_xml, encoding)
        cert_der = material.certificate.public_bytes(Encoding.DER)
        random_bytes = getattr(material, "random_number", None)
        suffix = document["suffix"]
        return {
            f"etan{suffix}": document["etan"],
            f"ognXML{suffix}": document["xml"],
            f"sSignData{suffix}": wire_signed_xml,
            f"xmlCntn{suffix}": wire_signed_xml,
            f"trnsXmlCntn{suffix}": document["transfer"],
            "brwerFg": "N",
            "userDn": getattr(
                material,
                "certsubjectRFC",
                material.certificate.subject.rfc4514_string(),
            ),
            "rValue": base64.b64encode(random_bytes or b"").decode("ascii"),
            "signCert": base64.b64encode(cert_der).decode("ascii"),
        }

    @staticmethod
    def _commit_payload_from_xml_response(
        prepared: PreparedInvoiceOperation, xml_response: dict, signatures: list[dict]
    ) -> dict:
        commit_payload = copy.deepcopy(prepared.payload["commit_payload"])
        response = _response(xml_response)
        request = commit_payload["request"]
        for signature in signatures:
            request.update(signature)
        for suffix in ["", "2"] if len(prepared.documents) == 2 else [""]:
            for base_key in (
                "tteetbm101DVO",
                "tteetbd102DVOList",
                "tteetbd103DVOList",
                "tteetbd104DVOList",
            ):
                key = base_key + suffix
                value = xml_response.get(key)
                expected_type = dict if base_key == "tteetbm101DVO" else list
                if not isinstance(value, expected_type):
                    raise changed()
                commit_payload[key] = copy.deepcopy(value)
        for key, value in response.items():
            if key.startswith(("xmlCntn", "trnsXmlCntn", "etan")) and key not in request:
                request[key] = value
        return commit_payload

    @staticmethod
    def _encode_signed_xml(signed_xml: str, encoding: str) -> str:
        if encoding == "raw":
            return signed_xml
        if encoding == "base64":
            return base64.b64encode(signed_xml.encode("utf-8")).decode("ascii")
        raise changed()

    @staticmethod
    def _approval_numbers(data: dict, *, expected: int) -> list[str]:
        response = _response(data)
        candidates = [response.get("apprvNo") or response.get("etan")]
        rows = data.get("tteetbl109DVOList")
        if isinstance(rows, list):
            candidates.extend(
                row.get("apprvNo") or row.get("etan") for row in rows if isinstance(row, dict)
            )
        approvals = []
        for candidate in candidates:
            if candidate in (None, ""):
                continue
            approvals.append(approval_number(candidate))
        unique = list(dict.fromkeys(approvals))
        if len(unique) != expected:
            raise changed()
        return unique

    async def _verify_readback(
        self, prepared: PreparedInvoiceOperation, approval_numbers: list[str]
    ) -> None:
        for number, expected in zip(approval_numbers, prepared.documents, strict=True):
            data = await self._detail_action(number, "sales")
            _ok(data, "INVOICE_READBACK_FAILED", "발급 후 세금계산서 확인에 실패했습니다.")
            body = data.get("etxivIsnBrkdTermDVO")
            rows = data.get("lsatInfrBizSVOList")
            if not isinstance(body, dict) or not isinstance(rows, list):
                raise changed()
            if approval_number(body.get("etan")) != number:
                raise changed()
            actual = self._document_from_detail(body, rows)
            if not _same_document(actual, expected, number):
                raise LoginError(
                    "INVOICE_READBACK_MISMATCH",
                    "발급 결과가 미리보기와 일치하지 않습니다.",
                    409,
                )

    @staticmethod
    def _assert_xml_matches_documents(
        xml_text: str,
        documents: list[InvoiceDocumentPreview],
        *,
        expected_issue_id: str | None = None,
    ) -> None:
        parser = etree.XMLParser(
            encoding="utf-8",
            resolve_entities=False,
            no_network=True,
            load_dtd=False,
        )
        try:
            root = etree.fromstring(xml_text.encode("utf-8"), parser=parser)
        except etree.XMLSyntaxError as exc:
            raise LoginError(
                "INVOICE_XML_UNSUPPORTED",
                "홈택스 XML을 파싱하지 못했습니다.",
            ) from exc
        root_name = etree.QName(root)
        if root_name.localname != "TaxInvoice" or root_name.namespace != TAX_NS:
            raise LoginError("INVOICE_XML_UNSUPPORTED", "지원하지 않는 홈택스 XML 루트입니다.")
        _assert_tax_namespace(root)
        xml_documents = _xml_documents(root)
        if len(xml_documents) != len(documents):
            raise LoginError(
                "INVOICE_XML_MISMATCH",
                "홈택스 XML 문서 수가 미리보기와 일치하지 않습니다.",
                409,
            )
        for xml_document, document in zip(xml_documents, documents, strict=True):
            if _document_from_xml(xml_document) != _document_binding(
                document, expected_issue_id=expected_issue_id
            ):
                raise LoginError(
                    "INVOICE_XML_MISMATCH",
                    "홈택스 XML이 미리보기 문서와 일치하지 않습니다.",
                    409,
                )

    @staticmethod
    def _request_as(model, request):
        try:
            return model.model_validate(request.model_dump())
        except (AttributeError, ValidationError):
            raise changed() from None


def operation_preview(operation_id: str, prepared: PreparedInvoiceOperation, expires_at):
    return InvoiceOperationPreview(
        operation_id=operation_id,
        client_reference=prepared.client_reference,
        operation=prepared.operation,
        expires_at=expires_at,
        documents=prepared.documents,
        content_digest=fingerprint(prepared.comparable()),
    )


def _line_from_request(line: InvoiceIssueLine) -> InvoiceDocumentLine:
    return InvoiceDocumentLine(
        supply_date=line.supply_date,
        name=line.name,
        specification=line.specification,
        quantity=line.quantity,
        unit_price=line.unit_price,
        supply_amount=line.supply_amount,
        tax_amount=line.tax_amount,
        remarks=line.remarks,
    )


def _same_document(
    actual: InvoiceDocumentPreview, expected: InvoiceDocumentPreview, number: str
) -> bool:
    return _document_binding(actual, expected_issue_id=number) == _document_binding(
        expected, expected_issue_id=number
    )


def _ok(data: dict, code: str, message: str) -> None:
    result = data.get("resultMsg")
    if (
        not isinstance(result, dict)
        or result.get("result") != "S"
        or result.get("errorMsg")
        or result.get("errorCd")
    ):
        raise LoginError(code, message)


def _response(data: dict) -> dict:
    response = data.get("response", data)
    if not isinstance(response, dict):
        raise changed()
    return response


def _text(value) -> str:
    if value is None:
        return ""
    if type(value) in (str, int):
        return str(value)
    raise changed()


def _business_number(value) -> str | None:
    if not isinstance(value, str):
        return None
    digits = value.replace("-", "")
    if re.fullmatch(r"[0-9]{10}", digits):
        return digits
    return None


def _date_text(value: date) -> str:
    return value.strftime("%Y%m%d")


def _date8(value) -> date:
    text = _text(value).replace("-", "")
    if not re.fullmatch(r"\d{8}", text):
        raise changed()
    return date(int(text[:4]), int(text[4:6]), int(text[6:8]))


def _decimal_text(value: Decimal | None) -> str:
    if value is None:
        return ""
    _assert_decimal_safe(value)
    text = format(value, "f")
    if "." in text:
        return text.rstrip("0").rstrip(".")
    return text


def _decimal_or_none(value) -> Decimal | None:
    if value in (None, ""):
        return None
    if type(value) in (str, int, float):
        try:
            decimal = Decimal(str(value))
        except InvalidOperation:
            raise changed() from None
        _assert_decimal_safe(decimal)
        return decimal
    raise changed()


def _assert_decimal_safe(value: Decimal) -> None:
    if not value.is_finite() or abs(value) > Decimal("999999999999"):
        raise changed()
    if abs(value.as_tuple().exponent) > 6:
        raise changed()


def _email_id(value: str) -> str:
    return value.split("@", 1)[0] if "@" in value else ""


def _email_domain(value: str) -> str:
    return value.split("@", 1)[1] if "@" in value else ""


def _xml_documents(root: etree._Element) -> list[etree._Element]:
    if etree.QName(root).localname == "TaxInvoice":
        return [root]
    raise LoginError("INVOICE_XML_UNSUPPORTED", "지원하지 않는 홈택스 XML 루트입니다.")


def _document_from_xml(node: etree._Element) -> dict:
    document = _required(node, "TaxInvoiceDocument")
    settlement = _required(node, "TaxInvoiceTradeSettlement")
    lines = _children(node, "TaxInvoiceTradeLineItem")
    if not lines:
        raise LoginError("INVOICE_XML_MISMATCH", "홈택스 XML 품목이 없습니다.", 409)
    supplier = _required(settlement, "InvoicerParty")
    recipient = _required(settlement, "InvoiceeParty")
    summary = _required(settlement, "SpecifiedMonetarySummation")
    return {
        "issue_id": _required_text(document, "IssueID"),
        "supplier_business_number": _business_number_strict(_required_text(supplier, "ID")),
        "recipient_business_number": _business_number_strict(_required_text(recipient, "ID")),
        "supplier_tax_registration_id": _head_registration_id(
            _optional_path_text(supplier, "SpecifiedOrganization", "TaxRegistrationID")
        ),
        "recipient_tax_registration_id": _head_registration_id(
            _optional_path_text(recipient, "SpecifiedOrganization", "TaxRegistrationID")
        ),
        "supplier_name": _required_text(supplier, "NameText"),
        "recipient_name": _required_text(recipient, "NameText"),
        "supplier_business_type": _optional_text(supplier, "TypeCode"),
        "recipient_business_type": _optional_text(recipient, "TypeCode"),
        "supplier_business_item": _optional_text(supplier, "ClassificationCode"),
        "recipient_business_item": _optional_text(recipient, "ClassificationCode"),
        "supplier_representative_name": _optional_path_text(
            supplier, "SpecifiedPerson", "NameText"
        ),
        "recipient_representative_name": _optional_path_text(
            recipient, "SpecifiedPerson", "NameText"
        ),
        "supplier_address": _optional_path_text(supplier, "SpecifiedAddress", "LineOneText"),
        "recipient_address": _optional_path_text(recipient, "SpecifiedAddress", "LineOneText"),
        "supplier_email": _optional_path_text(supplier, "DefinedContact", "URICommunication"),
        "recipient_email": _optional_path_text(
            recipient, "PrimaryDefinedContact", "URICommunication"
        ),
        "recipient_secondary_email": _optional_path_text(
            recipient, "SecondaryDefinedContact", "URICommunication"
        ),
        "written_date": _date_text_strict(_required_text(document, "IssueDateTime")),
        "type_code": _required_text(document, "TypeCode"),
        "purpose": _required_text(document, "PurposeCode"),
        "correction_reason_code": _optional_text(document, "AmendmentStatusCode"),
        "original_approval_number": _optional_text(document, "OriginalIssueID"),
        "remarks": "".join(_leaf_text(e) for e in _children(document, "DescriptionText")),
        "supply_amount": _amount(_required_text(summary, "ChargeTotalAmount")),
        "tax_amount": _amount(_required_text(summary, "TaxTotalAmount")),
        "total_amount": _amount(_required_text(summary, "GrandTotalAmount")),
        "lines": [_line_from_xml(line) for line in lines],
    }


def _line_from_xml(node: etree._Element) -> dict:
    total_tax = _required(node, "TotalTax")
    return {
        "supply_date": _date_text_strict(_required_text(node, "PurchaseExpiryDateTime")),
        "name": _required_text(node, "NameText"),
        "quantity": _decimal_text(_decimal_or_none(_optional_text(node, "ChargeableUnitQuantity"))),
        "unit_price": _decimal_text(
            _decimal_or_none(_optional_path_text(node, "UnitPrice", "UnitAmount"))
        ),
        "supply_amount": _amount(_required_text(node, "InvoiceAmount")),
        "tax_amount": _amount(_required_text(total_tax, "CalculatedAmount")),
        "specification": _optional_text(node, "InformationText"),
        "remarks": _optional_text(node, "DescriptionText"),
    }


def _document_binding(
    document: InvoiceDocumentPreview, *, expected_issue_id: str | None = None
) -> dict:
    return {
        "issue_id": expected_issue_id or "",
        "supplier_business_number": document.supplier.business_number,
        "recipient_business_number": document.recipient.business_number,
        "supplier_tax_registration_id": "",
        "recipient_tax_registration_id": "",
        "supplier_name": document.supplier.name,
        "recipient_name": document.recipient.name,
        "supplier_business_type": document.supplier.business_type,
        "recipient_business_type": document.recipient.business_type,
        "supplier_business_item": document.supplier.business_item,
        "recipient_business_item": document.recipient.business_item,
        "supplier_representative_name": document.supplier.representative_name,
        "recipient_representative_name": document.recipient.representative_name,
        "supplier_address": document.supplier.address,
        "recipient_address": document.recipient.address,
        "supplier_email": document.supplier.email,
        "recipient_email": document.recipient.email,
        "recipient_secondary_email": document.recipient.secondary_email,
        "written_date": _date_text(document.written_date),
        "type_code": "0201" if document.correction_reason_code else "0101",
        "purpose": PURPOSE_CODES[document.purpose],
        "correction_reason_code": document.correction_reason_code or "",
        "original_approval_number": document.original_approval_number or "",
        "remarks": document.remarks,
        "supply_amount": document.supply_amount,
        "tax_amount": document.tax_amount,
        "total_amount": document.total_amount,
        "lines": [
            {
                "supply_date": _date_text(line.supply_date),
                "name": line.name,
                "quantity": _decimal_text(line.quantity),
                "unit_price": _decimal_text(line.unit_price),
                "supply_amount": line.supply_amount,
                "tax_amount": line.tax_amount,
                "specification": line.specification,
                "remarks": line.remarks,
            }
            for line in document.items
        ],
    }


def _required(node: etree._Element, localname: str) -> etree._Element:
    matches = [
        child
        for child in node
        if isinstance(child.tag, str)
        and etree.QName(child).localname == localname
        and etree.QName(child).namespace == TAX_NS
    ]
    if len(matches) != 1 or not isinstance(matches[0], etree._Element):
        raise LoginError("INVOICE_XML_MISMATCH", "홈택스 XML 구조가 예상과 다릅니다.", 409)
    return matches[0]


def _children(node: etree._Element, localname: str) -> list[etree._Element]:
    return [
        child
        for child in node
        if isinstance(child.tag, str)
        and etree.QName(child).localname == localname
        and etree.QName(child).namespace == TAX_NS
    ]


def _required_text(node: etree._Element, localname: str) -> str:
    child = _required(node, localname)
    if len(child):
        raise LoginError("INVOICE_XML_MISMATCH", "홈택스 XML 값 구조가 예상과 다릅니다.", 409)
    value = "".join(child.itertext()).strip()
    if not value:
        raise LoginError("INVOICE_XML_MISMATCH", "홈택스 XML 필수 값이 비어 있습니다.", 409)
    return value


def _optional_text(node: etree._Element, localname: str) -> str:
    matches = _children(node, localname)
    if len(matches) > 1:
        raise LoginError("INVOICE_XML_MISMATCH", "홈택스 XML 구조가 예상과 다릅니다.", 409)
    if not matches:
        return ""
    child = matches[0]
    if len(child):
        raise LoginError("INVOICE_XML_MISMATCH", "홈택스 XML 값 구조가 예상과 다릅니다.", 409)
    return "".join(child.itertext()).strip()


def _optional_path_text(node: etree._Element, *localnames: str) -> str:
    current = node
    for localname in localnames:
        matches = _children(current, localname)
        if len(matches) > 1:
            raise LoginError("INVOICE_XML_MISMATCH", "홈택스 XML 구조가 예상과 다릅니다.", 409)
        if not matches:
            return ""
        current = matches[0]
    if len(current):
        raise LoginError("INVOICE_XML_MISMATCH", "홈택스 XML 값 구조가 예상과 다릅니다.", 409)
    return "".join(current.itertext()).strip()


def _assert_tax_namespace(root: etree._Element) -> None:
    for node in root.iter():
        if not isinstance(node.tag, str):
            continue
        if etree.QName(node).namespace != TAX_NS:
            raise LoginError(
                "INVOICE_XML_UNSUPPORTED",
                "홈택스 표준 세금계산서 네임스페이스가 아닙니다.",
                409,
            )


def _head_registration_id(value: str) -> str:
    if value in {"", "0", "0000"}:
        return ""
    raise LoginError("INVOICE_BRANCH_UNSUPPORTED", "종사업장 XML은 지원하지 않습니다.", 409)


def _leaf_text(node: etree._Element) -> str:
    if len(node):
        raise changed()
    return (node.text or "").strip()


def _business_number_strict(value: str) -> str:
    if not re.fullmatch(r"\d{10}", value):
        raise LoginError(
            "INVOICE_XML_MISMATCH",
            "홈택스 XML 사업자번호 형식이 예상과 다릅니다.",
            409,
        )
    return value


def _date_text_strict(value: str) -> str:
    if not re.fullmatch(r"\d{8}", value):
        raise LoginError("INVOICE_XML_MISMATCH", "홈택스 XML 날짜 형식이 예상과 다릅니다.", 409)
    return value


def _amount(value: str) -> int:
    if not re.fullmatch(r"-?\d+", value.strip()):
        raise LoginError("INVOICE_XML_MISMATCH", "홈택스 XML 금액 형식이 예상과 다릅니다.", 409)
    return int(value)
