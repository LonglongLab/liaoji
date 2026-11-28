"""Chunked AES-256-GCM stream encryption for optional v2 off-site backups (PSU-2.279)."""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import struct
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

BACKUP_ENC_MAGIC = b"CABK2\x00"
BACKUP_ENC_ALGORITHM = "aes-256-gcm-chunked-v1"
DEFAULT_ENC_CHUNK_SIZE = 1024 * 1024
MIN_ENC_CHUNK_SIZE = 1024
MAX_ENC_CHUNK_SIZE = 16 * 1024 * 1024
_NONCE_PREFIX_BYTES = 8
_HEADER_STRUCT = struct.Struct(">8sI")
_FRAME_STRUCT = struct.Struct(">IB")
_AAD_TAIL_STRUCT = struct.Struct(">IB")
_KEY_BYTES = 32
_MAX_KEY_FILE_BYTES = 4096


class BackupCryptoError(RuntimeError):
    """Backup encryption key or encrypted payload verification failed."""


def parse_backup_encryption_key(raw: bytes) -> bytes:
    """Decode a 256-bit backup key supplied as raw bytes, hex, or base64 text."""
    if len(raw) == _KEY_BYTES:
        return raw
    try:
        text = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        raise BackupCryptoError("backup encryption key format is invalid") from None
    if not text or any(ch.isspace() for ch in text):
        raise BackupCryptoError("backup encryption key format is invalid")
    if len(text) == 64:
        try:
            decoded = bytes.fromhex(text)
        except ValueError:
            decoded = b""
        if len(decoded) == _KEY_BYTES:
            return decoded
    padded = text + ("=" * ((4 - len(text) % 4) % 4))
    for candidate in (padded, padded.replace("-", "+").replace("_", "/")):
        try:
            decoded = base64.b64decode(candidate.encode("ascii"), validate=True)
        except ValueError:
            continue
        if len(decoded) == _KEY_BYTES:
            return decoded
    raise BackupCryptoError("backup encryption key must be 32 bytes")


def load_backup_encryption_key(path: Path) -> bytes:
    """Load an external 256-bit backup encryption key from an operator file."""
    if not path.is_absolute():
        raise BackupCryptoError("backup encryption key path must be absolute")
    try:
        raw = path.read_bytes()
    except OSError:
        raise BackupCryptoError("backup encryption key file is unreadable") from None
    if not raw or len(raw) > _MAX_KEY_FILE_BYTES:
        raise BackupCryptoError("backup encryption key file is invalid")
    return parse_backup_encryption_key(raw)


def _validate_key(key: bytes) -> None:
    if len(key) != _KEY_BYTES:
        raise BackupCryptoError("backup encryption key must be 32 bytes")


def key_fingerprint(key: bytes) -> str:
    """Return a non-secret 16-hex-char fingerprint for matching restore keys."""
    _validate_key(key)
    return hashlib.sha256(b"chat-archive-backup-key-fp-v1\0" + key).hexdigest()[:16]


def _frame_aad(backup_id: str, item_name: str, index: int, is_final: bool) -> bytes:
    return (
        b"cabk2|"
        + backup_id.encode("ascii")
        + b"|"
        + item_name.encode("ascii")
        + b"|"
        + _AAD_TAIL_STRUCT.pack(index, 1 if is_final else 0)
    )


