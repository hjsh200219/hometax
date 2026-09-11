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

from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .certificates import CertificateError, load_certificate
from .errors import LoginError
from .protocol import HometaxClient
from .sessions import SessionStore


@dataclass
class Settings:
    api_keys: tuple[str, ...] = field(repr=False)
    session_ttl: int = 600
    session_capacity: int = 128
    concurrent_logins: int = 4

    def __post_init__(self):
        if not self.api_keys or any(len(k) < 32 for k in self.api_keys):
            raise ValueError("HOMETAX_API_KEYS must contain API keys of at least 32 characters")
        if not 30 <= self.session_ttl <= 3600:
            raise ValueError("Session TTL must be between 30 and 3600 seconds")
        if not 1 <= self.session_capacity <= 1024 or not 1 <= self.concurrent_logins <= 16:
            raise ValueError("Invalid session capacity or login concurrency")

    @classmethod
    def from_env(cls):
        return cls(
            api_keys=tuple(
                k.strip() for k in os.getenv("HOMETAX_API_KEYS", "").split(",") if k.strip()
            ),
            session_ttl=int(os.getenv("HOMETAX_SESSION_TTL", "600")),
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

    app = FastAPI(title="HomeTax Login API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(BodyLimit)
    app.state.client_factory = HometaxClient
    app.state.sessions = store
    bearer = HTTPBearer(auto_error=False)

    async def authorize(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)] = None,
    ):
        token = credentials.credentials if credentials else ""
        matches = [hmac.compare_digest(token.encode(), key.encode()) for key in settings.api_keys]
        if not any(matches):
            raise LoginError("UNAUTHORIZED", "API 인증이 필요합니다.", 401)
        return hashlib.sha256(token.encode()).hexdigest()

    @app.exception_handler(LoginError)
    async def login_error(request: Request, error: LoginError):
        return JSONResponse(
            {"error": {"code": error.code, "message": error.message}}, status_code=error.status
        )

    @app.exception_handler(CertificateError)
    async def certificate_error(request: Request, error: CertificateError):
        return JSONResponse(
            {"error": {"code": error.code, "message": error.message}}, status_code=422
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        # Pydantic's default error input may contain certificate bytes or passwords.
        return JSONResponse(
            {"error": {"code": "INVALID_REQUEST", "message": "요청 필드와 형식을 확인하세요."}},
            status_code=422,
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
