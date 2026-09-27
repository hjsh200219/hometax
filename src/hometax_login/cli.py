"""hometax 명령줄 도구.

인증서 선택 → 로그인 → 세션 캐시 → 조회. 비밀번호는 인자로 받지 않고
`HOMETAX_PW` 환경변수 또는 입력 프롬프트로만 받는다.
"""

from __future__ import annotations

import argparse
import asyncio
import calendar
import functools
import getpass
import json
import os
import re
import sys
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .cert_discovery import (
    ALIAS_PATTERN,
    CertificateEntry,
    discover,
    display_name,
    home_dir,
    is_personal,
    load_aliases,
    load_selection,
    ordered,
    resolve_company,
    resolve_selection,
    save_alias,
    save_selection,
    usable,
)
from .certificates import CertificateError, load_certificate
from .counterparty_changes import CounterpartyCreate, CounterpartyPatch
from .counterparty_changes import fingerprint as content_fingerprint
from .errors import LoginError
from .financials import (
    BusinessAccountQuery,
    BusinessCardQuery,
    CardSalesQuery,
    CashReceiptPurchaseQuery,
    YearQuery,
)
from .invoices import CounterpartyQuery, InvoiceFilters, InvoiceQuery
from .issuance_models import (
    InvoiceCancelRequest,
    InvoiceCorrectionRequest,
    InvoiceIssueRequest,
    issue_content,
)
from .local_session import (
    active_sessions,
    clear_all_sessions,
    clear_session,
    client_from_session,
    load_session,
    remaining_seconds,
    save_session,
)
from .protocol import HometaxClient
from .revenue import (
    build_counterparty_report,
    build_report,
    build_vat_report,
    counterparty_key,
    included_tax,
    month_key,
    month_keys,
    monthly_totals,
    render,
    render_counterparties,
    render_vat,
    table,
    vat_period,
)
from .write_journal import WriteJournal

MAX_MONTHS_PER_QUERY = 3
MAX_PAGES_PER_QUERY = 200
PAGE_DELAY_SECONDS = 0.2
SEOUL = ZoneInfo("Asia/Seoul")
REASON_MESSAGE = {
    "missing": "저장해 둔 인증서 경로가 사라졌습니다. 다시 고릅니다.",
    "changed": "저장해 둔 경로에 다른 인증서가 있습니다(갱신된 것으로 보입니다). 다시 고릅니다.",
    "expired": "저장해 둔 인증서가 만료됐습니다. 다시 고릅니다.",
    "choose": "사용할 인증서를 고르세요.",
}


class CommandError(Exception):
    pass


@dataclass
class Context:
    args: argparse.Namespace

    @property
    def as_json(self) -> bool:
        return bool(getattr(self.args, "json", False))


def build_client() -> HometaxClient:
    """테스트에서 교체할 수 있도록 한 곳에 모아 둔다."""
    return HometaxClient()


# ------------------------------------------------------------------ 인증서


def pick_certificate(args: argparse.Namespace) -> CertificateEntry:
    entries = discover()
    if getattr(args, "company", None) and getattr(args, "cert", None):
        raise CommandError("--company 와 --cert 는 함께 쓸 수 없습니다. 하나만 지정하세요.")
    if getattr(args, "company", None):
        try:
            entry = resolve_company(args.company, entries)
        except LookupError as error:
            raise CommandError(str(error)) from None
        if entry.is_expired():
            raise CommandError(
                f"{display_name(entry.common_name)} 인증서가 만료됐습니다({entry.label()})."
            )
        return entry
    if getattr(args, "cert", None):
        wanted = Path(args.cert).expanduser()
        for entry in entries:
            if same_path(entry.cert_path, wanted) or same_path(entry.cert_path.parent, wanted):
                return entry
        raise CommandError(f"지정한 경로에서 인증서를 찾지 못했습니다: {args.cert}")

    live = usable(entries)
    saved = None if getattr(args, "choose", False) else load_selection()
    selected, reason = resolve_selection(entries, saved)
    if selected is not None:
        return selected
    if reason == "empty":
        scanned = ", ".join(str(p) for p in _scanned_paths())
        raise CommandError(
            "쓸 수 있는 인증서가 없습니다. 만료됐거나 폴더가 비어 있습니다.\n"
            f"찾아본 곳: {scanned}\nHOMETAX_NPKI_PATH 로 경로를 지정할 수 있습니다."
        )
    if reason in REASON_MESSAGE:
        print(REASON_MESSAGE[reason], file=sys.stderr)
    return prompt_for_certificate(live)


def same_path(left: Path, right: Path) -> bool:
    """macOS 는 파일명을 NFD 로 저장하고 셸 인자는 NFC 라 문자열 비교가 어긋난다.
    같은 파일인지 inode 로 판정하고, 없는 경로는 정규화해서 비교한다."""
    try:
        if left.exists() and right.exists():
            return os.path.samefile(left, right)
    except OSError:
        return False
    normalize = unicodedata.normalize
    return normalize("NFC", str(left.resolve())) == normalize("NFC", str(right.resolve()))


def _scanned_paths() -> list[Path]:
    from .cert_discovery import default_search_paths

    return default_search_paths()


