import asyncio
import copy
import sqlite3

import pytest
from pydantic import ValidationError

from hometax_login.counterparty_changes import (
    ApplyCounterpartyChange,
    ContactData,
    CounterpartyChangeManager,
    CounterpartyCreate,
    CounterpartyPatch,
    CounterpartySnapshot,
)
from hometax_login.errors import LoginError
from hometax_login.write_journal import WriteJournal

OWNER_TIN = "1112223334"
OWNER_BRANCH = "0001"
RECIPIENT_TIN = "9998887776"
BUSINESS_NUMBER = "1234567890"
BRANCH_NUMBER = "0002"


class FakeClock:
    def __init__(self, now=1_735_689_600.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def journal_path(tmp_path):
    return tmp_path / "private" / "journal.db"


def journal_status(path, plan_id):
    with sqlite3.connect(path) as conn:
        return conn.execute(
            "SELECT status FROM write_attempts WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()[0]


def sample_data(**overrides):
    data = {
        "name": "예시거래처",
        "representative_name": "예시대표",
        "address": "예시주소",
        "business_type": "서비스",
        "business_item": "테스트",
        "primary_contact": {
            "department": "정산",
            "name": "담당자",
            "telephone": "021234567",
            "mobile": "01012345678",
            "fax": "",
            "email": "contact@example.test",
            "remarks": "기본",
        },
        "secondary_contact": {
            "department": "",
            "name": "",
            "telephone": "",
            "mobile": "",
            "fax": "",
            "email": "",
            "remarks": "",
        },
    }
    data.update(overrides)
    return data


def create_payload(**overrides):
    data = {
        "business_number": BUSINESS_NUMBER,
        "branch_number": BRANCH_NUMBER,
        **sample_data(),
    }
    data.update(overrides)
    return CounterpartyCreate(**data)


def snapshot(number=BUSINESS_NUMBER, branch=BRANCH_NUMBER, *, data=None, revision="r1"):
    return CounterpartySnapshot(
        company_name="예시공급사",
        owner_tin=OWNER_TIN,
        owner_branch=OWNER_BRANCH,
        business_number=number,
        branch_number=branch,
        recipient_tin=RECIPIENT_TIN,
        data=copy.deepcopy(data if data is not None else sample_data()),
        contact_ids={"primary_contact": "chrg-1", "secondary_contact": "chrg-2"},
        revision=revision,
        lookup_request={"synthetic": "only"},
    )


class FakeBackend:
    def __init__(self, state=None):
        initial = {(BUSINESS_NUMBER, BRANCH_NUMBER): snapshot()} if state is None else state
        self.state = {key: copy.deepcopy(value) for key, value in initial.items()}
        self.owner_tin = OWNER_TIN
        self.owner_branch = OWNER_BRANCH
        self.recipient_tin = RECIPIENT_TIN
        self.last_scope = None
        self.write_calls = []
        self.current_calls = []
        self.new_target_calls = []
        self.write_mode = None
        self.current_mode = None

    def scoped(self, snap):
        copied = copy.deepcopy(snap)
        copied.owner_tin = self.owner_tin
        copied.owner_branch = self.owner_branch
        copied.recipient_tin = self.recipient_tin
        return copied

    async def current(self, number, branch):
        self.current_calls.append((number, branch))
        self.last_scope = (self.owner_tin, self.owner_branch)
        if self.current_mode == "raise":
            raise RuntimeError("readback lost")
        if self.current_mode == "mismatch":
            changed = self.scoped(self.state[(number, branch)])
            changed.data = sample_data(address="다른주소")
            return changed
        if self.current_mode == "wrong_scope_absent":
            self.last_scope = ("2223334445", "0009")
            return None
        item = self.state.get((number, branch))
        return self.scoped(item) if item else None

    async def new_target(self, number, branch):
        self.new_target_calls.append((number, branch))
        self.last_scope = (self.owner_tin, self.owner_branch)
        if (number, branch) in self.state:
            raise LoginError("COUNTERPARTY_ALREADY_EXISTS", "이미 등록된 거래처입니다.", 409)
        return CounterpartySnapshot(
            company_name="예시공급사",
            owner_tin=self.owner_tin,
            owner_branch=self.owner_branch,
            business_number=number,
            branch_number=branch,
            recipient_tin=self.recipient_tin,
            data={},
            contact_ids={},
            revision="new",
            lookup_request={"synthetic": "new"},
        )

    async def write(self, operation, current, after):
        self.write_calls.append((operation, copy.deepcopy(current), copy.deepcopy(after)))
        if self.write_mode == "raise":
            raise RuntimeError("write failed")
        key = (current.business_number, current.branch_number)
        if operation == "create":
            self.state[key] = snapshot(*key, data=after, revision="created")
        elif operation == "update":
            updated = copy.deepcopy(current)
            updated.data = copy.deepcopy(after)
            updated.revision = current.revision
            self.state[key] = updated
        elif operation == "delete":
            self.state.pop(key, None)
        else:
            raise AssertionError(f"unexpected operation: {operation}")
        if self.write_mode == "after_current_raise":
            self.current_mode = "raise"
        elif self.write_mode == "after_current_mismatch":
            self.current_mode = "mismatch"
        elif self.write_mode == "after_wrong_scope_absent":
            self.current_mode = "wrong_scope_absent"


@pytest.fixture
def ids(monkeypatch):
    counter = {"value": 0}

    def token_urlsafe(_size):
        counter["value"] += 1
        return f"plan-{counter['value']:016d}"

    monkeypatch.setattr("hometax_login.counterparty_changes.secrets.token_urlsafe", token_urlsafe)


def manager(backend=None, clock=None):
    return CounterpartyChangeManager(
        invoices=None,
        backend=backend or FakeBackend(),
        clock=clock or FakeClock(),
    )


@pytest.mark.asyncio
async def test_preview_create_update_delete_do_not_write(ids):
    backend = FakeBackend(state={})
    mgr = manager(backend)

    create = await mgr.preview_create(create_payload())
    backend.state[(BUSINESS_NUMBER, BRANCH_NUMBER)] = snapshot()
    update = await mgr.preview_update(
        BUSINESS_NUMBER,
        BRANCH_NUMBER,
        CounterpartyPatch(address="변경주소"),
    )
    delete = await mgr.preview_delete(BUSINESS_NUMBER, BRANCH_NUMBER)

    assert [create.operation, update.operation, delete.operation] == [
        "create",
        "update",
        "delete",
    ]
    assert backend.write_calls == []


@pytest.mark.asyncio
async def test_update_preserves_unspecified_business_contact_fields_and_contact_ids(ids, tmp_path):
    backend = FakeBackend()
    mgr = manager(backend)

    preview = await mgr.preview_update(
        BUSINESS_NUMBER,
        BRANCH_NUMBER,
        CounterpartyPatch(
            address="변경주소",
            primary_contact={"email": "updated@example.test"},
        ),
    )
    assert preview.before.name == "예시거래처"
    assert preview.after.name == "예시거래처"
    assert preview.after.representative_name == "예시대표"
    assert preview.after.primary_contact.name == "담당자"
    assert preview.after.primary_contact.email == "updated@example.test"
    assert preview.after.secondary_contact.name == ""

    result = await mgr.apply(preview.change_id, WriteJournal(journal_path(tmp_path)))
    assert result.status == "applied"
    assert backend.write_calls[0][1].contact_ids == {
        "primary_contact": "chrg-1",
        "secondary_contact": "chrg-2",
    }


@pytest.mark.asyncio
async def test_plan_id_is_session_local_expires_and_capacity_is_limited(ids, tmp_path):
    clock = FakeClock()
    backend = FakeBackend()
    first = manager(backend, clock)
    preview = await first.preview_update(
        BUSINESS_NUMBER,
        BRANCH_NUMBER,
        CounterpartyPatch(address="변경주소"),
    )

    second = manager(backend, clock)
    with pytest.raises(LoginError) as caught:
        await second.apply(preview.change_id, WriteJournal(journal_path(tmp_path)))
    assert caught.value.code == "CHANGE_NOT_FOUND"

    clock.advance(301)
    with pytest.raises(LoginError) as expired:
        await first.apply(preview.change_id, WriteJournal(journal_path(tmp_path)))
    assert expired.value.code == "CHANGE_NOT_FOUND"

    capacity = manager(FakeBackend(), FakeClock())
    for index in range(100):
        await capacity.preview_update(
            BUSINESS_NUMBER,
            BRANCH_NUMBER,
            CounterpartyPatch(address=f"주소{index}"),
        )
    with pytest.raises(LoginError) as full:
        await capacity.preview_update(
            BUSINESS_NUMBER,
            BRANCH_NUMBER,
            CounterpartyPatch(address="초과주소"),
        )
    assert full.value.code == "CHANGE_CAPACITY"
    assert full.value.status == 429


@pytest.mark.parametrize(
    "factory",
    [
        lambda: CounterpartyCreate(**{**create_payload().model_dump(), "business_number": "123"}),
        lambda: ContactData(telephone="abc"),
        lambda: ContactData(email="not-an-email"),
        lambda: CounterpartyCreate(**{**create_payload().model_dump(), "name": "가" * 101}),
        lambda: CounterpartyCreate(**{**create_payload().model_dump(), "name": "bad\x00name"}),
        lambda: CounterpartyPatch(name=None),
        lambda: CounterpartyCreate(
            **{
                **create_payload().model_dump(),
                "name": "",
                "representative_name": "",
            }
        ),
        lambda: ApplyCounterpartyChange(confirm="true"),
        lambda: ApplyCounterpartyChange(confirm=False),
    ],
)
def test_validations_reject_bad_business_phone_email_text_null_empty_and_scalar(factory):
    with pytest.raises(ValidationError):
        factory()


@pytest.mark.asyncio
async def test_update_rejects_no_op_patch(ids):
    mgr = manager()

    with pytest.raises(LoginError) as caught:
        await mgr.preview_update(
            BUSINESS_NUMBER,
            BRANCH_NUMBER,
            CounterpartyPatch(address="예시주소"),
        )

    assert caught.value.code == "NO_CHANGE"
    assert caught.value.status == 409


@pytest.mark.asyncio
async def test_apply_update_writes_once_rechecks_and_marks_complete(ids, tmp_path):
    backend = FakeBackend()
    mgr = manager(backend)
    preview = await mgr.preview_update(
        BUSINESS_NUMBER,
        BRANCH_NUMBER,
        CounterpartyPatch(address="변경주소"),
    )
    path = journal_path(tmp_path)

    result = await mgr.apply(preview.change_id, WriteJournal(path))

    assert result.status == "applied"
    assert len(backend.write_calls) == 1
    assert backend.current_calls == [
        (BUSINESS_NUMBER, BRANCH_NUMBER),
        (BUSINESS_NUMBER, BRANCH_NUMBER),
        (BUSINESS_NUMBER, BRANCH_NUMBER),
    ]
    assert backend.state[(BUSINESS_NUMBER, BRANCH_NUMBER)].data["address"] == "변경주소"
    assert journal_status(path, preview.change_id) == "complete"


@pytest.mark.asyncio
async def test_same_plan_reapply_is_already_applied_without_second_write(ids, tmp_path):
    backend = FakeBackend()
    mgr = manager(backend)
    preview = await mgr.preview_update(
        BUSINESS_NUMBER,
        BRANCH_NUMBER,
        CounterpartyPatch(address="변경주소"),
    )
    path = journal_path(tmp_path)

    first = await mgr.apply(preview.change_id, WriteJournal(path))
    second = await mgr.apply(preview.change_id, WriteJournal(path))

    assert first.status == "applied"
    assert second.status == "already_applied"
    assert len(backend.write_calls) == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda backend: backend.state.__setitem__(
            (BUSINESS_NUMBER, BRANCH_NUMBER),
            snapshot(data=sample_data(address="외부변경"), revision="r2"),
        ),
        lambda backend: setattr(backend, "owner_tin", "2223334445"),
        lambda backend: setattr(backend, "owner_branch", "0009"),
        lambda backend: setattr(backend, "recipient_tin", "8887776665"),
    ],
)
@pytest.mark.asyncio
async def test_apply_rejects_stale_snapshot_scope_or_branch_change_without_write(
    mutate, ids, tmp_path
):
    backend = FakeBackend()
    mgr = manager(backend)
    preview = await mgr.preview_update(
        BUSINESS_NUMBER,
        BRANCH_NUMBER,
        CounterpartyPatch(address="변경주소"),
    )
    mutate(backend)

    with pytest.raises(LoginError) as caught:
        await mgr.apply(preview.change_id, WriteJournal(journal_path(tmp_path)))

    assert caught.value.code == "CHANGE_STALE"
    assert backend.write_calls == []


