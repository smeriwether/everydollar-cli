# everydollar-cli

Read-only command line access to your [EveryDollar](https://www.everydollar.com) budget, authenticated with the session you already have open in Chrome.

## Requirements

- **macOS.** The tool reads Chrome's cookie store, whose format and Keychain-backed encryption are macOS specific.
- **Google Chrome, logged in to EveryDollar.** The session it reads is the one your browser already holds, so sign in there first.
- **Xcode Command Line Tools**, only for installing through Homebrew — see [Troubleshooting](#troubleshooting).

## Install

```sh
brew tap smeriwether/everydollar
brew install everydollar
```

Homebrew 6 asks you to trust a third-party tap before it will load the formula. If it does:

```sh
brew trust smeriwether/everydollar
```

### From source

```sh
git clone https://github.com/smeriwether/everydollar-cli
cd everydollar-cli
uv sync
uv run everydollar status
```

Every command below then needs a `uv run` prefix.

### First run

```sh
everydollar status
```

macOS will ask for permission to read Chrome's encryption key from your Keychain. Choose **Always Allow** so later runs are silent. A successful run reports how many budget months it can see.

## Usage

```sh
everydollar status                      # check the Chrome session works
everydollar budget                      # this month's budget
everydollar budget --month 2026-03      # a specific month
everydollar transactions --limit 20
everydollar transactions --category groceries
everydollar transactions --search "whole foods"
everydollar transactions --start 2026-01-01 --end 2026-06-30 --csv > first-half.csv
everydollar accounts
everydollar months                      # every month that has a budget
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

## Troubleshooting

**`Your Xcode (…) is too outdated`** — Homebrew builds Python packages from source and refuses to do so when the selected developer tools are older than the current release. If your Command Line Tools are newer than your Xcode, point at them for the install:

```sh
sudo xcode-select --switch /Library/Developer/CommandLineTools
brew install everydollar
sudo xcode-select --switch /Applications/Xcode.app/Contents/Developer
```

`xcodebuild` will not work while switched away from Xcode, so switch back afterwards. Updating Xcode fixes it permanently.

**`Refusing to load formula … from untrusted tap`** — run `brew trust smeriwether/everydollar`.

**`Could not read Chrome's encryption key from the macOS Keychain`** — a permission prompt was dismissed. Re-run and choose **Always Allow**.

**`No SESSION cookie found`** — Chrome is not signed in to EveryDollar on this machine. Sign in and re-run. If you keep several Chrome profiles, name the one that is signed in:

```sh
everydollar budget --profile "Profile 1"
```

**`EveryDollar rejected the session cookie`** — the browser session ended. Log in again at [everydollar.com](https://www.everydollar.com) in Chrome; the next run picks the new cookie up automatically.

## Tests

```sh
uv run pytest
```

The suite covers URN parsing, cent conversion, split allocations, soft-delete filtering, expired-session handling, and Chrome's cookie decryption scheme (using locally encrypted fixtures rather than the real cookie store).

## Releasing

The [tap](https://github.com/smeriwether/homebrew-everydollar) pins an exact tarball, so a new version means tagging a release and updating the formula:

```sh
git tag v0.1.2 && git push origin v0.1.2
gh release create v0.1.2 --title v0.1.2 --notes "..."

# url and sha256 for the formula
curl -sL https://github.com/smeriwether/everydollar-cli/archive/refs/tags/v0.1.2.tar.gz | shasum -a 256
```

If the dependencies changed, regenerate the formula's `resource` blocks with `scripts/generate_resources.py` and paste them in. Homebrew's own `brew update-python-resources` passes a `--uploaded-prior-to` flag that pip 25.x does not accept, so it fails on current setups — hence the script.

Homebrew installs Python dependencies with `--no-binary=:all:`, so every dependency must publish an sdist and must not need a compiler. That is why this tool decrypts cookies with the system `openssl` rather than the `cryptography` package, which would drag in a Rust toolchain at build time.

## Scope

Read-only by design. The client issues `GET` requests only and sends no CSRF token, so it cannot modify your budget.

## License

[MIT](LICENSE)
