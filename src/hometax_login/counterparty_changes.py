"""Preview/confirmation workflow for registered counterparties, never tax invoice issuance."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError, model_validator

from .errors import LoginError
from .invoices import changed
from .write_journal import WriteJournal

BUSINESS_FIELDS = {
    "name": "tnmNm",
    "representative_name": "rprsFnm",
    "address": "pfbAdr",
    "business_type": "bcNm",
    "business_item": "itmNm",
}
CONTACT_FIELDS = {
    "department": "chrgDprtNm",
    "name": "chrgNm",
    "telephone": "chrgTelno",
    "mobile": "chrgMpno",
    "fax": "chrgFaxno",
    "email": "chrgEmlAdr",
    "remarks": "chrgRmrkCntn",
}
Operation = Literal["create", "update", "delete"]


def validate_text(values: dict, limits: dict[str, int]):
    for key, value in values.items():
        if key not in limits or value is None:
            continue
        if len(value.encode("utf-8")) > limits[key] or re.search(r"[\x00-\x1f\x7f]", value):
            raise ValueError("Text exceeds its UTF-8 byte limit or contains control characters")


class ContactData(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    department: str = ""
    name: str = ""
    telephone: str = ""
    mobile: str = ""
    fax: str = ""
    email: str = ""
    remarks: str = ""

    @model_validator(mode="after")
    def contact_format(self):
        validate_text(self.model_dump(), dict.fromkeys(CONTACT_FIELDS, 100))
        for key in ("telephone", "mobile", "fax"):
            value = getattr(self, key)
            if value and not re.fullmatch(r"[0-9][0-9-]{6,19}", value):
                raise ValueError("Use phone digits with optional hyphens")
            setattr(self, key, value.replace("-", ""))
        if self.email and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", self.email):
            raise ValueError("Invalid email format")
        return self


class CounterpartyData(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = ""
    representative_name: str = ""
    address: str = ""
    business_type: str = ""
    business_item: str = ""
    primary_contact: ContactData = Field(default_factory=ContactData)
    secondary_contact: ContactData = Field(default_factory=ContactData)

    @model_validator(mode="after")
    def business_format(self):
        validate_text(
            self.model_dump(),
            {
                "name": 200,
                "representative_name": 100,
                "address": 300,
                "business_type": 200,
                "business_item": 200,
            },
        )
        if not self.name and not self.representative_name:
            raise ValueError("Provide a business name or representative name")
        return self


class CounterpartyCreate(CounterpartyData):
    business_number: str = Field(pattern=r"^[0-9]{10}$")
    branch_number: str = Field(default="", pattern=r"^(?:[0-9]{4})?$")


class ContactPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    department: str | None = None
    name: str | None = None
    telephone: str | None = None
    mobile: str | None = None
    fax: str | None = None
    email: str | None = None
    remarks: str | None = None

    @model_validator(mode="after")
    def no_null(self):
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("Omit unchanged fields; use an empty string to clear a field")
        return self


class CounterpartyPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str | None = None
    representative_name: str | None = None
    address: str | None = None
    business_type: str | None = None
    business_item: str | None = None
    primary_contact: ContactPatch | None = None
    secondary_contact: ContactPatch | None = None

    @model_validator(mode="after")
    def meaningful_patch(self):
        if not self.model_fields_set or any(
            getattr(self, key) is None for key in self.model_fields_set
        ):
            raise ValueError("Provide changed fields, not nulls")
        return self


class ApplyCounterpartyChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: StrictBool

    @model_validator(mode="after")
    def confirmation(self):
        if not self.confirm:
            raise ValueError("Explicit confirmation is required")
        return self


class CounterpartyChangePreview(BaseModel):
    change_id: str
    operation: Operation
    expires_at: datetime
    company_name: str
    business_number: str
    branch_number: str
    before: CounterpartyData | None
    after: CounterpartyData | None
    requires_confirmation: Literal[True] = True


class CounterpartyChangeResult(BaseModel):
    change_id: str
    operation: Operation
    status: Literal["applied", "already_applied"]


@dataclass(repr=False)
class CounterpartySnapshot:
    company_name: str
    owner_tin: str
    owner_branch: str
    business_number: str
    branch_number: str
    recipient_tin: str
    data: dict
    contact_ids: dict[str, str] = field(default_factory=dict)
    revision: str = ""
    lookup_request: dict = field(default_factory=dict)


def fingerprint(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def snapshot_fingerprint(snapshot: CounterpartySnapshot) -> str:
    return fingerprint(
        [
            snapshot.owner_tin,
            snapshot.owner_branch,
            snapshot.recipient_tin,
            snapshot.business_number,
            snapshot.branch_number,
            snapshot.data,
            snapshot.contact_ids,
            snapshot.revision,
        ]
    )


@dataclass(repr=False)
class ChangePlan:
    preview: CounterpartyChangePreview
    snapshot: CounterpartySnapshot
    baseline: str
    target_key: str
    payload_hash: str


class CounterpartyChangeManager:
    def __init__(self, invoices, *, backend=None, clock=time.time):
        if backend is None:
            from .counterparty_backend import CounterpartyBackend

            backend = CounterpartyBackend(invoices)
        self.backend = backend
        self.clock = clock
        self.plans: dict[str, ChangePlan] = {}

    def clear(self):
        self.plans.clear()

    def _prune(self):
        now = self.clock()
        for key in list(self.plans):
            if self.plans[key].preview.expires_at.timestamp() <= now:
                del self.plans[key]

    def _prepare(self, operation, snapshot, before, after):
        snapshot = copy.deepcopy(snapshot)
        self._prune()
        if len(self.plans) >= 100:
            raise LoginError(
                "CHANGE_CAPACITY", "미리보기 한도입니다. 만료 후 다시 시도하세요.", 429
            )
        preview = CounterpartyChangePreview(
            change_id=secrets.token_urlsafe(32),
            operation=operation,
            expires_at=datetime.fromtimestamp(self.clock() + 300, UTC),
            company_name=snapshot.company_name,
            business_number=snapshot.business_number,
            branch_number=snapshot.branch_number,
            before=before,
            after=after,
        )
        target_key = fingerprint(
            [
                snapshot.owner_tin,
                snapshot.owner_branch,
                snapshot.business_number,
                snapshot.branch_number,
            ]
        )
        self.plans[preview.change_id] = ChangePlan(
            preview,
            snapshot,
            snapshot_fingerprint(snapshot),
            target_key,
            fingerprint([operation, target_key, after.model_dump() if after else None]),
        )
        return preview.model_copy(deep=True)

    async def preview_create(self, payload: CounterpartyCreate):
        snapshot = await self.backend.new_target(payload.business_number, payload.branch_number)
        after = CounterpartyData(**payload.model_dump(exclude={"business_number", "branch_number"}))
        return self._prepare("create", snapshot, None, after)

    async def _existing(self, number, branch):
        snapshot = await self.backend.current(number, branch)
        if snapshot is None:
            raise LoginError("COUNTERPARTY_NOT_FOUND", "등록된 거래처를 찾지 못했습니다.", 404)
        try:
            data = CounterpartyData.model_validate(snapshot.data)
        except ValidationError:
            raise changed() from None
        return snapshot, data

    async def preview_update(self, business_number, branch_number, payload: CounterpartyPatch):
        snapshot, before = await self._existing(business_number, branch_number)
        proposed = before.model_dump()
        for key, value in payload.model_dump(exclude_unset=True).items():
            if key in {"primary_contact", "secondary_contact"}:
                proposed[key].update(value)
            else:
                proposed[key] = value
        try:
            after = CounterpartyData.model_validate(proposed)
        except ValidationError:
            raise LoginError(
                "INVALID_REQUEST", "변경할 거래처 필드의 형식과 길이를 확인하세요.", 422
            ) from None
        if after == before:
            raise LoginError("NO_CHANGE", "변경된 필드가 없습니다.", 409)
        return self._prepare("update", snapshot, before, after)

    async def preview_delete(self, business_number, branch_number):
        snapshot, before = await self._existing(business_number, branch_number)
        return self._prepare("delete", snapshot, before, None)

    async def apply(self, change_id: str, journal: WriteJournal):
        self._prune()
        plan = self.plans.get(change_id)
        if plan is None:
            raise LoginError(
                "CHANGE_NOT_FOUND", "같은 세션에서 유효한 미리보기를 먼저 만드세요.", 404
            )
        result = CounterpartyChangeResult(
            change_id=change_id, operation=plan.preview.operation, status="applied"
        )
        state = journal.begin(change_id, plan.target_key, plan.payload_hash)
        if state == "complete":
            return result.model_copy(update={"status": "already_applied"})
        dispatched = False
        try:
            old = plan.snapshot
            if plan.preview.operation == "create":
                current = await self.backend.new_target(old.business_number, old.branch_number)
                stable = (current.owner_tin, current.owner_branch, current.recipient_tin) == (
                    old.owner_tin,
                    old.owner_branch,
                    old.recipient_tin,
                )
            else:
                current = await self.backend.current(old.business_number, old.branch_number)
                stable = current is not None and snapshot_fingerprint(current) == plan.baseline
            if not stable:
                raise LoginError(
                    "CHANGE_STALE",
                    "사업자 또는 거래처 정보가 변경됐습니다. 다시 미리보기 하세요.",
                    409,
                )
            # Durable in_flight reservation precedes the first possible external write.
            dispatched = True
            await self.backend.write(
                plan.preview.operation,
                current,
                plan.preview.after.model_dump() if plan.preview.after else None,
            )
            actual = await self.backend.current(old.business_number, old.branch_number)
            same_scope = getattr(self.backend, "last_scope", None) == (
                old.owner_tin,
                old.owner_branch,
            )
            if plan.preview.operation == "delete":
                verified = same_scope and actual is None
            else:
                verified = (
                    same_scope
                    and actual is not None
                    and (actual.owner_tin, actual.owner_branch, actual.recipient_tin)
                    == (old.owner_tin, old.owner_branch, old.recipient_tin)
                    and (CounterpartyData.model_validate(actual.data) == plan.preview.after)
                )
            if not verified:
                raise changed()
            journal.finish(change_id, "complete")
            return result
        except BaseException as exc:
            try:
                journal.finish(change_id, "unknown" if dispatched else "rejected")
            except Exception:
                # The durable in_flight row remains blocking if finalization itself fails.
                pass
            if isinstance(exc, asyncio.CancelledError) or not isinstance(exc, Exception):
                raise
            if dispatched:
                raise LoginError(
                    "WRITE_OUTCOME_UNKNOWN",
                    "반영 결과를 확정하지 못했습니다. 재전송하지 말고 홈택스에서 확인하세요.",
                    502,
                ) from None
            if isinstance(exc, LoginError):
                raise
            raise changed() from None
