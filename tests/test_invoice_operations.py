import asyncio
import copy
import sqlite3
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from hometax_login.certificates import CertificateMaterial
from hometax_login.counterparty_changes import fingerprint
from hometax_login.errors import LoginError
from hometax_login.invoice_operations import InvoiceOperations
from hometax_login.issuance_models import (
    InvoiceCancelRequest,
    InvoiceCorrectionRequest,
    InvoiceDocumentLine,
    InvoiceDocumentPreview,
    InvoiceIssueRequest,
    InvoiceOperationResult,
    InvoiceParty,
    PreparedInvoiceOperation,
)
from hometax_login.write_journal import WriteJournal

REFERENCE_A = UUID("11111111-1111-4111-8111-111111111111")
REFERENCE_B = UUID("22222222-2222-4222-8222-222222222222")
ORIGINAL_A = "20250101ABCDEFGH1234567Z"
ORIGINAL_B = "20250102ABCDEFGH1234567Z"
APPROVAL_A = "20250103ABCDEFGH1234567Z"
APPROVAL_B = "20250104ABCDEFGH1234567Z"


class FakeClock:
    def __init__(self, now=1_735_689_600.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeClient:
    def __init__(self, material=None, wire_profile="raw"):
        if material is None:
            material = certificate_material()
        self.signing_certificate_fingerprint = material.certificate.fingerprint(
            hashes.SHA256()
        ).hex()
        self.invoice_wire_encoding = wire_profile


def journal_path(tmp_path):
    return tmp_path / "private" / "invoice-journal.db"


def journal_status(path, plan_id):
    with sqlite3.connect(path) as conn:
        return conn.execute(
            "SELECT status FROM write_attempts WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()[0]


def certificate_material(*, random_number=b"\x00\x00synthetic-vid") -> CertificateMaterial:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "KR"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Synthetic Test"),
            x509.NameAttribute(NameOID.COMMON_NAME, "synthetic.invoice.test"),
        ]
    )
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    private_key_der = key.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return CertificateMaterial(cert, key, private_key_der, random_number)


def without_vid(material: CertificateMaterial) -> CertificateMaterial:
    return CertificateMaterial(
        material.certificate,
        material.private_key,
        material.private_key_der,
        None,
    )


def party(number="1234567890", name="예시회사") -> InvoiceParty:
    return InvoiceParty(
        business_number=number,
        name=name,
        representative_name="예시대표",
        address="예시주소",
        business_type="서비스",
        business_item="테스트",
        email="invoice@example.test",
    )


def document(*, original=None, reason=None, recipient=None, amount=1000) -> InvoiceDocumentPreview:
    return InvoiceDocumentPreview(
        supplier=party("1112223334", "예시공급사"),
        recipient=recipient or party("1234567890", "예시수신사"),
        written_date=date(2025, 1, 10),
        purpose="claim",
        items=[
            InvoiceDocumentLine(
                supply_date=date(2025, 1, 10),
                name="테스트 품목",
                specification="규격",
                quantity=1,
                unit_price=amount,
                supply_amount=amount,
                tax_amount=amount // 10,
                remarks="",
            )
        ],
        remarks="",
        supply_amount=amount,
        tax_amount=amount // 10,
        total_amount=amount + amount // 10,
        original_approval_number=original,
        correction_reason_code=reason,
    )


def issue_request(reference=REFERENCE_A) -> InvoiceIssueRequest:
    return InvoiceIssueRequest(
        client_reference=reference,
        recipient_business_number="1234567890",
        written_date=date(2025, 1, 10),
        purpose="claim",
        items=[
            {
                "supply_date": date(2025, 1, 10),
                "name": "테스트 품목",
                "supply_amount": 1000,
                "tax_amount": 100,
            }
        ],
    )


def correction_request(reference=REFERENCE_A) -> InvoiceCorrectionRequest:
    return InvoiceCorrectionRequest(
        **issue_request(reference).model_dump(),
        reason="clerical_error",
    )


