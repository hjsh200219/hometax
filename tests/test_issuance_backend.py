import json
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from hometax_login.counterparty_changes import CounterpartySnapshot
from hometax_login.errors import LoginError
from hometax_login.invoices import TaxInvoice
from hometax_login.issuance_backend import InvoiceBackend
from hometax_login.issuance_models import (
    InvoiceCancelRequest,
    InvoiceCorrectionRequest,
    InvoiceIssueLine,
    InvoiceIssueRequest,
)

OWNER_TIN = "1112223334"
RECIPIENT_TIN = "9998887776"
SUPPLIER_NUMBER = "1234567890"
RECIPIENT_NUMBER = "2223344445"
APPROVAL = "202501011234567890123456"
NEW_APPROVAL = "202501021234567890123456"


def ok(**values):
    return {"resultMsg": {"result": "S", "errorCd": "", "errorMsg": ""}, **values}


def issue_request(**overrides):
    payload = {
        "client_reference": uuid4(),
        "recipient_business_number": RECIPIENT_NUMBER,
        "written_date": date(2025, 1, 31),
        "purpose": "claim",
        "items": [
            InvoiceIssueLine(
                supply_date=date(2025, 1, 31),
                name="테스트 용역",
                specification="1식",
                quantity=Decimal("1"),
                unit_price=Decimal("1000"),
                supply_amount=1000,
                tax_amount=100,
                remarks="비고",
            )
        ],
        "remarks": "청구",
    }
    payload.update(overrides)
    return InvoiceIssueRequest(**payload)


def invoice_xml(
    *,
    supplier=SUPPLIER_NUMBER,
    recipient=RECIPIENT_NUMBER,
    written="20250131",
    purpose="02",
    supply=1000,
    tax=100,
    total=1100,
    line_name="테스트 용역",
    type_code="0101",
    amendment="",
    original="",
    issue_id="202501021234567890123456",
):
    correction = ""
    if amendment:
        correction += f"<AmendmentStatusCode>{amendment}</AmendmentStatusCode>"
    if original:
        correction += f"<OriginalIssueID>{original}</OriginalIssueID>"
    return f"""
    <TaxInvoice
      xmlns="urn:kr:or:kec:standard:Tax:ReusableAggregateBusinessInformationEntitySchemaModule:1:0">
      <TaxInvoiceDocument>
        <IssueID>{issue_id}</IssueID>
        <TypeCode>{type_code}</TypeCode>
        <DescriptionText>청구</DescriptionText>
        {correction}
        <IssueDateTime>{written}</IssueDateTime>
        <PurposeCode>{purpose}</PurposeCode>
      </TaxInvoiceDocument>
      <TaxInvoiceTradeSettlement>
        <InvoicerParty>
          <ID>{supplier}</ID>
          <TypeCode>서비스</TypeCode>
          <ClassificationCode>컨설팅</ClassificationCode>
          <NameText>예시공급사</NameText>
          <SpecifiedOrganization><TaxRegistrationID>0000</TaxRegistrationID></SpecifiedOrganization>
          <SpecifiedPerson><NameText>대표</NameText></SpecifiedPerson>
          <SpecifiedAddress><LineOneText>서울</LineOneText></SpecifiedAddress>
          <DefinedContact><URICommunication>s@example.test</URICommunication></DefinedContact>
        </InvoicerParty>
        <InvoiceeParty>
          <ID>{recipient}</ID>
          <TypeCode>제조</TypeCode>
          <ClassificationCode>부품</ClassificationCode>
          <NameText>예시거래처</NameText>
          <SpecifiedOrganization><TaxRegistrationID>0000</TaxRegistrationID></SpecifiedOrganization>
          <SpecifiedPerson><NameText>거래처대표</NameText></SpecifiedPerson>
          <SpecifiedAddress><LineOneText>부산</LineOneText></SpecifiedAddress>
          <PrimaryDefinedContact><URICommunication>r@example.test</URICommunication></PrimaryDefinedContact>
        </InvoiceeParty>
        <SpecifiedMonetarySummation>
          <ChargeTotalAmount>{supply}</ChargeTotalAmount>
          <TaxTotalAmount>{tax}</TaxTotalAmount>
          <GrandTotalAmount>{total}</GrandTotalAmount>
        </SpecifiedMonetarySummation>
      </TaxInvoiceTradeSettlement>
      <TaxInvoiceTradeLineItem>
        <SequenceNumeric>1</SequenceNumeric>
        <InvoiceAmount>{supply}</InvoiceAmount>
        <ChargeableUnitQuantity>1</ChargeableUnitQuantity>
        <InformationText>1식</InformationText>
        <DescriptionText>비고</DescriptionText>
        <NameText>{line_name}</NameText>
        <PurchaseExpiryDateTime>{written}</PurchaseExpiryDateTime>
        <TotalTax><CalculatedAmount>{tax}</CalculatedAmount></TotalTax>
        <UnitPrice><UnitAmount>{supply}</UnitAmount></UnitPrice>
      </TaxInvoiceTradeLineItem>
    </TaxInvoice>
    """


