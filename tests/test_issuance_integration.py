"""Actual API/client/manager/XML signer with synthetic certificates and mock HomeTax HTTP."""

import base64
import copy
import hashlib
import json

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from lxml import etree
from test_invoice_operations import certificate_material
from test_issuance_backend import (
    APPROVAL,
    NEW_APPROVAL,
    FakeCounterpartyBackend,
    FakeInvoices,
    detail_response,
    issue_request,
    ok,
)

from hometax_login.api import Settings, create_app
from hometax_login.invoice_signing import TAX_INVOICE_NS, verify_invoice_xml
from hometax_login.protocol import HometaxClient

KEY = "synthetic-issuance-api-key-" * 2
SECOND = "202501031234567890123456"


def xml_document(doc, number):
    ns = "{" + TAX_INVOICE_NS + "}"
    root = etree.Element(ns + "TaxInvoice", nsmap={None: TAX_INVOICE_NS})

    def el(parent, tag, text=None):
        value = etree.SubElement(parent, ns + tag)
        if text is not None:
            value.text = str(text)
        return value

    header = el(root, "TaxInvoiceDocument")
    el(header, "IssueID", number)
    el(header, "TypeCode", "0201" if doc["correction_reason_code"] else "0101")
    el(header, "DescriptionText", doc["remarks"])
    el(header, "IssueDateTime", doc["written_date"].replace("-", ""))
    if doc["correction_reason_code"]:
        el(header, "AmendmentStatusCode", doc["correction_reason_code"])
        el(header, "OriginalIssueID", doc["original_approval_number"])
    el(header, "PurposeCode", "02" if doc["purpose"] == "claim" else "01")
    settlement = el(root, "TaxInvoiceTradeSettlement")
    for label, name in [("supplier", "InvoicerParty"), ("recipient", "InvoiceeParty")]:
        party = doc[label]
        node = el(settlement, name)
        el(node, "ID", party["business_number"])
        el(node, "TypeCode", party["business_type"])
        el(node, "NameText", party["name"])
        el(node, "ClassificationCode", party["business_item"])
        el(el(node, "SpecifiedOrganization"), "TaxRegistrationID", "0000")
        el(el(node, "SpecifiedPerson"), "NameText", party["representative_name"])
        el(el(node, "SpecifiedAddress"), "LineOneText", party["address"])
        el(
            el(node, "DefinedContact" if label == "supplier" else "PrimaryDefinedContact"),
            "URICommunication",
            party["email"],
        )
        if label == "recipient":
            el(el(node, "SecondaryDefinedContact"), "URICommunication", party["secondary_email"])
    totals = el(settlement, "SpecifiedMonetarySummation")
    for name, field in [
        ("ChargeTotalAmount", "supply_amount"),
        ("TaxTotalAmount", "tax_amount"),
        ("GrandTotalAmount", "total_amount"),
    ]:
        el(totals, name, doc[field])
    for index, line in enumerate(doc["items"], 1):
        node = el(root, "TaxInvoiceTradeLineItem")
        for name, value in [
            ("SequenceNumeric", index),
            ("InvoiceAmount", line["supply_amount"]),
            ("ChargeableUnitQuantity", line["quantity"]),
            ("InformationText", line["specification"]),
            ("DescriptionText", line["remarks"]),
            ("NameText", line["name"]),
            ("PurchaseExpiryDateTime", line["supply_date"].replace("-", "")),
        ]:
            el(node, name, value)
        el(el(node, "TotalTax"), "CalculatedAmount", line["tax_amount"])
        el(el(node, "UnitPrice"), "UnitAmount", line["unit_price"])
    return etree.tostring(root, encoding="unicode")