def cancel_request(reference=REFERENCE_A) -> InvoiceCancelRequest:
    return InvoiceCancelRequest(
        client_reference=reference,
        reason="duplicate_issue",
        written_date=date(2025, 1, 10),
    )


def prepared(operation, request, *, documents=None, original=None, scope=("1112223334", "0001")):
    return PreparedInvoiceOperation(
        operation=operation,
        client_reference=request.client_reference,
        scope=scope,
        documents=copy.deepcopy(documents if documents is not None else [document()]),
        payload={"kind": operation, "reference": str(request.client_reference)},
        request=request,
        original_approval_number=original,
    )


class FakeBackend:
    def __init__(self):
        self.issue_prepared = prepared("issue", issue_request())
        self.correct_prepared = prepared(
            "correct",
            correction_request(),
            documents=[
                document(original=ORIGINAL_A, reason="04", amount=1000),
                document(original=ORIGINAL_A, reason="04", amount=1100),
            ],
            original=ORIGINAL_A,
        )
        self.cancel_prepared = prepared(
            "cancel",
            cancel_request(),
            documents=[document(original=ORIGINAL_A, reason="06")],
            original=ORIGINAL_A,
        )
        self.preview_calls = []
        self.refresh_calls = []
        self.issue_calls = []
        self.refresh_mode = None
        self.issue_mode = None

    async def preview_issue(self, request):
        self.preview_calls.append(("issue", copy.deepcopy(request)))
        prepared_doc = copy.deepcopy(self.issue_prepared)
        prepared_doc.client_reference = request.client_reference
        prepared_doc.request = request
        prepared_doc.payload["reference"] = str(request.client_reference)
        return prepared_doc

    async def preview_correct(self, original, request):
        self.preview_calls.append(("correct", original, copy.deepcopy(request)))
        prepared_doc = copy.deepcopy(self.correct_prepared)
        prepared_doc.client_reference = request.client_reference
        prepared_doc.request = request
        prepared_doc.payload["reference"] = str(request.client_reference)
        prepared_doc.original_approval_number = original
        for doc in prepared_doc.documents:
            doc.original_approval_number = original
        return prepared_doc

    async def preview_cancel(self, original, request):
        self.preview_calls.append(("cancel", original, copy.deepcopy(request)))
        prepared_doc = copy.deepcopy(self.cancel_prepared)
        prepared_doc.client_reference = request.client_reference
        prepared_doc.request = request
        prepared_doc.payload["reference"] = str(request.client_reference)
        prepared_doc.original_approval_number = original
        prepared_doc.documents[0].original_approval_number = original
        return prepared_doc

    async def refresh(self, prepared_doc):
        self.refresh_calls.append(copy.deepcopy(prepared_doc))
        fresh = copy.deepcopy(prepared_doc)
        if self.refresh_mode == "scope":
            fresh.scope = ("2223334445", "0009")
        elif self.refresh_mode == "party":
            fresh.documents[0].recipient = party("9876543210", "다른예시")
        elif self.refresh_mode == "docs":
            fresh.documents[0].remarks = "외부변경"
        return fresh

    async def issue(self, prepared_doc, material):
        self.issue_calls.append((copy.deepcopy(prepared_doc), material))
        if self.issue_mode in {"c04", "backend_error", "readback_exception"}:
            raise RuntimeError(self.issue_mode)
        if self.issue_mode == "cancel":
            raise asyncio.CancelledError
        if self.issue_mode == "partial":
            return [APPROVAL_A]
        if prepared_doc.operation == "correct":
            return [APPROVAL_A, APPROVAL_B]
        return [APPROVAL_A]


@pytest.fixture
def deterministic_ids(monkeypatch):
    counter = {"value": 0}

    def token_urlsafe(_size):
        counter["value"] += 1
        return f"invoice-plan-{counter['value']:012d}"

    monkeypatch.setattr("hometax_login.invoice_operations.secrets.token_urlsafe", token_urlsafe)