def xml_response(**response):
    body = {
        "response": {
            "etan": "202501021234567890123456",
            "xmlCntn": invoice_xml(),
            "trnsXmlCntn": "transfer",
        },
        "tteetbm101DVO": {"header": "from-c04"},
        "tteetbd102DVOList": [{"line": 1}],
        "tteetbd103DVOList": [{"party": "supplier"}],
        "tteetbd104DVOList": [{"party": "recipient"}],
    }
    body["response"].update(response)
    return ok(**body)


def detail_response(
    number=APPROVAL,
    *,
    supplier=SUPPLIER_NUMBER,
    recipient=RECIPIENT_NUMBER,
    supply="1000",
    tax="100",
    total="1100",
):
    return ok(
        etxivIsnBrkdTermDVO={
            "etan": number,
            "etxivMdfRsnCd": "ZZ",
            "dmnrTin": RECIPIENT_TIN,
            "dmnrMpbNo": "0",
            "sumSplCft": supply,
            "sumTxamt": tax,
            "totaAmt": total,
            "wrtDt": "20250131",
            "recApeClCd": "02",
            "splrTxprDscmNo": supplier,
            "splrTnmNm": "예시공급사",
            "splrRprsFnm": "대표",
            "splrPfbAdr": "서울",
            "splrBcNm": "서비스",
            "splrItmNm": "컨설팅",
            "splrMchrgEmlAdr": "s@example.test",
            "dmnrTxprDscmNo": recipient,
            "dmnrTnmNm": "예시거래처",
            "dmnrRprsFnm": "거래처대표",
            "dmnrPfbAdr": "부산",
            "dmnrBcNm": "제조",
            "dmnrItmNm": "부품",
            "dmnrMchrgEmlAdr": "r@example.test",
            "dmnrSchrgEmlAdr": "",
            "etxivSq1RmrkCntn": "청구",
        },
        lsatInfrBizSVOList=[
            {
                "lsatSplDt": "20250131",
                "lsatNm": "테스트 용역",
                "lsatRszeNm": "1식",
                "lsatQty": "1",
                "lsatUtprc": "1000",
                "lsatSplCft": supply,
                "lsatTxamt": tax,
                "lsatRmrkCntn": "비고",
            }
        ],
        sncClInfrBizSVO={"csh": "", "chck": "", "note": "", "crit": ""},
    )


class FakeClient:
    def __init__(self, responses=None, *, encoding="raw"):
        self.responses = {"ATEETBAA002R04": ok(sptxpTxivIsnClCd="ZZ"), **(responses or {})}
        self.calls = []
        self.invoice_wire_encoding = encoding
        self.invoices = FakeInvoices(self)
        self.counterparty_changes = FakeCounterpartyChanges()

    async def _request(self, method, path, *, params=None, json=None, **_kwargs):
        self.calls.append((method, path, dict(params or {}), json))
        action = params["actionId"]
        response = self.responses.get(action)
        if callable(response):
            response = response(json)
        if response is None:
            raise AssertionError(f"unexpected action {action}")
        return __import__("json").dumps(response, ensure_ascii=False)

    @staticmethod
    def _json(text):
        return json.loads(text)