def detail_for(doc, number):
    result = detail_response(number)
    body = result["etxivIsnBrkdTermDVO"]
    body.update(
        wrtDt=doc["written_date"].replace("-", ""),
        recApeClCd="02" if doc["purpose"] == "claim" else "01",
        etxivSq1RmrkCntn=doc["remarks"],
        sumSplCft=doc["supply_amount"],
        sumTxamt=doc["tax_amount"],
        totaAmt=doc["total_amount"],
        etxivMdfRsnCd=doc["correction_reason_code"] or "ZZ",
        tfstEtan=doc["original_approval_number"],
    )
    result["lsatInfrBizSVOList"] = [
        {
            "lsatSplDt": line["supply_date"].replace("-", ""),
            "lsatNm": line["name"],
            "lsatRszeNm": line["specification"],
            "lsatQty": line["quantity"],
            "lsatUtprc": line["unit_price"],
            "lsatSplCft": line["supply_amount"],
            "lsatTxamt": line["tax_amount"],
            "lsatRmrkCntn": line["remarks"],
        }
        for line in doc["items"]
    ]
    return result


class Portal:
    def __init__(self, material, encoding):
        self.material, self.encoding = material, encoding
        self.documents = []
        self.writes = []
        self.issued = {}
        self.tables = {}
        self.tamper_readback = False

    def __call__(self, request):
        action = request.url.params.get("actionId")
        if request.url.path == "/token.do":
            return httpx.Response(200, json={"token": "synthetic-token"})
        if request.url.path == "/permission.do":
            profile = FakeInvoices(None).business_profile
            return httpx.Response(
                200,
                json={
                    "resultMsg": {
                        "sessionMap": {
                            **profile,
                            "userId": "user",
                            "tin": "1112223334",
                            "cnvrTin": "1112223334",
                            "userClsfCd": "02",
                            "txfrBmanLgnYn": "N",
                        }
                    }
                },
            )
        payload = json.loads(request.content)
        if action == "ATEETBAA002R04":
            return httpx.Response(200, json=ok(sptxpTxivIsnClCd="ZZ"))
        if action == "ATEETBDA001R02":
            number = payload["etxivIsnBrkdTermDVOPrmt"]["etan"]
            result = detail_response(number) if number == APPROVAL else self.issued[number]
            return httpx.Response(200, json=copy.deepcopy(result))
        if action in {"ATEETBAA002C04", "ATEETBAA003C03"}:
            self.writes.append(action)
            assert payload["isnScrnClCd"] == "10"
            assert payload["dmnrInfrBizSVO"]["dmnrTin"] == "9998887776"
            data, response = {}, {}
            for index, (doc, number) in enumerate(
                zip(self.documents, [NEW_APPROVAL, SECOND][: len(self.documents)], strict=True)
            ):
                suffix = "2" if index else ""
                response.update(
                    {
                        "etan" + suffix: number,
                        "xmlCntn" + suffix: xml_document(doc, number),
                        "trnsXmlCntn" + suffix: "synthetic-transfer" + suffix,
                    }
                )
                data["tteetbm101DVO" + suffix] = {"marker": suffix or "first"}
                for table in ["tteetbd102DVOList", "tteetbd103DVOList", "tteetbd104DVOList"]:
                    data[table + suffix] = [{"marker": table + suffix}]
            self.tables = copy.deepcopy(data)
            return httpx.Response(200, json=ok(response=response, **data))
        if action in {"ATEETBAA002C05", "ATEETBAA003C04"}:
            self.writes.append(action)
            ids = [NEW_APPROVAL, SECOND][: len(self.documents)]
            for index, (doc, number) in enumerate(zip(self.documents, ids, strict=True)):
                signed = payload["xmlCntn" + ("2" if index else "")]
                if self.encoding == "base64":
                    signed = base64.b64decode(signed, validate=True).decode()
                verify_invoice_xml(signed, self.material.certificate)
                self.issued[number] = detail_for(doc, number)
                if self.tamper_readback:
                    self.issued[number]["etxivIsnBrkdTermDVO"]["recApeClCd"] = "01"
            assert all(payload[key] == value for key, value in self.tables.items())
            return httpx.Response(
                200, json=ok(tteetbl109DVOList=[{"apprvNo": n} for n in reversed(ids)])
            )
        pytest.fail(f"Unexpected upstream action: {action}")