def manager(client, backend=None, clock=None):
    return InvoiceOperations(client, backend=backend or FakeBackend(), clock=clock or FakeClock())


@pytest.mark.asyncio
async def test_preview_issue_correct_cancel_counts_and_do_not_mutate(deterministic_ids):
    material = certificate_material()
    backend = FakeBackend()
    ops = manager(FakeClient(material), backend)

    issue = await ops.preview_issue(issue_request())
    correct = await ops.preview_correct(ORIGINAL_A, correction_request(REFERENCE_B))
    cancel = await ops.preview_cancel(ORIGINAL_B, cancel_request(REFERENCE_B))

    assert [issue.operation, correct.operation, cancel.operation] == ["issue", "correct", "cancel"]
    assert [len(issue.documents), len(correct.documents), len(cancel.documents)] == [1, 2, 1]
    assert backend.issue_calls == []
    assert backend.refresh_calls == []


@pytest.mark.parametrize(
    "operation,documents",
    [
        ("issue", []),
        ("correct", [document(original=ORIGINAL_A, reason="04")]),
        (
            "cancel",
            [
                document(original=ORIGINAL_A, reason="06"),
                document(original=ORIGINAL_A, reason="06"),
            ],
        ),
    ],
)
@pytest.mark.asyncio
async def test_preview_enforces_document_count(operation, documents, deterministic_ids):
    backend = FakeBackend()
    if operation == "issue":
        backend.issue_prepared.documents = documents
        call = manager(FakeClient(), backend).preview_issue(issue_request())
    elif operation == "correct":
        backend.correct_prepared.documents = documents
        call = manager(FakeClient(), backend).preview_correct(ORIGINAL_A, correction_request())
    else:
        backend.cancel_prepared.documents = documents
        call = manager(FakeClient(), backend).preview_cancel(ORIGINAL_A, cancel_request())

    with pytest.raises(LoginError) as caught:
        await call
    assert caught.value.code == "INVOICE_RESPONSE_CHANGED"


@pytest.mark.asyncio
async def test_preview_digest_is_exact_and_prepared_snapshot_is_copied(deterministic_ids):
    backend = FakeBackend()
    ops = manager(FakeClient(), backend)

    preview = await ops.preview_issue(issue_request())
    plan = ops.plans[preview.operation_id]
    expected = fingerprint(plan.prepared.comparable())
    backend.issue_prepared.documents[0].remarks = "mutated-after-preview"
    preview.documents[0].remarks = "caller-mutated"

    assert preview.content_digest == expected
    assert ops.plans[preview.operation_id].preview.documents[0].remarks == ""
    assert ops.plans[preview.operation_id].prepared.documents[0].remarks == ""


@pytest.mark.parametrize(
    "client_factory,material_factory,expected_code",
    [
        (
            lambda good: FakeClient(certificate_material()),
            lambda good: good,
            "INVOICE_CERTIFICATE_MISMATCH",
        ),
        (
            lambda good: FakeClient(good),
            without_vid,
            "CERT_RANDOM_MISSING",
        ),
        (
            lambda good: FakeClient(good, wire_profile=""),
            lambda good: good,
            "INVOICE_SIGNING_PROFILE_REQUIRED",
        ),
    ],
)
@pytest.mark.asyncio
async def test_submit_requires_same_login_certificate_vid_and_wire_profile(
    client_factory, material_factory, expected_code, deterministic_ids, tmp_path
):
    good = certificate_material()
    backend = FakeBackend()
    ops = manager(client_factory(good), backend)
    preview = await ops.preview_issue(issue_request())

    with pytest.raises(LoginError) as caught:
        await ops.submit(
            preview.operation_id,
            material_factory(good),
            WriteJournal(journal_path(tmp_path)),
            preview.content_digest,
        )

    assert caught.value.code == expected_code
    assert backend.issue_calls == []


