"""HomeTax registered-counterparty write adapter.

All URLs and action IDs are fixed from the public WebSquare screens.  The
manager owns confirmation, journaling, and post-write verification; this module
does not retry writes.
"""

from __future__ import annotations

import re
from typing import Literal

from .counterparty_changes import (
    BUSINESS_FIELDS,
    CONTACT_FIELDS,
    CounterpartySnapshot,
    fingerprint,
)
from .errors import LoginError
from .invoices import ET_BASE, CounterpartyQuery, changed, integer

ROLE_BY_CODE = {"01": "primary_contact", "02": "secondary_contact"}
CODE_BY_ROLE = {value: key for key, value in ROLE_BY_CODE.items()}


def _text(value) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return value
    raise changed()


def _identifier(value) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return value
    if type(value) is int:
        return str(value)
    raise changed()


def _business_number(value) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"(?:[0-9]{10}|[0-9]{3}-[0-9]{2}-[0-9]{5})", value):
        return value.replace("-", "")
    return None


def _row_business_number(row: dict) -> str | None:
    visible = row.get("txprDscmNoEncCntnView")
    if visible not in (None, ""):
        return _business_number(visible)
    return _business_number(row.get("txprDscmNoEncCntn"))


def _branch(value) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text in {"", "0", "0000"}:
        return ""
    if re.fullmatch(r"[0-9]+", text):
        return str(int(text)).zfill(4)
    raise changed()


def _upstream_branch(value) -> str:
    return _branch(value) or "0"


def _ok(data: dict, code: str, message: str):
    result = data.get("resultMsg")
    if (
        not isinstance(result, dict)
        or result.get("result") != "S"
        or result.get("errorMsg")
        or result.get("errorCd")
    ):
        raise LoginError(code, message)


def _counterparty_data(row: dict, contacts: dict[str, dict]) -> dict:
    return {public: _text(row.get(upstream)) for public, upstream in BUSINESS_FIELDS.items()} | {
        role: {
            public: _text(contacts[role].get(upstream))
            for public, upstream in CONTACT_FIELDS.items()
        }
        for role in ("primary_contact", "secondary_contact")
    }


