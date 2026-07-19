"""Obtain a usable EveryDollar SESSION cookie, re-authenticating when it lapses.

Background
----------
EveryDollar's SESSION cookie is session-scoped: close Chrome and it is gone.
Reading it out of Chrome, which is all this tool used to do, therefore broke on
every browser restart -- yet the website itself never asks you to log in again.

It does not need to, because EveryDollar is a Spring Security OAuth2 client and
Auth0 (id.ramseysolutions.com) is the identity provider. Requesting a protected
page without a SESSION redirects to Auth0, which recognises its own *persistent*
SSO cookie, immediately redirects back with an authorization code, and Spring
exchanges that code for a brand new SESSION. No password, no prompt.

This module replays that handshake. Following the redirect chain with Auth0's SSO
cookies in the jar is precisely what the browser does when you reopen the tab.

What this does and does not buy
-------------------------------
Collection survives closing Chrome, and keeps working for as long as the Auth0
SSO session lives -- months rather than hours. It is not permanent: when that
session lapses the only remedy is logging in through a browser again, and the
CLI says so plainly rather than failing obscurely.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import httpx

from .cookies import COOKIE_HOST, COOKIE_NAME, SSO_HOST, CookieError, SsoCookies, read_session_cookie, read_sso_cookies

# Any authenticated page will do; this one is cheap and always exists.
PROTECTED_URL = "https://www.everydollar.com/app/budget"

# Auth0 tailors its response to the client, so present as an ordinary browser.
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

# Landing on either host means Auth0 declined to re-issue silently and wants a
# human at a login form.
_LOGIN_HOSTS = ("www.ramseysolutions.com", "id.ramseysolutions.com")


class SessionError(RuntimeError):
    """No usable SESSION could be obtained."""


class SsoExpired(SessionError):
    """Auth0's SSO session has lapsed; only a browser login can renew it."""


def _cache_path() -> Path:
    root = os.environ.get("XDG_CACHE_HOME")
    base = Path(root) if root else Path.home() / ".cache"
    return base / "everydollar-cli" / "session.json"


def _read_cache(path: Path) -> str | None:
    try:
        return json.loads(path.read_text()).get(COOKIE_NAME) or None
    except (OSError, ValueError):
        return None


def _write_cache(path: Path, session: str) -> None:
    """Persist the session, readable only by this user -- it is a live credential."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(stat.S_IRWXU)
        path.write_text(json.dumps({COOKIE_NAME: session}))
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        # A cache we cannot write costs a handshake per run, not correctness.
        pass


def _session_from_jar(jar: httpx.Cookies) -> str | None:
    """Pull SESSION out of the jar whichever domain form the server used.

    EveryDollar currently sets it host-only on www.everydollar.com, but matching
    that exactly would break silently if they ever widened it to the parent
    domain, so accept any everydollar.com cookie.
    """
    for cookie in jar.jar:
        if cookie.name == COOKIE_NAME and cookie.domain.lstrip(".").endswith(COOKIE_HOST.split(".", 1)[1]):
            return cookie.value
    return None


def silent_reauth(sso: SsoCookies, timeout: float = 30.0) -> str:
    """Walk the OAuth2 redirect chain with Auth0's SSO cookies and return a new SESSION."""
    jar = httpx.Cookies()
    for name, value in sso.values.items():
        jar.set(name, value, domain=SSO_HOST, path="/")

    with httpx.Client(
        cookies=jar,
        follow_redirects=True,
        timeout=timeout,
        headers={"User-Agent": _USER_AGENT, "Accept": "text/html,application/xhtml+xml,*/*"},
    ) as client:
        try:
            response = client.get(PROTECTED_URL)
        except httpx.HTTPError as exc:
            raise SessionError(f"Could not reach EveryDollar while re-authenticating: {exc}") from exc

        if response.url.host in _LOGIN_HOSTS:
            raise SsoExpired(
                "Auth0 asked for a login instead of renewing the session.\n"
                "  The single-sign-on session has expired. Log in at\n"
                "  https://www.everydollar.com in Chrome, then re-run."
            )

        session = _session_from_jar(client.cookies)
        if not session:
            raise SessionError(
                f"Re-authentication finished at {response.url} without issuing a "
                f"{COOKIE_NAME} cookie. EveryDollar may have changed its login flow."
            )
        return session


class SessionProvider:
    """Supplies a SESSION cookie, and can mint a fresh one on demand.

    Sources, in order: the on-disk cache, Chrome's live cookie, then the silent
    Auth0 handshake. The cache means the common case costs no network at all;
    the handshake means a closed browser is no longer fatal.
    """

    def __init__(
        self,
        profile: str | None = None,
        cache_path: Path | None = None,
        use_cache: bool = True,
    ) -> None:
        self._profile = profile
        self._cache_path = cache_path or _cache_path()
        self._use_cache = use_cache
        self._current: str | None = None
        self.source: str | None = None

    def get(self) -> str:
        if self._current:
            return self._current

        if self._use_cache:
            cached = _read_cache(self._cache_path)
            if cached:
                self._current, self.source = cached, "cache"
                return cached

        return self._acquire()

    def refresh(self) -> str:
        """Mint a new SESSION because the current one was rejected."""
        stale, self._current = self._current, None
        return self._acquire(stale=stale)

    def _acquire(self, stale: str | None = None) -> str:
        # Chrome's cookie is only worth trying when the browser is running and
        # holds something other than the value that just failed.
        try:
            from_chrome = read_session_cookie(self._profile)
        except CookieError:
            from_chrome = None

        if from_chrome and from_chrome != stale:
            return self._adopt(from_chrome, "chrome")

        session = silent_reauth(self._sso())
        return self._adopt(session, "sso")

    def _sso(self) -> SsoCookies:
        try:
            return read_sso_cookies(self._profile)
        except CookieError as exc:
            raise SessionError(str(exc)) from exc

    def _adopt(self, session: str, source: str) -> str:
        self._current, self.source = session, source
        if self._use_cache:
            _write_cache(self._cache_path, session)
        return session

    def invalidate(self) -> None:
        self._current = None
        self._cache_path.unlink(missing_ok=True)
