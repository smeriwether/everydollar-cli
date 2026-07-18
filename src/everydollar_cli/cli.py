"""Command line interface for reading EveryDollar data."""

from __future__ import annotations

import calendar
import csv
import hashlib
import json
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from .client import ApiError, AuthError, EveryDollarClient
from .cookies import CookieError, read_session_cookie
from .models import Budget, Transaction, spending_by_item, to_dollars

app = typer.Typer(
    help="Read-only access to your EveryDollar budget, using your Chrome session.",
    no_args_is_help=True,
    add_completion=False,
)

console = Console()
err_console = Console(stderr=True)

CHROME_PROFILE = typer.Option(None, "--profile", help="Chrome profile to read the cookie from.")


def _fail(message: str) -> None:
    err_console.print(f"[red]{message}[/red]")
    raise typer.Exit(code=1)


def _client(profile: str | None) -> EveryDollarClient:
    try:
        return EveryDollarClient(read_session_cookie(profile))
    except CookieError as exc:
        _fail(str(exc))
    raise AssertionError("unreachable")


def _parse_month(value: str | None) -> date:
    """Accept YYYY-MM (or YYYY-MM-DD) and return the first of that month."""
    if not value:
        return date.today().replace(day=1)
    try:
        parts = [int(p) for p in value.split("-")[:2]]
        return date(parts[0], parts[1], 1)
    except (ValueError, IndexError):
        _fail(f"Could not read {value!r} as a month. Use YYYY-MM, for example 2026-07.")
    raise AssertionError("unreachable")


def _month_end(month: date) -> date:
    return month.replace(day=calendar.monthrange(month.year, month.month)[1])


def _month_label(month: date) -> str:
    return month.strftime("%Y-%m")


def _money(cents: int) -> str:
    """Format cents as dollars, parenthesising negatives."""
    amount = to_dollars(abs(cents))
    return f"(${amount:,.2f})" if cents < 0 else f"${amount:,.2f}"


def _tone(cents: int) -> str:
    return "red" if cents < 0 else "green"


@app.command()
def budget(
    month: Optional[str] = typer.Option(None, "--month", "-m", help="Month as YYYY-MM. Defaults to this month."),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON instead of a table."),
    profile: Optional[str] = CHROME_PROFILE,
) -> None:
    """Show a month's budget with planned, spent and remaining amounts."""
    target = _parse_month(month)
    with _client(profile) as client:
        try:
            data = client.budget_for_month(target)
            transactions = client.transactions(target, _month_end(target))
        except (ApiError, AuthError) as exc:
            _fail(str(exc))

    spent = spending_by_item(data, transactions)

    if as_json:
        console.print_json(json.dumps(_budget_payload(data, spent)))
        return

    _render_budget(data, spent, target)


def _budget_payload(data: Budget, spent: dict[str, int]) -> dict:
    return {
        "id": data.id,
        "date": data.date.isoformat() if data.date else None,
        "plannedIncome": float(to_dollars(data.planned_income_cents)),
        "plannedSpending": float(to_dollars(data.planned_spending_cents)),
        "leftToBudget": float(to_dollars(data.left_to_budget_cents)),
        "groups": [
            {
                "label": group.label,
                "type": group.type,
                "items": [
                    {
                        "label": item.label,
                        "type": item.type,
                        "planned": float(to_dollars(item.budgeted_cents)),
                        "spent": float(to_dollars(abs(spent.get(item.id, 0)))),
                        "remaining": float(
                            to_dollars(item.budgeted_cents - abs(spent.get(item.id, 0)))
                        ),
                    }
                    for item in group.items
                ],
            }
            for group in data.groups
        ],
    }