class FakeInvoices:
    def __init__(self, client):
        self.client = client
        self.business_tin = OWNER_TIN
        self.business_mpb_no = ""
        self.business_profile = {
            "txprDscmNo": SUPPLIER_NUMBER,
            "tnmNm": "예시공급사",
            "rprsFnm": "대표",
            "adr": "서울",
            "bcNm": "서비스",
            "itmNm": "컨설팅",
            "emlAdr": "s@example.test",
            "pubcUserNo": "user-no",
            "crtfUqno": "cert-no",
            "etxivPkcYn": "Y",
        }
        self.references = {
            APPROVAL: (
                "sales",
                TaxInvoice(
                    approval_number=APPROVAL,
                    written_date=date(2025, 1, 31),
                    issued_date=date(2025, 1, 31),
                    transmitted_date=None,
                    counterparty_name="예시거래처",
                    item_name="테스트 용역",
                    supply_amount=1000,
                    tax_amount=100,
                    total_amount=1100,
                ),
            )
        }

    async def _business(self):
        return OWNER_TIN, "예시공급사"


class FakeCounterpartyChanges:
    def __init__(self):
        self.backend = FakeCounterpartyBackend()


class FakeCounterpartyBackend:
    async def current(self, number, branch):
        assert number == RECIPIENT_NUMBER
        assert branch == ""
        return CounterpartySnapshot(
            company_name="예시공급사",
            owner_tin=OWNER_TIN,
            owner_branch="",
            business_number=RECIPIENT_NUMBER,
            branch_number="",
            recipient_tin=RECIPIENT_TIN,
            data={
                "name": "예시거래처",
                "representative_name": "거래처대표",
                "address": "부산",
                "business_type": "제조",
                "business_item": "부품",
                "primary_contact": {"email": "r@example.test"},
                "secondary_contact": {"email": ""},
            },
        )


class FakeCertificate:
    def public_bytes(self, _encoding):
        return b"certificate-der"

    @property
    def subject(self):
        return self

    def rfc4514_string(self):
        return "CN=test"


class FakeMaterial:
    certificate = FakeCertificate()
    random_number = b"vid-random"
    certsubjectRFC = "CN=material"


@pytest.mark.asyncio
async def test_preview_issue_builds_exact_ordinary_payload():
    client = FakeClient()
    prepared = await InvoiceBackend(client).preview_issue(issue_request())

    assert prepared.operation == "issue"
    assert prepared.scope == (OWNER_TIN, "")
    assert prepared.payload["xml_action"] == "ATEETBAA002C04"
    payload = prepared.payload["xml_payload"]
    assert payload["request"]["etxivClsfCd"] == "01"
    assert payload["request"]["etxivKndCd"] == "01"
    assert payload["request"]["etxivDmnrClsfCd"] == "01"
    assert payload["request"]["pubcUserNo"] == "user-no"
    assert payload["splrInfrBizSVO"]["splrTxprDscmNo"] == SUPPLIER_NUMBER
    assert payload["dmnrInfrBizSVO"]["dmnrTxprDscmNo"] == RECIPIENT_NUMBER
    assert payload["lsatInfrBizSVOList"] == [
        {
            "lsatSplMm": "01",
            "lsatSplDd": "31",
            "lsatSplDt": "20250131",
            "lsatNm": "테스트 용역",
            "lsatRszeNm": "1식",
            "lsatQty": "1",
            "lsatUtprc": "1000",
            "lsatSplCft": "1000",
            "lsatTxamt": "100",
            "lsatRmrkCntn": "비고",
        }
    ]
    assert payload["sncInfrBizSVO"] == {
        "wrtDt": "20250131",
        "rmrkCntn": "청구",
        "splCft": "1000",
        "txamt": "100",
        "sumAmt": "1100",
        "isnDt": "",
    }
    assert payload["sncClInfrBizSVO"]["recApeClCd"] == "02"


