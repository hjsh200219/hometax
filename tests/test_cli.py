from __future__ import annotations

import json
import unicodedata
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from hometax_login import cli
from hometax_login.cert_discovery import discover, load_selection, save_selection
from hometax_login.financials import AmountBreakdown, CashReceiptPurchasePage
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


def test_parse_range_ytd_uses_the_seoul_clock():
    # 검증 게이트가 Asia/Seoul 기준이라 호스트 시간대와 무관하게 같은 날짜여야 한다.
    start, end = cli.parse_range(parse(["invoices", "--ytd"]))

    assert start == date(end.year, 1, 1)
    assert end == datetime.now(ZoneInfo("Asia/Seoul")).date()
    assert cli.today_kst() == end


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
    assert json.loads(capsys.readouterr().out) == {"active": []}

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


@pytest.mark.parametrize(
    "argv,message",
    [
        (
            ["invoices", "--from", "2026-02-30", "--to", "2026-03-01"],
            "날짜 형식",
        ),
        (
            ["invoices", "--from", "2026-03-02", "--to", "2026-03-01"],
            "시작일",
        ),
        (
            ["cards", "--from", "2026-03-02", "--to", "2026-03-01"],
            "시작일",
        ),
        (
            ["cash-purchases", "--from", "2026-03-02", "--to", "2026-03-01"],
            "시작일",
        ),
    ],
)
def test_date_query_commands_reject_bad_dates_before_session(
    npki, monkeypatch, capsys, argv, message
):
    async def unexpected_session(args):
        raise AssertionError("session_client must not run for invalid input")

    monkeypatch.setattr(cli, "session_client", unexpected_session)

    assert cli.main(argv) == 2

    captured = capsys.readouterr()
    assert message in captured.err
    assert "Traceback" not in captured.err


def test_date_query_commands_reject_future_dates_before_session(npki, monkeypatch, capsys):
    tomorrow = cli.today_kst() + timedelta(days=1)

    async def unexpected_session(args):
        raise AssertionError("session_client must not run for invalid input")

    monkeypatch.setattr(cli, "session_client", unexpected_session)

    assert (
        cli.main(
            [
                "summary",
                "--from",
                cli.today_kst().isoformat(),
                "--to",
                tomorrow.isoformat(),
            ]
        )
        == 2
    )

    captured = capsys.readouterr()
    assert "미래" in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize(
    "argv,message",
    [
        (["cash-sales", "--year", str(cli.today_kst().year + 1)], "미래"),
        (
            [
                "card-sales",
                "--year",
                str(cli.today_kst().year),
                "--quarter-from",
                "4",
                "--quarter-to",
                "1",
            ],
            "quarter-from",
        ),
    ],
)
def test_year_query_commands_validate_before_session(npki, monkeypatch, capsys, argv, message):
    async def unexpected_session(args):
        raise AssertionError("session_client must not run for invalid input")

    monkeypatch.setattr(cli, "session_client", unexpected_session)

    assert cli.main(argv) == 2

    captured = capsys.readouterr()
    assert message in captured.err
    assert "Traceback" not in captured.err


