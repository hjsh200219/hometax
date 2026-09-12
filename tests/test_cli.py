from __future__ import annotations

import json
import unicodedata
from datetime import UTC, date, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from hometax_login import cli
from hometax_login.cert_discovery import discover, load_selection, save_selection
from hometax_login.invoices import InvoiceFilters, TaxInvoiceSummary

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def write_certificate(directory, *, common_name="예시컨설팅", days_left=400):
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
    (directory / "signPri.key").write_bytes(b"encrypted-private-key")
    return directory


@pytest.fixture
def npki(tmp_path, monkeypatch):
    root = tmp_path / "NPKI"
    monkeypatch.setenv("HOMETAX_NPKI_PATH", str(root))
    monkeypatch.setenv("HOMETAX_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HOMETAX_PW", raising=False)
    return root


def parse(argv):
    return cli.build_parser().parse_args(argv)


def test_split_periods_respects_the_three_month_limit():
    spans = cli.split_periods(date(2026, 1, 1), date(2026, 9, 12))

    assert spans == [
        (date(2026, 1, 1), date(2026, 3, 31)),
        (date(2026, 4, 1), date(2026, 6, 30)),
        (date(2026, 7, 1), date(2026, 9, 12)),
    ]
    assert all((end - start).days < 93 for start, end in spans)


def test_split_periods_keeps_a_short_range_in_one_call():
    assert cli.split_periods(date(2026, 5, 1), date(2026, 5, 31)) == [
        (date(2026, 5, 1), date(2026, 5, 31))
    ]


def test_parse_range_ytd_starts_on_january_first():
    start, end = cli.parse_range(parse(["invoices", "--ytd"]))

    assert start == date(end.year, 1, 1)
    assert end == datetime.now(UTC).astimezone().date()


def test_parse_range_requires_a_period():
    with pytest.raises(cli.CommandError):
        cli.parse_range(parse(["invoices"]))


def test_pick_certificate_uses_the_remembered_choice_without_asking(npki, monkeypatch):
    write_certificate(npki / "cn=one", common_name="첫번째")
    write_certificate(npki / "cn=two", common_name="두번째")
    remembered = [e for e in discover([npki]) if e.common_name == "두번째"][0]
    save_selection(remembered)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)

    assert cli.pick_certificate(parse(["login"])).fingerprint == remembered.fingerprint


def test_pick_certificate_asks_again_when_the_saved_one_was_renewed(npki, monkeypatch, capsys):
    directory = write_certificate(npki / "cn=one")
    entry = discover([npki])[0]
    save_selection(entry)
    write_certificate(directory, days_left=800)  # 같은 경로에 갱신된 인증서
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)

    with pytest.raises(cli.CommandError):
        cli.pick_certificate(parse(["login"]))
    assert "갱신된" in capsys.readouterr().err


def test_pick_certificate_refuses_to_guess_when_several_are_usable(npki, monkeypatch):
    write_certificate(npki / "cn=one", common_name="첫번째")
    write_certificate(npki / "cn=two", common_name="두번째")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)

    with pytest.raises(cli.CommandError) as caught:
        cli.pick_certificate(parse(["login"]))
    assert "--cert" in str(caught.value)


def test_pick_certificate_reports_when_every_certificate_expired(npki):
    write_certificate(npki / "cn=old", days_left=-1)

    with pytest.raises(cli.CommandError) as caught:
        cli.pick_certificate(parse(["login"]))
    assert "쓸 수 있는 인증서가 없습니다" in str(caught.value)


def test_pick_certificate_accepts_a_folder_path_in_composed_unicode(npki):
    # 홈택스가 만든 폴더는 이름이 NFD 로 저장돼 있고, 사용자가 넘기는 인자는 NFC 다.
    # 문자열 비교로는 어긋나므로 같은 파일인지로 판정해야 한다.
    decomposed = unicodedata.normalize("NFD", "cn=예시컨설팅")
    directory = write_certificate(npki / decomposed)
    composed = unicodedata.normalize("NFC", str(directory))
    assert composed != str(directory)

    entry = cli.pick_certificate(parse(["login", "--cert", composed]))

    assert entry.cert_path.parent.samefile(directory)


def test_maybe_remember_saves_only_when_asked(npki, monkeypatch):
    write_certificate(npki / "cn=one")
    entry = discover([npki])[0]
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)

    cli.maybe_remember(entry, parse(["login", "--no-remember"]))
    assert load_selection() is None

    cli.maybe_remember(entry, parse(["login", "--remember"]))
    assert load_selection()["fingerprint"] == entry.fingerprint


def test_read_password_prefers_the_environment_and_refuses_to_block(npki, monkeypatch):
    monkeypatch.setenv("HOMETAX_PW", "from-env")
    assert cli.read_password() == "from-env"

    monkeypatch.delenv("HOMETAX_PW")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    with pytest.raises(cli.CommandError):
        cli.read_password()


def test_status_and_logout_report_a_missing_session(npki, capsys):
    assert cli.main(["--json", "status"]) == 1
    assert json.loads(capsys.readouterr().out) == {"active": False}

    assert cli.main(["logout"]) == 0
    assert "지웠습니다" in capsys.readouterr().out


def test_summary_splits_the_period_and_totals_every_span(npki, monkeypatch, capsys):
    calls = []

    class FakeInvoices:
        async def summary(self, filters: InvoiceFilters):
            calls.append((filters.start_date, filters.end_date))
            return TaxInvoiceSummary(
                company_name="예시컨설팅",
                filters=filters,
                total_count=2,
                supply_amount=1_000,
                tax_amount=100,
                total_amount=1_100,
            )

    class FakeClient:
        def __init__(self):
            self.invoices = FakeInvoices()

        async def close(self):
            calls.append("closed")

    async def fake_session(args):
        return FakeClient(), {"user_name": "예시컨설팅"}

    monkeypatch.setattr(cli, "session_client", fake_session)

    exit_code = cli.main(
        [
            "--json",
            "summary",
            "--from",
            "2026-01-01",
            "--to",
            "2026-09-12",
            "--direction",
            "purchases",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert len(payload["periods"]) == 3
    assert payload["count"] == 6
    assert payload["supply_amount"] == 3_000
    assert payload["direction"] == "purchases"
    assert calls[-1] == "closed"


def test_main_returns_two_on_a_usage_error(npki, capsys):
    assert cli.main(["invoices"]) == 2
    assert "--from" in capsys.readouterr().err