@pytest.mark.asyncio
async def test_issue_wires_c04_signer_c05_and_readback(monkeypatch):
    def fake_sign(xml, material):
        assert "TaxInvoice" in xml
        assert material is FakeMaterial
        return "<TaxInvoice><signed/></TaxInvoice>"

    monkeypatch.setattr("hometax_login.issuance_backend.sign_invoice_xml", fake_sign)
    client = FakeClient(
        {
            "ATEETBAA002C04": xml_response(),
            "ATEETBAA002C05": ok(response={"apprvNo": NEW_APPROVAL}),
            "ATEETBDA001R02": detail_response(NEW_APPROVAL),
        }
    )
    prepared = await InvoiceBackend(client).preview_issue(issue_request())

    approvals = await InvoiceBackend(client).issue(prepared, FakeMaterial)

    assert approvals == [NEW_APPROVAL]
    actions = [call[2]["actionId"] for call in client.calls]
    assert actions == [
        "ATEETBAA002R04",
        "ATEETBAA002R04",
        "ATEETBAA002C04",
        "ATEETBAA002C05",
        "ATEETBDA001R02",
    ]
    commit = next(call[3] for call in client.calls if call[2]["actionId"] == "ATEETBAA002C05")
    assert commit["brwerFg"] == "N"
    assert commit["etan"] == "202501021234567890123456"
    assert commit["ognXML"] == invoice_xml()
    assert commit["xmlCntn"] == "<TaxInvoice><signed/></TaxInvoice>"
    assert commit["sSignData"] == "<TaxInvoice><signed/></TaxInvoice>"
    assert commit["trnsXmlCntn"] == "transfer"
    assert commit["rValue"] == "dmlkLXJhbmRvbQ=="
    assert commit["signCert"] == "Y2VydGlmaWNhdGUtZGVy"
    assert commit["tteetbm101DVO"] == {"header": "from-c04"}
    assert commit["tteetbd102DVOList"] == [{"line": 1}]


@pytest.mark.asyncio
async def test_issue_correction_signs_both_c03_xml_documents(monkeypatch):
    signed = []

    def fake_sign(xml, _material):
        signed.append(xml)
        return f"<TaxInvoice>signed-{len(signed)}</TaxInvoice>"

    def detail_for_issue_readback(payload):
        number = payload["etxivIsnBrkdTermDVOPrmt"]["etan"]
        if number == "202501031234567890123456":
            result = detail_response(number, supply="-1000", tax="-100", total="-1100")
            result["lsatInfrBizSVOList"][0]["lsatUtprc"] = "-1000"
        else:
            result = detail_response(number)
        result["etxivIsnBrkdTermDVO"].update(etxivMdfRsnCd="01", tfstEtan=APPROVAL)
        return result

    monkeypatch.setattr("hometax_login.issuance_backend.sign_invoice_xml", fake_sign)
    client = FakeClient(
        {
            "ATEETBDA001R02": lambda payload: (
                detail_response(APPROVAL)
                if payload["etxivIsnBrkdTermDVOPrmt"]["etan"] == APPROVAL
                else detail_for_issue_readback(payload)
            ),
            "ATEETBAA003C03": ok(
                response={
                    "etan": "202501031234567890123456",
                    "xmlCntn": invoice_xml(
                        issue_id="202501031234567890123456",
                        supply=-1000,
                        tax=-100,
                        total=-1100,
                        type_code="0201",
                        amendment="01",
                        original=APPROVAL,
                    ),
                    "trnsXmlCntn": "transfer-negative",
                    "etan2": "202501041234567890123456",
                    "xmlCntn2": invoice_xml(
                        issue_id="202501041234567890123456",
                        type_code="0201",
                        amendment="01",
                        original=APPROVAL,
                    ),
                    "trnsXmlCntn2": "transfer-replacement",
                },
                tteetbm101DVO={"header": "c03"},
                tteetbd102DVOList=[{"line": "negative"}, {"line": "replacement"}],
                tteetbd103DVOList=[{"party": "supplier"}],
                tteetbd104DVOList=[{"party": "recipient"}],
                tteetbm101DVO2={"header": "c03-2"},
                tteetbd102DVOList2=[{"line": "replacement"}],
                tteetbd103DVOList2=[{"party": "supplier-2"}],
                tteetbd104DVOList2=[{"party": "recipient-2"}],
            ),
            "ATEETBAA003C04": ok(
                tteetbl109DVOList=[
                    {"apprvNo": "202501031234567890123456"},
                    {"apprvNo": "202501041234567890123456"},
                ]
            ),
        }
    )
    backend = InvoiceBackend(client)
    prepared = await backend.preview_correct(
        APPROVAL,
        InvoiceCorrectionRequest(**issue_request().model_dump(), reason="clerical_error"),
    )

    approvals = await backend.issue(prepared, FakeMaterial)

    assert approvals == [
        "202501031234567890123456",
        "202501041234567890123456",
    ]
    assert len(signed) == 2
    assert "<OriginalIssueID>" in signed[0]
    assert "<OriginalIssueID>" in signed[1]
    c03_payload = client.calls[2][3]
    assert c03_payload["userDn"] == "CN=material"
    c04_payload = client.calls[3][3]
    assert c04_payload["xmlCntn"] == "<TaxInvoice>signed-1</TaxInvoice>"
    assert c04_payload["xmlCntn2"] == "<TaxInvoice>signed-2</TaxInvoice>"
    assert c04_payload["ognXML"] == invoice_xml(
        issue_id="202501031234567890123456",
        supply=-1000,
        tax=-100,
        total=-1100,
        type_code="0201",
        amendment="01",
        original=APPROVAL,
    )
    assert c04_payload["ognXML2"] == invoice_xml(
        issue_id="202501041234567890123456",
        type_code="0201",
        amendment="01",
        original=APPROVAL,
    )
    assert c04_payload["tteetbd102DVOList"] == [
        {"line": "negative"},
        {"line": "replacement"},
    ]