@pytest.mark.asyncio
async def test_create_preview_rejects_already_registered_target_without_write(ids):
    backend = FakeBackend()
    mgr = manager(backend)

    with pytest.raises(LoginError) as caught:
        await mgr.preview_create(create_payload())

    assert caught.value.code == "COUNTERPARTY_ALREADY_EXISTS"
    assert backend.write_calls == []


@pytest.mark.asyncio
async def test_delete_success_requires_post_verification_absent(ids, tmp_path):
    backend = FakeBackend()
    mgr = manager(backend)
    preview = await mgr.preview_delete(BUSINESS_NUMBER, BRANCH_NUMBER)

    result = await mgr.apply(preview.change_id, WriteJournal(journal_path(tmp_path)))

    assert result.status == "applied"
    assert (BUSINESS_NUMBER, BRANCH_NUMBER) not in backend.state
    assert len(backend.write_calls) == 1


@pytest.mark.asyncio
async def test_delete_does_not_succeed_when_absent_readback_is_from_wrong_scope(ids, tmp_path):
    backend = FakeBackend()
    mgr = manager(backend)
    preview = await mgr.preview_delete(BUSINESS_NUMBER, BRANCH_NUMBER)
    backend.write_mode = "after_wrong_scope_absent"

    with pytest.raises(LoginError) as caught:
        await mgr.apply(preview.change_id, WriteJournal(journal_path(tmp_path)))

    assert caught.value.code == "WRITE_OUTCOME_UNKNOWN"


