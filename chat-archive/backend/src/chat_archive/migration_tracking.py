"""Optional local migration observer; remote runtimes keep the no-op default."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path

Observer = Callable[[Path, dict[str, str | int]], AbstractContextManager[None]]
_observer: Observer | None = None


def set_migration_observer(observer: Observer) -> None:
    global _observer
    _observer = observer


def migration_guard(path: Path, targets: dict[str, str | int]) -> AbstractContextManager[None]:
    return nullcontext() if _observer is None else _observer(path, targets)