@pytest.mark.asyncio
async def test_issue_rejects_unverified_wire_encoding_before_write():
    client = FakeClient(encoding=None)
    prepared = await InvoiceBackend(client).preview_issue(issue_request())

    with pytest.raises(LoginError) as caught:
        await InvoiceBackend(client).issue(prepared, FakeMaterial)

    assert caught.value.code == "INVOICE_WIRE_ENCODING_REQUIRED"
    assert [call[2]["actionId"] for call in client.calls] == ["ATEETBAA002R04"]


@pytest.mark.asyncio
async def test_issue_rejects_tampered_c04_xml_before_commit(monkeypatch):
    monkeypatch.setattr(
        "hometax_login.issuance_backend.sign_invoice_xml",
        lambda _xml, _material: "<TaxInvoice/>",
    )
    client = FakeClient(
        {"ATEETBAA002C04": xml_response(xmlCntn=invoice_xml(recipient="0000000000"))}
    )
    prepared = await InvoiceBackend(client).preview_issue(issue_request())

    with pytest.raises(LoginError) as caught:
        await InvoiceBackend(client).issue(prepared, FakeMaterial)

    assert caught.value.code == "INVOICE_XML_MISMATCH"
    assert [call[2]["actionId"] for call in client.calls] == [
        "ATEETBAA002R04",
        "ATEETBAA002R04",
        "ATEETBAA002C04",
    ]


@pytest.mark.asyncio
async def test_preview_correct_and_cancel_use_sales_original_and_expected_codes():
    client = FakeClient({"ATEETBDA001R02": detail_response(APPROVAL)})
    backend = InvoiceBackend(client)

    corrected = await backend.preview_correct(
        APPROVAL,
        InvoiceCorrectionRequest(**issue_request().model_dump(), reason="clerical_error"),
    )
    cancelled = await backend.preview_cancel(
        APPROVAL,
        InvoiceCancelRequest(
            client_reference=uuid4(),
            reason="contract_cancellation",
            written_date=date(2025, 1, 31),
            remarks="계약해제",
        ),
    )

    assert corrected.operation == "correct"
    assert len(corrected.documents) == 2
    assert corrected.documents[0].supply_amount == -1000
    assert corrected.documents[1].supply_amount == 1000
    assert corrected.payload["xml_action"] == "ATEETBAA003C03"
    assert corrected.payload["commit_action"] == "ATEETBAA003C04"
    assert corrected.payload["xml_payload"]["request"]["isnScrnClCd"] == "10"
    assert corrected.payload["screen_id"] == "UTEETBAA44"
    assert "splrInfrBIzSVO2" in corrected.payload["xml_payload"]
    assert corrected.payload["xml_payload"]["request"]["oldAprvNo"] == APPROVAL
    assert corrected.payload["xml_payload"]["request"]["tfstEtan"] == APPROVAL
    assert cancelled.operation == "cancel"
    assert len(cancelled.documents) == 1
    assert cancelled.documents[0].correction_reason_code == "04"
    assert cancelled.payload["xml_payload"]["request"]["isnScrnClCd"] == "10"
    assert cancelled.payload["screen_id"] == "UTEETBAA48"