@pytest.mark.parametrize("mode", ["raise", "current_raise", "current_mismatch"])
@pytest.mark.asyncio
async def test_dispatched_write_failures_or_readback_mismatch_are_unknown(mode, ids, tmp_path):
    backend = FakeBackend()
    mgr = manager(backend)
    preview = await mgr.preview_update(
        BUSINESS_NUMBER,
        BRANCH_NUMBER,
        CounterpartyPatch(address="변경주소"),
    )
    if mode == "raise":
        backend.write_mode = "raise"
    elif mode == "current_raise":
        backend.write_mode = "after_current_raise"
    else:
        backend.write_mode = "after_current_mismatch"

    path = journal_path(tmp_path)
    with pytest.raises(LoginError) as caught:
        await mgr.apply(preview.change_id, WriteJournal(path))

    assert caught.value.code == "WRITE_OUTCOME_UNKNOWN"
    assert journal_status(path, preview.change_id) == "unknown"


@pytest.mark.asyncio
async def test_cancelled_dispatched_write_marks_unknown_and_reraises(ids, tmp_path):
    class CancellingBackend(FakeBackend):
        async def write(self, operation, current, after):
            self.write_calls.append((operation, copy.deepcopy(current), copy.deepcopy(after)))
            raise asyncio.CancelledError

    backend = CancellingBackend()
    mgr = manager(backend)
    preview = await mgr.preview_update(
        BUSINESS_NUMBER,
        BRANCH_NUMBER,
        CounterpartyPatch(address="변경주소"),
    )
    path = journal_path(tmp_path)

    with pytest.raises(asyncio.CancelledError):
        await mgr.apply(preview.change_id, WriteJournal(path))

    assert journal_status(path, preview.change_id) == "unknown"


