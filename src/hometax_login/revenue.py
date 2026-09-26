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


def render(report: dict, *, monthly: bool) -> list[str]:
    headers = ["월", *(label for _, _, label in COLUMNS), "차액"]
    body = [(row["month"], row) for row in report["months"]] if monthly else []
    body.append(("누계", report["ytd"]))
    cells = [
        [label, *(f"{row[side][key]:,}" for side, key, _ in COLUMNS), f"{row['net']:,}"]
        for label, row in body
    ]
    widths = [max(_width(line[i]) for line in [headers, *cells]) for i in range(len(headers))]

    def line(values: list[str]) -> str:
        first = _left(values[0], widths[0])
        rest = (_right(value, width) for value, width in zip(values[1:], widths[1:], strict=True))
        return "  ".join([first, *rest])

    rule = "-" * _width(line(headers))
    lines = [line(headers), rule]
    for index, values in enumerate(cells):
        if monthly and index == len(cells) - 1:
            lines.append(rule)
        lines.append(line(values))
    return lines
