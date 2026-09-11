import json

import pytest

from hometax_login.counterparty_backend import CounterpartyBackend
from hometax_login.counterparty_changes import CounterpartySnapshot
from hometax_login.errors import LoginError

OWNER_TIN = "1112223334"
OWNER_BRANCH = ""
COMPANY = "예시공급사"
RECIPIENT_TIN = "9998887776"
BUSINESS_NUMBER = "1234567890"


@pytest.mark.parametrize(
    "data",
    [
        {"mpbCtlDVOList": []},
        {"mpbCtlDVOList": [], "pageInfoVO": {"totalCount": 1}},
        {"mpbCtlDVO": {}},
    ],
)
def test_unit_probe_does_not_turn_missing_or_inconsistent_count_into_zero(data):
    with pytest.raises(LoginError):
        CounterpartyBackend._has_branch_units(data)


def ok(**values):
    return {"resultMsg": {"result": "S", "errorCd": "", "errorMsg": ""}, **values}


def list_row(**overrides):
    row = {
        "splrTin": OWNER_TIN,
        "splrMpbNo": "0",
        "dmnrTin": RECIPIENT_TIN,
        "dmnrMpbNo": "0",
        "txprDscmNoEncCntnView": "123-45-67890",
        "txprDscmNoEncCntn": BUSINESS_NUMBER,
        "tnmNm": "예시거래처",
        "rprsFnm": "예시대표",
        "pfbAdr": "서울",
        "bcNm": "서비스",
        "itmNm": "교육",
    }
    row.update(overrides)
    return row


def detail_rows(**overrides):
    primary = {
        "splrTin": OWNER_TIN,
        "splrMpbNo": "0",
        "dmnrTin": RECIPIENT_TIN,
        "dmnrMpbNo": "0",
        "chrgSn": "1",
        "chrgHwfClCd": "01",
        "dmnrBsnoClCd": "02",
        "mateStatClCd": "01",
        "chrgDprtNm": "정산",
        "chrgNm": "주담당",
        "chrgTelno": "021234567",
        "chrgMpno": "01012345678",
        "chrgFaxno": "",
        "chrgEmlAdr": "main@example.test",
        "chrgRmrkCntn": "주",
    }
    secondary = {
        **primary,
        "chrgSn": "2",
        "chrgHwfClCd": "02",
        "chrgDprtNm": "",
        "chrgNm": "",
        "chrgTelno": "",
        "chrgMpno": "",
        "chrgEmlAdr": "",
        "chrgRmrkCntn": "",
    }
    primary.update(overrides.pop("primary", {}))
    secondary.update(overrides.pop("secondary", {}))
    return [primary, secondary]


class FakeClient:
    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

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
    def __init__(self, *, rows=None, total=None, client=None, branch=OWNER_BRANCH):
        self.client = client or FakeClient()
        self.business_tin = None
        self.business_mpb_no = branch
        self.rows = [] if rows is None else rows
        self.total = len(self.rows) if total is None else total
        self.queries = []

    async def _business(self):
        self.business_tin = OWNER_TIN
        return OWNER_TIN, COMPANY

    async def _counterparty_rows(self, query):
        await self._business()
        self.queries.append(query)
        return COMPANY, self.rows, self.total


def backend_for_current(rows=None, responses=None, total=None):
    client = FakeClient(
        responses={
            "ATEETBAE001R07": ok(myClplcDtlDVO=detail_rows()),
            **(responses or {}),
        }
    )
    return CounterpartyBackend(FakeInvoices(rows=rows or [list_row()], total=total, client=client))


def desired():
    return {
        "name": "새거래처",
        "representative_name": "새대표",
        "address": "부산",
        "business_type": "제조",
        "business_item": "부품",
        "primary_contact": {
            "department": "회계",
            "name": "김주",
            "telephone": "0211112222",
            "mobile": "01011112222",
            "fax": "",
            "email": "p@example.test",
            "remarks": "p",
        },
        "secondary_contact": {
            "department": "영업",
            "name": "이부",
            "telephone": "",
            "mobile": "",
            "fax": "",
            "email": "",
            "remarks": "",
        },
    }