def test_json_option_is_accepted_before_and_after_the_subcommand(npki, monkeypatch, capsys):
    calls = []

    class FakeInvoices:
        async def summary(self, filters: InvoiceFilters):
            calls.append((filters.start_date, filters.end_date))
            return TaxInvoiceSummary(
                company_name="예시컨설팅",
                filters=filters,
                total_count=1,
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

    for argv in (
        ["--json", "summary", "--from", "2026-09-01", "--to", "2026-09-12"],
        ["summary", "--from", "2026-09-01", "--to", "2026-09-12", "--json"],
    ):
        assert cli.main(argv) == 0
        assert json.loads(capsys.readouterr().out)["count"] == 1

    assert calls == [
        (date(2026, 9, 1), date(2026, 9, 12)),
        "closed",
        (date(2026, 9, 1), date(2026, 9, 12)),
        "closed",
    ]


@pytest.mark.parametrize(
    "argv,command",
    [
        (["cards", "--from", "2026-09-01", "--to", "2026-09-12"], "cards"),
        (["registered-cards"], "registered-cards"),
        (["cash-purchases", "--ytd"], "cash-purchases"),
        (["cash-sales", "--year", "2026"], "cash-sales"),
        (
            ["card-sales", "--year", "2026", "--quarter-from", "1", "--quarter-to", "3"],
            "card-sales",
        ),
        (["business-accounts"], "business-accounts"),
    ],
)
def test_financial_commands_are_registered(argv, command):
    args = parse(argv)

    assert args.command == command
    assert callable(args.handler)


def test_cash_purchase_cli_does_not_repeat_period_totals_for_every_page(npki, monkeypatch, capsys):
    calls = []
    totals = AmountBreakdown(
        count=2,
        supply_amount=1000,
        tax_amount=100,
        tax_exempt_amount=0,
        total_amount=1100,
    )
    empty = AmountBreakdown(
        count=0,
        supply_amount=0,
        tax_amount=0,
        tax_exempt_amount=0,
        total_amount=0,
    )

    class FakeFinancials:
        async def cash_receipt_purchases(self, query):
            calls.append(query.page)
            return CashReceiptPurchasePage(
                company_name="예시컨설팅",
                query=query,
                page=query.page,
                page_size=query.page_size,
                total_count=51,
                has_next=query.page == 1,
                eligible_total=totals,
                deductible=totals,
                optional_non_deductible=empty,
                mandatory_non_deductible=empty,
                items=[],
            )

    class FakeClient:
        financials = FakeFinancials()

        async def close(self):
            calls.append("closed")

    async def fake_session(args):
        return FakeClient(), {"user_name": "예시컨설팅"}

    monkeypatch.setattr(cli, "session_client", fake_session)

    exit_code = cli.main(
        [
            "--json",
            "cash-purchases",
            "--from",
            "2026-09-01",
            "--to",
            "2026-09-12",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert calls == [1, 2, "closed"]
    assert payload["eligible_total"]["count"] == 2
    assert payload["eligible_total"]["total_amount"] == 1100


def test_session_client_keeps_a_separate_session_per_certificate(npki, monkeypatch):
    """사업자 A 세션이 살아 있어도 B 를 고르면 B 세션을 찾고, 없으면 B 로 로그인한다."""
    first = write_certificate(npki / "a", common_name="가사업자")
    second = write_certificate(npki / "b", common_name="나사업자")
    entries = {entry.cert_path.parent.name: entry for entry in discover()}
    events = []

    class FakeClient:
        async def verify(self):
            events.append("reused")
            return {"user_name": "가사업자"}

        async def close(self):
            pass

    def fake_load(fingerprint):
        if fingerprint == entries["a"].fingerprint:
            return {"cookies": {}, "cert_fingerprint": fingerprint}
        return None

    monkeypatch.setattr(cli, "load_session", fake_load)
    monkeypatch.setattr(cli, "client_from_session", lambda body: FakeClient())

    async def fake_login(entry, args):
        events.append(("login", entry.cert_path.parent.name))
        return FakeClient(), {"user_name": entry.common_name}

    monkeypatch.setattr(cli, "login_with", fake_login)
    monkeypatch.setattr(cli, "maybe_remember", lambda entry, args: None)

    import asyncio

    asyncio.run(cli.session_client(parse(["summary", "--ytd", "--cert", str(second)])))
    assert events == [("login", "b")]

    events.clear()
    asyncio.run(cli.session_client(parse(["summary", "--ytd", "--company", "가사업자"])))
    assert events == ["reused"]
    assert first.name == "a"


def test_resolve_company_by_alias_number_and_unique_name(npki):
    from hometax_login.cert_discovery import resolve_company, save_alias

    write_certificate(npki / "a", common_name="에스에이치컨설팅")
    write_certificate(npki / "b", common_name="에스에이치랩")
    write_certificate(npki / "c", common_name="다른상사")
    entries = discover()
    listed = [entry.common_name for entry in cli.ordered(entries)]
    assert listed == sorted(listed)

    assert resolve_company("2", entries).common_name == listed[1]
    assert resolve_company("컨설팅", entries).common_name == "에스에이치컨설팅"
    with pytest.raises(LookupError, match="여러 사업자"):
        resolve_company("에스에이치", entries)
    with pytest.raises(LookupError, match="없습니다"):
        resolve_company("없는회사", entries)
    with pytest.raises(LookupError, match="1~3"):
        resolve_company("9", entries)

    lab = next(entry for entry in entries if entry.common_name == "에스에이치랩")
    save_alias("lab", lab)
    assert resolve_company("lab", entries) == lab


def test_saving_the_default_certificate_keeps_aliases(npki):
    from hometax_login.cert_discovery import load_aliases, save_alias

    write_certificate(npki / "a", common_name="가사업자")
    entry = discover()[0]
    save_alias("ga", entry)
    save_selection(entry)
    assert load_aliases() == {"ga": entry.fingerprint}
    assert load_selection()["fingerprint"] == entry.fingerprint


def test_list_numbers_companies_and_marks_default_and_alias(npki, capsys):
    write_certificate(npki / "a", common_name="가사업자")
    write_certificate(npki / "b", common_name="나사업자")
    write_certificate(npki / "c", common_name="다만료", days_left=-1)
    save_selection(next(e for e in discover() if e.common_name == "나사업자"))
    assert cli.main(["alias", "na", "나사업자"]) == 0
    assert "HOMETAX_PW_NA" in capsys.readouterr().out

    assert cli.main(["--json", "list"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [(r["number"], r["common_name"]) for r in rows] == [
        (1, "가사업자"),
        (2, "나사업자"),
        (3, "다만료"),
    ]
    assert rows[1]["default"] is True and rows[1]["aliases"] == ["na"]
    assert rows[2]["expired"] is True

    assert cli.main(["list"]) == 0
    text = capsys.readouterr().out
    assert "--company" in text and "만료" in text


def test_company_option_refuses_an_expired_certificate(npki, monkeypatch, capsys):
    write_certificate(npki / "c", common_name="다만료", days_left=-1)

    async def unexpected_login(entry, args):
        raise AssertionError("must not log in")

    monkeypatch.setattr(cli, "login_with", unexpected_login)
    assert cli.main(["summary", "--ytd", "--company", "1"]) == 2
    assert "만료" in capsys.readouterr().err


def test_alias_password_env_wins_over_the_shared_one(npki, monkeypatch):
    from hometax_login.cert_discovery import save_alias

    write_certificate(npki / "a", common_name="가사업자")
    entry = discover()[0]
    save_alias("ga-1", entry)
    monkeypatch.setenv("HOMETAX_PW", "shared")
    monkeypatch.setenv("HOMETAX_PW_GA_1", "own")

    assert cli.read_password(entry) == "own"
    monkeypatch.delenv("HOMETAX_PW_GA_1")
    assert cli.read_password(entry) == "shared"


def test_alias_rejects_names_that_cannot_become_env_vars(npki, capsys):
    write_certificate(npki / "a", common_name="가사업자")
    assert cli.main(["alias", "가", "1"]) == 2
    assert cli.main(["alias", "ok", "--remove"]) == 2
    assert cli.main(["alias", "ok", "1"]) == 0
    assert cli.main(["alias", "ok", "--remove"]) == 0


def test_sessions_are_stored_per_certificate_and_logout_clears_all(npki, capsys):
    import httpx

    from hometax_login.local_session import load_session, save_session, session_path
    from hometax_login.protocol import HometaxClient

    client = HometaxClient(transport=httpx.MockTransport(lambda request: None))
    save_session(client, {"user_name": "가"}, cert_fingerprint="a" * 64)
    save_session(client, {"user_name": "나"}, cert_fingerprint="b" * 64)
    legacy = session_path().parent.parent / "session.json"
    legacy.write_text("{}", encoding="utf-8")

    assert load_session("a" * 64)["identity"] == {"user_name": "가"}
    assert load_session("b" * 64)["identity"] == {"user_name": "나"}
    assert session_path("a" * 64) != session_path("b" * 64)

    assert cli.main(["logout"]) == 0
    assert "3개" in capsys.readouterr().out
    assert load_session("a" * 64) is None and not legacy.exists()


def test_display_name_drops_the_serial_and_marks_personal_certificates():
    from hometax_login.cert_discovery import display_name, is_personal

    assert display_name("예시컨설팅(HONG GIL DONG)00206822026042423395083") == (
        "예시컨설팅(HONG GIL DONG)"
    )
    assert display_name("홍길동()0020049201507212477229") == "홍길동"
    assert is_personal("홍길동()0020049201507212477229")
    assert not is_personal("예시컨설팅(HONG GIL DONG)00206822026042423395083")


def test_session_reused_without_company_when_it_is_the_only_live_one(npki, monkeypatch):
    """--company 로만 로그인해 기본값이 없어도, 비대화형 후속 명령이 그 세션을 쓴다."""
    write_certificate(npki / "a", common_name="가사업자")
    write_certificate(npki / "b", common_name="나사업자")
    target = next(entry for entry in discover() if entry.common_name == "나사업자")
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(cli, "active_sessions", lambda: [{"cert_fingerprint": target.fingerprint}])
    seen = []

    class FakeClient:
        async def verify(self):
            return {"user_name": "나"}

        async def close(self):
            pass

    def fake_load(fingerprint):
        seen.append(fingerprint)
        return {"cookies": {}, "cert_fingerprint": fingerprint}

    monkeypatch.setattr(cli, "load_session", fake_load)
    monkeypatch.setattr(cli, "client_from_session", lambda body: FakeClient())

    import asyncio

    asyncio.run(cli.session_client(parse(["summary", "--ytd"])))
    assert seen == [target.fingerprint]

    monkeypatch.setattr(cli, "active_sessions", lambda: [])
    with pytest.raises(cli.CommandError, match="대화형"):
        asyncio.run(cli.session_client(parse(["summary", "--ytd"])))


def test_config_survives_control_characters_in_names(npki):
    from hometax_login.cert_discovery import (
        load_aliases,
        load_config,
        save_alias,
        save_config,
    )

    write_certificate(npki / "a", common_name="가사업자")
    entry = discover()[0]
    save_config({"certificate": {"path": 'C:\\\\x\n"y"\t\x7fz', "fingerprint": "f"}})
    assert load_config()["certificate"]["path"] == 'C:\\\\x\n"y"\t\x7fz'
    save_alias("ga", entry)
    assert load_config()["certificate"]["fingerprint"] == "f"
    assert load_aliases() == {"ga": entry.fingerprint}


def test_alias_refuses_names_whose_password_env_would_clash(npki, capsys):
    write_certificate(npki / "a", common_name="가사업자")
    write_certificate(npki / "b", common_name="나사업자")
    assert cli.main(["alias", "a-b", "1"]) == 0
    assert cli.main(["alias", "a_b", "2"]) == 2
    assert "HOMETAX_PW_A_B" in capsys.readouterr().err
    assert cli.main(["alias", "a-b", "--remove", "1"]) == 2


def test_company_and_cert_together_are_rejected(npki, capsys):
    folder = write_certificate(npki / "a", common_name="가사업자")
    assert cli.main(["summary", "--ytd", "--company", "1", "--cert", str(folder)]) == 2
    assert "함께 쓸 수 없습니다" in capsys.readouterr().err


def test_company_with_remember_sets_the_default(npki):
    write_certificate(npki / "a", common_name="가사업자")
    write_certificate(npki / "b", common_name="나사업자")
    target = next(entry for entry in discover() if entry.common_name == "나사업자")

    cli.maybe_remember(target, parse(["login", "--company", "2"]))
    assert load_selection() is None

    cli.maybe_remember(target, parse(["login", "--company", "2", "--remember"]))
    assert load_selection()["fingerprint"] == target.fingerprint
