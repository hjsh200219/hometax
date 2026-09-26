from __future__ import annotations

import json
from datetime import date

import pytest

from hometax_login import cli
from hometax_login.financials import (
    AmountBreakdown,
    BusinessCardPage,
    BusinessCardTransaction,
    CardSalesMonth,
    CardSalesSummary,
    CashReceiptMerchantTotal,
    CashReceiptPurchasePage,
    CashReceiptSalesMonth,
    CashReceiptSalesSummary,
)
from hometax_login.invoices import TaxInvoice, TaxInvoicePage
from hometax_login.revenue import (
    build_counterparty_report,
    build_report,
    build_vat_report,
    counterparty_key,
    included_tax,
    month_keys,
    monthly_totals,
    render,
    render_counterparties,
    render_vat,
    vat_period,
)

EMPTY = AmountBreakdown(count=0, supply_amount=0, tax_amount=0, tax_exempt_amount=0, total_amount=0)
JANUARY_DEDUCTIBLE = AmountBreakdown(
    count=2, supply_amount=30_000, tax_amount=3_000, tax_exempt_amount=0, total_amount=33_000
)


def invoice(written: str, issued: str, total: int) -> TaxInvoice:
    return TaxInvoice(
        approval_number=f"{written}-{total}",
        written_date=date.fromisoformat(written),
        issued_date=date.fromisoformat(issued),
        transmitted_date=None,
        counterparty_name="예시상사",
        item_name="자문",
        supply_amount=total * 10 // 11,
        tax_amount=total - total * 10 // 11,
        total_amount=total,
    )


# 12월 작성·1월 발급 건이 작성일 기준이면 12월에 잡혀야 한다.
SALES_INVOICES = [
    invoice("2025-01-10", "2025-01-10", 1_100_000),
    invoice("2025-03-31", "2025-04-02", 550_000),
    invoice("2025-12-30", "2026-01-05", 220_000),
]
PURCHASE_INVOICES = [invoice("2025-01-20", "2025-01-20", 330_000)]


class FakeInvoices:
    def __init__(self, calls):
        self.calls = calls

    async def list(self, query):
        self.calls.append(("invoices", query.direction, query.start_date, query.page))
        source = SALES_INVOICES if query.direction == "sales" else PURCHASE_INVOICES
        selected = [
            item
            for item in source
            if query.start_date <= getattr(item, f"{query.date_basis}_date") <= query.end_date
        ]
        # 첫 쪽과 둘째 쪽으로 나눠 페이지 순회를 강제한다.
        first, rest = selected[:1], selected[1:]
        return TaxInvoicePage(
            company_name="예시컨설팅",
            filters=query,
            page=query.page,
            page_size=query.page_size,
            total_count=len(selected),
            has_next=query.page == 1 and bool(rest),
            items=first if query.page == 1 else rest,
        )