@pytest.mark.asyncio
async def test_submit_rejects_confirmation_digest_mismatch_before_issue(
    deterministic_ids, tmp_path
):
    material = certificate_material()
    backend = FakeBackend()
    ops = manager(FakeClient(material), backend)
    preview = await ops.preview_issue(issue_request())

    with pytest.raises(LoginError) as caught:
        await ops.submit(
            preview.operation_id,
            material,
            WriteJournal(journal_path(tmp_path)),
            "0" * 64,
        )

    assert caught.value.code == "INVOICE_CONFIRMATION_MISMATCH"
    assert backend.issue_calls == []


@pytest.mark.parametrize("mode", ["scope", "party", "docs"])
@pytest.mark.asyncio
async def test_submit_rejects_stale_scope_party_or_documents_before_issue(
    mode, deterministic_ids, tmp_path
):
    material = certificate_material()
    backend = FakeBackend()
    backend.refresh_mode = mode
    ops = manager(FakeClient(material), backend)
    preview = await ops.preview_issue(issue_request())

    with pytest.raises(LoginError) as caught:
        await ops.submit(
            preview.operation_id,
            material,
            WriteJournal(journal_path(tmp_path)),
            preview.content_digest,
        )

    assert caught.value.code == "INVOICE_PREVIEW_STALE"
    assert backend.issue_calls == []


@pytest.mark.asyncio
async def test_submit_issue_writes_once_and_reapply_returns_cached_result(
    deterministic_ids, tmp_path
):
    material = certificate_material()
    backend = FakeBackend()
    ops = manager(FakeClient(material), backend)
    preview = await ops.preview_issue(issue_request())
    path = journal_path(tmp_path)

    first = await ops.submit(
        preview.operation_id,
        material,
        WriteJournal(path),
        preview.content_digest,
    )
    second = await ops.submit(
        preview.operation_id,
        material,
        WriteJournal(path),
        preview.content_digest,
    )

    assert first.status == "issued"
    assert first.approval_numbers == [APPROVAL_A]
    assert second.status == "already_issued"
    assert second.approval_numbers == [APPROVAL_A]
    assert len(backend.issue_calls) == 1
    assert journal_status(path, preview.operation_id) == "complete"


@pytest.mark.asyncio
async def test_completed_issue_target_blocks_different_plan_for_same_client_reference(
    deterministic_ids, tmp_path
):
    material = certificate_material()
    backend = FakeBackend()
    ops = manager(FakeClient(material), backend)
    first = await ops.preview_issue(issue_request(REFERENCE_A))
    path = journal_path(tmp_path)
    await ops.submit(first.operation_id, material, WriteJournal(path), first.content_digest)

    second = await ops.preview_issue(issue_request(REFERENCE_A))
    with pytest.raises(LoginError) as caught:
        await ops.submit(second.operation_id, material, WriteJournal(path), second.content_digest)

    assert caught.value.code == "WRITE_TARGET_COMPLETE"
    assert caught.value.status == 409
    assert len(backend.issue_calls) == 1


@pytest.mark.asyncio
async def test_completed_correction_target_blocks_new_client_reference_for_same_original(
    deterministic_ids, tmp_path
):
    material = certificate_material()
    backend = FakeBackend()
    ops = manager(FakeClient(material), backend)
    first = await ops.preview_correct(ORIGINAL_A, correction_request(REFERENCE_A))
    path = journal_path(tmp_path)
    await ops.submit(first.operation_id, material, WriteJournal(path), first.content_digest)

    second = await ops.preview_correct(ORIGINAL_A, correction_request(REFERENCE_B))
    with pytest.raises(LoginError) as caught:
        await ops.submit(second.operation_id, material, WriteJournal(path), second.content_digest)

    assert caught.value.code == "WRITE_TARGET_COMPLETE"
    assert len(backend.issue_calls) == 1


