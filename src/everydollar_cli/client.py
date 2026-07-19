"""Read-only HTTP client for the EveryDollar web app's internal API.

Authentication is a SESSION cookie. Reads do not require the X-CSRF-TOKEN header
that the web app sends -- that guards writes, which this client never performs.

Because that cookie dies with the browser, the client accepts an on_auth_failure
callback and retries a rejected request once with a freshly minted session. See
session.py for how a new one is obtained without a login prompt.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

import httpx

from .models import Account, Budget, Transaction

BASE_URL = "https://www.everydollar.com/app/api"

_USER_AGENT_ATTRIBUTES = 'channel="web", client="everydollar-cli"'


class ApiError(RuntimeError):
    """A request to EveryDollar failed."""


class AuthError(ApiError):
    """The SESSION cookie was rejected."""


class EveryDollarClient:
    """Read-only access to budgets, transactions and accounts."""

    def __init__(
        self,
        session_cookie: str,
        timeout: float = 30.0,
        on_auth_failure: Callable[[], str] | None = None,
    ) -> None:
        # Supplied by SessionProvider.refresh, so a session that expired between
        # scheduled runs renews itself instead of failing the collection.
        self._on_auth_failure = on_auth_failure
        self._client = httpx.Client(
            base_url=BASE_URL,
            timeout=timeout,
            cookies={"SESSION": session_cookie},
            headers={
                "Accept": "application/json, text/plain, */*",
                "User-Agent-Attributes": _USER_AGENT_ATTRIBUTES,
            },
            follow_redirects=False,
        )

    def __enter__(self) -> "EveryDollarClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, **params: object) -> object:
        response = self._request(path, params)

        # An expired session redirects to the login page rather than returning 401.
        if self._rejected(response) and self._on_auth_failure is not None:
            # One retry only. If a session minted seconds ago is also rejected,
            # the problem is not staleness and retrying would just loop.
            self._client.cookies.set("SESSION", self._on_auth_failure(), domain=".everydollar.com")
            response = self._request(path, params)

        if self._rejected(response):
            raise AuthError(
                "EveryDollar rejected the session cookie.\n"
                "  Your Chrome session has expired. Log in again at\n"
                "  https://www.everydollar.com and re-run this command."
            )
        if response.status_code >= 400:
            raise ApiError(f"GET {path} failed with HTTP {response.status_code}.")

        try:
            return response.json()
        except ValueError as exc:
            raise ApiError(f"GET {path} returned a non-JSON response.") from exc

    def _request(self, path: str, params: dict) -> httpx.Response:
        try:
            return self._client.get(path, params=params or None)
        except httpx.HTTPError as exc:
            raise ApiError(f"Could not reach EveryDollar: {exc}") from exc

    @staticmethod
    def _rejected(response: httpx.Response) -> bool:
        return response.status_code in (401, 403) or response.is_redirect

    def budget_index(self) -> dict[int, dict[int, str]]:
        """Map each year and month to its budget id."""
        payload = self._get("/budgets")
        existence = (payload or {}).get("budgetExistence", {})
        return {
            int(year): {int(month): budget_id for month, budget_id in months.items()}
            for year, months in existence.items()
        }

    def budget_for_month(self, month: date) -> Budget:
        """Fetch the budget covering the given month."""
        anchor = month.replace(day=1).isoformat()
        payload = self._get("/budgets/search/getBudgetByDate", date=anchor)
        return Budget.from_api(payload or {})

    def budget(self, budget_id: str) -> Budget:
        return Budget.from_api(self._get(f"/budgets/{budget_id}") or {})

    def budget_payload(self, budget_id: str) -> dict:
        """Return the unmodified budget payload for archival exports."""
        payload = self._get(f"/budgets/{budget_id}") or {}
        if not isinstance(payload, dict):
            raise ApiError(f"GET /budgets/{budget_id} returned an unexpected payload.")
        return payload

    def transactions(
        self, start: date, end: date, include_deleted: bool = False
    ) -> list[Transaction]:
        """Fetch transactions in a date range, excluding soft-deleted ones by default."""
        payload = self._get(
            "/transactions/search/findByDateRange",
            startDate=start.isoformat(),
            endDate=end.isoformat(),
        )
        raw = (payload or {}).get("transactions", [])
        parsed = [Transaction.from_api(t) for t in raw]
        if include_deleted:
            return parsed
        return [t for t in parsed if not t.deleted]

    def transaction_payloads(self, start: date, end: date) -> list[dict]:
        """Return unmodified transaction payloads, including soft-deleted rows."""
        payload = self._get(
            "/transactions/search/findByDateRange",
            startDate=start.isoformat(),
            endDate=end.isoformat(),
        )
        raw = (payload or {}).get("transactions", [])
        if not isinstance(raw, list) or not all(isinstance(row, dict) for row in raw):
            raise ApiError("EveryDollar returned an unexpected transactions payload.")
        return raw

    def accounts(self) -> list[Account]:
        """Fetch every linked account."""
        payload = self._get("/accounts") or []
        return [Account.from_api(a) for a in payload]
