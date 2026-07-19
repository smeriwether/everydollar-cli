from datetime import date

import httpx
import pytest
import respx

from everydollar_cli.client import BASE_URL, ApiError, AuthError, EveryDollarClient


@pytest.fixture
def client():
    with EveryDollarClient("test-cookie") as instance:
        yield instance


@respx.mock
def test_sends_session_cookie(client):
    route = respx.get(f"{BASE_URL}/accounts").mock(return_value=httpx.Response(200, json=[]))

    client.accounts()

    assert "SESSION=test-cookie" in route.calls[0].request.headers["cookie"]


@respx.mock
def test_budget_index_maps_years_to_months(client):
    respx.get(f"{BASE_URL}/budgets").mock(
        return_value=httpx.Response(
            200, json={"budgetExistence": {"2026": {"1": "budget-a", "7": "budget-b"}}}
        )
    )

    assert client.budget_index() == {2026: {1: "budget-a", 7: "budget-b"}}


@respx.mock
def test_budget_for_month_anchors_to_first_of_month(client):
    route = respx.get(f"{BASE_URL}/budgets/search/getBudgetByDate").mock(
        return_value=httpx.Response(200, json={"id": "b1", "date": "2026-07-01", "groups": []})
    )

    client.budget_for_month(date(2026, 7, 18))

    assert route.calls[0].request.url.params["date"] == "2026-07-01"


@respx.mock
def test_budget_payload_preserves_raw_fields(client):
    payload = {"id": "b1", "date": "2026-07-01", "groups": [], "futureField": {"kept": True}}
    respx.get(f"{BASE_URL}/budgets/b1").mock(return_value=httpx.Response(200, json=payload))

    assert client.budget_payload("b1") == payload


@respx.mock
def test_transactions_exclude_soft_deleted_by_default(client):
    respx.get(f"{BASE_URL}/transactions/search/findByDateRange").mock(
        return_value=httpx.Response(
            200,
            json={
                "transactions": [
                    {"id": "1", "merchant": "Kept", "amount": -100},
                    {"id": "2", "merchant": "Removed", "amount": -200, "deletedAt": "2026-07-11"},
                ]
            },
        )
    )

    rows = client.transactions(date(2026, 7, 1), date(2026, 7, 31))

    assert [t.merchant for t in rows] == ["Kept"]


@respx.mock
def test_transactions_can_include_soft_deleted(client):
    respx.get(f"{BASE_URL}/transactions/search/findByDateRange").mock(
        return_value=httpx.Response(
            200,
            json={
                "transactions": [
                    {"id": "1", "merchant": "Kept", "amount": -100},
                    {"id": "2", "merchant": "Removed", "amount": -200, "deletedAt": "2026-07-11"},
                ]
            },
        )
    )

    rows = client.transactions(date(2026, 7, 1), date(2026, 7, 31), include_deleted=True)

    assert len(rows) == 2


@respx.mock
def test_transaction_payloads_preserve_raw_fields_and_deleted_rows(client):
    payload = {
        "transactions": [
            {"id": "1", "amount": -100, "futureField": "kept"},
            {"id": "2", "amount": -200, "deletedAt": "2026-07-11"},
        ]
    }
    respx.get(f"{BASE_URL}/transactions/search/findByDateRange").mock(
        return_value=httpx.Response(200, json=payload)
    )

    rows = client.transaction_payloads(date(2026, 7, 1), date(2026, 7, 31))

    assert rows == payload["transactions"]


@respx.mock
@pytest.mark.parametrize("status", [401, 403])
def test_rejected_cookie_raises_auth_error(client, status):
    respx.get(f"{BASE_URL}/accounts").mock(return_value=httpx.Response(status))

    with pytest.raises(AuthError, match="expired"):
        client.accounts()


@respx.mock
def test_login_redirect_is_treated_as_expired_session(client):
    respx.get(f"{BASE_URL}/accounts").mock(
        return_value=httpx.Response(302, headers={"location": "https://www.everydollar.com/sign-in"})
    )

    with pytest.raises(AuthError):
        client.accounts()


@respx.mock
def test_server_error_raises_api_error(client):
    respx.get(f"{BASE_URL}/accounts").mock(return_value=httpx.Response(500))

    with pytest.raises(ApiError):
        client.accounts()


# --- session renewal ---------------------------------------------------------


@respx.mock
def test_retries_once_with_a_renewed_session():
    """A rejected session is replaced mid-request rather than failing the run."""
    respx.get(f"{BASE_URL}/accounts").mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json=[])]
    )

    with EveryDollarClient("dead", on_auth_failure=lambda: "renewed") as client:
        assert client.accounts() == []

    assert "SESSION=renewed" in respx.calls[1].request.headers["cookie"]


@respx.mock
def test_treats_a_redirect_as_a_rejected_session():
    """An expired session redirects to the login page instead of returning 401."""
    respx.get(f"{BASE_URL}/accounts").mock(
        side_effect=[
            httpx.Response(302, headers={"location": "https://www.everydollar.com/app/login"}),
            httpx.Response(200, json=[]),
        ]
    )

    with EveryDollarClient("dead", on_auth_failure=lambda: "renewed") as client:
        assert client.accounts() == []


@respx.mock
def test_gives_up_after_one_retry():
    """If a freshly minted session is also rejected, retrying would just loop."""
    route = respx.get(f"{BASE_URL}/accounts").mock(return_value=httpx.Response(401))

    with EveryDollarClient("dead", on_auth_failure=lambda: "also-dead") as client:
        with pytest.raises(AuthError):
            client.accounts()

    assert route.call_count == 2


@respx.mock
def test_without_a_refresh_callback_it_fails_as_before(client):
    respx.get(f"{BASE_URL}/accounts").mock(return_value=httpx.Response(401))

    with pytest.raises(AuthError):
        client.accounts()
