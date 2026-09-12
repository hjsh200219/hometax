"""hometax 명령줄 도구.

인증서 선택 → 로그인 → 세션 캐시 → 조회. 비밀번호는 인자로 받지 않고
`HOMETAX_PW` 환경변수 또는 입력 프롬프트로만 받는다.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from .cert_discovery import (
    CertificateEntry,
    discover,
    load_selection,
    resolve_selection,
    save_selection,
    usable,
)
from .certificates import CertificateError, load_certificate
from .errors import LoginError
from .invoices import CounterpartyQuery, InvoiceFilters, InvoiceQuery
from .local_session import (
    clear_session,
    client_from_session,
    load_session,
    remaining_seconds,
    save_session,
)
from .protocol import HometaxClient

MAX_MONTHS_PER_QUERY = 3
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
    if getattr(args, "no_remember", False):
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


def read_password() -> str:
    password = os.environ.get("HOMETAX_PW", "")
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
        read_password(),
        "der",
    )
    client = build_client()
    try:
        identity = await client.login(material, getattr(args, "login_type", "04") or "04")
    except Exception:
        await client.close()
        raise
    save_session(client, identity)
    return client, identity


async def session_client(args: argparse.Namespace) -> tuple[HometaxClient, dict]:
    """캐시된 세션을 쓰고, 없거나 죽었으면 다시 로그인한다."""
    body = load_session()
    if body:
        client = client_from_session(body)
        try:
            identity = await client.verify()
            return client, identity
        except LoginError:
            await client.close()
            clear_session()
    entry = pick_certificate(args)
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


def parse_range(args: argparse.Namespace) -> tuple[date, date]:
    today = datetime.now(UTC).astimezone().date()
    if getattr(args, "ytd", False):
        return date(today.year, 1, 1), today
    if not args.start or not args.end:
        raise CommandError("--from 과 --to 를 지정하거나 --ytd 를 쓰세요.")
    return date.fromisoformat(args.start), date.fromisoformat(args.end)


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


async def cmd_status(context: Context) -> int:
    body = load_session()
    if not body:
        emit(context, {"active": False}, ["세션 없음. hometax login 을 실행하세요."])
        return 1
    left = remaining_seconds(body)
    emit(
        context,
        {"active": True, "identity": body.get("identity"), "expires_in": left},
        [f"세션 유효: {body.get('identity', {}).get('user_name', '?')} (남은 {left}초)"],
    )
    return 0


async def cmd_logout(context: Context) -> int:
    clear_session()
    emit(context, {"cleared": True}, ["로컬 세션을 지웠습니다(홈택스 원격 로그아웃 아님)."])
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


# ------------------------------------------------------------------ 진입점


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hometax", description="홈택스 전자세금계산서 조회 도구")
    parser.add_argument("--json", action="store_true", help="JSON 으로 출력")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_cert_options(target):
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

    status = sub.add_parser("status", help="세션 상태")
    status.set_defaults(handler=cmd_status)

    logout = sub.add_parser("logout", help="로컬 세션 삭제")
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

    counterparties = sub.add_parser("counterparties", help="등록 거래처 목록")
    counterparties.add_argument("--name", help="거래처명 검색")
    counterparties.add_argument("--business-number", help="사업자등록번호 10자리")
    counterparties.add_argument("--representative", help="대표자명 검색")
    add_cert_options(counterparties)
    counterparties.set_defaults(handler=cmd_counterparties)

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
