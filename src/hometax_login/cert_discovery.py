"""공동인증서(NPKI) 탐색과 선택 기억.

인증서 파일만 읽으므로 비밀번호가 필요 없습니다. 선택 결과는
`$HOMETAX_HOME/config.toml`(기본 `~/.hometax/config.toml`, 0600)에 경로와 지문만
남기며 비밀번호는 저장하지 않습니다.
"""

from __future__ import annotations

import hashlib
import os
import sys
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cryptography import x509
from cryptography.x509.oid import NameOID

CERT_FILE = "signCert.der"
KEY_FILE = "signPri.key"
SEARCH_PATH_ENV = "HOMETAX_NPKI_PATH"
HOME_ENV = "HOMETAX_HOME"


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class CertificateEntry:
    cert_path: Path
    key_path: Path
    subject: str
    common_name: str
    issuer: str
    valid_from: datetime
    valid_until: datetime
    fingerprint: str

    def is_expired(self, now: datetime | None = None) -> bool:
        return self.valid_until <= (now or datetime.now(UTC))

    def days_left(self, now: datetime | None = None) -> int:
        return (self.valid_until - (now or datetime.now(UTC))).days

    def label(self, now: datetime | None = None) -> str:
        until = self.valid_until.strftime("%Y-%m-%d")
        if self.is_expired(now):
            return f"{self.common_name} | {until} 만료됨"
        return f"{self.common_name} | {until}까지 ({self.days_left(now)}일 남음)"


def home_dir() -> Path:
    return Path(os.environ.get(HOME_ENV, "~/.hometax")).expanduser()


def config_path() -> Path:
    return home_dir() / "config.toml"


def default_search_paths() -> list[Path]:
    """환경변수 지정 경로가 있으면 그것만 본다. 없으면 표준 위치를 훑는다."""
    configured = os.environ.get(SEARCH_PATH_ENV, "").strip()
    if configured:
        return [Path(p).expanduser() for p in configured.split(os.pathsep) if p.strip()]

    paths = [Path("NPKI"), Path("~/NPKI").expanduser()]
    if sys.platform == "darwin":
        paths.append(Path("~/Library/Preferences/NPKI").expanduser())
        paths.extend(sorted(Path("/Volumes").glob("*/NPKI")))
    elif os.name == "nt":
        for variable in ("APPDATA", "LOCALAPPDATA", "USERPROFILE"):
            base = os.environ.get(variable)
            if base:
                paths.append(Path(base) / "NPKI")
                paths.append(Path(base) / "AppData" / "LocalLow" / "NPKI")
    else:
        paths.append(Path("~/.npki").expanduser())
    return paths


def _read_entry(directory: Path) -> CertificateEntry | None:
    cert_path, key_path = directory / CERT_FILE, directory / KEY_FILE
    if not cert_path.is_file() or not key_path.is_file():
        return None
    try:
        raw = cert_path.read_bytes()
        certificate = x509.load_der_x509_certificate(raw)
    except (OSError, ValueError):
        return None
    common_names = certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    issuer_names = certificate.issuer.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)
    return CertificateEntry(
        cert_path=cert_path,
        key_path=key_path,
        subject=certificate.subject.rfc4514_string(),
        common_name=str(common_names[0].value) if common_names else cert_path.parent.name,
        issuer=str(issuer_names[0].value) if issuer_names else "",
        valid_from=_as_utc(certificate.not_valid_before_utc),
        valid_until=_as_utc(certificate.not_valid_after_utc),
        fingerprint=hashlib.sha256(raw).hexdigest(),
    )


def discover(paths: list[Path] | None = None) -> list[CertificateEntry]:
    """주어진 경로(기본: 표준 위치)에서 인증서를 모은다. 만료분도 포함한다."""
    found: dict[str, CertificateEntry] = {}
    for root in paths if paths is not None else default_search_paths():
        root = Path(root).expanduser()
        if not root.is_dir():
            continue
        candidates = [root, *(p for p in root.rglob("*") if p.is_dir())]
        for directory in candidates:
            entry = _read_entry(directory)
            if entry and entry.fingerprint not in found:
                found[entry.fingerprint] = entry
    return sorted(found.values(), key=lambda item: item.valid_until, reverse=True)


def usable(entries: list[CertificateEntry], now: datetime | None = None) -> list[CertificateEntry]:
    """만료된 인증서를 제외한다. 남은 기간이 긴 순서다."""
    return [entry for entry in entries if not entry.is_expired(now)]


def load_selection() -> dict[str, str] | None:
    path = config_path()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    saved = data.get("certificate")
    if not isinstance(saved, dict) or not saved.get("path") or not saved.get("fingerprint"):
        return None
    return {str(key): str(value) for key, value in saved.items()}


def save_selection(entry: CertificateEntry) -> Path:
    directory = home_dir()
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    path = config_path()
    body = (
        "# hometax CLI 설정. 비밀번호는 저장하지 않습니다.\n"
        "[certificate]\n"
        f'path = "{entry.cert_path}"\n'
        f'fingerprint = "{entry.fingerprint}"\n'
        f'common_name = "{entry.common_name}"\n'
        f'valid_until = "{entry.valid_until.date().isoformat()}"\n'
        f'saved_at = "{datetime.now(UTC).date().isoformat()}"\n'
    )
    path.write_text(body, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def clear_selection() -> None:
    config_path().unlink(missing_ok=True)


def resolve_selection(
    entries: list[CertificateEntry],
    saved: dict[str, str] | None,
    now: datetime | None = None,
) -> tuple[CertificateEntry | None, str]:
    """저장된 선택을 그대로 쓸 수 있는지 판정한다.

    반환 사유: saved(그대로 사용) · changed(같은 경로에 다른 인증서) ·
    missing(경로 사라짐) · expired(저장된 것이 만료) · single(후보 1개) ·
    choose(사용자 선택 필요) · empty(쓸 수 있는 인증서 없음).
    """
    live = usable(entries, now)
    if saved:
        same_path = [e for e in entries if str(e.cert_path) == saved.get("path")]
        if not same_path:
            return None, "missing"
        entry = same_path[0]
        if entry.fingerprint != saved.get("fingerprint"):
            return None, "changed"
        if entry.is_expired(now):
            return None, "expired"
        return entry, "saved"
    if not live:
        return None, "empty"
    if len(live) == 1:
        return live[0], "single"
    return None, "choose"