@pytest.mark.asyncio
async def test_unknown_durable_restart_blocks_new_plan_for_same_target(ids, tmp_path):
    backend = FakeBackend()
    first = manager(backend)
    preview = await first.preview_update(
        BUSINESS_NUMBER,
        BRANCH_NUMBER,
        CounterpartyPatch(address="변경주소"),
    )
    path = journal_path(tmp_path)
    backend.write_mode = "raise"
    with pytest.raises(LoginError):
        await first.apply(preview.change_id, WriteJournal(path))

    backend.write_mode = None
    restarted = manager(backend)
    second = await restarted.preview_update(
        BUSINESS_NUMBER,
        BRANCH_NUMBER,
        CounterpartyPatch(address="다른변경주소"),
    )
    with pytest.raises(LoginError) as blocked:
        await restarted.apply(second.change_id, WriteJournal(path))

    assert blocked.value.code == "WRITE_OUTCOME_UNKNOWN"
    assert len(backend.write_calls) == 1


@pytest.mark.asyncio
async def test_initial_journal_begin_failure_happens_before_write(ids, tmp_path):
    backend = FakeBackend()
    mgr = manager(backend)
    preview = await mgr.preview_update(
        BUSINESS_NUMBER,
        BRANCH_NUMBER,
        CounterpartyPatch(address="변경주소"),
    )
    path = journal_path(tmp_path)
    plan = mgr.plans[preview.change_id]
    journal = WriteJournal(path)
    journal.begin("plan-existing0001", plan.target_key, "a" * 64)

    with pytest.raises(LoginError) as caught:
        await mgr.apply(preview.change_id, WriteJournal(path))

    assert caught.value.code == "WRITE_OUTCOME_UNKNOWN"
    assert backend.write_calls == []


@pytest.mark.asyncio
async def test_finalize_failure_leaves_inflight_row_blocking_same_target(ids, tmp_path):
    class FinishFailingJournal(WriteJournal):
        def finish(self, plan_id, status):
            raise RuntimeError("finalize failed")

    backend = FakeBackend()
    mgr = manager(backend)
    preview = await mgr.preview_update(
        BUSINESS_NUMBER,
        BRANCH_NUMBER,
        CounterpartyPatch(address="변경주소"),
    )
    path = journal_path(tmp_path)

    with pytest.raises(LoginError) as caught:
        await mgr.apply(preview.change_id, FinishFailingJournal(path))
    assert caught.value.code == "WRITE_OUTCOME_UNKNOWN"
    assert journal_status(path, preview.change_id) == "in_flight"

    second = manager(backend)
    second_preview = await second.preview_update(
        BUSINESS_NUMBER,
        BRANCH_NUMBER,
        CounterpartyPatch(address="다른변경주소"),
    )
    with pytest.raises(LoginError) as blocked:
        await second.apply(second_preview.change_id, WriteJournal(path))

    assert blocked.value.code == "WRITE_OUTCOME_UNKNOWN"
