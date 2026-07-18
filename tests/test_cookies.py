"""Tests for Chrome's cookie decryption scheme.

Values are encrypted here with the same parameters Chrome uses so the decryption
path can be exercised without touching the real cookie store.
"""

import hashlib
import subprocess

import pytest

from everydollar_cli.cookies import _IV, _OPENSSL, CookieError, _decrypt, _derive_key

KEY = _derive_key(b"test-password")


def encrypt(plaintext: bytes, prefix: bytes = b"v10") -> bytes:
    """Encrypt with the same parameters Chrome uses, via the system openssl."""
    padding = 16 - (len(plaintext) % 16)
    padded = plaintext + bytes([padding]) * padding
    result = subprocess.run(
        [_OPENSSL, "enc", "-aes-128-cbc", "-nopad", "-K", KEY.hex(), "-iv", _IV.hex()],
        input=padded,
        capture_output=True,
        check=True,
    )
    return prefix + result.stdout


def test_decrypts_a_v10_cookie():
    assert _decrypt(encrypt(b"session-value-123"), KEY) == "session-value-123"


def test_decrypts_a_v11_cookie():
    assert _decrypt(encrypt(b"session-value-123", prefix=b"v11"), KEY) == "session-value-123"


def test_strips_the_domain_hash_prefix_used_by_newer_chrome():
    # Chrome 127+ prepends a 32-byte SHA-256 domain hash to the plaintext.
    domain_hash = hashlib.sha256(b"www.everydollar.com").digest()
    value = b"c2Vzc2lvbi12YWx1ZS1wbGFjZWhvbGRlcg"

    assert _decrypt(encrypt(domain_hash + value), KEY) == value.decode()


def test_returns_unencrypted_values_verbatim():
    assert _decrypt(b"plain-value", KEY) == "plain-value"


def test_rejects_an_empty_value():
    with pytest.raises(CookieError, match="empty"):
        _decrypt(b"", KEY)


def test_rejects_a_truncated_payload():
    with pytest.raises(CookieError, match="unexpected length"):
        _decrypt(b"v10" + b"short", KEY)


def test_key_derivation_is_deterministic():
    assert _derive_key(b"test-password") == KEY
    assert _derive_key(b"other-password") != KEY
