"""CLI용 로컬 세션 캐시.

명령마다 인증서 로그인을 다시 하지 않도록 홈택스 세션 쿠키를
`$HOMETAX_HOME/session.json`(기본 `~/.hometax/session.json`, 0600)에 보관한다.
인증서·비밀번호는 저장하지 않으며, 만료된 파일은 읽지 않는다.

HTTP API 서버는 이 모듈을 쓰지 않는다. 서버는 쿠키를 프로세스 메모리에만 둔다.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .cert_discovery import home_dir
from .protocol import HometaxClient

DEFAULT_TTL_SECONDS = 600
SESSION_FILE = "session.json"


def session_path() -> Path:
    return home_dir() / SESSION_FILE


def default_ttl() -> int:
    raw = os.environ.get("HOMETAX_SESSION_TTL", "").strip()
    try:
        ttl = int(raw)
    except ValueError:
        return DEFAULT_TTL_SECONDS
    return ttl if ttl > 0 else DEFAULT_TTL_SECONDS


def save_session(client: HometaxClient, identity: dict, ttl_seconds: int | None = None) -> Path:
    ttl = default_ttl() if ttl_seconds is None else ttl_seconds
    now = time.time()
    body = {
        "cookies": {name: value for name, value in client.http.cookies.items()},
        "identity": {str(k): v for k, v in (identity or {}).items()},
        "saved_at": now,
        "expires_at": now + ttl,
    }
    directory = home_dir()
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    path = session_path()
    path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def load_session() -> dict | None:
    """만료·손상·부재면 None. 호출자는 새로 로그인하면 된다."""
    try:
        body = json.loads(session_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(body, dict) or not isinstance(body.get("cookies"), dict):
        return None
    expires_at = body.get("expires_at")
    if not isinstance(expires_at, int | float) or expires_at <= time.time():
        return None
    return body


def clear_session() -> None:
    session_path().unlink(missing_ok=True)


def client_from_session(body: dict | None) -> HometaxClient:
    """저장된 쿠키로 클라이언트를 복원한다. 유효성은 verify() 로 확인할 것."""
    if not body or not isinstance(body.get("cookies"), dict):
        raise ValueError("복원할 세션이 없습니다")
    client = HometaxClient()
    for name, value in body["cookies"].items():
        client.http.cookies.set(str(name), str(value), domain="hometax.go.kr")
    return client


def remaining_seconds(body: dict | None) -> int:
    if not body:
        return 0
    return max(0, int(body.get("expires_at", 0) - time.time()))
