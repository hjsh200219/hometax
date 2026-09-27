"""공동인증서(NPKI) 탐색과 선택 기억.

인증서 파일만 읽으므로 비밀번호가 필요 없습니다. 기본 인증서 선택과 사업자 별칭은
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


def display_name(common_name: str) -> str:
    """CN 끝의 인증서 일련번호와 빈 괄호를 떼고 보여 준다."""
    import re

    return re.sub(r"\d{8,}$", "", common_name).replace("()", "").strip() or common_name


def is_personal(common_name: str) -> bool:
    """개인 인증서 CN 은 `이름()일련번호` 꼴이다. 사업자 자료가 바로 조회되지 않는다."""
    return "()" in common_name


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


ALIAS_PATTERN = r"[A-Za-z][A-Za-z0-9_-]{0,31}"


def load_config() -> dict:
    """[certificate](기본 인증서)와 [aliases](별칭 → 지문)만 읽는다. 손상되면 빈 설정."""
    try:
        data = tomllib.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _toml_string(value: str) -> str:
    """TOML 기본 문자열. JSON 이스케이프(\\n·\\uXXXX 등)는 TOML 에서도 유효하고,
    JSON 이 그대로 두는 DEL(0x7F)만 TOML 이 금지하므로 따로 바꾼다."""
    import json

    return json.dumps(str(value), ensure_ascii=False).replace("\x7f", "\\u007f")


def save_config(config: dict) -> Path:
    """문자열 값만 담은 두 표를 쓴다. 다른 표는 보존하지 않는다(이 파일은 CLI 전용)."""
    directory = home_dir()
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    lines = ["# hometax CLI 설정. 비밀번호는 저장하지 않습니다."]
    for table in ("certificate", "aliases"):
        values = config.get(table)
        if not isinstance(values, dict) or not values:
            continue
        lines.append(f"[{table}]")
        lines += [f"{key} = {_toml_string(value)}" for key, value in values.items()]
    path = config_path()
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def load_selection() -> dict[str, str] | None:
    saved = load_config().get("certificate")
    if not isinstance(saved, dict) or not saved.get("path") or not saved.get("fingerprint"):
        return None
    return {str(key): str(value) for key, value in saved.items()}


def save_selection(entry: CertificateEntry) -> Path:
    config = load_config()
    config["certificate"] = {
        "path": str(entry.cert_path),
        "fingerprint": entry.fingerprint,
        "common_name": entry.common_name,
        "valid_until": entry.valid_until.date().isoformat(),
        "saved_at": datetime.now(UTC).date().isoformat(),
    }
    return save_config(config)


def clear_selection() -> None:
    config = load_config()
    config.pop("certificate", None)
    save_config(config)


def load_aliases() -> dict[str, str]:
    """별칭 → 인증서 지문."""
    aliases = load_config().get("aliases")
    if not isinstance(aliases, dict):
        return {}
    return {str(name): str(value) for name, value in aliases.items() if isinstance(value, str)}


def save_alias(name: str, entry: CertificateEntry | None) -> Path:
    """entry 가 None 이면 별칭을 지운다."""
    config = load_config()
    aliases = dict(load_aliases())
    if entry is None:
        aliases.pop(name, None)
    else:
        aliases[name] = entry.fingerprint
    config["aliases"] = aliases
    return save_config(config)


def ordered(entries: list[CertificateEntry]) -> list[CertificateEntry]:
    """`hometax list` 번호가 실행마다 바뀌지 않게 상호·지문 순으로 고정한다."""
    return sorted(entries, key=lambda entry: (entry.common_name, entry.fingerprint))


def resolve_company(query: str, entries: list[CertificateEntry]) -> CertificateEntry:
    """별칭 → `hometax list` 번호 → 상호 일부(유일할 때) 순으로 찾는다."""
    import unicodedata

    listed = ordered(entries)
    by_fingerprint = {entry.fingerprint: entry for entry in listed}
    aliases = load_aliases()
    if query in aliases:
        entry = by_fingerprint.get(aliases[query])
        if entry is None:
            raise LookupError(f"별칭 {query} 의 인증서를 찾지 못했습니다(옮겼거나 갱신됨).")
        return entry
    if query.isdigit():
        index = int(query)
        if 1 <= index <= len(listed):
            return listed[index - 1]
        raise LookupError(f"목록 번호는 1~{len(listed)} 입니다: {query}")
    wanted = unicodedata.normalize("NFC", query).casefold()
    matches = [
        entry
        for entry in listed
        if wanted in unicodedata.normalize("NFC", entry.common_name).casefold()
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise LookupError(f"'{query}' 에 맞는 사업자가 없습니다. hometax list 로 확인하세요.")
    names = ", ".join(display_name(entry.common_name) for entry in matches)
    raise LookupError(f"'{query}' 가 여러 사업자에 맞습니다: {names}. 번호나 별칭을 쓰세요.")


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