@pytest.mark.asyncio
async def test_preview_cancel_rejects_purchase_original_without_detail_call():
    client = FakeClient()
    client.invoices.references[APPROVAL] = (  # type: ignore[index]
        "purchases",
        client.invoices.references[APPROVAL][1],
    )

    with pytest.raises(LoginError) as caught:
        await InvoiceBackend(client).preview_cancel(
            APPROVAL,
            InvoiceCancelRequest(
                client_reference=uuid4(),
                reason="duplicate_issue",
                written_date=date(2025, 1, 31),
            ),
        )

    assert caught.value.code == "INVOICE_SALES_ONLY"
    assert client.calls == []


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("etxivIsnBrkdTermDVO", "recApeClCd", "01"),
        ("etxivIsnBrkdTermDVO", "splrTnmNm", "다른 공급자"),
        ("etxivIsnBrkdTermDVO", "dmnrPfbAdr", "다른 주소"),
        ("etxivIsnBrkdTermDVO", "dmnrMchrgEmlAdr", "other@example.test"),
        ("etxivIsnBrkdTermDVO", "etxivSq1RmrkCntn", "다른 비고"),
        ("line", "lsatNm", "다른 품목"),
        ("line", "lsatQty", "2"),
        ("line", "lsatUtprc", "2000"),
        ("line", "lsatRszeNm", "다른 규격"),
        ("line", "lsatRmrkCntn", "다른 품목 비고"),
    ],
)
async def test_readback_must_match_all_confirmed_fields(section, key, value):
    response = detail_response(NEW_APPROVAL)
    target = response["lsatInfrBizSVOList"][0] if section == "line" else response[section]
    target[key] = value
    backend = InvoiceBackend(FakeClient({"ATEETBDA001R02": response}))
    prepared = await backend.preview_issue(issue_request())
    with pytest.raises(LoginError) as caught:
        await backend._verify_readback(prepared, [NEW_APPROVAL])
    assert caught.value.code == "INVOICE_READBACK_MISMATCH"


@pytest.mark.parametrize(
    "reason,original",
    [
        ("04", APPROVAL),
        ("01", "202501051234567890123456"),
    ],
)
async def test_readback_must_match_correction_reason_and_original(reason, original):
    client = FakeClient({"ATEETBDA001R02": detail_response(APPROVAL)})
    backend = InvoiceBackend(client)
    prepared = await backend.preview_correct(
        APPROVAL, InvoiceCorrectionRequest(**issue_request().model_dump(), reason="clerical_error")
    )
    response = detail_response(NEW_APPROVAL, supply="-1000", tax="-100", total="-1100")
    response["etxivIsnBrkdTermDVO"].update(etxivMdfRsnCd=reason, tfstEtan=original)
    response["lsatInfrBizSVOList"][0]["lsatUtprc"] = "-1000"
    client.responses["ATEETBDA001R02"] = response
    with pytest.raises(LoginError) as caught:
        await backend._verify_readback(prepared, [NEW_APPROVAL, "202501041234567890123456"])
    assert caught.value.code == "INVOICE_READBACK_MISMATCH"


@pytest.mark.parametrize(
    "response",
    [
        ok(),
        ok(sptxpTxivIsnClCd="XX"),
        ok(sptxpTxivIsnClCd="ZZ", isnImpTxfrBmanYn="Y"),
        ok(sptxpTxivIsnClCd="ZZ", crpCntcOficYn="Y"),
    ],
)
async def test_preview_checks_server_issuance_eligibility(response):
    client = FakeClient({"ATEETBAA002R04": response})
    with pytest.raises(LoginError):
        await InvoiceBackend(client).preview_issue(issue_request())
    assert not any("C0" in call[2]["actionId"] for call in client.calls)
