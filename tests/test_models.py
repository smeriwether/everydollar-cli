from datetime import date
from decimal import Decimal

from everydollar_cli.models import (
    Account,
    Budget,
    Transaction,
    short_id,
    spending_by_item,
    to_dollars,
)

BUDGET_URN = "urn:everydollar:budget:abc12345:item:4242424242"


def test_short_id_takes_final_urn_segment():
    assert short_id(BUDGET_URN) == "4242424242"


def test_short_id_passes_through_bare_ids():
    assert short_id("4242424242") == "4242424242"


def test_short_id_handles_missing_value():
    assert short_id(None) == ""


def test_to_dollars_converts_cents():
    assert to_dollars(500000) == Decimal("5000.00")


def test_to_dollars_handles_negative_and_none():
    assert to_dollars(-384) == Decimal("-3.84")
    assert to_dollars(None) == Decimal("0.00")


def _transaction(**overrides) -> dict:
    raw = {
        "id": "urn:everydollar:budget:transaction:11112222",
        "date": "2026-07-10",
        "merchant": "Corner Cafe",
        "amount": -384,
        "allocations": [
            {"budgetItemId": BUDGET_URN, "label": "Coffee Shops", "amount": -384}
        ],
    }
    raw.update(overrides)
    return raw


def test_transaction_parses_core_fields():
    parsed = Transaction.from_api(_transaction())

    assert parsed.merchant == "Corner Cafe"
    assert parsed.date == date(2026, 7, 10)
    assert parsed.amount_cents == -384
    assert parsed.categories == ["Coffee Shops"]


def test_transaction_strips_fn_prefix_from_account_id():
    parsed = Transaction.from_api(_transaction(financialAccountId="fn12345"))

    assert parsed.account_id == "12345"


def test_transaction_without_allocations_is_uncategorized():
    parsed = Transaction.from_api(_transaction(allocations=[]))

    assert parsed.categories == ["Uncategorized"]
    assert parsed.is_split is False


def test_transaction_with_multiple_allocations_is_a_split():
    parsed = Transaction.from_api(
        _transaction(
            amount=-30000,
            allocations=[
                {"budgetItemId": "urn:x:item:1", "label": "Rent", "amount": -20000},
                {"budgetItemId": "urn:x:item:2", "label": "Utilities", "amount": -10000},
            ],
        )
    )

    assert parsed.is_split is True
    assert parsed.categories == ["Rent", "Utilities"]


def test_transaction_flags_soft_deletion():
    assert Transaction.from_api(_transaction(deletedAt="2026-07-11")).deleted is True
    assert Transaction.from_api(_transaction()).deleted is False


def _budget() -> Budget:
    return Budget.from_api(
        {
            "id": "abc12345",
            "date": "2026-07-01",
            "bufferAmountCents": 0,
            "groups": [
                {
                    "id": "urn:x:group:1",
                    "label": "Income",
                    "type": "income",
                    "budgetItems": [
                        {"id": "urn:x:item:10", "label": "Paycheck", "amountBudgeted": 500000}
                    ],
                },
                {
                    "id": "urn:x:group:2",
                    "label": "Essentials",
                    "type": "expense",
                    "budgetItems": [
                        {"id": BUDGET_URN, "label": "Coffee Shops", "amountBudgeted": 5000},
                        {"id": "urn:x:item:99", "label": "Groceries", "amountBudgeted": 80000},
                    ],
                },
            ],
        }
    )


def test_budget_separates_planned_income_from_spending():
    budget = _budget()

    assert budget.planned_income_cents == 500000
    assert budget.planned_spending_cents == 85000
    assert budget.left_to_budget_cents == 415000


def test_budget_flattens_items_across_groups():
    assert len(_budget().items) == 3


def test_spending_by_item_sums_allocations_against_short_ids():
    transactions = [
        Transaction.from_api(_transaction()),
        Transaction.from_api(_transaction(amount=-616)),
    ]

    totals = spending_by_item(_budget(), transactions)

    assert totals["4242424242"] == -768


def test_spending_by_item_splits_across_categories():
    split = Transaction.from_api(
        _transaction(
            amount=-30000,
            allocations=[
                {"budgetItemId": BUDGET_URN, "label": "Coffee Shops", "amount": -10000},
                {"budgetItemId": "urn:x:item:99", "label": "Groceries", "amount": -20000},
            ],
        )
    )

    totals = spending_by_item(_budget(), [split])

    assert totals["4242424242"] == -10000
    assert totals["99"] == -20000


def test_account_reads_cents_and_display_number():
    account = Account.from_api(
        {
            "id": "900000000000000001",
            "name": "Example Rewards Card",
            "type": "DEBT",
            "institutionName": "Example Bank",
            "balanceCents": -123456,
            "accountNumberDisplay": "4321",
        }
    )

    assert account.balance_cents == -123456
    assert account.last4 == "4321"
    assert account.institution == "Example Bank"
    assert account.is_debt is True
