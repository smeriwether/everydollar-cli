"""Read EveryDollar's authentication cookies out of Chrome's local cookie store.

Two different cookies matter, and the difference is the whole reason this module
reads more than one.

The SESSION cookie on www.everydollar.com is Spring Session's. It is HttpOnly and
carries no expiry, so it dies the moment Chrome closes. Reading it alone made the
CLI stop working after every browser restart.

The durable credential is Auth0's SSO cookie on id.ramseysolutions.com, which is
persistent and lives for months. EveryDollar is a Spring OAuth2 client against
Auth0, so a request with a live SSO cookie and no SESSION is silently issued a
new SESSION through the authorization-code redirect chain -- exactly what the
browser does when you reopen the tab without logging in. See session.py.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

COOKIE_NAME = "SESSION"
COOKIE_HOST = "www.everydollar.com"

# Auth0 is the identity provider behind EveryDollar. "auth0" is the SSO session
# itself; "did" identifies the device. The *_compat variants exist for browsers
# that reject SameSite=None, and Auth0 accepts either, so we send whatever is
# present rather than requiring the full set.
SSO_HOST = "id.ramseysolutions.com"
SSO_COOKIE_NAMES = ("auth0", "auth0_compat", "did", "did_compat")

# The one whose expiry actually bounds how long unattended collection works.
SSO_SESSION_COOKIE = "auth0"

# Chrome stores expiries in microseconds since 1601-01-01.
_WINDOWS_EPOCH_OFFSET = 11_644_473_600

# Fixed parameters of Chrome's macOS cookie encryption scheme.
_SALT = b"saltysalt"
_ITERATIONS = 1003
_KEY_LENGTH = 16
_IV = b" " * 16

# Shipped with macOS, so decryption needs no third-party crypto library.
_OPENSSL = "/usr/bin/openssl"

_KEYCHAIN_SERVICE = "Chrome Safe Storage"
_KEYCHAIN_ACCOUNTS = ("Chrome", "Google Chrome")

_CHROME_PROFILE_DIRS = ("Default", "Profile 1", "Profile 2", "Profile 3")


class CookieError(RuntimeError):
    """Raised when the SESSION cookie cannot be read or decrypted."""


@dataclass(frozen=True)
class ChromeProfile:
    name: str
    cookie_db: Path


def _chrome_root() -> Path:
    return Path.home() / "Library" / "Application Support" / "Google" / "Chrome"


def find_profiles() -> list[ChromeProfile]:
    """Return every Chrome profile that has a cookie database."""
    root = _chrome_root()
    profiles = []
    for name in _CHROME_PROFILE_DIRS:
        db = root / name / "Cookies"
        if db.exists():
            profiles.append(ChromeProfile(name=name, cookie_db=db))
    return profiles


def _keychain_password() -> bytes:
    """Fetch Chrome's encryption passphrase from the macOS Keychain.

    This may surface a system prompt the first time; granting "Always Allow"
    makes subsequent runs silent.
    """
    failures = []
    for account in _KEYCHAIN_ACCOUNTS:
        result = subprocess.run(
            ["security", "find-generic-password", "-w", "-s", _KEYCHAIN_SERVICE, "-a", account],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().encode("utf-8")
        # Report every account. Only one of these ever exists on a given machine,
        # so the "not found" from the others would otherwise bury the real error.
        detail = result.stderr.strip() or f"exit status {result.returncode}"
        failures.append(f"account {account!r}: {detail}")

    raise CookieError(
        "Could not read Chrome's encryption key from the macOS Keychain.\n  "
        + "\n  ".join(failures)
        + "\n  If a prompt appeared, choose 'Always Allow' and re-run."
    )


def _derive_key(password: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha1", password, _SALT, _ITERATIONS, _KEY_LENGTH)


def _aes_cbc_decrypt(payload: bytes, key: bytes) -> bytes:
    """Decrypt AES-128-CBC using the openssl binary that ships with macOS.

    openssl only accepts the key as a command line argument, which is briefly
    visible to other users of this machine via `ps`. That is an acceptable
    trade on a personal machine, and it keeps the tool free of any compiled
    crypto dependency.
    """
    result = subprocess.run(
        [_OPENSSL, "enc", "-d", "-aes-128-cbc", "-nopad", "-K", key.hex(), "-iv", _IV.hex()],
        input=payload,
        capture_output=True,
    )
    if result.returncode != 0:
        raise CookieError(f"openssl could not decrypt the cookie: {result.stderr.decode().strip()}")
    return result.stdout


def _decrypt(encrypted: bytes, key: bytes) -> str:
    """Decrypt one Chrome cookie value."""
    if not encrypted:
        raise CookieError("Cookie value is empty.")

    version, payload = encrypted[:3], encrypted[3:]
    if version not in (b"v10", b"v11"):
        # Unencrypted values are stored verbatim on some older builds.
        return encrypted.decode("utf-8", errors="replace")

    if not payload or len(payload) % 16 != 0:
        raise CookieError("Encrypted cookie has an unexpected length; Chrome may have changed format.")

    plaintext = _aes_cbc_decrypt(payload, key)

    # Strip PKCS#7 padding.
    if plaintext:
        pad = plaintext[-1]
        if 1 <= pad <= 16:
            plaintext = plaintext[:-pad]

    # Chrome 127+ on macOS prefixes the plaintext with a 32-byte SHA-256 hash of
    # the cookie's domain. The real value follows it.
    if len(plaintext) > 32 and not _is_plausible_value(plaintext):
        plaintext = plaintext[32:]

    return plaintext.decode("utf-8", errors="replace")


def _is_plausible_value(raw: bytes) -> bool:
    """A cookie value should be printable ASCII; a domain hash prefix will not be."""
    sample = raw[:32]
    return all(32 <= byte < 127 for byte in sample)


def _query_cookies(db_path: Path, host: str, names: tuple[str, ...]) -> dict[str, tuple[bytes, int]]:
    """Read encrypted cookie values and expiries, copying the DB since Chrome locks it."""
    placeholders = ",".join("?" for _ in names)
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "Cookies"
        try:
            shutil.copy2(db_path, copy)
        except OSError as exc:
            raise CookieError(f"Could not read Chrome's cookie database: {exc}") from exc

        connection = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                f"SELECT name, encrypted_value, expires_utc FROM cookies "
                f"WHERE host_key = ? AND name IN ({placeholders})",
                (host, *names),
            ).fetchall()
        finally:
            connection.close()

    return {name: (value, expires) for name, value, expires in rows}


def _query_cookie(db_path: Path) -> bytes:
    """Read the encrypted SESSION value."""
    found = _query_cookies(db_path, COOKIE_HOST, (COOKIE_NAME,))
    if COOKIE_NAME not in found:
        raise CookieError(
            f"No {COOKIE_NAME} cookie found for {COOKIE_HOST} in this Chrome profile.\n"
            "  Log in to https://www.everydollar.com in Chrome, then re-run."
        )
    return found[COOKIE_NAME][0]


def chrome_expiry(expires_utc: int) -> dt.datetime | None:
    """Convert Chrome's expiry stamp; None for a session-only cookie."""
    if not expires_utc:
        return None
    return dt.datetime.fromtimestamp(
        expires_utc / 1_000_000 - _WINDOWS_EPOCH_OFFSET, tz=dt.timezone.utc
    )


