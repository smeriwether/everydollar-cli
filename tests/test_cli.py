from datetime import date, datetime, timezone

import pytest

from everydollar_cli.cli import _build_snapshot
from everydollar_cli.client import ApiError


class FakeClient:
    def budget_index(self):
        return {2026: {4: "budget-april"}}

    def budget_payload(self, budget_id):
        assert budget_id == "budget-april"
        return {
            "id": budget_id,
            "date": "2026-04-01",
            "groups": [
                {
                    "id": "group-1",
                    "label": "Expenses",
                    "type": "expense",
                    "budgetItems": [
                        {"id": "urn:everydollar:budget:budget-april:item:1", "label": "Food"},
                        {"id": "urn:everydollar:budget:budget-april:item:2", "label": "Travel"},
                    ],
                }
            ],
        }

    def transaction_payloads(self, start, end):
        assert start == date(2026, 2, 15)
        assert end == date(2026, 6, 14)
        return [
            {
                "id": "transaction-2",
                "date": "2026-05-01",
                "amount": -3000,
                "deletedAt": "2026-05-02T00:00:00Z",
                "allocations": [
                    {
                        "budgetItemId": "urn:everydollar:budget:budget-april:item:1",
                        "label": "Food",
                        "amount": -1000,
                    },
                    {
                        "budgetItemId": "urn:everydollar:budget:another-month:item:9",
                        "label": "Other",
                        "amount": -2000,
                    },
                ],
            },
            {
                "id": "unrelated",
                "date": "2026-04-10",
                "amount": -500,
                "allocations": [
                    {
                        "budgetItemId": "urn:everydollar:budget:another-month:item:9",
                        "label": "Other",
                        "amount": -500,
                    }
                ],
            },
            {
                "id": "unassigned-active",
                "date": "2026-04-15",
                "amount": -500,
                "allocations": [],
            },
            {
                "id": "unassigned-deleted",
                "date": "2026-04-16",
                "amount": -600,
                "deletedAt": "2026-04-17T00:00:00Z",
                "allocations": [],
            },
            {
                "id": "unassigned-next-month",
                "date": "2026-05-02",
                "amount": -700,
                "allocations": [],
            },
            {
                "id": "transaction-1",
                "date": "2026-03-31",
                "amount": -2500,
                "allocations": [
                    {
                        "budgetItemId": "urn:everydollar:budget:budget-april:item:2",
                        "label": "Travel",
                        "amount": -2500,
                    }
                ],
            },
        ]


def test_snapshot_uses_budget_allocation_identity_and_preserves_out_of_month_rows():
    captured_at = datetime(2026, 7, 18, 12, 30, tzinfo=timezone.utc)

    payload = _build_snapshot(FakeClient(), date(2026, 4, 1), captured_at=captured_at)

    assert payload["budgetMonth"] == "2026-04"
    assert payload["capturedAt"] == "2026-07-18T12:30:00Z"
    assert payload["counts"] == {
        "transactions": 2,
        "allocations": 2,
        "softDeletedTransactions": 1,
        "outOfMonthTransactions": 2,
        "unassignedTransactions": 2,
        "uncategorizedTransactions": 1,
    }
    assert [row["id"] for row in payload["transactions"]] == ["transaction-1", "transaction-2"]
    assert len(payload["transactions"][1]["allocations"]) == 2
    assert [row["id"] for row in payload["unassignedTransactions"]] == [
        "unassigned-active",
        "unassigned-deleted",
    ]
    assert payload["transactionQuery"]["windowDays"] == 45


def test_snapshot_hash_ignores_capture_time():
    first = _build_snapshot(
        FakeClient(),
        date(2026, 4, 1),
        captured_at=datetime(2026, 7, 18, tzinfo=timezone.utc),
    )
    second = _build_snapshot(
        FakeClient(),
        date(2026, 4, 1),
        captured_at=datetime(2026, 7, 19, tzinfo=timezone.utc),
    )

    assert first["contentHash"] == second["contentHash"]


def test_snapshot_rejects_a_month_without_a_budget():
    with pytest.raises(ApiError, match="no budget"):
        _build_snapshot(FakeClient(), date(2026, 5, 1))