def encrypt_file_stream(
    source: Path,
    destination: Path,
    *,
    key: bytes,
    backup_id: str,
    item_name: str,
    chunk_size: int = DEFAULT_ENC_CHUNK_SIZE,
) -> int:
    """Encrypt ``source`` to ``destination`` chunk-by-chunk with per-frame GCM tags."""
    _validate_key(key)
    if not MIN_ENC_CHUNK_SIZE <= chunk_size <= MAX_ENC_CHUNK_SIZE:
        raise BackupCryptoError("backup encryption chunk size is out of bounds")
    aesgcm = AESGCM(key)
    nonce_prefix = secrets.token_bytes(_NONCE_PREFIX_BYTES)
    try:
        with source.open("rb") as reader, destination.open("wb") as writer:
            writer.write(BACKUP_ENC_MAGIC)
            writer.write(_HEADER_STRUCT.pack(nonce_prefix, chunk_size))
            current = reader.read(chunk_size)
            index = 0
            while True:
                nxt = reader.read(chunk_size)
                is_final = len(nxt) == 0
                nonce = nonce_prefix + struct.pack(">I", index)
                aad = _frame_aad(backup_id, item_name, index, is_final)
                sealed = aesgcm.encrypt(nonce, current, aad)
                writer.write(_FRAME_STRUCT.pack(len(sealed), 1 if is_final else 0))
                writer.write(sealed)
                if is_final:
                    break
                current = nxt
                index += 1
        os.chmod(destination, 0o600)
        return destination.stat().st_size
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def decrypt_file_stream(
    source: Path,
    destination: Path,
    *,
    key: bytes,
    backup_id: str,
    item_name: str,
    max_output_bytes: int,
) -> int:
    """Authenticate and decrypt a v2 encrypted backup member into ``destination``."""
    _validate_key(key)
    if max_output_bytes < 0:
        raise BackupCryptoError("max output limit is invalid")
    aesgcm = AESGCM(key)
    written = 0
    saw_final = False
    try:
        with source.open("rb") as reader, destination.open("wb") as writer:
            magic = reader.read(len(BACKUP_ENC_MAGIC))
            if magic != BACKUP_ENC_MAGIC:
                raise BackupCryptoError("encrypted backup header is invalid")
            header = reader.read(_HEADER_STRUCT.size)
            if len(header) != _HEADER_STRUCT.size:
                raise BackupCryptoError("encrypted backup header is truncated")
            nonce_prefix, chunk_size = _HEADER_STRUCT.unpack(header)
            if not MIN_ENC_CHUNK_SIZE <= chunk_size <= MAX_ENC_CHUNK_SIZE:
                raise BackupCryptoError("encrypted backup chunk size is invalid")
            max_sealed_len = chunk_size + 16
            index = 0
            while True:
                frame_hdr = reader.read(_FRAME_STRUCT.size)
                if not frame_hdr:
                    break
                if saw_final or len(frame_hdr) != _FRAME_STRUCT.size:
                    raise BackupCryptoError("encrypted backup stream is malformed")
                sealed_len, flags = _FRAME_STRUCT.unpack(frame_hdr)
                if flags not in (0, 1) or sealed_len < 16 or sealed_len > max_sealed_len:
                    raise BackupCryptoError("encrypted backup frame header is invalid")
                sealed = reader.read(sealed_len)
                if len(sealed) != sealed_len:
                    raise BackupCryptoError("encrypted backup frame is truncated")
                is_final = flags == 1
                nonce = nonce_prefix + struct.pack(">I", index)
                aad = _frame_aad(backup_id, item_name, index, is_final)
                try:
                    plaintext = aesgcm.decrypt(nonce, sealed, aad)
                except InvalidTag:
                    raise BackupCryptoError(
                        "encrypted backup authentication failed"
                    ) from None
                if written + len(plaintext) > max_output_bytes:
                    raise BackupCryptoError(
                        "decrypted backup exceeds declared archive bounds"
                    )
                writer.write(plaintext)
                written += len(plaintext)
                saw_final = is_final
                index += 1
        if not saw_final:
            raise BackupCryptoError("encrypted backup stream ended before final frame")
        return written
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


__all__ = [
    "BACKUP_ENC_ALGORITHM",
    "BACKUP_ENC_MAGIC",
    "DEFAULT_ENC_CHUNK_SIZE",
    "MAX_ENC_CHUNK_SIZE",
    "MIN_ENC_CHUNK_SIZE",
    "BackupCryptoError",
    "decrypt_file_stream",
    "encrypt_file_stream",
    "key_fingerprint",
    "load_backup_encryption_key",
    "parse_backup_encryption_key",
]
