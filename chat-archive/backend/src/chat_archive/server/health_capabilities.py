"""Nonblocking upload-mode snapshots for the public liveness endpoint."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from chat_archive.ports.objectstore import ObjectStore
from chat_archive.server.blob_routes import blob_upload_modes, store_capabilities
from chat_archive.server.object_storage_config import (
    ConfigurableObjectStore,
    ObjectStoreNotConfiguredError,
)

HEALTH_CAPABILITY_TTL_SECONDS = 300.0
HEALTH_CAPABILITY_RETRY_SECONDS = 30.0
_CONSERVATIVE_MODES = ("proxy", "multipart")
_logger = logging.getLogger(__name__)


class HealthUploadModes:
    """Refresh outside request threads; never hold the snapshot lock over I/O.

    Health polling triggers an expired refresh and still receives the previous
    complete result. One daemon performs the existing provider probe, then exits.
    Configurable stores bind snapshots to both identity and generation so an old
    bucket's result cannot be advertised after a configuration replacement.
    """

    def __init__(
        self,
        object_store: ObjectStore,
        *,
        record_claim: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._object_store = object_store
        self._record_claim = record_claim
        self._clock = clock
        self._lock = threading.Lock()
        self._source: ObjectStore | None = None
        self._generation = -1
        self._modes = _CONSERVATIVE_MODES
        self._object_status = "unknown"
        self._expires_at = 0.0
        self._worker: threading.Thread | None = None
        self._closed = False

    def _current_source(self) -> tuple[ObjectStore | None, int]:
        if isinstance(self._object_store, ConfigurableObjectStore):
            try:
                return self._object_store.snapshot()
            except ObjectStoreNotConfiguredError:
                return None, 0
        return self._object_store, 0

    def _accept_source(self, source: ObjectStore | None, generation: int) -> None:
        # Caller owns _lock; this does not call the provider.
        if source is not self._source or generation != self._generation:
            self._source = source
            self._generation = generation
            self._modes = _CONSERVATIVE_MODES
            self._object_status = "unknown"
            self._expires_at = 0.0

    def read(self) -> tuple[str, ...]:
        source, generation = self._current_source()
        worker: threading.Thread | None = None
        with self._lock:
            if self._closed:
                return _CONSERVATIVE_MODES
            self._accept_source(source, generation)
            if source is not None and self._worker is None and self._clock() >= self._expires_at:
                worker = threading.Thread(
                    target=self._refresh, name="chat-archive-health-capabilities", daemon=True
                )
                self._worker = worker
            modes = self._modes
        if worker is not None:
            try:
                worker.start()
            except RuntimeError as error:
                with self._lock:
                    self._worker = None
                    self._expires_at = self._clock() + HEALTH_CAPABILITY_RETRY_SECONDS
                _logger.warning("health capability worker could not start (%s)", type(error).__name__)
        return modes

    def read_status(self) -> str:
        """Return only a fresh probe verdict; never wait for provider I/O."""
        self.read()
        with self._lock:
            if self._closed:
                return "unknown"
            if self._source is None:
                return "not_configured"
            return self._object_status if self._clock() < self._expires_at else "unknown"

    def _refresh(self) -> None:
        while True:
            with self._lock:
                if self._closed or self._source is None:
                    self._worker = None
                    return
                source, generation = self._source, self._generation
            started = time.monotonic()
            modes: tuple[str, ...] | None = None
            error_type: str | None = None
            object_status = "unknown"
            try:
                capabilities = store_capabilities(source)
                modes = blob_upload_modes(source, record_claim=self._record_claim, capabilities=capabilities)
                object_status = "healthy" if not capabilities.missing() else "degraded"
            except Exception as error:  # noqa: BLE001 - a failed probe must remain retryable
                # Provider exception messages can contain addresses or credentials.
                error_type = type(error).__name__
            current_source, current_generation = self._current_source()
            discarded = False
            with self._lock:
                if self._closed:
                    self._worker = None
                    return
                self._accept_source(current_source, current_generation)
                if source is not current_source or generation != current_generation:
                    # Discard the old result; the same single worker follows the
                    # latest configuration without starting parallel probes.
                    discarded = True
                elif modes is not None:
                    self._modes = modes
                    self._object_status = object_status
                    self._expires_at = self._clock() + HEALTH_CAPABILITY_TTL_SECONDS
                elif error_type is not None:
                    self._object_status = "unknown"
                    self._expires_at = self._clock() + HEALTH_CAPABILITY_RETRY_SECONDS
                if not discarded:
                    self._worker = None
            if error_type is not None:
                _logger.warning("health capability refresh failed (%s)", error_type)
            if discarded:
                continue
            if modes is not None:
                _logger.info(
                    "health capability snapshot refreshed: mode_count=%d elapsed_seconds=%.3f",
                    len(modes),
                    time.monotonic() - started,
                )
            return

    def close(self) -> None:
        """Do not start more probes or publish an in-flight result after shutdown."""
        with self._lock:
            self._closed = True