@pytest.mark.asyncio
async def test_current_filters_exact_business_number_branch_reads_detail_and_preserves_contacts():
    backend = backend_for_current(
        rows=[
            list_row(txprDscmNoEncCntnView="000-00-00000"),
            list_row(dmnrMpbNo="0000"),
        ]
    )

    snap = await backend.current(BUSINESS_NUMBER, "")

    assert snap.business_number == BUSINESS_NUMBER
    assert snap.branch_number == ""
    assert snap.recipient_tin == RECIPIENT_TIN
    assert snap.contact_ids == {"primary_contact": "1", "secondary_contact": "2"}
    assert snap.data["primary_contact"]["email"] == "main@example.test"
    assert backend.last_scope == (OWNER_TIN, "")
    assert backend.invoices.queries[0].business_number == BUSINESS_NUMBER
    action = backend.client.calls[0]
    assert action[2]["actionId"] == "ATEETBAE001R07"
    assert action[3] == {
        "splrTin": OWNER_TIN,
        "splrMpbNo": "0",
        "dmnrTin": RECIPIENT_TIN,
        "dmnrMpbNo": "0",
    }


@pytest.mark.asyncio
async def test_current_rejects_too_many_or_duplicate_matching_rows():
    backend = backend_for_current(total=51)
    with pytest.raises(LoginError) as too_many:
        await backend.current(BUSINESS_NUMBER, "")
    assert too_many.value.code == "COUNTERPARTY_LOOKUP_AMBIGUOUS"

    duplicate = backend_for_current(rows=[list_row(), list_row()])
    with pytest.raises(LoginError) as caught:
        await duplicate.current(BUSINESS_NUMBER, "")
    assert caught.value.code == "COUNTERPARTY_LOOKUP_AMBIGUOUS"


@pytest.mark.asyncio
async def test_current_rejects_masked_or_mismatched_search_rows_but_allows_other_branch():
    masked = backend_for_current(rows=[list_row(txprDscmNoEncCntnView="***-**-67890")])
    with pytest.raises(LoginError) as masked_error:
        await masked.current(BUSINESS_NUMBER, "")
    assert masked_error.value.code == "INVOICE_RESPONSE_CHANGED"

    mismatched = backend_for_current(rows=[list_row(txprDscmNoEncCntnView="111-22-33333")])
    with pytest.raises(LoginError) as mismatch_error:
        await mismatched.current(BUSINESS_NUMBER, "")
    assert mismatch_error.value.code == "INVOICE_RESPONSE_CHANGED"

    other_branch = backend_for_current(rows=[list_row(dmnrMpbNo="0001")])
    assert await other_branch.current(BUSINESS_NUMBER, "") is None


@pytest.mark.asyncio
async def test_current_rejects_unknown_or_duplicate_contact_roles():
    cases = [
        detail_rows(primary={"chrgHwfClCd": "03"}),
        detail_rows(secondary={"chrgHwfClCd": "01"}),
    ]
    for rows in cases:
        backend = backend_for_current(responses={"ATEETBAE001R07": ok(myClplcDtlDVO=rows)})
        with pytest.raises(LoginError) as caught:
            await backend.current(BUSINESS_NUMBER, "")
        assert caught.value.code == "COUNTERPARTY_CONTACT_UNSUPPORTED"


@pytest.mark.asyncio
async def test_current_allows_one_or_zero_contacts_as_blank_roles():
    one_contact = backend_for_current(
        responses={"ATEETBAE001R07": ok(myClplcDtlDVO=detail_rows()[:1])}
    )
    one = await one_contact.current(BUSINESS_NUMBER, "")
    assert one.contact_ids == {"primary_contact": "1", "secondary_contact": ""}
    assert one.data["primary_contact"]["name"] == "주담당"
    assert one.data["secondary_contact"] == {
        "department": "",
        "name": "",
        "telephone": "",
        "mobile": "",
        "fax": "",
        "email": "",
        "remarks": "",
    }

    no_contacts = backend_for_current(responses={"ATEETBAE001R07": ok(myClplcDtlDVO=[])})
    empty = await no_contacts.current(BUSINESS_NUMBER, "")
    assert empty.contact_ids == {"primary_contact": "", "secondary_contact": ""}
    assert empty.data["primary_contact"]["name"] == ""
    assert empty.data["secondary_contact"]["name"] == ""


