"""Typed views over the EveryDollar API payloads.

Two conventions run through the whole API and are normalised here:

* Every monetary field is an integer count of cents.
* Expenses are negative and income is positive.

Identifiers arrive as URNs such as
``urn:everydollar:budget:<budget>:item:4242424242``. Only the trailing segment is
stable enough to join on, so :func:`short_id` is applied wherever ids are matched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


def short_id(urn: str | None) -> str:
    """Return the final segment of a colon-namespaced EveryDollar id."""
    if not urn:
        return ""
    return urn.rsplit(":", 1)[-1]


def to_dollars(cents: int | None) -> Decimal:
    """Convert an integer cent amount to a Decimal in dollars."""
    return (Decimal(cents or 0) / 100).quantize(Decimal("0.01"))


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value[:10])


@dataclass(frozen=True)
class Allocation:
    """A slice of a transaction applied to one budget item.

    A transaction with more than one allocation is a split.
    """

    budget_item_id: str
    label: str
    amount_cents: int

    @classmethod
    def from_api(cls, raw: dict) -> "Allocation":
        return cls(
            budget_item_id=short_id(raw.get("budgetItemId")),
            label=raw.get("label") or "",
            amount_cents=raw.get("amount") or 0,
        )


@dataclass(frozen=True)
class Transaction:
    id: str
    date: date | None
    merchant: str
    amount_cents: int
    note: str
    deleted: bool
    account_id: str
    allocations: list[Allocation] = field(default_factory=list)

    @classmethod
    def from_api(cls, raw: dict) -> "Transaction":
        # financialAccountId is prefixed with "fn" and joins against an
        # account's vendorId rather than its id.
        raw_account = raw.get("financialAccountId") or ""
        return cls(
            id=short_id(raw.get("id")),
            date=parse_date(raw.get("date")),
            merchant=raw.get("merchant") or raw.get("description") or "",
            amount_cents=raw.get("amount") or 0,
            note=raw.get("note") or "",
            deleted=bool(raw.get("deletedAt")),
            account_id=raw_account.removeprefix("fn"),
            allocations=[Allocation.from_api(a) for a in raw.get("allocations") or []],
        )

    @property
    def is_split(self) -> bool:
        return len(self.allocations) > 1

    @property
    def is_income(self) -> bool:
        return self.amount_cents > 0

    @property
    def categories(self) -> list[str]:
        return [a.label for a in self.allocations] or ["Uncategorized"]


@dataclass(frozen=True)
class BudgetItem:
    id: str
    label: str
    type: str
    budgeted_cents: int
    note: str
    due_date: date | None

    @classmethod
    def from_api(cls, raw: dict) -> "BudgetItem":
        return cls(
            id=short_id(raw.get("id")),
            label=raw.get("label") or "",
            type=raw.get("type") or "",
            budgeted_cents=raw.get("amountBudgeted") or 0,
            note=raw.get("note") or "",
            due_date=parse_date(raw.get("dueDate")),
        )


@dataclass(frozen=True)
class BudgetGroup:
    id: str
    label: str
    type: str
    items: list[BudgetItem]

    @classmethod
    def from_api(cls, raw: dict) -> "BudgetGroup":
        return cls(
            id=short_id(raw.get("id")),
            label=raw.get("label") or "",
            type=raw.get("type") or "",
            items=[BudgetItem.from_api(i) for i in raw.get("budgetItems") or []],
        )

    @property
    def budgeted_cents(self) -> int:
        return sum(item.budgeted_cents for item in self.items)


@dataclass(frozen=True)
class Budget:
    id: str
    date: date | None
    buffer_cents: int
    groups: list[BudgetGroup]

    @classmethod
    def from_api(cls, raw: dict) -> "Budget":
        return cls(
            id=short_id(raw.get("id")),
            date=parse_date(raw.get("date")),
            buffer_cents=raw.get("bufferAmountCents") or 0,
            groups=[BudgetGroup.from_api(g) for g in raw.get("groups") or []],
        )

    @property
    def items(self) -> list[BudgetItem]:
        return [item for group in self.groups for item in group.items]

    def items_by_id(self) -> dict[str, BudgetItem]:
        return {item.id: item for item in self.items}

    @property
    def planned_income_cents(self) -> int:
        return sum(g.budgeted_cents for g in self.groups if g.type == "income")

    @property
    def planned_spending_cents(self) -> int:
        return sum(g.budgeted_cents for g in self.groups if g.type != "income")

    @property
    def left_to_budget_cents(self) -> int:
        return self.planned_income_cents - self.planned_spending_cents


@dataclass(frozen=True)
class Account:
    id: str
    name: str
    type: str
    balance_cents: int
    last4: str
    institution: str

    @classmethod
    def from_api(cls, raw: dict) -> "Account":
        return cls(
            id=str(raw.get("id") or ""),
            name=raw.get("name") or "",
            type=(raw.get("type") or "").title(),
            balance_cents=raw.get("balanceCents") or 0,
            last4=str(raw.get("accountNumberDisplay") or ""),
            institution=raw.get("institutionName") or "",
        )

    @property
    def is_debt(self) -> bool:
        return self.type.upper() == "DEBT"


def spending_by_item(
    budget: Budget, transactions: list[Transaction]
) -> dict[str, int]:
    """Total the allocations applied to each budget item.

    Allocations are used rather than transaction totals so that split
    transactions land in each of their categories.
    """
    totals: dict[str, int] = {}
    for transaction in transactions:
        for allocation in transaction.allocations:
            totals[allocation.budget_item_id] = (
                totals.get(allocation.budget_item_id, 0) + allocation.amount_cents
            )
    return totals
