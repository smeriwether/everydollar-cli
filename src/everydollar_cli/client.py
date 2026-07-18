"""Read-only HTTP client for the EveryDollar web app's internal API.

Authentication is the browser's own SESSION cookie and nothing else. Reads do
not require the X-CSRF-TOKEN header that the web app sends -- that guards writes,
which this client never performs.
"""

from __future__ import annotations

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

    def __init__(self, session_cookie: str, timeout: float = 30.0) -> None:
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
        try:
            response = self._client.get(path, params=params or None)
        except httpx.HTTPError as exc:
            raise ApiError(f"Could not reach EveryDollar: {exc}") from exc

        # An expired session redirects to the login page rather than returning 401.
        if response.status_code in (401, 403) or response.is_redirect:
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

    def accounts(self) -> list[Account]:
        """Fetch every linked account."""
        payload = self._get("/accounts") or []
        return [Account.from_api(a) for a in payload]