class CounterpartyBackend:
    def __init__(self, invoices):
        self.invoices = invoices
        self.client = invoices.client
        self.last_scope: tuple[str, str] | None = None

    async def _scope(self) -> tuple[str, str, str]:
        owner_tin, company = await self.invoices._business()
        owner_branch = _branch(self.invoices.business_mpb_no)
        self.last_scope = (owner_tin, owner_branch)
        return owner_tin, owner_branch, company

    async def current(self, number: str, branch: str) -> CounterpartySnapshot | None:
        target_number, target_branch, company, matches = await self._lookup_rows(number, branch)
        owner_tin = self.invoices.business_tin
        owner_branch = _branch(self.invoices.business_mpb_no)
        if not isinstance(owner_tin, str) or not owner_tin:
            raise changed()
        if not matches:
            return None
        if len(matches) != 1:
            raise LoginError(
                "COUNTERPARTY_LOOKUP_AMBIGUOUS",
                "동일한 사업자등록번호의 거래처가 여러 건입니다.",
                409,
            )
        row = matches[0]
        recipient_tin = _identifier(row.get("dmnrTin"))
        row_owner = _identifier(row.get("splrTin"))
        row_owner_branch = _identifier(row.get("splrMpbNo"))
        if (
            not recipient_tin
            or (row_owner and row_owner != owner_tin)
            or (row_owner_branch and _branch(row_owner_branch) != owner_branch)
        ):
            raise changed()
        lookup_request = {
            "splrTin": owner_tin,
            "splrMpbNo": _upstream_branch(owner_branch),
            "dmnrTin": recipient_tin,
            "dmnrMpbNo": _upstream_branch(target_branch),
        }
        details = await self._detail(lookup_request)
        contacts, contact_ids = self._contacts(details, lookup_request)
        data = _counterparty_data(row, contacts)
        return CounterpartySnapshot(
            company_name=company,
            owner_tin=owner_tin,
            owner_branch=owner_branch,
            business_number=target_number,
            branch_number=target_branch,
            recipient_tin=recipient_tin,
            data=data,
            contact_ids=contact_ids,
            revision=fingerprint([data, contact_ids, lookup_request]),
            lookup_request=lookup_request,
        )

    async def new_target(self, number: str, branch: str) -> CounterpartySnapshot:
        target_number = _business_number(number)
        if target_number is None:
            raise LoginError(
                "INVALID_COUNTERPARTY_TARGET",
                "거래처 사업자등록번호를 확인하세요.",
                422,
            )
        if _branch(branch):
            raise LoginError(
                "COUNTERPARTY_BRANCH_UNSUPPORTED",
                "신규 종사업장 거래처 등록은 자동 선택 없이 처리할 수 없습니다.",
                409,
            )
        _number, _branch_number, _company, existing = await self._lookup_rows(target_number, "")
        if existing:
            raise LoginError("COUNTERPARTY_ALREADY_EXISTS", "이미 등록된 거래처입니다.", 409)
        owner_tin, owner_branch, company = await self._scope()
        branch_probe = await self._action(
            ET_BASE,
            "ATTABZAA001R05",
            "UTEETBAB04",
            {"tin": "", "bsno": target_number},
        )
        _ok(branch_probe, "COUNTERPARTY_LOOKUP_FAILED", "거래처 사업자번호 확인에 실패했습니다.")
        if self._has_branch_units(branch_probe):
            raise LoginError(
                "COUNTERPARTY_BRANCH_UNSUPPORTED",
                "종사업장 거래처는 홈택스 선택 팝업 확인이 필요합니다.",
                409,
            )
        taxpayer = await self._action(
            ET_BASE,
            "ATTABZAA001R01",
            "UTEETBAB04",
            {
                "tin": "",
                "txprClsfCd": "99",
                "txprDscmNo": target_number,
                "txprDscmNoClCd": "",
                "txprDscmDt": "",
                "searchOrder": "",
                "outDes": "txprInfoDes",
                "txprNm": "",
                "crpTin": "",
                "mntgTxprIcldYn": "",
                "resnoAltHstrInqrYn": "",
                "resnoAltHstrInqrBaseDtm": "",
                "sameBmanInqrYn": "N",
                "rpnBmanRetrYn": "N",
            },
        )
        _ok(taxpayer, "COUNTERPARTY_LOOKUP_FAILED", "거래처 사업자번호 확인에 실패했습니다.")
        info = taxpayer.get("bmanCrpNtplDVO") or taxpayer.get("txprInfoDes")
        if not isinstance(info, dict):
            raise changed()
        recipient_tin = _identifier(info.get("tin"))
        if not recipient_tin:
            raise LoginError("COUNTERPARTY_LOOKUP_FAILED", "유효한 사업자등록번호가 아닙니다.", 422)
        if info.get("txprStatCd") == "23":
            raise LoginError("COUNTERPARTY_CLOSED", "폐업된 사업자입니다.", 409)
        dmnr_code = self._counterparty_kind(_text(info.get("txprClsfCd")))
        if dmnr_code != "01":
            raise LoginError(
                "COUNTERPARTY_BUSINESS_REQUIRED",
                "사업자등록번호 거래처만 자동 등록할 수 있습니다.",
                422,
            )
        lookup_request = {
            "splrTin": owner_tin,
            "splrMpbNo": _upstream_branch(owner_branch),
            "dmnrTin": recipient_tin,
            "dmnrMpbNo": "0",
            "txprDscmNoEncCntn": _text(info.get("txprDscmNoEncCntn")) or target_number,
            "txprFg": "",
            "clplcCnt": "",
            "dmnrBsnoClCd": dmnr_code,
        }
        preflight = await self._action(ET_BASE, "ATEETBAA001R04", "UTEETBAB04", lookup_request)
        _ok(preflight, "COUNTERPARTY_LOOKUP_FAILED", "거래처 등록 전 확인에 실패했습니다.")
        response = preflight.get("response")
        if response is None and "clplcCnt" in preflight:
            response = preflight  # Live JSON uses flattened search output.
        if not isinstance(response, dict):
            raise changed()
        clplc_count = integer(response.get("clplcCnt"))
        if clplc_count < 0:
            raise changed()
        if clplc_count > 0:
            raise LoginError("COUNTERPARTY_ALREADY_EXISTS", "이미 등록된 거래처입니다.", 409)
        lookup_request["clplcCnt"] = "0"
        return CounterpartySnapshot(
            company_name=company,
            owner_tin=owner_tin,
            owner_branch=owner_branch,
            business_number=target_number,
            branch_number="",
            recipient_tin=recipient_tin,
            data={},
            contact_ids={},
            revision=fingerprint([branch_probe, taxpayer, preflight, lookup_request]),
            lookup_request=lookup_request,
        )

    @staticmethod
    def _has_branch_units(data: dict) -> bool:
        units = data.get("mpbCtlDVOList")
        if isinstance(units, list):
            if any(not isinstance(row, dict) for row in units):
                raise changed()
            if units:
                return True
            page = data.get("pageInfoVO")
            if not isinstance(page, dict) or integer(page.get("totalCount")) != 0:
                raise changed()
            return False
        unit = data.get("mpbCtlDVO")
        if isinstance(unit, dict) and "count" in unit:
            count = unit.get("count")
            return count not in (None, "", "0", 0)
        if "mpbCtlDVOList" in data or "mpbCtlDVO" in data:
            raise changed()
        raise changed()

    async def _lookup_rows(self, number: str, branch: str) -> tuple[str, str, str, list[dict]]:
        target_number = _business_number(number)
        if target_number is None:
            raise LoginError(
                "INVALID_COUNTERPARTY_TARGET",
                "거래처 사업자등록번호를 확인하세요.",
                422,
            )
        target_branch = _branch(branch)
        company, rows, total = await self.invoices._counterparty_rows(
            CounterpartyQuery(business_number=target_number, page=1, page_size=50)
        )
        self.last_scope = (self.invoices.business_tin, _branch(self.invoices.business_mpb_no))
        if total > 50:
            raise LoginError(
                "COUNTERPARTY_LOOKUP_AMBIGUOUS",
                "거래처 검색 결과가 너무 많습니다. 홈택스에서 직접 확인하세요.",
                409,
            )
        numbered_rows = []
        for row in rows:
            if not isinstance(row, dict):
                raise changed()
            row_number = _row_business_number(row)
            if row_number is None:
                raise changed()
            numbered_rows.append((row, row_number, _branch(row.get("dmnrMpbNo"))))
        matches = [
            row
            for row, row_number, row_branch in numbered_rows
            if row_number == target_number and row_branch == target_branch
        ]
        if (
            not matches
            and rows
            and target_number not in {row_number for _, row_number, _ in numbered_rows}
        ):
            raise changed()
        return target_number, target_branch, company, matches

    async def write(
        self,
        operation: Literal["create", "update", "delete"],
        snapshot: CounterpartySnapshot,
        desired: dict | None,
    ) -> None:
        owner_tin, owner_branch, _company = await self._scope()
        if (owner_tin, owner_branch) != (snapshot.owner_tin, snapshot.owner_branch):
            raise LoginError(
                "CHANGE_STALE",
                "사업자 정보가 변경됐습니다. 다시 미리보기 하세요.",
                409,
            )
        if operation == "create":
            if desired is None:
                raise changed()
            payload = self._save_payload(snapshot, desired, create=True)
            data = await self._action(ET_BASE, "ATEETBAA001C01", "UTEETBAB04", payload)
            _ok(data, "COUNTERPARTY_WRITE_FAILED", "홈택스 거래처 등록에 실패했습니다.")
        elif operation == "update":
            if desired is None:
                raise changed()
            payload = self._save_payload(snapshot, desired, create=False)
            data = await self._action(ET_BASE, "ATEETBAE001U02", "UTEETBAB03", payload)
            _ok(data, "COUNTERPARTY_WRITE_FAILED", "홈택스 거래처 수정에 실패했습니다.")
        elif operation == "delete":
            data = await self._action(
                ET_BASE,
                "ATEETBAE001D04",
                "UTEETBAB03",
                {
                    "dmnrTin": snapshot.recipient_tin,
                    "dmnrMpbNo": _upstream_branch(snapshot.branch_number),
                },
            )
            _ok(data, "COUNTERPARTY_WRITE_FAILED", "홈택스 거래처 삭제에 실패했습니다.")
        else:
            raise changed()

    async def _detail(self, lookup_request: dict) -> list[dict]:
        data = await self._action(ET_BASE, "ATEETBAE001R07", "UTEETBAB03", lookup_request)
        _ok(data, "COUNTERPARTY_DETAIL_FAILED", "홈택스 거래처 상세 조회에 실패했습니다.")
        rows = data.get("myClplcDtlDVO")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise changed()
        return rows

    def _contacts(
        self, rows: list[dict], lookup_request: dict
    ) -> tuple[dict[str, dict], dict[str, str]]:
        contacts = {}
        contact_ids = {}
        for row in rows:
            if (
                _identifier(row.get("splrTin")) != lookup_request["splrTin"]
                or _branch(row.get("splrMpbNo")) != _branch(lookup_request["splrMpbNo"])
                or _identifier(row.get("dmnrTin")) != lookup_request["dmnrTin"]
                or _branch(row.get("dmnrMpbNo")) != _branch(lookup_request["dmnrMpbNo"])
            ):
                raise changed()
            role = ROLE_BY_CODE.get(row.get("chrgHwfClCd"))
            chrg_sn = _identifier(row.get("chrgSn"))
            if role is None or role in contacts:
                raise LoginError(
                    "COUNTERPARTY_CONTACT_UNSUPPORTED",
                    "홈택스 담당자 정보 구조가 예상과 달라 자동 변경할 수 없습니다.",
                    409,
                )
            contacts[role] = row
            contact_ids[role] = chrg_sn
        for role in ("primary_contact", "secondary_contact"):
            contacts.setdefault(role, {})
            contact_ids.setdefault(role, "")
        return contacts, contact_ids

    def _save_payload(self, snapshot: CounterpartySnapshot, desired: dict, *, create: bool) -> dict:
        business = {
            "splrTin": snapshot.owner_tin,
            "dmnrTin": snapshot.recipient_tin,
            "splrMpbNo": _upstream_branch(snapshot.owner_branch),
            "dmnrMpbNo": _upstream_branch(snapshot.branch_number),
            **{
                upstream: _text(desired.get(public)) for public, upstream in BUSINESS_FIELDS.items()
            },
        }
        if create:
            business["dmnrBsnoClCd"] = snapshot.lookup_request.get("dmnrBsnoClCd", "01")
            business["mateStatClCd"] = "01"
        contacts = [
            self._contact_payload(snapshot, desired, "primary_contact", create=create),
            self._contact_payload(snapshot, desired, "secondary_contact", create=create),
        ]
        if create:
            return {
                **{
                    key: snapshot.lookup_request.get(key, "")
                    for key in (
                        "splrTin",
                        "splrMpbNo",
                        "dmnrTin",
                        "dmnrMpbNo",
                        "txprDscmNoEncCntn",
                        "txprFg",
                        "clplcCnt",
                    )
                },
                "tteetbd105DVO": business,
                "tteetbd106DVO": contacts,
            }
        return {"tteetbd105DVO": business, "tteetbd106DVO": contacts}

    def _contact_payload(
        self, snapshot: CounterpartySnapshot, desired: dict, role: str, *, create: bool
    ) -> dict:
        contact = desired.get(role)
        if not isinstance(contact, dict):
            raise changed()
        row = {
            "splrTin": snapshot.owner_tin,
            "dmnrTin": snapshot.recipient_tin,
            "splrMpbNo": _upstream_branch(snapshot.owner_branch),
            "dmnrMpbNo": _upstream_branch(snapshot.branch_number),
            "dmnrBsnoClCd": snapshot.lookup_request.get("dmnrBsnoClCd", "01") if create else "02",
            "mateStatClCd": "01",
            "chrgHwfClCd": CODE_BY_ROLE[role],
            **{upstream: _text(contact.get(public)) for public, upstream in CONTACT_FIELDS.items()},
        }
        if not create:
            if role not in snapshot.contact_ids:
                raise changed()
            row["chrgSn"] = snapshot.contact_ids[role]
        return row

    async def _action(self, base: str, action_id: str, screen_id: str, payload: dict) -> dict:
        return self.client._json(
            await self.client._request(
                "POST",
                base + "/wqAction.do",
                params={
                    "actionId": action_id,
                    "screenId": screen_id,
                    "popupYn": "false",
                    "realScreenId": "",
                },
                json=payload,
            )
        )

    @staticmethod
    def _counterparty_kind(txpr_clsf_cd: str) -> str:
        if txpr_clsf_cd == "01":
            return "02"
        if txpr_clsf_cd == "02":
            return "01"
        if txpr_clsf_cd == "03":
            return "03"
        if txpr_clsf_cd == "04":
            return "02"
        return "ZZ"
