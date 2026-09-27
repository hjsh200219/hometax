"""매입매출 리포트 — 홈택스 자료 다섯 가지를 월 단위로 모아 연간 누계와 함께 낸다.

금액은 전부 합계금액(부가세 포함)이다. 카드매출은 공급가액과 세액을 나눠 주지 않으므로
출처를 한 기준으로 더하려면 합계금액밖에 없다.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from datetime import date

SALES_SOURCES = ("tax_invoice", "card", "cash_receipt")
PURCHASE_SOURCES = ("tax_invoice", "business_card", "cash_receipt")
COLUMNS = (
    ("sales", "tax_invoice", "매출:계산서"),
    ("sales", "card", "매출:카드"),
    ("sales", "cash_receipt", "매출:현금"),
    ("sales", "total", "매출 합계"),
    ("purchases", "tax_invoice", "매입:계산서"),
    ("purchases", "business_card", "매입:카드"),
    ("purchases", "cash_receipt", "매입:현금"),
    ("purchases", "total", "매입 합계"),
)

MonthlyAmounts = dict[str, int]


def month_key(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def month_keys(start: date, end: date) -> list[str]:
    keys, year, month = [], start.year, start.month
    while (year, month) <= (end.year, end.month):
        keys.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return keys


def monthly_totals(rows: Iterable[tuple[str, int]]) -> MonthlyAmounts:
    """(YYYY-MM, 금액) 쌍을 월별로 더한다."""
    totals: MonthlyAmounts = {}
    for month, amount in rows:
        totals[month] = totals.get(month, 0) + amount
    return totals


def _side(sources: tuple[str, ...], data: dict[str, MonthlyAmounts], months: list[str]) -> dict:
    side = {source: sum(data[source].get(month, 0) for month in months) for source in sources}
    side["total"] = sum(side.values())
    return side


def build_report(
    months: list[str],
    sales: dict[str, MonthlyAmounts],
    purchases: dict[str, MonthlyAmounts],
) -> dict:
    """조회 범위 밖의 달은 버린다(연 단위 응답이 범위 뒤의 달을 담아 올 수 있다)."""
    rows = []
    for month in months:
        sale = _side(SALES_SOURCES, sales, [month])
        purchase = _side(PURCHASE_SOURCES, purchases, [month])
        rows.append(
            {
                "month": month,
                "sales": sale,
                "purchases": purchase,
                "net": sale["total"] - purchase["total"],
            }
        )
    sale = _side(SALES_SOURCES, sales, months)
    purchase = _side(PURCHASE_SOURCES, purchases, months)
    return {
        "months": rows,
        "ytd": {"sales": sale, "purchases": purchase, "net": sale["total"] - purchase["total"]},
    }


def _width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _right(text: str, width: int) -> str:
    return " " * (width - _width(text)) + text


def _left(text: str, width: int) -> str:
    return text + " " * (width - _width(text))


def table(
    headers: list[str],
    cells: list[list[str]],
    *,
    rules_before: set[int] = frozenset(),
    left: int = 1,
):
    """앞 left 개 열은 왼쪽, 나머지는 오른쪽 정렬. 한글은 두 칸으로 센다."""
    widths = [max(_width(line[i]) for line in [headers, *cells]) for i in range(len(headers))]

    def line(values: list[str]) -> str:
        return "  ".join(
            _left(value, width) if index < left else _right(value, width)
            for index, (value, width) in enumerate(zip(values, widths, strict=True))
        )

    rule = "-" * _width(line(headers))
    lines = [line(headers), rule]
    for index, values in enumerate(cells):
        if index in rules_before:
            lines.append(rule)
        lines.append(line(values))
    return lines


def render(report: dict, *, monthly: bool) -> list[str]:
    headers = ["월", *(label for _, _, label in COLUMNS), "차액"]
    body = [(row["month"], row) for row in report["months"]] if monthly else []
    body.append(("누계", report["ytd"]))
    cells = [
        [label, *(f"{row[side][key]:,}" for side, key, _ in COLUMNS), f"{row['net']:,}"]
        for label, row in body
    ]
    return table(headers, cells, rules_before={len(cells) - 1} if monthly else set())


# ------------------------------------------------------------------ 예상 부가세


def included_tax(amount: int) -> int:
    """부가세 포함 금액에서 세액을 환산한다(공급가액 원 미만 버림)."""
    return amount - amount * 10 // 11


def vat_period(year: int, half: int) -> tuple[date, date, list[tuple[str, list[str]]]]:
    """과세기간과 그 안의 두 분기(예정·확정). 분기는 (이름, 월 목록)."""
    first = 1 if half == 1 else 7
    start, end = date(year, first, 1), date(year, first + 5, 30 if half == 1 else 31)
    quarters = [
        (f"{first}~{first + 2}월 예정", month_keys(start, date(year, first + 2, 1))),
        (f"{first + 3}~{first + 5}월 확정", month_keys(date(year, first + 3, 1), end)),
    ]
    return start, end, quarters


VAT_ROWS = (
    ("sales", "tax_invoice", "  세금계산서"),
    ("sales", "card", "  카드매출(환산)"),
    ("sales", "cash_receipt", "  현금영수증"),
    ("sales", "total", "매출세액"),
    ("purchases", "tax_invoice", "  세금계산서"),
    ("purchases", "business_card", "  사업용카드(공제)"),
    ("purchases", "cash_receipt", "  현금영수증(공제)"),
    ("purchases", "total", "매입세액"),
)


def build_vat_report(
    quarters: list[tuple[str, list[str]]],
    sales: dict[str, MonthlyAmounts],
    purchases: dict[str, MonthlyAmounts],
) -> dict:
    """아직 시작하지 않은 달은 quarters 에서 이미 빠져 있어야 한다."""
    columns = [(label, months) for label, months in quarters if months]
    columns.append(("합계", [month for _, months in quarters for month in months]))
    result = []
    for label, months in columns:
        sale = _side(SALES_SOURCES, sales, months)
        purchase = _side(PURCHASE_SOURCES, purchases, months)
        result.append(
            {
                "label": label,
                "months": months,
                "sales_tax": sale,
                "purchase_tax": purchase,
                "payable": sale["total"] - purchase["total"],
            }
        )
    return {"columns": result}


def render_vat(report: dict) -> list[str]:
    columns = report["columns"]
    headers = ["구분", *(column["label"] for column in columns)]
    keys = {"sales": "sales_tax", "purchases": "purchase_tax"}
    cells = [
        [label, *(f"{column[keys[side]][key]:,}" for column in columns)]
        for side, key, label in VAT_ROWS
    ]
    cells.append(["예상 납부세액", *(f"{column['payable']:,}" for column in columns)])
    return table(headers, cells, rules_before={4, len(cells) - 1})


# ------------------------------------------------------------------ 거래처별


def counterparty_key(name: str | None, item_name: str | None) -> str:
    """상호가 비어 오는 행은 품목으로라도 묶는다(매입 임대 건이 이렇게 온다)."""
    return name or f"(상호 없음) {item_name or ''}".rstrip()


def build_counterparty_report(
    months: list[str], rows: Iterable[tuple[str, str, int]], *, recurring_months: int = 3
) -> list[dict]:
    """(거래처, YYYY-MM, 금액) 행을 거래처별로 모은다. 합계 큰 순."""
    grouped: dict[str, dict[str, int]] = {}
    counts: dict[str, int] = {}
    for name, month, amount in rows:
        if month not in months:
            continue
        grouped.setdefault(name, {})
        grouped[name][month] = grouped[name].get(month, 0) + amount
        counts[name] = counts.get(name, 0) + 1
    report = []
    for name, by_month in grouped.items():
        active = sum(1 for month in months if month in by_month)
        report.append(
            {
                "counterparty": name,
                "count": counts[name],
                "total": sum(by_month.values()),
                "months": {month: by_month.get(month, 0) for month in months},
                "active_months": active,
                "recurring": active >= recurring_months,
            }
        )
    return sorted(report, key=lambda row: (-row["total"], row["counterparty"]))


def render_counterparties(rows: list[dict], months: list[str], *, monthly: bool) -> list[str]:
    month_headers = [month[5:] + "월" for month in months] if monthly else []
    headers = ["거래처", *month_headers, "건수", "합계", "개월"]
    cells = []
    for row in rows:
        by_month = [f"{row['months'][month]:,}" if monthly else "" for month in months]
        cells.append(
            [
                row["counterparty"] + (" *" if row["recurring"] else ""),
                *(by_month if monthly else []),
                str(row["count"]),
                f"{row['total']:,}",
                f"{row['active_months']}/{len(months)}",
            ]
        )
    total_by_month = (
        [f"{sum(row['months'][m] for row in rows):,}" for m in months] if monthly else []
    )
    cells.append(
        [
            "합계",
            *total_by_month,
            str(sum(row["count"] for row in rows)),
            f"{sum(row['total'] for row in rows):,}",
            "",
        ]
    )
    return table(headers, cells, rules_before={len(cells) - 1})
