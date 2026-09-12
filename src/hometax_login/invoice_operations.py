"""Session-bound invoice previews and one-shot, journaled XML-signed submissions."""

from __future__ import annotations

import asyncio
import copy
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from cryptography.hazmat.primitives import hashes

from .counterparty_changes import fingerprint
from .errors import LoginError
from .invoices import approval_number, changed
from .issuance_models import (
    InvoiceCancelRequest,
    InvoiceCorrectionRequest,
    InvoiceIssueRequest,
    InvoiceOperationPreview,
    InvoiceOperationResult,
    PreparedInvoiceOperation,
    issue_content,
)


@dataclass(repr=False)
class InvoicePlan:
    prepared: PreparedInvoiceOperation
    preview: InvoiceOperationPreview
    target_key: str
    result: InvoiceOperationResult | None = None


class InvoiceOperations:
    def __init__(self, client, *, backend=None, clock=time.time):
        if backend is None:
            from .issuance_backend import InvoiceBackend

            backend = InvoiceBackend(client)
        self.client = client
        self.backend = backend
        self.clock = clock
        self.plans: dict[str, InvoicePlan] = {}

    def clear(self):
        self.plans.clear()

    def _prune(self):
        for key in list(self.plans):
            if self.plans[key].preview.expires_at.timestamp() <= self.clock():
                del self.plans[key]

    def _prepare(self, prepared: PreparedInvoiceOperation):
        self._prune()
        if len(self.plans) >= 100:
            raise LoginError(
                "INVOICE_PREVIEW_CAPACITY", "미리보기 한도입니다. 만료 후 다시 시도하세요.", 429
            )
        prepared = copy.deepcopy(prepared)
        expected_count = 2 if prepared.operation == "correct" else 1
        if len(prepared.documents) != expected_count:
            raise changed()
        digest = fingerprint(prepared.comparable())
        preview = InvoiceOperationPreview(
            operation_id=secrets.token_urlsafe(32),
            client_reference=prepared.client_reference,
            operation=prepared.operation,
            expires_at=datetime.fromtimestamp(self.clock() + 300, UTC),
            documents=prepared.documents,
            content_digest=digest,
        )
        # A correction/cancellation is one-shot for the original invoice. New references cannot
        # bypass an uncertain or completed correction; further edits must target the new invoice.
        identity = (
            ("invoice-issue", str(prepared.client_reference))
            if prepared.operation == "issue"
            else (
                "invoice-correction",
                prepared.original_approval_number,
            )
        )
        key = fingerprint([prepared.scope, identity])
        self.plans[preview.operation_id] = InvoicePlan(prepared, preview, key)
        return preview.model_copy(deep=True)

    async def preview_issue(self, request: InvoiceIssueRequest):
        return self._prepare(await self.backend.preview_issue(request))

    async def preview_correct(self, original: str, request: InvoiceCorrectionRequest):
        approval_number(original)
        return self._prepare(await self.backend.preview_correct(original, request))

    async def preview_cancel(self, original: str, request: InvoiceCancelRequest):
        approval_number(original)
        return self._prepare(await self.backend.preview_cancel(original, request))

    async def submit(self, operation_id, material, journal, content_digest: str):
        self._prune()
        plan = self.plans.get(operation_id)
        if plan is None:
            raise LoginError(
                "INVOICE_PREVIEW_NOT_FOUND", "같은 세션에서 유효한 미리보기를 먼저 만드세요.", 404
            )
        if content_digest != plan.preview.content_digest:
            raise LoginError(
                "INVOICE_CONFIRMATION_MISMATCH", "확인한 미리보기 내용이 일치하지 않습니다.", 409
            )
        certificate_hash = material.certificate.fingerprint(hashes.SHA256()).hex()
        if not self.client.signing_certificate_fingerprint or (
            certificate_hash != self.client.signing_certificate_fingerprint
        ):
            raise LoginError(
                "INVOICE_CERTIFICATE_MISMATCH", "로그인에 사용한 인증서가 필요합니다.", 403
            )
        if not material.random_number:
            raise LoginError("CERT_RANDOM_MISSING", "NPKI VID 난수가 필요합니다.", 422)
        if self.client.invoice_wire_encoding not in {"raw", "base64"}:
            raise LoginError(
                "INVOICE_SIGNING_PROFILE_REQUIRED", "검증할 XML 서명 전송 프로필을 설정하세요.", 503
            )
        state = journal.begin(
            operation_id,
            plan.target_key,
            plan.preview.content_digest,
            unique_target=True,
            guard_keys=(
                fingerprint(
                    [
                        plan.prepared.scope,
                        "invoice-issue-content",
                        issue_content(plan.prepared.request),
                    ]
                ),
            )
            if plan.prepared.operation == "issue"
            else (),
        )
        if state == "complete":
            if plan.result is None:
                raise LoginError(
                    "INVOICE_RESULT_NOT_CACHED", "발행 기록이 있습니다. 조회 API로 확인하세요.", 409
                )
            return plan.result.model_copy(update={"status": "already_issued"}, deep=True)
        dispatched = False
        try:
            fresh = await self.backend.refresh(plan.prepared)
            if fingerprint(fresh.comparable()) != plan.preview.content_digest:
                raise LoginError(
                    "INVOICE_PREVIEW_STALE",
                    "사업자·거래처·원본 정보가 바뀌었습니다. 다시 미리보기 하세요.",
                    409,
                )
            # C04/C03 XML generation may reserve identifiers. Guard the entire external operation,
            # not merely its final C05/C04 submission. Never auto-retry a partial pair.
            dispatched = True
            approvals = await self.backend.issue(fresh, material)
            if not isinstance(approvals, list) or len(approvals) != len(fresh.documents):
                raise changed()
            approvals = [approval_number(value) for value in approvals]
            if len(set(approvals)) != len(approvals):
                raise changed()
            result = InvoiceOperationResult(
                operation_id=operation_id,
                client_reference=plan.preview.client_reference,
                operation=plan.preview.operation,
                status="issued",
                approval_numbers=approvals,
            )
            journal.finish(operation_id, "complete")
            plan.result = result
            return result.model_copy(deep=True)
        except BaseException as exc:
            try:
                journal.finish(operation_id, "unknown" if dispatched else "rejected")
            except Exception:
                pass  # The previously committed in_flight row remains blocking.
            if isinstance(exc, asyncio.CancelledError) or not isinstance(exc, Exception):
                raise
            if dispatched:
                raise LoginError(
                    "INVOICE_OUTCOME_UNKNOWN",
                    "발행 결과를 확정하지 못했습니다. 새 요청으로 재발행하지 마세요.",
                    502,
                ) from None
            if isinstance(exc, LoginError):
                raise
            raise changed() from None
