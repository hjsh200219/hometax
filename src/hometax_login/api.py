import asyncio
import base64
import binascii
import hashlib
import hmac
import os
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StrictBool, model_validator

from .certificates import CertificateError, load_certificate
from .counterparty_changes import (
    ApplyCounterpartyChange,
    CounterpartyChangePreview,
    CounterpartyChangeResult,
    CounterpartyCreate,
    CounterpartyPatch,
)
from .errors import LoginError
from .invoices import (
    CounterpartyPage,
    CounterpartyQuery,
    InvoiceFilters,
    InvoiceQuery,
    TaxInvoiceDetail,
    TaxInvoicePage,
    TaxInvoiceSummary,
)
from .issuance_models import (
    InvoiceCancelRequest,
    InvoiceCorrectionRequest,
    InvoiceIssueRequest,
    InvoiceOperationPreview,
    InvoiceOperationResult,
)
from .protocol import HometaxClient
from .sessions import SessionStore
from .write_journal import WriteJournal


@dataclass
class Settings:
    api_keys: tuple[str, ...] = field(repr=False)
    session_ttl: int = 600
    session_capacity: int = 128
    concurrent_logins: int = 4
    counterparty_writes_enabled: bool = False
    invoice_writes_enabled: bool = False
    write_journal_path: str = ".state/counterparty-writes.sqlite3"
    invoice_wire_encoding: Literal["raw", "base64"] | None = None

    def __post_init__(self):
        if type(self.counterparty_writes_enabled) is not bool:
            raise ValueError("counterparty_writes_enabled must be a boolean")
        if type(self.invoice_writes_enabled) is not bool:
            raise ValueError("invoice_writes_enabled must be a boolean")
        if not isinstance(self.write_journal_path, str) or not self.write_journal_path.strip():
            raise ValueError("write_journal_path must be a non-empty path")
        if self.invoice_wire_encoding not in {None, "raw", "base64"}:
            raise ValueError("invoice_wire_encoding must be 'raw', 'base64', or None")
        if self.invoice_writes_enabled and self.invoice_wire_encoding is None:
            raise ValueError("invoice_wire_encoding is required when invoice writes are enabled")
        if not self.api_keys or any(len(k) < 32 for k in self.api_keys):
            raise ValueError("HOMETAX_API_KEYS must contain API keys of at least 32 characters")
        if not 30 <= self.session_ttl <= 3600:
            raise ValueError("Session TTL must be between 30 and 3600 seconds")
        if not 1 <= self.session_capacity <= 1024 or not 1 <= self.concurrent_logins <= 16:
            raise ValueError("Invalid session capacity or login concurrency")

    @classmethod
    def from_env(cls):
        counterparty_writes_enabled = env_bool("HOMETAX_COUNTERPARTY_WRITES_ENABLED", "false")
        invoice_writes_enabled = env_bool("HOMETAX_INVOICE_WRITES_ENABLED", "false")
        invoice_wire_encoding = os.getenv("HOMETAX_INVOICE_WIRE_ENCODING", "") or None
        return cls(
            api_keys=tuple(
                k.strip() for k in os.getenv("HOMETAX_API_KEYS", "").split(",") if k.strip()
            ),
            session_ttl=int(os.getenv("HOMETAX_SESSION_TTL", "600")),
            counterparty_writes_enabled=counterparty_writes_enabled,
            invoice_writes_enabled=invoice_writes_enabled,
            write_journal_path=os.getenv(
                "HOMETAX_WRITE_JOURNAL_PATH", ".state/counterparty-writes.sqlite3"
            ),
            invoice_wire_encoding=invoice_wire_encoding,
        )


class CertificateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cert_type: Literal["der", "pfx"]
    cert_file: SecretStr
    key_file: SecretStr | None = None
    password: SecretStr = Field(min_length=1, max_length=1024)


class LoginRequest(CertificateRequest):
    # Explicit because public reference implementations differ; do not silently retry modes.
    login_type: Literal["03", "04"]


class SubmitRequest(CertificateRequest):
    cert_type: Literal["der"]
    key_file: SecretStr
    confirm: StrictBool
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def confirmation(self):
        if not self.confirm:
            raise ValueError("Explicit confirmation is required")
        return self


def env_bool(name: str, default: str) -> bool:
    value = os.getenv(name, default)
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be 'true' or 'false'")
    return value == "true"


