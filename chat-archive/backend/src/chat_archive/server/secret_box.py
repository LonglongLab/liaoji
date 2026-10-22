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

import os
import secrets
from pathlib import Path

_O_BINARY = getattr(os, "O_BINARY", 0)
MASTER_KEY_BYTES = 32


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
        staging = self._path.with_name(f".{self._path.name}.{secrets.token_hex(8)}.tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                staging,
                # O_BINARY matters on Windows, where os.open defaults to text
                # mode and would rewrite any 0x0A byte in the random key as
                # CRLF — corrupting roughly one key in eight on write.
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY,
                0o600,
            )
            try:
                os.write(descriptor, self._version + key)
            finally:
                os.close(descriptor)
            os.replace(staging, self._path)
        except OSError:
            raise self._error("master key file is unavailable") from None
        # Windows ignores the POSIX mode; the file inherits the data directory
        # ACL there, which is the same boundary the SQLite databases rely on.
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass
        return key


__all__ = ["MASTER_KEY_BYTES", "MasterKey"]