class FakeFinancials:
    def __init__(self, calls):
        self.calls = calls

    async def card_sales(self, query):
        self.calls.append(("card_sales", query.quarter_to))
        items = [
            CardSalesMonth(
                month="2025-02",
                data_type="카드사 제출",
                count=3,
                total_sales_amount=99_000,
                credit_card_amount=99_000,
                purchase_card_amount=0,
                service_charge_amount=0,
            ),
            CardSalesMonth(
                month="2025-02",
                data_type="판매(결제)대행",
                count=1,
                total_sales_amount=11_000,
                credit_card_amount=11_000,
                purchase_card_amount=0,
                service_charge_amount=0,
            ),
        ]
        return CardSalesSummary(
            company_name="예시컨설팅",
            query=query,
            count=4,
            total_sales_amount=110_000,
            credit_card_amount=110_000,
            purchase_card_amount=0,
            service_charge_amount=0,
            items=items,
        )

    async def cash_receipt_sales(self, query):
        self.calls.append(("cash_sales", query.year))
        item = CashReceiptSalesMonth(
            month="2025-03",
            count=1,
            supply_amount=20_000,
            tax_amount=2_000,
            service_charge_amount=0,
            total_amount=22_000,
        )
        return CashReceiptSalesSummary(
            company_name="예시컨설팅",
            query=query,
            count=1,
            supply_amount=20_000,
            tax_amount=2_000,
            service_charge_amount=0,
            total_amount=22_000,
            items=[item],
        )

    async def business_cards(self, query):
        self.calls.append(("business_cards", query.start_date, query.deduction))
        items = []
        if query.start_date <= date(2025, 5, 15) <= query.end_date:
            items.append(
                BusinessCardTransaction(
                    transaction_date=date(2025, 5, 15),
                    merchant_name="예시문구",
                    supply_amount=40_000,
                    tax_amount=4_000,
                    tax_exempt_amount=0,
                    total_amount=44_000,
                )
            )
        return BusinessCardPage(
            company_name="예시컨설팅",
            query=query,
            page=1,
            page_size=50,
            total_count=len(items),
            has_next=False,
            total_amount=sum(item.total_amount for item in items),
            items=items,
        )

    async def cash_receipt_purchases(self, query):
        self.calls.append(("cash_purchases", query.start_date, query.end_date))
        items = []
        if query.start_date.month == 1:
            items.append(
                CashReceiptMerchantTotal(
                    merchant_name="예시식당",
                    count=2,
                    supply_amount=30_000,
                    tax_amount=3_000,
                    tax_exempt_amount=0,
                    total_amount=33_000,
                )
            )
        return CashReceiptPurchasePage(
            company_name="예시컨설팅",
            query=query,
            page=1,
            page_size=50,
            total_count=len(items),
            has_next=False,
            eligible_total=EMPTY,
            deductible=JANUARY_DEDUCTIBLE if query.start_date.month == 1 else EMPTY,
            optional_non_deductible=EMPTY,
            mandatory_non_deductible=EMPTY,
            items=items,
        )


@pytest.fixture
def fake_session(monkeypatch):
    calls = []

    class FakeClient:
        invoices = FakeInvoices(calls)
        financials = FakeFinancials(calls)

        async def close(self):
            calls.append("closed")

    async def session(args):
        return FakeClient(), {"user_name": "예시컨설팅"}

    monkeypatch.setattr(cli, "session_client", session)
    monkeypatch.setattr(cli, "PAGE_DELAY_SECONDS", 0)
    return calls


def test_month_keys_cross_the_year_boundary():
    assert month_keys(date(2025, 11, 20), date(2026, 2, 1)) == [
        "2025-11",
        "2025-12",
        "2026-01",
        "2026-02",
    ]


def test_build_report_drops_months_outside_the_range():
    sales = {
        "tax_invoice": {"2026-01": 100},
        "card": {"2026-01": 10, "2026-12": 999},
        "cash_receipt": {},
    }
    purchases = {"tax_invoice": {}, "business_card": {"2026-01": 30}, "cash_receipt": {}}

    report = build_report(["2026-01"], sales, purchases)

    assert report["months"][0]["sales"]["total"] == 110
    assert report["ytd"]["sales"]["card"] == 10
    assert report["ytd"]["net"] == 80


def test_monthly_totals_adds_rows_of_the_same_month():
    assert monthly_totals([("2026-01", 5), ("2026-01", 7), ("2026-02", 1)]) == {
        "2026-01": 12,
        "2026-02": 1,
    }


def test_render_shows_month_rows_only_when_monthly():
    report = build_report(
        ["2026-01", "2026-02"],
        {"tax_invoice": {"2026-01": 1_000}, "card": {}, "cash_receipt": {}},
        {"tax_invoice": {}, "business_card": {}, "cash_receipt": {"2026-02": 300}},
    )

    summary = render(report, monthly=False)
    monthly = render(report, monthly=True)

    assert not any(line.startswith("2026-01") for line in summary)
    assert summary[-1].startswith("누계")
    assert monthly[2].startswith("2026-01")
    assert monthly[-1].startswith("누계") and monthly[-1].endswith("700")
    # 한글 머리글이 두 칸을 차지해도 열 끝이 맞아야 한다.
    widths = {cli_width(line) for line in monthly if not line.startswith("-")}
    assert len(widths) == 1