@pytest.mark.asyncio
async def test_current_accepts_integer_contact_ids_but_rejects_non_text_contact_values():
    numeric_id = backend_for_current(
        responses={"ATEETBAE001R07": ok(myClplcDtlDVO=detail_rows(primary={"chrgSn": 1}))}
    )
    assert (await numeric_id.current(BUSINESS_NUMBER, "")).contact_ids["primary_contact"] == "1"

    bad_contact = backend_for_current(
        responses={
            "ATEETBAE001R07": ok(
                myClplcDtlDVO=detail_rows(primary={"chrgEmlAdr": {"nested": "bad"}})
            )
        }
    )
    with pytest.raises(LoginError) as caught:
        await bad_contact.current(BUSINESS_NUMBER, "")
    assert caught.value.code == "INVOICE_RESPONSE_CHANGED"


@pytest.mark.asyncio
async def test_new_target_read_sequence_and_payloads_for_non_branch_counterparty():
    client = FakeClient(
        responses={
            "ATEETBAE001R07": ok(myClplcDtlDVO=detail_rows()),
            "ATTABZAA001R05": ok(mpbCtlDVO={"count": ""}),
            "ATTABZAA001R01": ok(
                bmanCrpNtplDVO={
                    "tin": RECIPIENT_TIN,
                    "txprClsfCd": "02",
                    "txprDscmNoEncCntn": BUSINESS_NUMBER,
                    "txprStatCd": "21",
                }
            ),
            "ATEETBAA001R04": ok(clplcCnt=0),
        }
    )
    backend = CounterpartyBackend(FakeInvoices(rows=[], client=client))

    snap = await backend.new_target(BUSINESS_NUMBER, "")

    assert snap.recipient_tin == RECIPIENT_TIN
    assert [call[2]["actionId"] for call in client.calls] == [
        "ATTABZAA001R05",
        "ATTABZAA001R01",
        "ATEETBAA001R04",
    ]
    assert client.calls[0][3] == {"tin": "", "bsno": BUSINESS_NUMBER}
    assert client.calls[1][3]["txprClsfCd"] == "99"
    assert client.calls[1][3]["txprDscmNo"] == BUSINESS_NUMBER
    assert client.calls[2][3]["dmnrTin"] == RECIPIENT_TIN
    assert client.calls[2][3]["splrTin"] == OWNER_TIN


@pytest.mark.asyncio
async def test_new_target_accepts_flat_r05_list_shape_observed_in_readonly_probe():
    client = FakeClient(
        responses={
            "ATTABZAA001R05": ok(
                inqGb="",
                txtnEndDt="",
                txtnStrtDt="",
                bsno=None,
                pageInfoVO={"pageNum": "1", "pageSize": "10", "totalCount": "0"},
                ymdgNm="",
                tin="",
                cmOgzCd="",
                adrClCd="",
                hofcYn="",
                mpbSn="",
                ofbDt="",
                mpbCtlDVOList=[],
            ),
            "ATTABZAA001R01": ok(
                bmanCrpNtplDVO={
                    "tin": RECIPIENT_TIN,
                    "txprClsfCd": "02",
                    "txprDscmNoEncCntn": BUSINESS_NUMBER,
                    "txprStatCd": "21",
                }
            ),
            "ATEETBAA001R04": ok(response={"clplcCnt": "0"}),
        }
    )
    backend = CounterpartyBackend(FakeInvoices(rows=[], client=client))

    snap = await backend.new_target(BUSINESS_NUMBER, "")

    assert snap.recipient_tin == RECIPIENT_TIN
    assert client.calls[0][3] == {"tin": "", "bsno": BUSINESS_NUMBER}


@pytest.mark.asyncio
async def test_new_target_rejects_branch_units_from_r05_list_shape():
    client = FakeClient(responses={"ATTABZAA001R05": ok(mpbCtlDVOList=[{"mpbSn": "0001"}])})
    backend = CounterpartyBackend(FakeInvoices(rows=[], client=client))

    with pytest.raises(LoginError) as caught:
        await backend.new_target(BUSINESS_NUMBER, "")

    assert caught.value.code == "COUNTERPARTY_BRANCH_UNSUPPORTED"
    assert [call[2]["actionId"] for call in client.calls] == ["ATTABZAA001R05"]


@pytest.mark.asyncio
async def test_new_target_rejects_existing_or_branch_ambiguous_without_write_actions():
    existing = backend_for_current()
    with pytest.raises(LoginError) as duplicate:
        await existing.new_target(BUSINESS_NUMBER, "")
    assert duplicate.value.code == "COUNTERPARTY_ALREADY_EXISTS"
    write_actions = {"ATEETBAA001C01", "ATEETBAE001U02", "ATEETBAE001D04"}
    assert all(call[2]["actionId"] not in write_actions for call in existing.client.calls)

    branched = CounterpartyBackend(FakeInvoices(rows=[]))
    with pytest.raises(LoginError) as branch:
        await branched.new_target(BUSINESS_NUMBER, "0001")
    assert branch.value.code == "COUNTERPARTY_BRANCH_UNSUPPORTED"