def prompt_for_certificate(live: list[CertificateEntry]) -> CertificateEntry:
    if not sys.stdin.isatty():
        listing = "\n".join(f"  {e.cert_path}" for e in live)
        raise CommandError(
            "인증서를 고를 수 없습니다(대화형 입력이 아닙니다). --cert 로 지정하세요.\n" + listing
        )
    for index, entry in enumerate(live, start=1):
        print(f"  {index}) {entry.label()}", file=sys.stderr)
    while True:
        answer = input(f"인증서 번호 [1-{len(live)}]: ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(live):
            return live[int(answer) - 1]
        print("목록에 있는 번호를 입력하세요.", file=sys.stderr)


def maybe_remember(entry: CertificateEntry, args: argparse.Namespace) -> None:
    # --company 는 이번 실행만 고르는 것이라 기본 인증서를 바꾸지 않는다.
    # 단 --remember 를 함께 주면 기본으로 삼는다(hometax login --company 2 --remember).
    if getattr(args, "no_remember", False):
        return
    if getattr(args, "company", None) and not getattr(args, "remember", False):
        return
    saved = load_selection()
    if (
        saved
        and saved.get("fingerprint") == entry.fingerprint
        and not getattr(args, "choose", False)
    ):
        return
    if getattr(args, "remember", False) or not sys.stdin.isatty():
        if getattr(args, "remember", False):
            save_selection(entry)
            print(f"다음에도 이 인증서를 씁니다: {entry.common_name}", file=sys.stderr)
        return
    answer = input("다음에도 이 인증서를 쓸까요? [Y/n]: ").strip().lower()
    if answer in ("", "y", "yes", "ㅇ"):
        save_selection(entry)
        print("저장했습니다. 바꾸려면 --choose 를 쓰세요.", file=sys.stderr)


def alias_env_name(alias: str) -> str:
    return "HOMETAX_PW_" + re.sub(r"[^A-Z0-9]", "_", alias.upper())


def password_env_names(entry: CertificateEntry | None) -> list[str]:
    """별칭이 붙은 인증서는 HOMETAX_PW_<별칭> 을 먼저 본다(사업자마다 비밀번호가 다를 때)."""
    fingerprint = getattr(entry, "fingerprint", None)
    names = [
        alias_env_name(alias)
        for alias, target in sorted(load_aliases().items())
        if fingerprint and target == fingerprint
    ]
    return [*names, "HOMETAX_PW"]


def read_password(entry: CertificateEntry | None = None) -> str:
    for name in password_env_names(entry):
        password = os.environ.get(name, "")
        if password:
            return password
    if not sys.stdin.isatty():
        raise CommandError("인증서 비밀번호가 필요합니다. HOMETAX_PW 를 지정하세요.")
    return getpass.getpass("인증서 비밀번호: ")


async def login_with(
    entry: CertificateEntry, args: argparse.Namespace
) -> tuple[HometaxClient, dict]:
    material = load_certificate(
        entry.cert_path.read_bytes(),
        entry.key_path.read_bytes(),
        read_password(entry),
        "der",
    )
    client = build_client()
    try:
        identity = await client.login(material, getattr(args, "login_type", "04") or "04")
    except Exception:
        await client.close()
        raise
    save_session(client, identity, cert_fingerprint=entry.fingerprint)
    return client, identity


def implicit_certificate(args: argparse.Namespace) -> CertificateEntry | None:
    """사업자를 지정하지 않았을 때 묻지 않고 정할 수 있으면 정한다.

    기본 인증서가 있으면 그것, 없으면 살아 있는 세션이 딱 하나일 때 그 세션의 인증서.
    `--company` 로만 로그인해 기본값이 없는 상태에서 후속 명령이 막히지 않게 한다."""
    if any(getattr(args, name, None) for name in ("company", "cert", "choose")):
        return None
    entries = discover()
    selected, _reason = resolve_selection(entries, load_selection())
    if selected is not None:
        return selected
    live = {body.get("cert_fingerprint") for body in active_sessions()}
    matches = [entry for entry in usable(entries) if entry.fingerprint in live]
    return matches[0] if len(matches) == 1 else None


async def session_client(args: argparse.Namespace) -> tuple[HometaxClient, dict]:
    """고른 인증서(사업자)의 캐시 세션을 쓰고, 없거나 죽었으면 다시 로그인한다.

    세션은 인증서 지문별 파일이라 사업자를 바꿔도 다른 사업자의 세션을 집어 오지 않는다."""
    entry = implicit_certificate(args) or pick_certificate(args)
    body = load_session(entry.fingerprint)
    if body and body.get("cert_fingerprint") != entry.fingerprint:
        body = None
    if body:
        client = client_from_session(body)
        try:
            identity = await client.verify()
            return client, identity
        except LoginError:
            await client.close()
            clear_session(entry.fingerprint)
    client, identity = await login_with(entry, args)
    maybe_remember(entry, args)
    return client, identity


# ------------------------------------------------------------------ 출력


def emit(context: Context, payload, lines: list[str]) -> None:
    if context.as_json:
        print(json.dumps(payload, ensure_ascii=False, default=str))
    else:
        print("\n".join(lines))


def money(value: int) -> str:
    return f"{value:,}"


def split_periods(start: date, end: date) -> list[tuple[date, date]]:
    """홈택스 3개월 제한에 맞춰 기간을 쪼갠다."""
    spans, cursor = [], start
    while cursor <= end:
        month = cursor.month - 1 + MAX_MONTHS_PER_QUERY
        year = cursor.year + month // 12
        stop = date(year, month % 12 + 1, 1) - timedelta(days=1)
        spans.append((cursor, min(stop, end)))
        cursor = min(stop, end) + timedelta(days=1)
    return spans


def today_kst() -> date:
    """검증 게이트가 한국 시간 기준으로 판정하므로 날짜도 같은 시계에서 얻는다.
    UTC 호스트에서 date.today() 를 쓰면 KST 00시~09시에 어제가 들어간다."""
    return datetime.now(SEOUL).date()


def parse_cli_date(value: str, option: str) -> date:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise CommandError(f"{option} 날짜 형식은 YYYY-MM-DD 이어야 합니다: {value}")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise CommandError(f"{option} 날짜 형식은 YYYY-MM-DD 이어야 합니다: {value}") from None


def parse_range(args: argparse.Namespace) -> tuple[date, date]:
    today = today_kst()
    if getattr(args, "ytd", False):
        return date(today.year, 1, 1), today
    if not args.start or not args.end:
        raise CommandError("--from 과 --to 를 지정하거나 --ytd 를 쓰세요.")
    start = parse_cli_date(args.start, "--from")
    end = parse_cli_date(args.end, "--to")
    if start > end:
        raise CommandError("시작일은 종료일보다 늦을 수 없습니다.")
    if end > today:
        raise CommandError("미래 날짜는 조회할 수 없습니다.")
    return start, end


def validate_year(year: int) -> None:
    if year > today_kst().year:
        raise CommandError("미래 연도는 조회할 수 없습니다.")


def validate_card_sales_args(args: argparse.Namespace) -> None:
    validate_year(args.year)
    if args.quarter_from > args.quarter_to:
        raise CommandError("quarter-from 은 quarter-to 보다 클 수 없습니다.")


def build_cash_sales_query(args: argparse.Namespace) -> YearQuery:
    validate_year(args.year)
    return YearQuery(year=args.year)


def build_card_sales_query(args: argparse.Namespace) -> CardSalesQuery:
    validate_card_sales_args(args)
    return CardSalesQuery(
        year=args.year,
        quarter_from=args.quarter_from,
        quarter_to=args.quarter_to,
    )


def add_json_option(target: argparse.ArgumentParser) -> None:
    target.add_argument(
        "--json",
        dest="json",
        action="store_true",
        default=argparse.SUPPRESS,
        help=argparse.SUPPRESS,
    )


# ------------------------------------------------------------------ 명령


async def cmd_certs(context: Context) -> int:
    entries = discover()
    saved = load_selection()
    if not entries:
        raise CommandError("인증서를 찾지 못했습니다. HOMETAX_NPKI_PATH 로 경로를 지정하세요.")
    payload, lines = [], []
    for entry in entries:
        chosen = saved and saved.get("fingerprint") == entry.fingerprint
        payload.append(
            {
                "common_name": entry.common_name,
                "valid_until": entry.valid_until.date().isoformat(),
                "expired": entry.is_expired(),
                "remembered": bool(chosen),
                "path": str(entry.cert_path),
            }
        )
        mark = "*" if chosen else " "
        lines.append(f"{mark} {entry.label()}")
    emit(context, payload, lines)
    return 0


async def cmd_login(context: Context) -> int:
    entry = pick_certificate(context.args)
    client, identity = await login_with(entry, context.args)
    try:
        maybe_remember(entry, context.args)
        emit(
            context,
            {"identity": identity, "certificate": entry.common_name},
            [f"로그인: {identity.get('user_name') or identity}", f"인증서: {entry.label()}"],
        )
    finally:
        await client.close()
    return 0


def company_rows() -> list[dict]:
    """조회할 수 있는 사업자(인증서) 목록. 번호는 `--company <번호>` 와 같다."""
    entries = ordered(discover())
    saved = load_selection() or {}
    aliases: dict[str, list[str]] = {}
    for name, fingerprint in sorted(load_aliases().items()):
        aliases.setdefault(fingerprint, []).append(name)
    sessions = {body.get("cert_fingerprint"): body for body in active_sessions()}
    rows = []
    for index, entry in enumerate(entries, start=1):
        session = sessions.get(entry.fingerprint)
        rows.append(
            {
                "number": index,
                "common_name": entry.common_name,
                "name": display_name(entry.common_name),
                "kind": "personal" if is_personal(entry.common_name) else "business",
                "aliases": aliases.get(entry.fingerprint, []),
                "valid_until": entry.valid_until.date().isoformat(),
                "expired": entry.is_expired(),
                "default": saved.get("fingerprint") == entry.fingerprint,
                "session_seconds": remaining_seconds(session) if session else 0,
                "path": str(entry.cert_path),
            }
        )
    return rows


async def cmd_list(context: Context) -> int:
    rows = company_rows()
    if not rows:
        raise CommandError("인증서를 찾지 못했습니다. HOMETAX_NPKI_PATH 로 경로를 지정하세요.")
    cells = []
    for row in rows:
        state = "만료" if row["expired"] else ("기본" if row["default"] else "")
        kind = "개인" if row["kind"] == "personal" else "사업자"
        if row["session_seconds"]:
            state = f"{state} 세션 {row['session_seconds'] // 60}분".strip()
        cells.append(
            [
                str(row["number"]),
                row["name"],
                kind,
                ",".join(row["aliases"]) or "-",
                row["valid_until"],
                state,
            ]
        )
    lines = [
        *table(["번호", "인증서", "종류", "별칭", "만료일", "상태"], cells, left=6),
        "",
        "고르기: --company <번호|별칭|상호 일부>  ·  별칭 붙이기: hometax alias <이름> <번호>",
        "개인 인증서는 로그인은 되지만 사업자 자료 조회에는 사업자용 인증서가 필요합니다.",
    ]
    emit(context, rows, lines)
    return 0


async def cmd_alias(context: Context) -> int:
    args = context.args
    if not re.fullmatch(ALIAS_PATTERN, args.name):
        raise CommandError("별칭은 영문으로 시작하는 영문·숫자·_·- 32자 이하입니다.")
    if args.remove:
        if args.target:
            raise CommandError(
                "--remove 에는 사업자를 함께 쓰지 않습니다: hometax alias <이름> --remove"
            )
        if args.name not in load_aliases():
            raise CommandError(f"별칭 {args.name} 이 없습니다.")
        save_alias(args.name, None)
        emit(context, {"removed": args.name}, [f"별칭 {args.name} 을 지웠습니다."])
        return 0
    if not args.target:
        raise CommandError("붙일 사업자를 지정하세요: hometax alias <이름> <번호|상호 일부>")
    try:
        entry = resolve_company(args.target, discover())
    except LookupError as error:
        raise CommandError(str(error)) from None
    env = alias_env_name(args.name)
    clashes = [
        name
        for name, fingerprint in load_aliases().items()
        if name != args.name and alias_env_name(name) == env and fingerprint != entry.fingerprint
    ]
    if clashes:
        raise CommandError(
            f"별칭 {clashes[0]} 과 비밀번호 환경변수({env})가 겹칩니다. 다른 이름을 쓰세요."
        )
    save_alias(args.name, entry)
    emit(
        context,
        {"alias": args.name, "common_name": entry.common_name},
        [
            f"{args.name} → {display_name(entry.common_name)}",
            f"이 사업자의 인증서 비밀번호가 다르면 {env} 로 줄 수 있습니다.",
        ],
    )
    return 0


async def cmd_status(context: Context) -> int:
    sessions = [row for row in company_rows() if row["session_seconds"]]
    if not sessions:
        emit(context, {"active": []}, ["세션 없음. hometax login 을 실행하세요."])
        return 1
    emit(
        context,
        {"active": sessions},
        [f"{row['name']}: 남은 {row['session_seconds']}초" for row in sessions],
    )
    return 0


async def cmd_logout(context: Context) -> int:
    if getattr(context.args, "company", None):
        entry = pick_certificate(context.args)
        clear_session(entry.fingerprint)
        message = (
            f"{display_name(entry.common_name)} 로컬 세션을 지웠습니다(홈택스 원격 로그아웃 아님)."
        )
        emit(context, {"cleared": 1}, [message])
        return 0
    removed = clear_all_sessions()
    emit(
        context,
        {"cleared": removed},
        [f"로컬 세션 {removed}개를 지웠습니다(홈택스 원격 로그아웃 아님)."],
    )
    return 0


async def cmd_invoices(context: Context) -> int:
    args = context.args
    start, end = parse_range(args)
    client, _ = await session_client(args)
    items, totals = [], [0, 0, 0]
    try:
        for span_start, span_end in split_periods(start, end):
            page_number = 1
            while True:
                query = InvoiceQuery(
                    start_date=span_start,
                    end_date=span_end,
                    direction=args.direction,
                    date_basis=args.basis,
                    page=page_number,
                    page_size=50,
                )
                page = await client.invoices.list(query)
                items.extend(page.items)
                if not page.has_next:
                    break
                page_number += 1
                if page_number > MAX_PAGES_PER_QUERY:
                    raise CommandError(
                        f"페이지가 {MAX_PAGES_PER_QUERY}쪽을 넘었습니다. 기간을 좁혀 주세요."
                    )
                await asyncio.sleep(PAGE_DELAY_SECONDS)
    finally:
        await client.close()
    for item in items:
        totals[0] += item.supply_amount
        totals[1] += item.tax_amount
        totals[2] += item.total_amount
    payload = {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "direction": args.direction,
        "count": len(items),
        "supply_amount": totals[0],
        "tax_amount": totals[1],
        "total_amount": totals[2],
        "items": [item.model_dump(mode="json") for item in items],
    }
    lines = [
        f"{item.issued_date} {item.counterparty_name or '(상호 없음)'} "
        f"| {item.item_name or ''} | {money(item.supply_amount)}"
        for item in items
    ]
    lines.append(
        f"합계 {len(items)}건 공급가액 {money(totals[0])} 세액 {money(totals[1])} "
        f"합계 {money(totals[2])}"
    )
    emit(context, payload, lines)
    return 0


async def cmd_summary(context: Context) -> int:
    args = context.args
    start, end = parse_range(args)
    client, _ = await session_client(args)
    rows = []
    try:
        for span_start, span_end in split_periods(start, end):
            filters = InvoiceFilters(
                start_date=span_start,
                end_date=span_end,
                direction=args.direction,
                date_basis=args.basis,
            )
            summary = await client.invoices.summary(filters)
            rows.append((span_start, span_end, summary))
    finally:
        await client.close()
    payload = {
        "direction": args.direction,
        "periods": [
            {
                "start": s.isoformat(),
                "end": e.isoformat(),
                **summary.model_dump(mode="json"),
            }
            for s, e, summary in rows
        ],
    }
    lines = [
        f"{s}~{e} {summary.total_count}건 공급가액 {money(summary.supply_amount)} "
        f"세액 {money(summary.tax_amount)}"
        for s, e, summary in rows
    ]
    total_count = sum(summary.total_count for _, _, summary in rows)
    total_supply = sum(summary.supply_amount for _, _, summary in rows)
    total_tax = sum(summary.tax_amount for _, _, summary in rows)
    payload["count"] = total_count
    payload["supply_amount"] = total_supply
    payload["tax_amount"] = total_tax
    lines.append(f"합계 {total_count}건 공급가액 {money(total_supply)} 세액 {money(total_tax)}")
    emit(context, payload, lines)
    return 0


async def cmd_business_cards(context: Context) -> int:
    args = context.args
    start, end = parse_range(args)
    client, _ = await session_client(args)
    items = []
    try:
        for span_start, span_end in split_periods(start, end):
            page_number = 1
            while True:
                page = await client.financials.business_cards(
                    BusinessCardQuery(
                        start_date=span_start,
                        end_date=span_end,
                        deduction=args.deduction.replace("-", "_"),
                        page=page_number,
                        page_size=50,
                    )
                )
                items.extend(page.items)
                if not page.has_next:
                    break
                page_number += 1
                if page_number > MAX_PAGES_PER_QUERY:
                    raise CommandError(
                        f"페이지가 {MAX_PAGES_PER_QUERY}쪽을 넘었습니다. 기간을 좁혀 주세요."
                    )
                await asyncio.sleep(PAGE_DELAY_SECONDS)
    finally:
        await client.close()
    payload = {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "deduction": args.deduction,
        "count": len(items),
        "supply_amount": sum(item.supply_amount for item in items),
        "tax_amount": sum(item.tax_amount for item in items),
        "tax_exempt_amount": sum(item.tax_exempt_amount for item in items),
        "total_amount": sum(item.total_amount for item in items),
        "items": [item.model_dump(mode="json") for item in items],
    }
    lines = [
        f"{item.transaction_date} {item.merchant_name or '(상호 없음)'} | "
        f"{money(item.total_amount)} | {item.deduction_name or '미분류'}"
        for item in items
    ]
    lines.append(
        f"합계 {len(items)}건 공급가액 {money(payload['supply_amount'])} "
        f"세액 {money(payload['tax_amount'])} 합계 {money(payload['total_amount'])}"
    )
    emit(context, payload, lines)
    return 0


async def cmd_registered_business_cards(context: Context) -> int:
    client, _ = await session_client(context.args)
    items, page_number = [], 1
    try:
        while True:
            page = await client.financials.registered_business_cards(
                BusinessAccountQuery(page=page_number, page_size=50)
            )
            items.extend(page.items)
            if not page.has_next:
                break
            page_number += 1
            if page_number > MAX_PAGES_PER_QUERY:
                raise CommandError(
                    f"페이지가 {MAX_PAGES_PER_QUERY}쪽을 넘었습니다. 조건을 좁혀 주세요."
                )
            await asyncio.sleep(PAGE_DELAY_SECONDS)
    finally:
        await client.close()
    payload = {"count": len(items), "items": [item.model_dump(mode="json") for item in items]}
    lines = [
        f"{item.card_type or '(카드구분 없음)'} {item.card_number or '(카드번호 없음)'} | "
        f"{item.status or ''}"
        for item in items
    ]
    lines.append(f"합계 {len(items)}장")
    emit(context, payload, lines)
    return 0


async def cmd_cash_receipt_purchases(context: Context) -> int:
    args = context.args
    start, end = parse_range(args)
    client, _ = await session_client(args)
    items, pages = [], []
    try:
        for span_start, span_end in split_periods(start, end):
            page_number = 1
            while True:
                page = await client.financials.cash_receipt_purchases(
                    CashReceiptPurchaseQuery(
                        start_date=span_start,
                        end_date=span_end,
                        deduction=args.deduction.replace("-", "_"),
                        page=page_number,
                        page_size=50,
                    )
                )
                if page_number == 1:
                    pages.append(page)
                items.extend(page.items)
                if not page.has_next:
                    break
                page_number += 1
                if page_number > MAX_PAGES_PER_QUERY:
                    raise CommandError(
                        f"페이지가 {MAX_PAGES_PER_QUERY}쪽을 넘었습니다. 기간을 좁혀 주세요."
                    )
                await asyncio.sleep(PAGE_DELAY_SECONDS)
    finally:
        await client.close()
    payload = {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "deduction": args.deduction,
        "merchant_count": len(items),
        "eligible_total": {
            "count": sum(page.eligible_total.count for page in pages),
            "supply_amount": sum(page.eligible_total.supply_amount for page in pages),
            "tax_amount": sum(page.eligible_total.tax_amount for page in pages),
            "tax_exempt_amount": sum(page.eligible_total.tax_exempt_amount for page in pages),
            "total_amount": sum(page.eligible_total.total_amount for page in pages),
        },
        "items": [item.model_dump(mode="json") for item in items],
    }
    lines = [
        f"{item.merchant_name or '(상호 없음)'} | {item.count}건 | {money(item.total_amount)}"
        for item in items
    ]
    total = payload["eligible_total"]
    lines.append(
        f"공제대상 합계 {total['count']}건 공급가액 {money(total['supply_amount'])} "
        f"세액 {money(total['tax_amount'])} 합계 {money(total['total_amount'])}"
    )
    emit(context, payload, lines)
    return 0


async def cmd_cash_receipt_sales(context: Context) -> int:
    query = build_cash_sales_query(context.args)
    client, _ = await session_client(context.args)
    try:
        summary = await client.financials.cash_receipt_sales(query)
    finally:
        await client.close()
    payload = summary.model_dump(mode="json")
    lines = [
        f"{item.month} {item.count}건 | 공급가액 {money(item.supply_amount)} | "
        f"합계 {money(item.total_amount)}"
        for item in summary.items
    ]
    lines.append(
        f"합계 {summary.count}건 공급가액 {money(summary.supply_amount)} "
        f"세액 {money(summary.tax_amount)} 합계 {money(summary.total_amount)}"
    )
    emit(context, payload, lines)
    return 0


async def cmd_card_sales(context: Context) -> int:
    args = context.args
    query = build_card_sales_query(args)
    client, _ = await session_client(args)
    try:
        summary = await client.financials.card_sales(query)
    finally:
        await client.close()
    payload = summary.model_dump(mode="json")
    lines = [
        f"{item.month} {item.data_type or ''} | {item.count}건 | "
        f"매출 {money(item.total_sales_amount)}"
        for item in summary.items
    ]
    lines.append(
        f"합계 {summary.count}건 매출 {money(summary.total_sales_amount)} "
        f"신용카드 {money(summary.credit_card_amount)}"
    )
    emit(context, payload, lines)
    return 0


async def collect_pages(fetch, query) -> list:
    pages, page_number = [], 1
    while True:
        page = await fetch(query.model_copy(update={"page": page_number}))
        pages.append(page)
        if not page.has_next:
            return pages
        page_number += 1
        if page_number > MAX_PAGES_PER_QUERY:
            raise CommandError(
                f"페이지가 {MAX_PAGES_PER_QUERY}쪽을 넘었습니다. 기간을 좁혀 주세요."
            )
        await asyncio.sleep(PAGE_DELAY_SECONDS)


async def all_pages(fetch, query) -> list:
    return [item for page in await collect_pages(fetch, query) for item in page.items]


async def fetch_invoices(client, start: date, end: date, direction: str, basis: str) -> list:
    invoices = []
    for span_start, span_end in split_periods(start, end):
        query = InvoiceQuery(
            start_date=span_start,
            end_date=span_end,
            direction=direction,
            date_basis=basis,
            page_size=50,
        )
        invoices.extend(await all_pages(client.invoices.list, query))
    return invoices


async def fetch_business_cards(client, start: date, end: date, deduction: str) -> list:
    items = []
    for span_start, span_end in split_periods(start, end):
        query = BusinessCardQuery(
            start_date=span_start, end_date=span_end, deduction=deduction, page_size=50
        )
        items.extend(await all_pages(client.financials.business_cards, query))
    return items


async def fetch_cash_purchase_months(client, months: list[str], end: date) -> list:
    """현금영수증 매입은 기간 전체의 가맹점별 합계만 준다. 월별 값은 한 달씩 조회해야 나온다.
    (월, 그 달의 쪽 목록)을 돌려준다 — 공제 합계는 모든 쪽에 같은 값으로 실려 온다."""
    result = []
    for month in months:
        month_start = date.fromisoformat(f"{month}-01")
        last_day = calendar.monthrange(month_start.year, month_start.month)[1]
        month_end = min(month_start.replace(day=last_day), end)
        query = CashReceiptPurchaseQuery(start_date=month_start, end_date=month_end, page_size=50)
        result.append((month, await collect_pages(client.financials.cash_receipt_purchases, query)))
    return result


def invoice_month(item, basis: str) -> str:
    return month_key(getattr(item, f"{basis}_date"))


def quarter_of(day: date) -> int:
    return (day.month - 1) // 3 + 1


def year_range(year: int) -> tuple[date, date]:
    validate_year(year)
    if year < 2000:
        raise CommandError("2000년 이후 연도만 조회할 수 있습니다.")
    return date(year, 1, 1), min(date(year, 12, 31), today_kst())


def revenue_mode(args: argparse.Namespace) -> tuple[int, bool]:
    """`revenue 2026` 은 월별+누계, `revenue -ytd 2026` 은 누계만. 연도를 빼면 올해."""
    ytd = getattr(args, "ytd", None)
    years = {value for value in (args.year, None if ytd is True else ytd) if value is not None}
    if len(years) > 1:
        raise CommandError("연도를 서로 다르게 두 번 지정했습니다.")
    year = years.pop() if years else today_kst().year
    return year, ytd is None or args.monthly


BASIS_LABEL = {"written": "작성일", "issued": "발급일", "transmitted": "전송일"}


async def cmd_revenue(context: Context) -> int:
    args = context.args
    year, monthly = revenue_mode(args)
    start, end = year_range(year)
    months = month_keys(start, end)
    basis = BASIS_LABEL[args.basis]
    if args.by == "counterparty":
        return await revenue_by_counterparty(context, start, end, months, monthly, basis)
    client, _ = await session_client(args)
    sales: dict[str, dict[str, int]] = {}
    purchases: dict[str, dict[str, int]] = {}
    try:
        for direction, table in (("sales", sales), ("purchases", purchases)):
            invoices = await fetch_invoices(client, start, end, direction, args.basis)
            table["tax_invoice"] = monthly_totals(
                (invoice_month(item, args.basis), item.total_amount) for item in invoices
            )

        card_sales = await client.financials.card_sales(
            CardSalesQuery(year=year, quarter_from=1, quarter_to=quarter_of(end))
        )
        sales["card"] = monthly_totals(
            (item.month, item.total_sales_amount) for item in card_sales.items
        )
        cash_sales = await client.financials.cash_receipt_sales(YearQuery(year=year))
        sales["cash_receipt"] = monthly_totals(
            (item.month, item.total_amount) for item in cash_sales.items
        )

        card_purchases = await fetch_business_cards(client, start, end, "all")
        purchases["business_card"] = monthly_totals(
            (month_key(item.transaction_date), item.total_amount) for item in card_purchases
        )
        cash_months = await fetch_cash_purchase_months(client, months, end)
        purchases["cash_receipt"] = monthly_totals(
            (month, item.total_amount)
            for month, pages in cash_months
            for page in pages
            for item in page.items
        )
    finally:
        await client.close()

    report = build_report(months, sales, purchases)
    payload = {
        "year": year,
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "amount": "total_amount",
        "invoice_date_basis": args.basis,
        **report,
    }
    lines = [
        f"{start} ~ {end} 매입매출 (합계금액·부가세 포함, 세금계산서 {basis} 기준)",
        *render(report, monthly=monthly),
    ]
    emit(context, payload, lines)
    return 0


async def revenue_by_counterparty(
    context: Context, start: date, end: date, months: list[str], monthly: bool, basis: str
) -> int:
    """전자세금계산서만 거래처를 준다. 카드·현금영수증 매출은 월 합계뿐이라 빠진다."""
    args = context.args
    client, _ = await session_client(args)
    reports = {}
    try:
        for direction in ("sales", "purchases"):
            invoices = await fetch_invoices(client, start, end, direction, args.basis)
            reports[direction] = build_counterparty_report(
                months,
                (
                    (
                        counterparty_key(item.counterparty_name, item.item_name),
                        invoice_month(item, args.basis),
                        item.total_amount,
                    )
                    for item in invoices
                ),
            )
    finally:
        await client.close()
    payload = {
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "amount": "total_amount",
        "invoice_date_basis": args.basis,
        "recurring_months": 3,
        **reports,
    }
    lines = [
        f"{start} ~ {end} 거래처별 세금계산서 (합계금액·부가세 포함, {basis} 기준, "
        "* = 3개월 이상 반복)",
    ]
    for direction, title in (("sales", "매출"), ("purchases", "매입")):
        lines += ["", f"[{title}]"]
        if reports[direction]:
            lines += render_counterparties(reports[direction], months, monthly=monthly)
        else:
            lines.append("자료 없음")
    emit(context, payload, lines)
    return 0


def parse_vat_period(value: str | None) -> tuple[int, int]:
    today = today_kst()
    if value is None:
        return today.year, 1 if today.month <= 6 else 2
    match = re.fullmatch(r"(\d{4})-([12])", value)
    if not match:
        raise CommandError(f"과세기간은 YYYY-1 또는 YYYY-2 형식이어야 합니다: {value}")
    year, half = int(match[1]), int(match[2])
    if year < 2000:
        raise CommandError("2000년 이후 연도만 조회할 수 있습니다.")
    if date(year, 1 if half == 1 else 7, 1) > today:
        raise CommandError("아직 시작하지 않은 과세기간입니다.")
    return year, half


async def cmd_vat(context: Context) -> int:
    args = context.args
    year, half = parse_vat_period(args.period)
    start, period_end, quarters = vat_period(year, half)
    end = min(period_end, today_kst())
    started = month_key(end)
    quarters = [(label, [m for m in months if m <= started]) for label, months in quarters]
    months = [month for _, quarter_months in quarters for month in quarter_months]
    client, _ = await session_client(args)
    sales: dict[str, dict[str, int]] = {}
    purchases: dict[str, dict[str, int]] = {}
    try:
        for direction, table in (("sales", sales), ("purchases", purchases)):
            invoices = await fetch_invoices(client, start, end, direction, "written")
            table["tax_invoice"] = monthly_totals(
                (invoice_month(item, "written"), item.tax_amount) for item in invoices
            )

        card_sales = await client.financials.card_sales(
            CardSalesQuery(year=year, quarter_from=quarter_of(start), quarter_to=quarter_of(end))
        )
        # 같은 달에 카드사 제출분과 대행분이 따로 오므로 달별로 합친 뒤 한 번만 환산한다.
        # 봉사료가 결제액에 들어 있는지 확인하지 못해 빼지 않는다(세액을 적게 잡지 않는 쪽).
        card_months = monthly_totals(
            (item.month, item.total_sales_amount) for item in card_sales.items
        )
        sales["card"] = {month: included_tax(amount) for month, amount in card_months.items()}
        cash_sales = await client.financials.cash_receipt_sales(YearQuery(year=year))
        sales["cash_receipt"] = monthly_totals(
            (item.month, item.tax_amount) for item in cash_sales.items
        )

        cards = await fetch_business_cards(client, start, end, "deductible")
        purchases["business_card"] = monthly_totals(
            (month_key(item.transaction_date), item.tax_amount) for item in cards
        )
        cash_months = await fetch_cash_purchase_months(client, months, end)
        purchases["cash_receipt"] = {
            month: pages[0].deductible.tax_amount for month, pages in cash_months
        }
    finally:
        await client.close()

    report = build_vat_report(quarters, sales, purchases)
    payload = {
        "period": f"{year}-{half}",
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "invoice_date_basis": "written",
        **report,
    }
    lines = [
        f"{year}년 {half}기 예상 부가세 ({start} ~ {end}, 세금계산서 작성일 기준)",
        *render_vat(report),
        "",
        "참고용 추정입니다. 카드매출 세액은 합계금액÷11 환산,",
        "사업용카드·현금영수증 매입은 홈택스 공제분류 기준입니다.",
        "세금계산서 매입의 불공제 대상(접대비·비영업용 승용차 등),",
        "개인사업자 신용카드매출 세액공제, 예정고지·기납부세액, 가산세는 반영하지 않았습니다.",
    ]
    emit(context, payload, lines)
    return 0


async def cmd_business_accounts(context: Context) -> int:
    client, _ = await session_client(context.args)
    items, page_number = [], 1
    try:
        while True:
            page = await client.financials.business_accounts(
                BusinessAccountQuery(page=page_number, page_size=50)
            )
            items.extend(page.items)
            if not page.has_next:
                break
            page_number += 1
            if page_number > MAX_PAGES_PER_QUERY:
                raise CommandError(
                    f"페이지가 {MAX_PAGES_PER_QUERY}쪽을 넘었습니다. 조건을 좁혀 주세요."
                )
            await asyncio.sleep(PAGE_DELAY_SECONDS)
    finally:
        await client.close()
    payload = {"count": len(items), "items": [item.model_dump(mode="json") for item in items]}
    lines = [
        f"{item.bank_name or '(은행 없음)'} {item.account_number or '(계좌 없음)'} | "
        f"{item.account_type or ''} | {item.status or ''}"
        for item in items
    ]
    lines.append(f"합계 {len(items)}개")
    emit(context, payload, lines)
    return 0


async def cmd_counterparties(context: Context) -> int:
    args = context.args
    client, _ = await session_client(args)
    items, page_number = [], 1
    try:
        while True:
            query = CounterpartyQuery(
                name=args.name or "",
                business_number=args.business_number or "",
                representative_name=args.representative or "",
                page=page_number,
                page_size=50,
            )
            page = await client.invoices.counterparties(query)
            items.extend(page.items)
            if not page.has_next:
                break
            page_number += 1
            if page_number > MAX_PAGES_PER_QUERY:
                raise CommandError(
                    f"페이지가 {MAX_PAGES_PER_QUERY}쪽을 넘었습니다. 조건을 좁혀 주세요."
                )
            await asyncio.sleep(PAGE_DELAY_SECONDS)
    finally:
        await client.close()
    payload = {"count": len(items), "items": [item.model_dump(mode="json") for item in items]}
    lines = [
        f"{item.name} | {item.business_number} | 대표 {item.representative_name or ''}"
        for item in items
    ]
    lines.append(f"합계 {len(items)}곳")
    emit(context, payload, lines)
    return 0


# ------------------------------------------------------------------ 쓰기


def journal_path() -> Path:
    directory = home_dir()
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory / "writes.sqlite3"


def open_journal() -> WriteJournal:
    """중복 전송을 막는 영속 저널. 미확정 작업이 남으면 같은 대상을 차단한다."""
    return WriteJournal(journal_path())


def load_payload(args: argparse.Namespace) -> dict:
    path = getattr(args, "file", None)
    if not path:
        return {}
    try:
        body = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise CommandError(f"입력 파일을 읽지 못했습니다: {error}") from None
    if not isinstance(body, dict):
        raise CommandError("입력 파일은 JSON 객체여야 합니다.")
    return body


def show_preview(context: Context, preview, applied=None) -> None:
    payload = {"preview": preview.model_dump(mode="json")}
    if applied is not None:
        payload["result"] = applied.model_dump(mode="json")
    lines = [json.dumps(payload, ensure_ascii=False, indent=1, default=str)]
    if applied is None:
        lines.append("미리보기만 했습니다. 실제로 반영하려면 같은 명령에 --yes 를 붙이세요.")
    emit(context, payload, lines)


async def counterparty_change(context: Context, action: str) -> int:
    args = context.args
    payload = load_payload(args)
    client, _ = await session_client(args)
    try:
        manager = client.counterparty_changes
        if action == "add":
            fields = {
                "business_number": args.business_number,
                "branch_number": getattr(args, "branch", "") or "",
                "name": args.name,
                "representative_name": getattr(args, "representative", None) or "",
                **payload,
            }
            preview = await manager.preview_create(CounterpartyCreate(**fields))
        elif action == "edit":
            if not payload:
                raise CommandError("바꿀 내용을 --file 로 지정하세요.")
            preview = await manager.preview_update(
                args.business_number,
                getattr(args, "branch", "") or "",
                CounterpartyPatch(**payload),
            )
        else:
            preview = await manager.preview_delete(
                args.business_number, getattr(args, "branch", "") or ""
            )
        applied = None
        if args.yes:
            applied = await manager.apply(preview.change_id, open_journal())
        show_preview(context, preview, applied)
    finally:
        await client.close()
    return 0


async def invoice_operation(context: Context, action: str) -> int:
    args = context.args
    payload = load_payload(args)
    if action == "issue" and "client_reference" not in payload:
        # Same validated input yields the same reference across CLI restarts. Scope is also
        # included by the server-side journal, so different issuers remain independent.
        draft = InvoiceIssueRequest(client_reference=uuid.UUID(int=0), **payload)
        payload["client_reference"] = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "hometax:invoice:issue:" + content_fingerprint(issue_content(draft)),
            )
        )
    if args.yes and not getattr(args, "wire", None):
        raise CommandError(
            "전송에는 --wire raw 또는 --wire base64 가 필요합니다(홈택스 전송 형식)."
        )
    entry = pick_certificate(args) if args.yes else None
    material = (
        load_certificate(
            entry.cert_path.read_bytes(), entry.key_path.read_bytes(), read_password(entry), "der"
        )
        if entry
        else None
    )
    # Cached cookies deliberately carry no trusted certificate binding. Authenticate once with
    # the selected signing key before preview/submit; never trust a fingerprint from a file.
    client = build_client() if args.yes else (await session_client(args))[0]
    try:
        if args.yes:
            identity = await client.login(material, getattr(args, "login_type", "04") or "04")
            save_session(client, identity, cert_fingerprint=getattr(entry, "fingerprint", None))
        operations = client.invoice_operations
        if action == "issue":
            preview = await operations.preview_issue(InvoiceIssueRequest(**payload))
        elif action == "correct":
            payload.setdefault("client_reference", str(uuid.uuid4()))
            payload.setdefault("reason", "clerical_error")
            preview = await operations.preview_correct(
                args.approval, InvoiceCorrectionRequest(**payload)
            )
        else:
            payload.setdefault("client_reference", str(uuid.uuid4()))
            fields = {
                "reason": args.reason,
                "written_date": args.written_date or today_kst().isoformat(),
                **payload,
            }
            preview = await operations.preview_cancel(args.approval, InvoiceCancelRequest(**fields))
        applied = None
        if args.yes:
            client.invoice_wire_encoding = args.wire
            applied = await operations.submit(
                preview.operation_id, material, open_journal(), preview.content_digest
            )
        show_preview(context, preview, applied)
    finally:
        await client.close()
    return 0