def cli_width(text: str) -> int:
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def test_revenue_collects_every_source_by_month(npki_home, fake_session, capsys):
    exit_code = cli.main(["--json", "revenue", "2025"])

    payload = json.loads(capsys.readouterr().out)
    months = {row["month"]: row for row in payload["months"]}
    assert exit_code == 0
    assert payload["period"] == {"start": "2025-01-01", "end": "2025-12-31"}
    assert payload["invoice_date_basis"] == "written"
    assert len(payload["months"]) == 12
    assert months["2025-01"]["sales"]["tax_invoice"] == 1_100_000
    assert months["2025-03"]["sales"]["tax_invoice"] == 550_000
    assert months["2025-12"]["sales"]["tax_invoice"] == 220_000
    assert months["2025-02"]["sales"]["card"] == 110_000
    assert months["2025-03"]["sales"]["cash_receipt"] == 22_000
    assert months["2025-01"]["purchases"]["tax_invoice"] == 330_000
    assert months["2025-05"]["purchases"]["business_card"] == 44_000
    assert months["2025-01"]["purchases"]["cash_receipt"] == 33_000
    assert payload["ytd"]["sales"]["total"] == 1_100_000 + 550_000 + 220_000 + 110_000 + 22_000
    assert payload["ytd"]["purchases"]["total"] == 330_000 + 44_000 + 33_000
    assert payload["ytd"]["net"] == 2_002_000 - 407_000

    cash_purchase_spans = [call[1:] for call in fake_session if call[0] == "cash_purchases"]
    assert len(cash_purchase_spans) == 12
    assert cash_purchase_spans[1] == (date(2025, 2, 1), date(2025, 2, 28))
    assert ("card_sales", 4) in fake_session
    assert ("invoices", "sales", date(2025, 1, 1), 2) in fake_session
    assert fake_session[-1] == "closed"


def test_revenue_issued_basis_moves_the_year_end_invoice(npki_home, fake_session, capsys):
    cli.main(["--json", "revenue", "2025", "--basis", "issued"])

    payload = json.loads(capsys.readouterr().out)
    months = {row["month"]: row for row in payload["months"]}
    assert months["2025-12"]["sales"]["tax_invoice"] == 0
    assert months["2025-04"]["sales"]["tax_invoice"] == 550_000


def test_revenue_with_a_year_lists_every_month(npki_home, fake_session, capsys):
    assert cli.main(["revenue", "2025"]) == 0

    out = capsys.readouterr().out
    assert "작성일 기준" in out
    assert "2025-01" in out and "2025-12" in out
    assert out.rstrip().splitlines()[-1].startswith("누계")


def test_revenue_rejects_a_future_year_before_login(npki_home, monkeypatch, capsys):
    async def unexpected_session(args):
        raise AssertionError("session must not start")

    monkeypatch.setattr(cli, "session_client", unexpected_session)

    assert cli.main(["revenue", str(cli.today_kst().year + 1)]) == 2
    assert "미래 연도" in capsys.readouterr().err


def test_revenue_rejects_a_year_before_2000_before_login(npki_home, monkeypatch, capsys):
    async def unexpected_session(args):
        raise AssertionError("session must not start")

    monkeypatch.setattr(cli, "session_client", unexpected_session)

    assert cli.main(["revenue", "500"]) == 2
    assert "2000년 이후" in capsys.readouterr().err


def test_revenue_ytd_prints_only_the_running_total(npki_home, fake_session, capsys):
    assert cli.main(["revenue", "-ytd", "2025"]) == 0

    out = capsys.readouterr().out
    assert "2025-01-01 ~ 2025-12-31" in out
    assert "2025-01 " not in out
    assert out.rstrip().splitlines()[-1].startswith("누계")