@pytest.mark.parametrize(
    "operation,encoding",
    [
        ("issue", "raw"),
        ("issue", "base64"),
        ("correct", "raw"),
        ("contract_cancellation", "raw"),
        ("duplicate_issue", "raw"),
        ("tampered_readback", "raw"),
    ],
)
async def test_full_api_real_client_real_signature_mock_submission(
    tmp_path, monkeypatch, operation, encoding
):
    material = certificate_material()
    portal = Portal(material, encoding)
    portal.tamper_readback = operation == "tampered_readback"
    client = HometaxClient(transport=httpx.MockTransport(portal))
    client.invoice_wire_encoding = encoding
    client.signing_certificate_fingerprint = material.certificate.fingerprint(hashes.SHA256()).hex()
    client.counterparty_changes.backend = FakeCounterpartyBackend()
    client.invoices.references.update(FakeInvoices(None).references)
    # Keep scope initialization from invalidating a deliberately preloaded read-only list fixture.
    client.invoices.business_tin = "1112223334"
    app = create_app(
        Settings(
            api_keys=(KEY,),
            invoice_writes_enabled=True,
            invoice_wire_encoding=encoding,
            write_journal_path=str(tmp_path / "private" / "writes.sqlite3"),
        )
    )
    session = await app.state.sessions.add(
        hashlib.sha256(KEY.encode()).hexdigest(), client, {"user_id": "user"}
    )
    monkeypatch.setattr("hometax_login.api.load_certificate", lambda *args: material)
    base = f"/v1/hometax/sessions/{session.id}"
    request = issue_request().model_dump(mode="json")
    route = "/tax-invoices/drafts"
    if operation == "correct":
        request["reason"] = "clerical_error"
        route = f"/tax-invoices/{APPROVAL}/corrections"
    elif operation not in {"issue", "tampered_readback"}:
        request = {k: request[k] for k in ("client_reference", "written_date")}
        request["reason"] = operation
        route = f"/tax-invoices/{APPROVAL}/cancellations"
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://local",
            headers={"Authorization": f"Bearer {KEY}"},
        ) as api:
            preview = await api.post(base + route, json=request)
            assert preview.status_code == 200
            body = preview.json()
            assert portal.writes == []
            portal.documents = body["documents"]
            submit = {
                "confirm": True,
                "content_digest": body["content_digest"],
                "cert_type": "der",
                "cert_file": "Y2VydA==",
                "key_file": "a2V5",
                "password": "synthetic-password",
            }
            path = base + f"/tax-invoice-operations/{body['operation_id']}/submit"
            issued = await api.post(path, json=submit)
            if portal.tamper_readback:
                assert issued.status_code == 502
                assert issued.json()["error"]["code"] == "INVOICE_OUTCOME_UNKNOWN"
                sent = list(portal.writes)
                retry = await api.post(path, json=submit)
                assert retry.status_code == 409
                assert retry.json()["error"]["code"] == "WRITE_OUTCOME_UNKNOWN"
                assert portal.writes == sent
                assert "synthetic-password" not in issued.text
                return
            assert issued.status_code == 200, issued.text
            assert issued.json()["status"] == "issued"
            assert len(issued.json()["approval_numbers"]) == len(body["documents"])
            actions = list(portal.writes)
            retry = await api.post(path, json=submit)
            assert retry.status_code == 200
            assert retry.json()["status"] == "already_issued"
            assert retry.json()["approval_numbers"] == issued.json()["approval_numbers"]
            assert portal.writes == actions
            assert len(actions) == 2
            assert issued.headers["cache-control"] == "no-store"
    finally:
        await app.state.sessions.close()
