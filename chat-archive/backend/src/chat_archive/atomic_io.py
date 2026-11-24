"""Shared atomic file-write and staging primitives."""

from __future__ import annotations

import os
import secrets
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

_O_BINARY = getattr(os, "O_BINARY", 0)


def _staging_path(path: Path, *, prefix: str | None = None, suffix: str = ".tmp") -> Path:
    stem = f".{path.name}." if prefix is None else prefix
    return path.with_name(f"{stem}{secrets.token_hex(8)}{suffix}")


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    written = 0
    while written < len(view):
        written += os.write(descriptor, view[written:])


def _fsync_descriptor(descriptor: int) -> None:
    os.fsync(descriptor)


def atomic_write_bytes(
    path: Path,
    data: bytes,
    *,
    mkdir: bool = False,
    exclusive_open: bool = False,
    mode: int | None = None,
    fsync: bool = False,
    prefix: str | None = None,
    suffix: str = ".tmp",
) -> None:
    """Write ``data`` to a sibling staging file and atomically replace ``path``."""
    if mkdir:
        path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _staging_path(path, prefix=prefix, suffix=suffix)
    try:
        if exclusive_open or mode is not None:
            file_mode = 0o600 if mode is None else mode
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY,
                file_mode,
            )
            try:
                _write_all(descriptor, data)
                if fsync:
                    _fsync_descriptor(descriptor)
            finally:
                os.close(descriptor)
        else:
            with temporary.open("wb") as handle:
                handle.write(data)
                if fsync:
                    handle.flush()
                    os.fsync(handle.fileno())
        if mode is not None:
            try:
                os.chmod(temporary, mode)
            except OSError:
                pass
        os.replace(temporary, path)
        if mode is not None:
            try:
                os.chmod(path, mode)
            except OSError:
                pass
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def atomic_write_text(
    path: Path,
    text: str,
    *,
    encoding: str = "utf-8",
    mkdir: bool = False,
    exclusive_open: bool = False,
    mode: int | None = None,
    fsync: bool = False,
    prefix: str | None = None,
    suffix: str = ".tmp",
) -> None:
    """Atomically write LF-preserving text to ``path``."""
    if not exclusive_open and mode is None and not fsync:
        if mkdir:
            path.parent.mkdir(parents=True, exist_ok=True)
        temporary = _staging_path(path, prefix=prefix, suffix=suffix)
        try:
            temporary.write_text(text, encoding=encoding, newline="\n")
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        return
    atomic_write_bytes(
        path,
        text.encode(encoding),
        mkdir=mkdir,
        exclusive_open=exclusive_open,
        mode=mode,
        fsync=fsync,
        prefix=prefix,
        suffix=suffix,
    )


def atomic_write_exclusive(
    path: Path,
    data: bytes,
    *,
    mode: int | None = None,
    prefix: str | None = None,
) -> None:
    """Write ``data`` via a synced staging file and link to ``path`` without overwriting."""
    descriptor: int | None = None
    staging: Path | None = None
    linked = False
    try:
        descriptor, raw_staging = tempfile.mkstemp(
            prefix=f".{path.name}-" if prefix is None else prefix,
            dir=path.parent,
        )
        staging = Path(raw_staging)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            staging.chmod(mode)
        os.link(staging, path)
        linked = True
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if staging is not None:
            try:
                staging.unlink()
            except OSError:
                if not linked:
                    pass


@contextmanager
def atomic_staging_file(
    path: Path,
    *,
    mkdir: bool = False,
    mode: int | None = None,
    prefix: str | None = None,
    suffix: str = ".tmp",
) -> Generator[Path]:
    """Yield a sibling staging path and atomically replace ``path`` on clean exit."""
    if mkdir:
        path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _staging_path(path, prefix=prefix, suffix=suffix)
    try:
        yield temporary
        if mode is not None:
            try:
                os.chmod(temporary, mode)
            except OSError:
                pass
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


__all__ = [
    "atomic_staging_file",
    "atomic_write_bytes",
    "atomic_write_exclusive",
    "atomic_write_text",
]