def _render_budget(data: Budget, spent: dict[str, int], target: date) -> None:
    heading = target.strftime("%B %Y")
    console.print()
    console.print(f"[bold]{heading}[/bold]")

    left = data.left_to_budget_cents
    label = "left to budget" if left >= 0 else "over budget"
    console.print(f"[{_tone(left)}]{_money(abs(left))}[/{_tone(left)}] {label}\n")

    for group in data.groups:
        if not group.items:
            continue

        table = Table(title=group.label, title_justify="left", header_style="dim", expand=False)
        table.add_column("Item")
        table.add_column("Planned", justify="right")
        table.add_column("Spent", justify="right")
        table.add_column("Remaining", justify="right")

        for item in group.items:
            # Spending arrives negative; compare against the plan as a magnitude.
            used = abs(spent.get(item.id, 0))
            remaining = item.budgeted_cents - used
            table.add_row(
                item.label,
                _money(item.budgeted_cents),
                _money(used),
                f"[{_tone(remaining)}]{_money(remaining)}[/{_tone(remaining)}]",
            )

        used_total = sum(abs(spent.get(i.id, 0)) for i in group.items)
        remaining_total = group.budgeted_cents - used_total
        table.add_section()
        table.add_row(
            "[bold]Total[/bold]",
            f"[bold]{_money(group.budgeted_cents)}[/bold]",
            f"[bold]{_money(used_total)}[/bold]",
            f"[bold {_tone(remaining_total)}]{_money(remaining_total)}[/bold {_tone(remaining_total)}]",
        )
        console.print(table)
        console.print()


@app.command()
def transactions(
    month: Optional[str] = typer.Option(None, "--month", "-m", help="Month as YYYY-MM. Defaults to this month."),
    start: Optional[str] = typer.Option(None, "--start", help="Start date YYYY-MM-DD. Overrides --month."),
    end: Optional[str] = typer.Option(None, "--end", help="End date YYYY-MM-DD."),
    category: Optional[str] = typer.Option(None, "--category", "-c", help="Filter by budget category, case-insensitive."),
    search: Optional[str] = typer.Option(None, "--search", "-s", help="Filter by merchant text."),
    limit: int = typer.Option(50, "--limit", "-n", help="Maximum rows to show. Use 0 for all."),
    include_deleted: bool = typer.Option(False, "--include-deleted", help="Include soft-deleted transactions."),
    as_json: bool = typer.Option(False, "--json", help="Emit JSON."),
    as_csv: bool = typer.Option(False, "--csv", help="Emit CSV to stdout."),
    profile: Optional[str] = CHROME_PROFILE,
) -> None:
    """List transactions over a month or an explicit date range."""
    if start:
        try:
            start_date = date.fromisoformat(start)
            end_date = date.fromisoformat(end) if end else date.today()
        except ValueError:
            _fail("Dates must be in YYYY-MM-DD form.")
    else:
        target = _parse_month(month)
        start_date, end_date = target, _month_end(target)

    with _client(profile) as client:
        try:
            rows = client.transactions(start_date, end_date, include_deleted=include_deleted)
        except (ApiError, AuthError) as exc:
            _fail(str(exc))

    if category:
        needle = category.lower()
        rows = [t for t in rows if any(needle in c.lower() for c in t.categories)]
    if search:
        needle = search.lower()
        rows = [t for t in rows if needle in t.merchant.lower()]

    rows.sort(key=lambda t: (t.date or date.min), reverse=True)
    total = len(rows)
    if limit > 0:
        rows = rows[:limit]

    if as_csv:
        _write_csv(rows)
        return
    if as_json:
        console.print_json(json.dumps([_transaction_payload(t) for t in rows]))
        return

    _render_transactions(rows, total, start_date, end_date)


def _transaction_payload(t: Transaction) -> dict:
    return {
        "id": t.id,
        "date": t.date.isoformat() if t.date else None,
        "merchant": t.merchant,
        "amount": float(to_dollars(t.amount_cents)),
        "categories": t.categories,
        "split": t.is_split,
        "note": t.note,
    }