# ------------------------------------------------------------------ 진입점


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hometax", description="홈택스 전자세금계산서 조회 도구")
    parser.add_argument("--json", action="store_true", help="JSON 으로 출력")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_cert_options(target):
        target.add_argument("--company", help="조회할 사업자: hometax list 의 번호·별칭·상호 일부")
        target.add_argument("--cert", help="사용할 인증서 경로(폴더 또는 signCert.der)")
        target.add_argument(
            "--choose", action="store_true", help="저장된 선택을 무시하고 다시 고름"
        )
        target.add_argument("--remember", action="store_true", help="묻지 않고 선택을 저장")
        target.add_argument("--no-remember", action="store_true", help="선택을 저장하지 않음")
        target.add_argument("--login-type", default="04", choices=["03", "04"])

    certs = sub.add_parser("certs", help="인증서 목록")
    certs.set_defaults(handler=cmd_certs)

    login = sub.add_parser("login", help="인증서로 로그인하고 세션을 저장")
    add_cert_options(login)
    login.set_defaults(handler=cmd_login)

    listing = sub.add_parser("list", help="조회할 수 있는 사업자 목록")
    listing.set_defaults(handler=cmd_list)

    alias = sub.add_parser("alias", help="사업자에 별칭 붙이기/지우기")
    alias.add_argument("name", help="별칭(영문)")
    alias.add_argument("target", nargs="?", help="hometax list 번호 또는 상호 일부")
    alias.add_argument("--remove", action="store_true", help="별칭 지우기")
    alias.set_defaults(handler=cmd_alias)

    status = sub.add_parser("status", help="사업자별 세션 상태")
    status.set_defaults(handler=cmd_status)

    logout = sub.add_parser("logout", help="로컬 세션 삭제(기본 전체)")
    logout.add_argument("--company", help="이 사업자 세션만 지움")
    logout.set_defaults(handler=cmd_logout)

    def add_period_options(target):
        target.add_argument("--from", dest="start", help="시작일 YYYY-MM-DD")
        target.add_argument("--to", dest="end", help="종료일 YYYY-MM-DD")
        target.add_argument("--ytd", action="store_true", help="올해 1월 1일부터 오늘까지")
        target.add_argument(
            "--direction", default="sales", choices=["sales", "purchases"], help="매출/매입"
        )
        target.add_argument(
            "--basis", default="issued", choices=["issued", "written", "transmitted"]
        )
        add_cert_options(target)

    invoices = sub.add_parser("invoices", help="세금계산서 목록")
    add_period_options(invoices)
    invoices.set_defaults(handler=cmd_invoices)

    summary = sub.add_parser("summary", help="기간 합계")
    add_period_options(summary)
    summary.set_defaults(handler=cmd_summary)

    def add_financial_period_options(target):
        target.add_argument("--from", dest="start", help="시작일 YYYY-MM-DD")
        target.add_argument("--to", dest="end", help="종료일 YYYY-MM-DD")
        target.add_argument("--ytd", action="store_true", help="올해 1월 1일부터 오늘까지")
        target.add_argument(
            "--deduction",
            default="all",
            choices=["all", "deductible", "non-deductible"],
            help="공제 분류",
        )
        add_cert_options(target)

    cards = sub.add_parser("cards", help="사업용 신용카드 매입내역")
    add_financial_period_options(cards)
    cards.set_defaults(handler=cmd_business_cards)

    registered_cards = sub.add_parser("registered-cards", help="등록된 사업용 신용카드")
    add_cert_options(registered_cards)
    registered_cards.set_defaults(handler=cmd_registered_business_cards)

    cash_purchases = sub.add_parser("cash-purchases", help="현금영수증 매입 공제내역")
    add_financial_period_options(cash_purchases)
    cash_purchases.set_defaults(handler=cmd_cash_receipt_purchases)

    cash_sales = sub.add_parser("cash-sales", help="현금영수증 매출 월별 합계")
    cash_sales.add_argument("--year", type=int, default=today_kst().year)
    add_cert_options(cash_sales)
    cash_sales.set_defaults(handler=cmd_cash_receipt_sales)

    card_sales = sub.add_parser("card-sales", help="신용카드 매출 월별 합계")
    card_sales.add_argument("--year", type=int, default=today_kst().year)
    card_sales.add_argument("--quarter-from", type=int, default=1, choices=range(1, 5))
    card_sales.add_argument(
        "--quarter-to",
        type=int,
        default=(today_kst().month - 1) // 3 + 1,
        choices=range(1, 5),
    )
    add_cert_options(card_sales)
    card_sales.set_defaults(handler=cmd_card_sales)

    revenue = sub.add_parser(
        "revenue", help="매입매출 리포트(`revenue 2026` 월별+누계, `revenue -ytd 2026` 누계만)"
    )
    revenue.add_argument("year", nargs="?", type=int, help="연도(생략하면 올해)")
    revenue.add_argument(
        "-ytd",
        "--ytd",
        dest="ytd",
        nargs="?",
        type=int,
        const=True,
        metavar="YEAR",
        help="월별 행 없이 누계만(연도 생략 시 올해)",
    )
    revenue.add_argument("-m", "--monthly", action="store_true", help="월별 행을 보여 줍니다")
    revenue.add_argument(
        "--by", choices=["counterparty"], help="counterparty: 세금계산서 거래처별 월 표"
    )
    revenue.add_argument(
        "--basis",
        default="written",
        choices=["written", "issued", "transmitted"],
        help="세금계산서 월 귀속 기준일(기본 작성일)",
    )
    add_cert_options(revenue)
    revenue.set_defaults(handler=cmd_revenue)

    vat = sub.add_parser("vat", help="예상 부가세(과세기간 YYYY-1|YYYY-2, 생략 시 현재 기간)")
    vat.add_argument("period", nargs="?", help="과세기간 예: 2026-2")
    add_cert_options(vat)
    vat.set_defaults(handler=cmd_vat)

    business_accounts = sub.add_parser("business-accounts", help="사업용계좌 신고현황")
    add_cert_options(business_accounts)
    business_accounts.set_defaults(handler=cmd_business_accounts)

    counterparties = sub.add_parser("counterparties", help="등록 거래처 목록")
    counterparties.add_argument("--name", help="거래처명 검색")
    counterparties.add_argument("--business-number", help="사업자등록번호 10자리")
    counterparties.add_argument("--representative", help="대표자명 검색")
    add_cert_options(counterparties)
    counterparties.set_defaults(handler=cmd_counterparties)

    def add_write_options(target):
        target.add_argument(
            "--yes", action="store_true", help="미리보기 뒤 실제로 홈택스에 전송합니다"
        )
        target.add_argument("--file", help="요청 본문 JSON 파일")
        add_cert_options(target)

    add_cp = sub.add_parser("add-counterparty", help="거래처 등록(기본 미리보기)")
    add_cp.add_argument("--business-number", required=True, help="사업자등록번호 10자리")
    add_cp.add_argument("--name", required=True, help="거래처명")
    add_cp.add_argument("--representative", help="대표자명")
    add_cp.add_argument("--branch", default="", help="종사업장번호 4자리")
    add_write_options(add_cp)
    add_cp.set_defaults(handler=functools.partial(counterparty_change, action="add"))

    edit_cp = sub.add_parser("edit-counterparty", help="거래처 수정(기본 미리보기)")
    edit_cp.add_argument("--business-number", required=True)
    edit_cp.add_argument("--branch", default="")
    add_write_options(edit_cp)
    edit_cp.set_defaults(handler=functools.partial(counterparty_change, action="edit"))

    remove_cp = sub.add_parser("remove-counterparty", help="거래처 삭제(기본 미리보기)")
    remove_cp.add_argument("--business-number", required=True)
    remove_cp.add_argument("--branch", default="")
    add_write_options(remove_cp)
    remove_cp.set_defaults(handler=functools.partial(counterparty_change, action="remove"))

    def add_wire_option(target):
        target.add_argument(
            "--wire", choices=["raw", "base64"], help="홈택스 전송 형식(--yes 일 때 필수)"
        )

    issue = sub.add_parser("issue", help="세금계산서 발행(기본 미리보기)")
    issue.add_argument("--file", dest="file", required=True, help="발행 요청 JSON")
    issue.add_argument("--yes", action="store_true", help="미리보기 뒤 실제로 발행합니다")
    add_wire_option(issue)
    add_cert_options(issue)
    issue.set_defaults(handler=functools.partial(invoice_operation, action="issue"))

    correct = sub.add_parser("correct", help="기재사항 정정(기본 미리보기)")
    correct.add_argument("--approval", required=True, help="원본 승인번호")
    correct.add_argument("--file", dest="file", required=True, help="정정 요청 JSON")
    correct.add_argument("--yes", action="store_true")
    add_wire_option(correct)
    add_cert_options(correct)
    correct.set_defaults(handler=functools.partial(invoice_operation, action="correct"))

    cancel = sub.add_parser("cancel", help="전액 취소(기본 미리보기)")
    cancel.add_argument("--approval", required=True, help="원본 승인번호")
    cancel.add_argument(
        "--reason", required=True, choices=["contract_cancellation", "duplicate_issue"]
    )
    cancel.add_argument("--written-date", dest="written_date", help="작성일 YYYY-MM-DD")
    cancel.add_argument("--file", dest="file", help="추가 필드 JSON")
    cancel.add_argument("--yes", action="store_true")
    add_wire_option(cancel)
    add_cert_options(cancel)
    cancel.set_defaults(handler=functools.partial(invoice_operation, action="cancel"))

    for command in sub.choices.values():
        add_json_option(command)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    context = Context(args=args)
    try:
        return asyncio.run(args.handler(context))
    except CommandError as error:
        print(f"오류: {error}", file=sys.stderr)
        return 2
    except (LoginError, CertificateError) as error:
        print(f"오류: {error.message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
