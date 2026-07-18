# everydollar-cli

Read-only command line access to your [EveryDollar](https://www.everydollar.com) budget, authenticated with the session you already have open in Chrome.

## Install

```sh
uv sync
```

## Usage

```sh
uv run everydollar status                      # check the Chrome session works
uv run everydollar budget                      # this month's budget
uv run everydollar budget --month 2026-03      # a specific month
uv run everydollar transactions --limit 20
uv run everydollar transactions --category groceries
uv run everydollar transactions --search "whole foods"
uv run everydollar transactions --start 2026-01-01 --end 2026-06-30 --csv > first-half.csv
uv run everydollar accounts
uv run everydollar months                      # every month that has a budget
```

`budget`, `transactions` and `accounts` all accept `--json`; `transactions` also accepts `--csv`. Both write to stdout so they pipe into `jq`, `duckdb` or a spreadsheet.

## Authentication

EveryDollar's web app authenticates with a single `SESSION` cookie. This tool reads that cookie straight out of Chrome's local cookie store on each run, so there is no token to paste and nothing to keep in sync.

Two things follow from how that cookie works:

- It is `HttpOnly`, so page JavaScript cannot see it. Reading it means decrypting Chrome's cookie database, which needs the encryption key from your macOS Keychain. The first run may raise a Keychain prompt — choose **Always Allow** to make later runs silent.
- It carries **no expiry**. It is a true browser-session cookie that lives as long as your Chrome session and is dropped when Chrome fully quits. There is no refresh token to rotate, which is exactly why this tool re-reads Chrome every time: whenever Chrome renews your session, the CLI picks it up with no action from you.

When the session does end, every command fails with a message telling you to log in again at everydollar.com in Chrome. Nothing else is needed.

Use `--profile` if you run more than one Chrome profile:

```sh
uv run everydollar budget --profile "Profile 1"
```

## The API

The web app talks to `https://www.everydollar.com/app/api`, a same-origin REST/JSON service. Reads need only the cookie — the `X-CSRF-TOKEN` header the web app sends guards writes, and this client never writes.

| Endpoint | Purpose |
| --- | --- |
| `GET /budgets` | year → month → budget id index |
| `GET /budgets/search/getBudgetByDate?date=YYYY-MM-01` | a month's budget |
| `GET /budgets/{id}` | a budget by id |
| `GET /transactions/search/findByDateRange?startDate=&endDate=` | transactions in a range |
| `GET /accounts` | linked accounts and balances |

### Conventions worth knowing

These are the things that make the data easy to get subtly wrong, all handled in `models.py`:

- **Every amount is an integer count of cents.** `500000` is `$5,000.00`.
- **Expenses are negative, income positive.** Budget views compare spending against the plan as a magnitude.
- **Ids are URNs.** `urn:everydollar:budget:<budget>:item:4242424242` — only the trailing segment is stable enough to join on.
- **Spending is tracked through allocations, not transactions.** A transaction carries one allocation per category, so a split transaction lands in each of its categories. Totals are summed from allocations for that reason.
- **Transactions are soft-deleted.** Rows with a `deletedAt` are excluded by default; pass `--include-deleted` to keep them.
- **Not every transaction is categorized.** Bank imports arrive with no allocation and show as `Uncategorized`.

## Tests

```sh
uv run pytest
```

The suite covers URN parsing, cent conversion, split allocations, soft-delete filtering, expired-session handling, and Chrome's cookie decryption scheme (using locally encrypted fixtures rather than the real cookie store).

## Scope

Read-only by design. The client issues `GET` requests only and sends no CSRF token, so it cannot modify your budget.