def _build_snapshot(
    client: EveryDollarClient,
    target: date,
    *,
    window_days: int = 45,
    captured_at: datetime | None = None,
) -> dict:
    """Build an archival snapshot using allocation identity, not transaction date."""
    index = client.budget_index()
    budget_id = index.get(target.year, {}).get(target.month)
    if not budget_id:
        raise ApiError(f"EveryDollar has no budget for {_month_label(target)}.")

    budget = client.budget_payload(budget_id)
    item_ids = {
        item.get("id")
        for group in budget.get("groups", [])
        for item in group.get("budgetItems", [])
        if item.get("id")
    }
    start = target - timedelta(days=window_days)
    end = _month_end(target) + timedelta(days=window_days)
    candidates = client.transaction_payloads(start, end)

    transactions = []
    matching_allocation_count = 0
    for transaction in candidates:
        matching_allocations = [
            allocation
            for allocation in transaction.get("allocations") or []
            if allocation.get("budgetItemId") in item_ids
        ]
        if not matching_allocations:
            continue
        matching_allocation_count += len(matching_allocations)
        transactions.append(transaction)

    transactions.sort(key=lambda row: ((row.get("date") or ""), (row.get("id") or "")))
    month_start = target.isoformat()
    month_end = _month_end(target).isoformat()
    unassigned = [
        transaction
        for transaction in candidates
        if month_start <= (transaction.get("date") or "")[:10] <= month_end
        and not (transaction.get("allocations") or [])
    ]
    unassigned.sort(key=lambda row: ((row.get("date") or ""), (row.get("id") or "")))
    content = {
        "budget": budget,
        "transactions": transactions,
        "unassignedTransactions": unassigned,
    }
    content_hash = hashlib.sha256(
        json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    captured = captured_at or datetime.now(timezone.utc)

    return {
        "schemaVersion": 1,
        "source": "everydollar",
        "capturedAt": captured.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "budgetMonth": _month_label(target),
        "budgetId": budget_id,
        "contentHash": content_hash,
        "transactionQuery": {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "windowDays": window_days,
            "association": "allocation budgetItemId belongs to the selected budget",
        },
        "counts": {
            "transactions": len(transactions),
            "allocations": matching_allocation_count,
            "softDeletedTransactions": sum(1 for row in transactions if row.get("deletedAt")),
            "outOfMonthTransactions": sum(
                1
                for row in transactions
                if not month_start <= (row.get("date") or "")[:10] <= month_end
            ),
            "unassignedTransactions": len(unassigned),
            "uncategorizedTransactions": sum(1 for row in unassigned if not row.get("deletedAt")),
        },
        "budget": budget,
        "transactions": transactions,
        "unassignedTransactions": unassigned,
    }


@app.command()
def snapshot(
    month: str = typer.Option(..., "--month", "-m", help="Budget month as YYYY-MM."),
    as_json: bool = typer.Option(False, "--json", help="Emit the complete archival JSON snapshot."),
    window_days: int = typer.Option(
        45,
        "--window-days",
        min=0,
        help="Days on either side of the month to inspect for assigned transactions.",
    ),
    profile: Optional[str] = CHROME_PROFILE,
) -> None:
    """Snapshot one budget month, including split and out-of-month allocations."""
    target = _parse_month(month)
    with _client(profile) as client:
        try:
            payload = _build_snapshot(client, target, window_days=window_days)
        except (ApiError, AuthError) as exc:
            _fail(str(exc))

    if as_json:
        console.print_json(json.dumps(payload))
        return

    counts = payload["counts"]
    console.print()
    console.print(f"  [bold]{payload['budgetMonth']}[/bold]  {counts['transactions']} transactions, "
                  f"{counts['allocations']} allocations, "
                  f"{counts['uncategorizedTransactions']} uncategorized")
    console.print(f"  content {payload['contentHash']}")
    console.print()


def _write_csv(rows: list[Transaction]) -> None:
    writer = csv.writer(sys.stdout)
    writer.writerow(["date", "merchant", "amount", "categories", "split", "note"])
    for t in rows:
        writer.writerow(
            [
                t.date.isoformat() if t.date else "",
                t.merchant,
                f"{to_dollars(t.amount_cents):.2f}",
                "; ".join(t.categories),
                "yes" if t.is_split else "",
                t.note,
            ]
        )


def _render_transactions(rows: list[Transaction], total: int, start: date, end: date) -> None:
    table = Table(
        title=f"Transactions {start.isoformat()} to {end.isoformat()}",
        title_justify="left",
        header_style="dim",
    )
    table.add_column("Date")
    table.add_column("Merchant")
    table.add_column("Category")
    table.add_column("Amount", justify="right")

    for t in rows:
        categories = ", ".join(t.categories)
        if t.is_split:
            categories += " [dim](split)[/dim]"
        table.add_row(
            t.date.isoformat() if t.date else "",
            t.merchant or "[dim]—[/dim]",
            categories,
            f"[{_tone(t.amount_cents)}]{_money(t.amount_cents)}[/{_tone(t.amount_cents)}]",
        )

    console.print()
    console.print(table)

    spent = sum(t.amount_cents for t in rows if t.amount_cents < 0)
    income = sum(t.amount_cents for t in rows if t.amount_cents > 0)
    console.print(f"  Showing {len(rows)} of {total}   spent {_money(spent)}   income {_money(income)}")
    console.print()


@app.command()
def accounts(
    as_json: bool = typer.Option(False, "--json", help="Emit JSON."),
    profile: Optional[str] = CHROME_PROFILE,
) -> None:
    """List linked accounts and balances."""
    with _client(profile) as client:
        try:
            rows = client.accounts()
        except (ApiError, AuthError) as exc:
            _fail(str(exc))

    if as_json:
        console.print_json(
            json.dumps(
                [
                    {
                        "name": a.name,
                        "institution": a.institution,
                        "type": a.type,
                        "last4": a.last4,
                        "balance": float(to_dollars(a.balance_cents)),
                    }
                    for a in rows
                ]
            )
        )
        return

    table = Table(title="Accounts", title_justify="left", header_style="dim")
    table.add_column("Account")
    table.add_column("Institution")
    table.add_column("Type")
    table.add_column("Last 4")
    table.add_column("Balance", justify="right")

    for a in rows:
        table.add_row(
            a.name,
            a.institution,
            a.type,
            a.last4 or "[dim]—[/dim]",
            f"[{_tone(a.balance_cents)}]{_money(a.balance_cents)}[/{_tone(a.balance_cents)}]",
        )

    cash = sum(a.balance_cents for a in rows if not a.is_debt)
    debt = sum(a.balance_cents for a in rows if a.is_debt)
    table.add_section()
    table.add_row(
        "[bold]Net[/bold]", "", "", "",
        f"[bold {_tone(cash + debt)}]{_money(cash + debt)}[/bold {_tone(cash + debt)}]",
    )

    console.print()
    console.print(table)
    console.print(f"  cash {_money(cash)}   debt {_money(debt)}")
    console.print()


@app.command()
def months(
    as_json: bool = typer.Option(False, "--json", help="Emit machine-readable JSON."),
    profile: Optional[str] = CHROME_PROFILE,
) -> None:
    """List every month that has a budget."""
    with _client(profile) as client:
        try:
            index = client.budget_index()
        except (ApiError, AuthError) as exc:
            _fail(str(exc))

    available = [f"{year:04d}-{month:02d}" for year in sorted(index) for month in sorted(index[year])]
    if as_json:
        console.print_json(json.dumps({"months": available, "count": len(available)}))
        return

    console.print()
    for year in sorted(index):
        labels = [date(year, m, 1).strftime("%b") for m in sorted(index[year])]
        console.print(f"  [bold]{year}[/bold]  {' '.join(labels)}")
    console.print()


@app.command()
def status(profile: Optional[str] = CHROME_PROFILE) -> None:
    """Check that the Chrome session cookie works."""
    try:
        cookie = read_session_cookie(profile)
    except CookieError as exc:
        _fail(str(exc))

    console.print(f"  Cookie found in Chrome ({len(cookie)} chars)")
    with EveryDollarClient(cookie) as client:
        try:
            index = client.budget_index()
        except AuthError as exc:
            _fail(str(exc))
        except ApiError as exc:
            _fail(str(exc))

    total = sum(len(v) for v in index.values())
    console.print(f"  [green]Session is valid[/green] — {total} budget months available")


if __name__ == "__main__":
    app()