@pytest.mark.parametrize(
    "argv,expected",
    [
        (["revenue"], ("this-year", True)),
        (["revenue", "2025"], (2025, True)),
        (["revenue", "-ytd"], ("this-year", False)),
        (["revenue", "-ytd", "2025"], (2025, False)),
        (["revenue", "--ytd", "2025", "-m"], (2025, True)),
        (["revenue", "2025", "-ytd"], (2025, False)),
    ],
)
def test_revenue_mode_reads_year_and_ytd(argv, expected):
    year, monthly = cli.revenue_mode(cli.build_parser().parse_args(argv))
    wanted_year = cli.today_kst().year if expected[0] == "this-year" else expected[0]
    assert (year, monthly) == (wanted_year, expected[1])


def test_revenue_mode_rejects_two_different_years():
    args = cli.build_parser().parse_args(["revenue", "2025", "-ytd", "2024"])
    with pytest.raises(cli.CommandError):
        cli.revenue_mode(args)


def test_included_tax_takes_the_tax_out_of_a_vat_inclusive_amount():
    assert included_tax(9_900) == 900
    assert included_tax(110_000) == 10_000
    assert included_tax(0) == 0


def test_vat_period_splits_each_half_into_preliminary_and_final_quarters():
    start, end, quarters = vat_period(2026, 2)
    assert (start, end) == (date(2026, 7, 1), date(2026, 12, 31))
    assert quarters == [
        ("7~9월 예정", ["2026-07", "2026-08", "2026-09"]),
        ("10~12월 확정", ["2026-10", "2026-11", "2026-12"]),
    ]
    assert vat_period(2026, 1)[1] == date(2026, 6, 30)


def test_vat_report_drops_quarters_without_started_months():
    report = build_vat_report(
        [("7~9월 예정", ["2026-07"]), ("10~12월 확정", [])],
        {"tax_invoice": {"2026-07": 100}, "card": {}, "cash_receipt": {}},
        {"tax_invoice": {"2026-07": 30}, "business_card": {}, "cash_receipt": {}},
    )
    assert [column["label"] for column in report["columns"]] == ["7~9월 예정", "합계"]
    assert report["columns"][-1]["payable"] == 70
    lines = render_vat(report)
    assert lines[-1].startswith("예상 납부세액") and lines[-1].endswith("70")


def test_counterparty_report_groups_blank_names_by_item_and_flags_recurring():
    months = ["2026-01", "2026-02", "2026-03", "2026-04"]
    rows = [
        (counterparty_key(None, "임대"), "2026-01", 990),
        (counterparty_key(None, "임대"), "2026-02", 990),
        (counterparty_key(None, "임대"), "2026-03", 990),
        (counterparty_key("예시상사", "자문"), "2026-04", 5_000),
        (counterparty_key("예시상사", "자문"), "2026-04", 1_000),
        (counterparty_key("범위밖", "x"), "2025-12", 7),
    ]

    report = build_counterparty_report(months, rows)

    assert [row["counterparty"] for row in report] == ["예시상사", "(상호 없음) 임대"]
    assert report[0]["count"] == 2 and report[0]["active_months"] == 1
    assert report[0]["recurring"] is False
    assert report[1]["recurring"] is True and report[1]["total"] == 2_970
    monthly = render_counterparties(report, months, monthly=True)
    assert "04월" in monthly[0]
    assert monthly[-1].split()[-1] == "8,970"
    assert len({cli_width(line) for line in monthly if not line.startswith("-")}) == 1


def test_counterparty_report_counts_a_zero_amount_month_as_active():
    months = ["2026-01", "2026-02", "2026-03"]
    rows = [("A", "2026-01", 1_000), ("A", "2026-02", 1_000), ("A", "2026-03", 0)]

    report = build_counterparty_report(months, rows)

    assert report[0]["active_months"] == 3 and report[0]["recurring"] is True


