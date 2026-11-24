"""The data-directory master key file, shared by everything that seals a secret.

There is exactly one shape for "a 32-byte AES-GCM key the server mints for
itself and nobody ever backs up": a versioned file created ``O_EXCL`` at mode
``0600`` beside the databases. Object storage credentials (PSU-2.16-A) needed
one; the browser login's TOTP secret (PSU-2.29) needs another. They must not
share a key — a leak of one should not read the other — but they must share
this code, because the tricky parts (``O_BINARY`` on Windows, staging plus
``os.replace``, the mode that Windows quietly ignores) are exactly the parts
that rot when copied.

The caller supplies its own error type so a failure still reports itself in the
vocabulary of the subsystem that asked, rather than leaking a shared one.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import cast

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from chat_archive.atomic_io import atomic_write_bytes
from chat_archive.canonical_json import canonical_json_bytes

MASTER_KEY_BYTES = 32
_AEAD_NONCE_BYTES = 12


class MasterKey:
    """One key file. Users never create, edit or back this up."""

    def __init__(self, path: Path, *, version: bytes, error: type[Exception]) -> None:
        if not version.endswith(b"\0"):
            raise ValueError("master key version prefix must be NUL-terminated")
        self._path = path
        self._version = version
        self._error = error

    def _decode(self, raw: bytes) -> bytes:
        if len(raw) != len(self._version) + MASTER_KEY_BYTES or not raw.startswith(
            self._version
        ):
            raise self._error("master key file is unusable")
        return raw[len(self._version) :]

    def load_existing(self) -> bytes:
        """Read the key, never creating one — the decrypt side."""
        try:
            raw = self._path.read_bytes()
        except OSError:
            raise self._error("master key file is unavailable") from None
        return self._decode(raw)

    def load_or_create(self) -> bytes:
        """Read the key, minting one on first use — the encrypt side."""
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return self._create()
        except OSError:
            raise self._error("master key file is unavailable") from None
        return self._decode(raw)

    def _create(self) -> bytes:
        key = secrets.token_bytes(MASTER_KEY_BYTES)
        try:
            atomic_write_bytes(
                self._path,
                self._version + key,
                mkdir=True,
                exclusive_open=True,
                mode=0o600,
            )
        except OSError:
            raise self._error("master key file is unavailable") from None
        return key


def seal_secret_pair(
    master_key: MasterKey,
    *,
    secret_id: str,
    secret_key: str,
    context: bytes,
) -> tuple[bytes, bytes]:
    """Encrypt a ``(secret_id, secret_key)`` pair under ``master_key`` with AES-256-GCM."""
    key = master_key.load_or_create()
    nonce = secrets.token_bytes(_AEAD_NONCE_BYTES)
    payload = canonical_json_bytes({"secret_id": secret_id, "secret_key": secret_key})
    return nonce, AESGCM(key).encrypt(nonce, payload, context)


def open_secret_pair(
    master_key: MasterKey,
    *,
    key_version: int,
    expected_version: int,
    nonce: bytes,
    ciphertext: bytes,
    context: bytes,
    error: type[Exception],
    version_error_message: str,
    decrypt_error_message: str,
    payload_error_message: str,
) -> tuple[str, str]:
    """Decrypt and validate a ``(secret_id, secret_key)`` pair sealed by ``seal_secret_pair``."""
    if key_version != expected_version:
        raise error(version_error_message)
    key = master_key.load_existing()
    try:
        payload = AESGCM(key).decrypt(nonce, ciphertext, context)
    except InvalidTag:
        raise error(decrypt_error_message) from None
    try:
        value: object = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise error(payload_error_message) from None
    if not isinstance(value, dict):
        raise error(payload_error_message)
    decoded = cast(dict[object, object], value)
    secret_id = decoded.get("secret_id")
    secret_key = decoded.get("secret_key")
    if not isinstance(secret_id, str) or not isinstance(secret_key, str):
        raise error(payload_error_message)
    return secret_id, secret_key


__all__ = [
    "MASTER_KEY_BYTES",
    "MasterKey",
    "open_secret_pair",
    "seal_secret_pair",
]
