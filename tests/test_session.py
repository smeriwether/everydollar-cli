"""Tests for obtaining and renewing the SESSION cookie.

The redirect chain is mocked rather than hit for real, but it is mocked faithfully:
EveryDollar bounces to Auth0, Auth0 bounces back to the callback, and only the
callback sets SESSION. A version of the code that failed to follow redirects, or
that dropped cookies across the domain hop, would fail these.
"""

import httpx
import pytest
import respx

from everydollar_cli.cookies import SsoCookies
from everydollar_cli.session import (
    PROTECTED_URL,
    SessionError,
    SessionProvider,
    SsoExpired,
    silent_reauth,
)

AUTHORIZE = "https://id.ramseysolutions.com/authorize"
CALLBACK = "https://www.everydollar.com/app/login/oauth2/code/auth0"

SSO = SsoCookies(values={"auth0": "sso-value", "did": "device-value"}, expires=None)


def mock_successful_chain(session_value: str = "fresh-session") -> respx.Route:
    """EveryDollar -> Auth0 -> callback, with SESSION set only at the end."""
    authorize = respx.get(AUTHORIZE).mock(
        return_value=httpx.Response(302, headers={"location": CALLBACK})
    )
    respx.get(PROTECTED_URL).mock(return_value=httpx.Response(302, headers={"location": AUTHORIZE}))
    # Host-only, exactly as EveryDollar sets it (no Domain attribute).
    respx.get(CALLBACK).mock(
        return_value=httpx.Response(
            200,
            headers={"set-cookie": f"SESSION={session_value}; Path=/; HttpOnly"},
        )
    )
    return authorize


def test_accepts_a_session_widened_to_the_parent_domain():
    """A host-only match would break if EveryDollar ever set Domain=.everydollar.com."""
    jar = httpx.Cookies()
    jar.set("SESSION", "widened", domain=".everydollar.com", path="/")

    from everydollar_cli.session import _session_from_jar

    assert _session_from_jar(jar) == "widened"


@respx.mock
def test_walks_the_redirect_chain_and_returns_the_new_session():
    mock_successful_chain()

    assert silent_reauth(SSO) == "fresh-session"


@respx.mock
def test_presents_the_sso_cookies_to_auth0():
    authorize = mock_successful_chain()

    silent_reauth(SSO)

    cookie_header = authorize.calls[0].request.headers["cookie"]
    assert "auth0=sso-value" in cookie_header
    assert "did=device-value" in cookie_header


@respx.mock
def test_does_not_leak_sso_cookies_to_everydollar():
    mock_successful_chain()

    silent_reauth(SSO)

    first = respx.calls[0].request
    assert "auth0=sso-value" not in first.headers.get("cookie", "")


@respx.mock
def test_reports_an_expired_sso_session_when_auth0_asks_for_a_login():
    login = "https://www.ramseysolutions.com/signin"
    respx.get(PROTECTED_URL).mock(return_value=httpx.Response(302, headers={"location": AUTHORIZE}))
    respx.get(AUTHORIZE).mock(return_value=httpx.Response(302, headers={"location": login}))
    respx.get(login).mock(return_value=httpx.Response(200, text="<html>sign in</html>"))

    with pytest.raises(SsoExpired, match="single-sign-on session has expired"):
        silent_reauth(SSO)


@respx.mock
def test_errors_when_the_chain_completes_without_a_session():
    respx.get(PROTECTED_URL).mock(return_value=httpx.Response(200, text="<html></html>"))

    with pytest.raises(SessionError, match="without issuing a SESSION"):
        silent_reauth(SSO)


@respx.mock
def test_surfaces_a_network_failure():
    respx.get(PROTECTED_URL).mock(side_effect=httpx.ConnectError("boom"))

    with pytest.raises(SessionError, match="Could not reach EveryDollar"):
        silent_reauth(SSO)


# --- SessionProvider ---------------------------------------------------------


@pytest.fixture
def cache(tmp_path):
    return tmp_path / "session.json"


def provider(cache, monkeypatch, chrome=None, sso_session="sso-session"):
    """A provider with Chrome and the Auth0 handshake both stubbed."""
    from everydollar_cli import session as module

    def fake_chrome(_profile):
        if chrome is None:
            raise module.CookieError("no chrome cookie")
        return chrome

    monkeypatch.setattr(module, "read_session_cookie", fake_chrome)
    monkeypatch.setattr(module, "read_sso_cookies", lambda _profile: SSO)
    monkeypatch.setattr(module, "silent_reauth", lambda _sso: sso_session)
    return SessionProvider(cache_path=cache)


def test_prefers_the_cache(cache, monkeypatch):
    cache.write_text('{"SESSION": "cached"}')

    instance = provider(cache, monkeypatch, chrome="from-chrome")

    assert instance.get() == "cached"
    assert instance.source == "cache"


def test_falls_back_to_chrome_when_the_cache_is_empty(cache, monkeypatch):
    instance = provider(cache, monkeypatch, chrome="from-chrome")

    assert instance.get() == "from-chrome"
    assert instance.source == "chrome"


# The whole point of the change: a closed browser has no SESSION cookie at all.
def test_re_authenticates_when_chrome_has_no_session(cache, monkeypatch):
    instance = provider(cache, monkeypatch, chrome=None)

    assert instance.get() == "sso-session"
    assert instance.source == "sso"


def test_caches_what_it_acquires(cache, monkeypatch):
    provider(cache, monkeypatch, chrome=None).get()

    assert '"SESSION": "sso-session"' in cache.read_text()


def test_cache_is_not_world_readable(cache, monkeypatch):
    provider(cache, monkeypatch, chrome=None).get()

    assert cache.stat().st_mode & 0o077 == 0


# Chrome still holds the value that was just rejected, so returning it again
# would retry with a cookie already known to be dead.
def test_refresh_skips_the_value_that_just_failed(cache, monkeypatch):
    instance = provider(cache, monkeypatch, chrome="stale")
    assert instance.get() == "stale"

    assert instance.refresh() == "sso-session"
    assert instance.source == "sso"


def test_refresh_accepts_a_different_chrome_session(cache, monkeypatch):
    cache.write_text('{"SESSION": "cached-and-dead"}')
    instance = provider(cache, monkeypatch, chrome="newer-chrome")
    assert instance.get() == "cached-and-dead"

    assert instance.refresh() == "newer-chrome"


def test_invalidate_removes_the_cache(cache, monkeypatch):
    instance = provider(cache, monkeypatch, chrome="from-chrome")
    instance.get()

    instance.invalidate()

    assert not cache.exists()


def test_reports_a_helpful_error_when_nothing_is_available(cache, monkeypatch):
    from everydollar_cli import session as module

    monkeypatch.setattr(module, "read_session_cookie", lambda _p: (_ for _ in ()).throw(module.CookieError("no chrome")))
    monkeypatch.setattr(module, "read_sso_cookies", lambda _p: (_ for _ in ()).throw(module.CookieError("no sso cookies")))

    with pytest.raises(SessionError, match="no sso cookies"):
        SessionProvider(cache_path=cache).get()
