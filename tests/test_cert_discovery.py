from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from hometax_login.cert_discovery import (
    CertificateEntry,
    discover,
    load_selection,
    resolve_selection,
    save_selection,
    usable,
)

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def write_certificate(
    directory: Path,
    *,
    common_name: str = "예시컨설팅(HONG GIL DONG)",
    days_left: int = 400,
    with_key: bool = True,
) -> Path:
    now = datetime.now(UTC)
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "KR"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "yessign"),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ]
    )
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(KEY.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=30))
        .not_valid_after(now + timedelta(days=days_left))
        .sign(KEY, hashes.SHA256())
    )
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "signCert.der").write_bytes(certificate.public_bytes(serialization.Encoding.DER))
    if with_key:
        (directory / "signPri.key").write_bytes(b"encrypted-private-key")
    return directory


def test_discover_reads_every_certificate_directory_and_skips_incomplete_ones(tmp_path):
    root = tmp_path / "NPKI"
    write_certificate(root / "cn=valid", common_name="유효회사", days_left=400)
    write_certificate(root / "cn=expired", common_name="만료회사", days_left=-1)
    write_certificate(root / "cn=nokey", common_name="키없음", with_key=False)
    (root / "cn=broken").mkdir(parents=True)
    (root / "cn=broken" / "signCert.der").write_bytes(b"not-a-certificate")
    (root / "cn=broken" / "signPri.key").write_bytes(b"x")

    entries = discover([root])

    names = {entry.common_name for entry in entries}
    assert names == {"유효회사", "만료회사"}
    assert {entry.is_expired() for entry in entries} == {False, True}
    assert all(entry.key_path.name == "signPri.key" for entry in entries)


def test_usable_drops_expired_entries_and_orders_by_remaining_validity(tmp_path):
    root = tmp_path / "NPKI"
    write_certificate(root / "a", common_name="짧은쪽", days_left=10)
    write_certificate(root / "b", common_name="긴쪽", days_left=500)
    write_certificate(root / "c", common_name="만료", days_left=-5)

    live = usable(discover([root]))

    assert [entry.common_name for entry in live] == ["긴쪽", "짧은쪽"]


def test_discover_deduplicates_the_same_certificate_found_twice(tmp_path):
    first = write_certificate(tmp_path / "one" / "cn=same")
    second = tmp_path / "two" / "cn=same"
    second.mkdir(parents=True)
    (second / "signCert.der").write_bytes((first / "signCert.der").read_bytes())
    (second / "signPri.key").write_bytes(b"encrypted-private-key")

    entries = discover([tmp_path / "one", tmp_path / "two"])

    assert len(entries) == 1
    assert entries[0].cert_path.parent == first


def test_save_and_load_selection_round_trip_with_owner_only_permissions(tmp_path, monkeypatch):
    monkeypatch.setenv("HOMETAX_HOME", str(tmp_path / "home"))
    entry = discover([write_certificate(tmp_path / "NPKI" / "cn=one").parent])[0]

    save_selection(entry)
    saved = load_selection()

    assert saved is not None
    assert saved["path"] == str(entry.cert_path)
    assert saved["fingerprint"] == entry.fingerprint
    config = tmp_path / "home" / "config.toml"
    assert config.stat().st_mode & 0o777 == 0o600
    assert config.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize(
    "case, expected",
    [
        ("match", "saved"),
        ("renewed", "changed"),
        ("gone", "missing"),
        ("expired", "expired"),
        ("single", "single"),
        ("many", "choose"),
        ("none", "empty"),
    ],
)
def test_resolve_selection_reports_why_a_certificate_needs_confirming(tmp_path, case, expected):
    root = tmp_path / "NPKI"
    first = discover([write_certificate(root / "cn=one", common_name="첫번째").parent])[0]
    saved = {"path": str(first.cert_path), "fingerprint": first.fingerprint}

    if case == "match":
        entries, saved_record = [first], saved
    elif case == "renewed":
        entries, saved_record = [first], {**saved, "fingerprint": "0" * 64}
    elif case == "gone":
        entries, saved_record = [first], {**saved, "path": str(tmp_path / "moved" / "signCert.der")}
    elif case == "expired":
        write_certificate(root / "cn=old", common_name="만료", days_left=-1)
        stale = [e for e in discover([root]) if e.common_name == "만료"][0]
        entries = discover([root])
        saved_record = {"path": str(stale.cert_path), "fingerprint": stale.fingerprint}
    elif case == "single":
        entries, saved_record = [first], None
    elif case == "many":
        write_certificate(root / "cn=two", common_name="두번째")
        entries, saved_record = discover([root]), None
    else:
        entries, saved_record = [], None

    selected, reason = resolve_selection(entries, saved_record)

    assert reason == expected
    if expected == "saved":
        assert selected == first
    elif expected == "single":
        assert selected == first
    else:
        assert selected is None


def test_entry_label_shows_name_and_expiry(tmp_path):
    entry = discover([write_certificate(tmp_path / "cn=one", days_left=400).parent])[0]

    label = entry.label()

    assert "예시컨설팅" in label
    assert entry.valid_until.strftime("%Y-%m-%d") in label
    assert isinstance(entry, CertificateEntry)