@pytest.mark.parametrize("mode", ["c04", "backend_error", "partial", "readback_exception"])
@pytest.mark.asyncio
async def test_dispatched_failures_leave_unknown_and_block_new_plan(
    mode, deterministic_ids, tmp_path
):
    material = certificate_material()
    backend = FakeBackend()
    backend.issue_mode = mode
    ops = manager(FakeClient(material), backend)
    if mode == "partial":
        first = await ops.preview_correct(ORIGINAL_A, correction_request(REFERENCE_A))
    else:
        first = await ops.preview_issue(issue_request(REFERENCE_A))
    path = journal_path(tmp_path)

    with pytest.raises(LoginError) as caught:
        await ops.submit(first.operation_id, material, WriteJournal(path), first.content_digest)

    assert caught.value.code == "INVOICE_OUTCOME_UNKNOWN"
    assert journal_status(path, first.operation_id) == "unknown"

    backend.issue_mode = None
    restarted = manager(FakeClient(material), backend)
    if mode == "partial":
        second = await restarted.preview_correct(ORIGINAL_A, correction_request(REFERENCE_B))
    else:
        second = await restarted.preview_issue(issue_request(REFERENCE_A))
    with pytest.raises(LoginError) as blocked:
        await restarted.submit(
            second.operation_id,
            material,
            WriteJournal(path),
            second.content_digest,
        )

    assert blocked.value.code == "WRITE_OUTCOME_UNKNOWN"
    assert len(backend.issue_calls) == 1


@pytest.mark.asyncio
async def test_cancelled_submission_leaves_unknown_and_reraises(deterministic_ids, tmp_path):
    material = certificate_material()
    backend = FakeBackend()
    backend.issue_mode = "cancel"
    ops = manager(FakeClient(material), backend)
    preview = await ops.preview_issue(issue_request())
    path = journal_path(tmp_path)

    with pytest.raises(asyncio.CancelledError):
        await ops.submit(preview.operation_id, material, WriteJournal(path), preview.content_digest)

    assert journal_status(path, preview.operation_id) == "unknown"

    backend.issue_mode = None
    restarted = manager(FakeClient(material), backend)
    second = await restarted.preview_issue(issue_request(REFERENCE_A))
    with pytest.raises(LoginError) as blocked:
        await restarted.submit(
            second.operation_id,
            material,
            WriteJournal(path),
            second.content_digest,
        )
    assert blocked.value.code == "WRITE_OUTCOME_UNKNOWN"
    assert len(backend.issue_calls) == 1


@pytest.mark.asyncio
async def test_plan_expires_and_capacity_is_limited(deterministic_ids, tmp_path):
    material = certificate_material()
    clock = FakeClock()
    ops = manager(FakeClient(material), FakeBackend(), clock)
    preview = await ops.preview_issue(issue_request())

    clock.advance(301)
    with pytest.raises(LoginError) as expired:
        await ops.submit(
            preview.operation_id,
            material,
            WriteJournal(journal_path(tmp_path)),
            preview.content_digest,
        )
    assert expired.value.code == "INVOICE_PREVIEW_NOT_FOUND"

    capacity = manager(FakeClient(material), FakeBackend(), FakeClock())
    for index in range(100):
        await capacity.preview_issue(issue_request(UUID(f"10000000-0000-4000-8000-{index:012d}")))
    with pytest.raises(LoginError) as full:
        await capacity.preview_issue(issue_request(REFERENCE_B))
    assert full.value.code == "INVOICE_PREVIEW_CAPACITY"
    assert full.value.status == 429


def test_synthetic_certificate_material_signs_and_exposes_vid():
    material = certificate_material()
    signature = material.sign(b"payload")

    material.certificate.public_key().verify(
        signature,
        b"payload",
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    assert material.random_number == b"\x00\x00synthetic-vid"


def test_operation_result_copy_is_not_shared():
    result = InvoiceOperationResult(
        operation_id="invoice-plan-000000000001",
        client_reference=REFERENCE_A,
        operation="issue",
        status="issued",
        approval_numbers=[APPROVAL_A],
    )
    clone = result.model_copy(deep=True)
    clone.approval_numbers.append(APPROVAL_B)

    assert result.approval_numbers == [APPROVAL_A]