def decode_file(value: SecretStr | None) -> bytes | None:
    if value is None:
        return None
    encoded = value.get_secret_value()
    if not encoded or len(encoded) > 350_000:
        raise LoginError("INVALID_CERT_FILE", "인증서 파일 크기가 잘못됐습니다.", 422)
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise LoginError(
            "INVALID_CERT_FILE", "인증서 파일은 Base64 형식이어야 합니다.", 422
        ) from None
    if not data or len(data) > 256 * 1024:
        raise LoginError("INVALID_CERT_FILE", "인증서 파일 크기가 잘못됐습니다.", 422)
    return data


class BodyLimit:
    """Bound the body before JSON parsing; also handles chunked request bodies."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        messages, total = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            total += len(message.get("body", b""))
            if total > 2 * 1024 * 1024:
                response = JSONResponse({"error": {"code": "BODY_TOO_LARGE"}}, status_code=413)
                return await response(scope, receive, send)
            messages.append(message)
            if not message.get("more_body", False):
                break

        async def replay():
            if messages:
                return messages.pop(0)
            return await receive()

        await self.app(scope, replay, send)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = SessionStore(settings.session_ttl, settings.session_capacity)
    semaphore = asyncio.Semaphore(settings.concurrent_logins)

    @asynccontextmanager
    async def lifespan(app):
        async def reap():
            while True:
                await asyncio.sleep(30)
                await store.prune()

        task = asyncio.create_task(reap())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            await store.close()

    app = FastAPI(title="HomeTax API", version="0.4.0", lifespan=lifespan)
    app.add_middleware(BodyLimit)
    app.state.client_factory = HometaxClient
    write_journal = (
        WriteJournal(settings.write_journal_path)
        if settings.counterparty_writes_enabled or settings.invoice_writes_enabled
        else None
    )
    app.state.counterparty_journal = write_journal if settings.counterparty_writes_enabled else None
    app.state.invoice_journal = write_journal if settings.invoice_writes_enabled else None
    app.state.sessions = store
    bearer = HTTPBearer(auto_error=False)

    @app.middleware("http")
    async def hometax_no_store(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/v1/hometax"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"] = "no-cache"
        return response

    async def authorize(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)] = None,
    ):
        token = credentials.credentials if credentials else ""
        matches = [hmac.compare_digest(token.encode(), key.encode()) for key in settings.api_keys]
        if not any(matches):
            raise LoginError("UNAUTHORIZED", "API 인증이 필요합니다.", 401)
        return hashlib.sha256(token.encode()).hexdigest()

    def hometax_cache_headers(request: Request):
        if not request.url.path.startswith("/v1/hometax"):
            return {}
        return {"Cache-Control": "no-store", "Pragma": "no-cache"}

    @app.exception_handler(LoginError)
    async def login_error(request: Request, error: LoginError):
        return JSONResponse(
            {"error": {"code": error.code, "message": error.message}},
            status_code=error.status,
            headers=hometax_cache_headers(request),
        )

    @app.exception_handler(CertificateError)
    async def certificate_error(request: Request, error: CertificateError):
        return JSONResponse(
            {"error": {"code": error.code, "message": error.message}},
            status_code=422,
            headers=hometax_cache_headers(request),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        # Pydantic's default error input may contain certificate bytes or passwords.
        return JSONResponse(
            {"error": {"code": "INVALID_REQUEST", "message": "요청 필드와 형식을 확인하세요."}},
            status_code=422,
            headers=hometax_cache_headers(request),
        )

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    async def material_for(payload: CertificateRequest):
        return await asyncio.to_thread(
            load_certificate,
            decode_file(payload.cert_file),
            decode_file(payload.key_file),
            payload.password.get_secret_value(),
            payload.cert_type,
        )

    @app.post("/v1/certificates/validate")
    async def validate(payload: CertificateRequest, owner: Annotated[str, Depends(authorize)]):
        if semaphore.locked():
            raise LoginError("BUSY", "인증 요청을 처리 중입니다. 잠시 후 다시 시도하세요.", 429)
        async with semaphore:
            material = await material_for(payload)
            return {
                "certificate": material.metadata(),
                "hometax_random_present": bool(material.random_number),
            }

    @app.post("/v1/hometax/sessions", status_code=201)
    async def login(payload: LoginRequest, owner: Annotated[str, Depends(authorize)]):
        if semaphore.locked():
            raise LoginError("BUSY", "인증 요청을 처리 중입니다. 잠시 후 다시 시도하세요.", 429)
        async with semaphore:
            client = app.state.client_factory()
            client.invoice_wire_encoding = settings.invoice_wire_encoding
            transferred = False
            try:
                async with asyncio.timeout(60):
                    material = await material_for(payload)
                    identity = await client.login(material, payload.login_type)
                    item = await store.add(owner, client, identity)
                    transferred = True
                    return session_response(item)
            except TimeoutError:
                raise LoginError("LOGIN_TIMEOUT", "전체 로그인 시간이 초과됐습니다.", 504) from None
            finally:
                if not transferred:
                    await client.close()

    @app.get("/v1/hometax/sessions/{session_id}")
    async def session_status(session_id: str, owner: Annotated[str, Depends(authorize)]):
        async with store.lease(session_id, owner) as item:
            item.identity = await item.client.verify()
            return session_response(item)

    @asynccontextmanager
    async def invoice_service(session_id: str, owner: str):
        try:
            async with asyncio.timeout(60):
                async with store.lease(session_id, owner) as item:
                    yield item.client.invoices
        except TimeoutError:
            raise LoginError("INVOICE_TIMEOUT", "홈택스 조회 시간이 초과됐습니다.", 504) from None

    @asynccontextmanager
    async def management_service(session_id: str, owner: str):
        try:
            async with asyncio.timeout(60):
                async with store.lease(session_id, owner) as item:
                    yield item.client.counterparty_changes
        except TimeoutError:
            raise LoginError("INVOICE_TIMEOUT", "홈택스 조회 시간이 초과됐습니다.", 504) from None

    @asynccontextmanager
    async def invoice_operation_service(session_id: str, owner: str):
        try:
            async with asyncio.timeout(60):
                async with store.lease(session_id, owner) as item:
                    yield item.client.invoice_operations
        except TimeoutError:
            raise LoginError("INVOICE_TIMEOUT", "홈택스 조회 시간이 초과됐습니다.", 504) from None

    @app.get(
        "/v1/hometax/sessions/{session_id}/tax-invoices",
        response_model=TaxInvoicePage,
    )
    async def tax_invoices(
        session_id: str,
        owner: Annotated[str, Depends(authorize)],
        query: Annotated[InvoiceQuery, Query()],
    ):
        async with invoice_service(session_id, owner) as invoices:
            return await invoices.list(query)

    @app.get(
        "/v1/hometax/sessions/{session_id}/tax-invoices/summary",
        response_model=TaxInvoiceSummary,
    )
    async def tax_invoice_summary(
        session_id: str,
        owner: Annotated[str, Depends(authorize)],
        filters: Annotated[InvoiceFilters, Query()],
    ):
        async with invoice_service(session_id, owner) as invoices:
            return await invoices.summary(filters)

    @app.post(
        "/v1/hometax/sessions/{session_id}/tax-invoices/drafts",
        response_model=InvoiceOperationPreview,
    )
    async def preview_invoice_issue(
        session_id: str,
        payload: InvoiceIssueRequest,
        owner: Annotated[str, Depends(authorize)],
    ):
        async with invoice_operation_service(session_id, owner) as operations:
            return await operations.preview_issue(payload)

    @app.get(
        "/v1/hometax/sessions/{session_id}/tax-invoices/{approval_number}",
        response_model=TaxInvoiceDetail,
    )
    async def tax_invoice_detail(
        session_id: str,
        approval_number: Annotated[str, Path(pattern=r"^[0-9]{8}[A-Za-z0-9]{16}$")],
        owner: Annotated[str, Depends(authorize)],
    ):
        async with invoice_service(session_id, owner) as invoices:
            return await invoices.detail(approval_number)

    @app.post(
        "/v1/hometax/sessions/{session_id}/tax-invoices/{approval_number}/corrections",
        response_model=InvoiceOperationPreview,
    )
    async def preview_invoice_correction(
        session_id: str,
        approval_number: Annotated[str, Path(pattern=r"^[0-9]{8}[A-Za-z0-9]{16}$")],
        payload: InvoiceCorrectionRequest,
        owner: Annotated[str, Depends(authorize)],
    ):
        async with invoice_operation_service(session_id, owner) as operations:
            return await operations.preview_correct(approval_number, payload)

    @app.post(
        "/v1/hometax/sessions/{session_id}/tax-invoices/{approval_number}/cancellations",
        response_model=InvoiceOperationPreview,
    )
    async def preview_invoice_cancellation(
        session_id: str,
        approval_number: Annotated[str, Path(pattern=r"^[0-9]{8}[A-Za-z0-9]{16}$")],
        payload: InvoiceCancelRequest,
        owner: Annotated[str, Depends(authorize)],
    ):
        async with invoice_operation_service(session_id, owner) as operations:
            return await operations.preview_cancel(approval_number, payload)

    @app.post(
        "/v1/hometax/sessions/{session_id}/tax-invoice-operations/{operation_id}/submit",
        response_model=InvoiceOperationResult,
    )
    async def submit_invoice_operation(
        session_id: str,
        operation_id: Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{16,100}$")],
        payload: SubmitRequest,
        owner: Annotated[str, Depends(authorize)],
    ):
        journal = app.state.invoice_journal
        if journal is None:
            raise LoginError(
                "INVOICE_WRITES_DISABLED",
                "세금계산서 발행 기능이 비활성화되어 있습니다.",
                403,
            )
        async with invoice_operation_service(session_id, owner) as operations:
            material = await material_for(payload)
            return await operations.submit(
                operation_id,
                material,
                journal,
                payload.content_digest,
            )

    @app.get(
        "/v1/hometax/sessions/{session_id}/counterparties",
        response_model=CounterpartyPage,
    )
    async def counterparties(
        session_id: str,
        owner: Annotated[str, Depends(authorize)],
        query: Annotated[CounterpartyQuery, Query()],
    ):
        async with invoice_service(session_id, owner) as invoices:
            return await invoices.counterparties(query)

    @app.post(
        "/v1/hometax/sessions/{session_id}/counterparties",
        response_model=CounterpartyChangePreview,
    )
    async def preview_counterparty_create(
        session_id: str,
        payload: CounterpartyCreate,
        owner: Annotated[str, Depends(authorize)],
    ):
        async with management_service(session_id, owner) as changes:
            return await changes.preview_create(payload)

    @app.patch(
        "/v1/hometax/sessions/{session_id}/counterparties/{business_number}",
        response_model=CounterpartyChangePreview,
    )
    async def preview_counterparty_update(
        session_id: str,
        business_number: Annotated[str, Path(pattern=r"^[0-9]{10}$")],
        payload: CounterpartyPatch,
        owner: Annotated[str, Depends(authorize)],
        branch_number: Annotated[str, Query(pattern=r"^(?:[0-9]{4})?$")] = "",
    ):
        async with management_service(session_id, owner) as changes:
            return await changes.preview_update(business_number, branch_number, payload)

    @app.delete(
        "/v1/hometax/sessions/{session_id}/counterparties/{business_number}",
        response_model=CounterpartyChangePreview,
    )
    async def preview_counterparty_delete(
        session_id: str,
        business_number: Annotated[str, Path(pattern=r"^[0-9]{10}$")],
        owner: Annotated[str, Depends(authorize)],
        branch_number: Annotated[str, Query(pattern=r"^(?:[0-9]{4})?$")] = "",
    ):
        async with management_service(session_id, owner) as changes:
            return await changes.preview_delete(business_number, branch_number)

    @app.post(
        "/v1/hometax/sessions/{session_id}/counterparty-changes/{change_id}/apply",
        response_model=CounterpartyChangeResult,
    )
    async def apply_counterparty_change(
        session_id: str,
        change_id: Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{16,100}$")],
        payload: ApplyCounterpartyChange,
        owner: Annotated[str, Depends(authorize)],
    ):
        journal = app.state.counterparty_journal
        if journal is None:
            raise LoginError(
                "COUNTERPARTY_WRITES_DISABLED",
                "거래처 쓰기 기능이 비활성화되어 있습니다.",
                403,
            )
        async with management_service(session_id, owner) as changes:
            return await changes.apply(change_id, journal)

    @app.delete("/v1/hometax/sessions/{session_id}", status_code=204)
    async def close_session(session_id: str, owner: Annotated[str, Depends(authorize)]):
        await store.remove(session_id, owner)
        return Response(status_code=204)

    return app


def session_response(item):
    return {
        "session_id": item.id,
        "expires_at": datetime.fromtimestamp(item.expires_at, UTC).isoformat(),
        "identity": item.identity,
    }