def test_revenue_by_counterparty_uses_only_tax_invoices(npki_home, fake_session, capsys):
    assert cli.main(["--json", "revenue", "2025", "--by", "counterparty"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["sales"][0]["counterparty"] == "예시상사"
    assert payload["sales"][0]["total"] == 1_870_000
    assert payload["sales"][0]["active_months"] == 3
    assert payload["purchases"][0]["total"] == 330_000
    assert not any(call[0] in {"card_sales", "cash_sales"} for call in fake_session[:-1])


def test_vat_sums_tax_by_quarter(npki_home, fake_session, capsys):
    assert cli.main(["--json", "vat", "2025-1"]) == 0

    payload = json.loads(capsys.readouterr().out)
    first, second, total = payload["columns"]
    assert first["label"] == "1~3월 예정"
    # 매출세액: 세금계산서 100,000 + 50,000, 카드 110,000÷11, 현금영수증 2,000
    assert first["sales_tax"] == {
        "tax_invoice": 150_000,
        "card": 10_000,
        "cash_receipt": 2_000,
        "total": 162_000,
    }
    # 매입세액: 세금계산서 30,000, 현금영수증 공제 3,000
    assert first["purchase_tax"]["total"] == 33_000
    assert second["purchase_tax"]["business_card"] == 4_000
    assert total["payable"] == 162_000 - 33_000 - 4_000
    assert all(call[2] == "deductible" for call in fake_session if call[0] == "business_cards")


def test_vat_converts_card_sales_once_per_month(npki_home, fake_session, monkeypatch, capsys):
    original = FakeFinancials.card_sales

    async def split_rows(self, query):
        summary = await original(self, query)
        first, second = summary.items
        items = [
            first.model_copy(update={"total_sales_amount": 95}),
            second.model_copy(update={"total_sales_amount": 15}),
        ]
        return summary.model_copy(update={"items": items})

    monkeypatch.setattr(FakeFinancials, "card_sales", split_rows)

    assert cli.main(["--json", "vat", "2025-1"]) == 0

    payload = json.loads(capsys.readouterr().out)
    # 95 과 15 를 따로 환산하면 8 + 2 = 10 이 아니라 11 이 나온다. 합친 110 의 세액은 10.
    assert payload["columns"][0]["sales_tax"]["card"] == 10


def test_vat_takes_cash_purchase_totals_once_across_pages(
    npki_home, fake_session, monkeypatch, capsys
):
    original = FakeFinancials.cash_receipt_purchases

    async def two_pages(self, query):
        page = await original(self, query)
        return page.model_copy(update={"has_next": query.page == 1, "page": query.page})

    monkeypatch.setattr(FakeFinancials, "cash_receipt_purchases", two_pages)

    assert cli.main(["--json", "vat", "2025-1"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["columns"][0]["purchase_tax"]["cash_receipt"] == 3_000
    pages = [call for call in fake_session if call[0] == "cash_purchases"]
    assert len(pages) == 12


@pytest.mark.parametrize(
    "argv,message",
    [
        (["vat", "2026-3"], "YYYY-1"),
        (["vat", "26-1"], "YYYY-1"),
        (["vat", "1999-1"], "2000년 이후"),
        (["vat", f"{date.today().year + 1}-1"], "아직 시작하지 않은"),
    ],
)
def test_vat_rejects_bad_periods_before_login(npki_home, monkeypatch, capsys, argv, message):
    async def unexpected_session(args):
        raise AssertionError("session must not start")

    monkeypatch.setattr(cli, "session_client", unexpected_session)

    assert cli.main(argv) == 2
    assert message in capsys.readouterr().err


@pytest.fixture
def npki_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOMETAX_NPKI_PATH", str(tmp_path / "NPKI"))
    monkeypatch.setenv("HOMETAX_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HOMETAX_PW", raising=False)