def snapshot():
    return CounterpartySnapshot(
        company_name=COMPANY,
        owner_tin=OWNER_TIN,
        owner_branch="",
        business_number=BUSINESS_NUMBER,
        branch_number="",
        recipient_tin=RECIPIENT_TIN,
        data={},
        contact_ids={"primary_contact": "1", "secondary_contact": "2"},
        revision="r1",
        lookup_request={
            "splrTin": OWNER_TIN,
            "splrMpbNo": "0",
            "dmnrTin": RECIPIENT_TIN,
            "dmnrMpbNo": "0",
            "txprDscmNoEncCntn": BUSINESS_NUMBER,
            "txprFg": "",
            "clplcCnt": "",
            "dmnrBsnoClCd": "01",
        },
    )


@pytest.mark.asyncio
async def test_write_create_update_delete_payloads_and_contact_id_preservation():
    client = FakeClient(
        responses={
            "ATEETBAA001C01": ok(response={}),
            "ATEETBAE001U02": ok(tteetbd105DVO={}),
            "ATEETBAE001D04": ok(response={}),
        }
    )
    backend = CounterpartyBackend(FakeInvoices(rows=[], client=client))
    snap = snapshot()

    await backend.write("create", snap, desired())
    await backend.write("update", snap, desired())
    await backend.write("delete", snap, None)

    create_payload = client.calls[0][3]
    assert client.calls[0][2]["actionId"] == "ATEETBAA001C01"
    assert create_payload["tteetbd105DVO"]["dmnrBsnoClCd"] == "01"
    assert "chrgSn" not in create_payload["tteetbd106DVO"][0]
    update_payload = client.calls[1][3]
    assert client.calls[1][2]["actionId"] == "ATEETBAE001U02"
    assert update_payload["tteetbd106DVO"][0]["chrgSn"] == "1"
    assert update_payload["tteetbd106DVO"][1]["chrgSn"] == "2"
    assert update_payload["tteetbd106DVO"][0]["dmnrBsnoClCd"] == "02"
    assert client.calls[2][2]["actionId"] == "ATEETBAE001D04"
    assert client.calls[2][3] == {"dmnrTin": RECIPIENT_TIN, "dmnrMpbNo": "0"}


@pytest.mark.asyncio
async def test_write_update_sends_blank_missing_secondary_id_but_rejects_missing_role_key():
    client = FakeClient(responses={"ATEETBAE001U02": ok(tteetbd105DVO={})})
    backend = CounterpartyBackend(FakeInvoices(rows=[], client=client))
    snap = snapshot()
    snap.contact_ids = {"primary_contact": "1", "secondary_contact": ""}

    await backend.write("update", snap, desired())

    contacts = client.calls[0][3]["tteetbd106DVO"]
    assert contacts[0]["chrgSn"] == "1"
    assert contacts[1]["chrgSn"] == ""

    missing_role = snapshot()
    missing_role.contact_ids = {"primary_contact": "1"}
    with pytest.raises(LoginError) as caught:
        await backend.write("update", missing_role, desired())
    assert caught.value.code == "INVOICE_RESPONSE_CHANGED"


@pytest.mark.asyncio
async def test_write_rejects_wrong_scope_or_upstream_error_without_retry():
    client = FakeClient(
        responses={
            "ATEETBAE001U02": {"resultMsg": {"result": "E", "errorCd": "X", "errorMsg": "bad"}}
        }
    )
    backend = CounterpartyBackend(FakeInvoices(rows=[], client=client, branch="0001"))
    with pytest.raises(LoginError) as stale:
        await backend.write("update", snapshot(), desired())
    assert stale.value.code == "CHANGE_STALE"
    assert client.calls == []

    backend = CounterpartyBackend(FakeInvoices(rows=[], client=client))
    with pytest.raises(LoginError) as failed:
        await backend.write("update", snapshot(), desired())
    assert failed.value.code == "COUNTERPARTY_WRITE_FAILED"
    assert [call[2]["actionId"] for call in client.calls] == ["ATEETBAE001U02"]