def read_session_cookie(profile: str | None = None) -> str:
    """Return the current EveryDollar SESSION cookie value from Chrome.

    Searches every Chrome profile unless one is named explicitly.
    """
    profiles = find_profiles()
    if not profiles:
        raise CookieError(f"No Chrome cookie database found under {_chrome_root()}.")

    if profile is not None:
        profiles = [p for p in profiles if p.name == profile]
        if not profiles:
            raise CookieError(f"Chrome profile {profile!r} not found.")

    key = _derive_key(_keychain_password())

    errors = []
    for candidate in profiles:
        try:
            value = _decrypt(_query_cookie(candidate.cookie_db), key)
        except CookieError as exc:
            errors.append(f"{candidate.name}: {exc}")
            continue
        if value:
            return value

    raise CookieError("Could not read a SESSION cookie from any Chrome profile.\n  " + "\n  ".join(errors))


@dataclass(frozen=True)
class SsoCookies:
    """Auth0's persistent single-sign-on cookies, and when the SSO session lapses."""

    values: dict[str, str]
    expires: dt.datetime | None

    def __bool__(self) -> bool:
        return bool(self.values)


def read_sso_cookies(profile: str | None = None) -> SsoCookies:
    """Return Auth0's SSO cookies from Chrome.

    Unlike SESSION these are persistent, so they survive closing the browser and
    are what lets an unattended run re-authenticate itself.
    """
    profiles = find_profiles()
    if not profiles:
        raise CookieError(f"No Chrome cookie database found under {_chrome_root()}.")

    if profile is not None:
        profiles = [p for p in profiles if p.name == profile]
        if not profiles:
            raise CookieError(f"Chrome profile {profile!r} not found.")

    key = _derive_key(_keychain_password())

    for candidate in profiles:
        try:
            found = _query_cookies(candidate.cookie_db, SSO_HOST, SSO_COOKIE_NAMES)
        except CookieError:
            continue

        values = {}
        for name, (encrypted, _) in found.items():
            try:
                decrypted = _decrypt(encrypted, key)
            except CookieError:
                # One unreadable cookie should not discard the others; Auth0
                # accepts the compat variants interchangeably.
                continue
            if decrypted:
                values[name] = decrypted

        if values:
            stamp = found.get(SSO_SESSION_COOKIE)
            return SsoCookies(values=values, expires=chrome_expiry(stamp[1]) if stamp else None)

    raise CookieError(
        f"No Auth0 SSO cookies found for {SSO_HOST} in Chrome.\n"
        "  Log in to https://www.everydollar.com in Chrome once, then re-run."
    )
