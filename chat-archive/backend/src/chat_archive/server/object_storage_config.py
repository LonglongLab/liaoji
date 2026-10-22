"""Page-managed Tencent COS configuration and hot-swappable object storage.

Credentials live in SQLite as AES-256-GCM ciphertext under a master key the
server mints on first use into a ``0600`` file beside the database. Nothing in
this module returns, logs or reprs a secret: the summary type has no field for
one, so an echo is not merely avoided but structurally impossible.

``storage_generation`` is the answer to "which bucket is this attachment in".
Rotating only the credentials keeps the generation; changing bucket or region
advances it and pushes the previous configuration into an encrypted history
table, so an attachment written under an older generation can be named
precisely instead of failing as a bare error.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, cast
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from chat_archive.ports.objectstore import (
    CapabilityCache,
    CompletedPart,
    ObjectAttributes,
    ObjectHead,
    ObjectHeadReader,
    ObjectReconciliation,
    ObjectStore,
    ObjectStoreCapabilityError,
    ObjectStoreError,
    PresignedRequest,
    StoreCapabilities,
    UploadPlan,
)
from chat_archive.server.secret_box import MasterKey

PROVIDER = "tencent_cos"
VERIFICATION_SCOPE = "verification"

_MASTER_KEY_NAME = "object-storage-master.key"
_MASTER_KEY_VERSION = b"v1\0"
_KEY_VERSION = 1
_AEAD_CONTEXT = b"chat-archive-cos-v1"
_BUCKET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{1,126}[a-z0-9]$")
_REGION_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")
# Any S3-compatible access key, not just Tencent's.
#
# This was ``^AKID[A-Za-z0-9_-]{6,60}$`` — Tencent SecretIds are ``AKID`` plus
# 32 characters, and Tencent was the only target when it was written. PSU-2.31
# makes the bundled MinIO the *default* object store, and its access key is
# ``archive_<24 hex>`` (`deploy/self-hosted/initialize-secrets.py`), which that
# pattern rejects — the settings page could not configure the object store the
# deployment ships with.
#
# What the pattern is actually for is unchanged and still enforced: no
# whitespace, no newline, no NUL, no unbounded length. It was never an
# authenticity check — a well-formed key that is wrong fails the round-trip
# verification, which is the check that matters.
_SECRET_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_MAX_SECRET_KEY_LENGTH = 512
_MAX_ENDPOINT_LENGTH = 512


class ObjectStoreNotConfiguredError(ObjectStoreError):
    """Attachment operations are unavailable until COS is configured."""


class ObjectStorageConfigurationError(RuntimeError):
    """A managed COS configuration is invalid or cannot be decrypted."""


class ObjectStorageGenerationNotFoundError(RuntimeError):
    """No stored generation carries the requested storage generation."""


@dataclass(frozen=True)
class ObjectStorageCredentials:
    """Decrypted credentials. Never serialized, never logged."""

    endpoint: str
    bucket: str
    region: str
    secret_id: str
    secret_key: str
    storage_generation: int


@dataclass(frozen=True)
class ObjectStorageGeneration:
    """One historical bucket/region pairing, without any credential material."""

    storage_generation: int
    endpoint: str
    bucket: str
    region: str
    access_key_hint: str
    updated_at: int
    active: bool


@dataclass(frozen=True)
class ObjectStorageConfigSummary:
    """Everything the UI may see. There is deliberately no secret field."""

    configured: bool
    provider: str = PROVIDER
    endpoint: str | None = None
    bucket: str | None = None
    region: str | None = None
    access_key_hint: str | None = None
    storage_generation: int = 1
    status: str = "not_configured"
    updated_at: int | None = None
    generations: tuple[ObjectStorageGeneration, ...] = ()


@dataclass(frozen=True)
class ObjectStorageVerification:
    verified: bool
    status: str
    capabilities: Mapping[str, object]


def access_key_hint(secret_id: str) -> str:
    """The first four characters, then ``****``, then the last four — never more.

    Was a literal ``AKID****last4``, which only read correctly for Tencent. A
    MinIO key rendered that way claimed a prefix it does not have, and an
    operator comparing the page against their credentials file would see a
    mismatch that was not real.

    Four leading characters is enough to tell two keys apart without being
    enough to help guess one: the key's entropy is in its tail, and the tail
    is already the four characters shown.
    """
    return f"{secret_id[:4]}****{secret_id[-4:]}"


def validate_configuration(
    *,
    endpoint: str,
    bucket: str,
    region: str,
    secret_id: str,
    secret_key: str,
) -> None:
    """Reject a candidate configuration without echoing any of it back."""
    if (
        not endpoint.startswith("https://")
        or len(endpoint) > _MAX_ENDPOINT_LENGTH
        or any(character in endpoint for character in "\r\n\x00 ")
    ):
        raise ObjectStorageConfigurationError("object storage endpoint is invalid")
    if _BUCKET_PATTERN.fullmatch(bucket) is None or ".." in bucket:
        raise ObjectStorageConfigurationError("object storage bucket is invalid")
    if _REGION_PATTERN.fullmatch(region) is None:
        raise ObjectStorageConfigurationError("object storage region is invalid")
    if _SECRET_ID_PATTERN.fullmatch(secret_id) is None:
        raise ObjectStorageConfigurationError("object storage secret id is invalid")
    if not secret_key or len(secret_key) > _MAX_SECRET_KEY_LENGTH:
        raise ObjectStorageConfigurationError("object storage secret key is invalid")


class SqliteObjectStorageConfigStore:
    """Encrypted current-plus-history configuration state for the self-hosted runtime."""

    def __init__(
        self,
        database_path: Path,
        data_dir: Path | None = None,
        *,
        now: Callable[[], int] | None = None,
    ) -> None:
        self._database_path = database_path
        self._data_dir = database_path.parent if data_dir is None else data_dir
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._master_key = MasterKey(
            self._data_dir / _MASTER_KEY_NAME,
            version=_MASTER_KEY_VERSION,
            error=ObjectStorageConfigurationError,
        )
        self._now: Callable[[], int] = (
            (lambda: max(1, int(time.time()))) if now is None else now
        )
        self._lock = threading.RLock()
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @property
    def database_path(self) -> Path:
        return self._database_path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._lock, closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS object_storage_config (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    provider TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    bucket TEXT NOT NULL,
                    region TEXT NOT NULL,
                    access_key_hint TEXT NOT NULL,
                    storage_generation INTEGER NOT NULL CHECK (storage_generation >= 1),
                    key_version INTEGER NOT NULL,
                    nonce BLOB NOT NULL,
                    ciphertext BLOB NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS object_storage_config_history (
                    storage_generation INTEGER PRIMARY KEY CHECK (storage_generation >= 1),
                    provider TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    bucket TEXT NOT NULL,
                    region TEXT NOT NULL,
                    access_key_hint TEXT NOT NULL,
                    key_version INTEGER NOT NULL,
                    nonce BLOB NOT NULL,
                    ciphertext BLOB NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                """
            )
            connection.commit()

    def _encrypt(self, secret_id: str, secret_key: str) -> tuple[bytes, bytes]:
        key = self._master_key.load_or_create()
        nonce = secrets.token_bytes(12)
        payload = json.dumps(
            {"secret_id": secret_id, "secret_key": secret_key},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return nonce, AESGCM(key).encrypt(nonce, payload, _AEAD_CONTEXT)

    def _decrypt(self, row: sqlite3.Row) -> tuple[str, str]:
        if int(row["key_version"]) != _KEY_VERSION:
            raise ObjectStorageConfigurationError("master key version is unsupported")
        key = self._master_key.load_existing()
        try:
            payload = AESGCM(key).decrypt(
                bytes(row["nonce"]),
                bytes(row["ciphertext"]),
                _AEAD_CONTEXT,
            )
        except InvalidTag:
            raise ObjectStorageConfigurationError(
                "master key cannot decrypt the stored configuration"
            ) from None
        try:
            value: object = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ObjectStorageConfigurationError(
                "stored configuration payload is unreadable"
            ) from None
        if not isinstance(value, dict):
            raise ObjectStorageConfigurationError("stored configuration payload is unreadable")
        decoded = cast(dict[str, object], value)
        secret_id = decoded.get("secret_id")
        secret_key = decoded.get("secret_key")
        if not isinstance(secret_id, str) or not isinstance(secret_key, str):
            raise ObjectStorageConfigurationError("stored configuration payload is unreadable")
        return secret_id, secret_key

    @staticmethod
    def _generation(row: sqlite3.Row, *, active: bool) -> ObjectStorageGeneration:
        return ObjectStorageGeneration(
            storage_generation=int(row["storage_generation"]),
            endpoint=str(row["endpoint"]),
            bucket=str(row["bucket"]),
            region=str(row["region"]),
            access_key_hint=str(row["access_key_hint"]),
            updated_at=int(row["updated_at"]),
            active=active,
        )

    def _current_row(self, connection: sqlite3.Connection) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM object_storage_config WHERE id = 1"
        ).fetchone()

    def _history_rows(self, connection: sqlite3.Connection) -> list[sqlite3.Row]:
        return connection.execute(
            """
            SELECT storage_generation, endpoint, bucket, region,
                   access_key_hint, updated_at
            FROM object_storage_config_history
            ORDER BY storage_generation
            """
        ).fetchall()

    def _summary(self, connection: sqlite3.Connection) -> ObjectStorageConfigSummary:
        current = self._current_row(connection)
        if current is None:
            return ObjectStorageConfigSummary(configured=False)
        try:
            self._decrypt(current)
        except ObjectStorageConfigurationError:
            return ObjectStorageConfigSummary(configured=False)
        active_generation = int(current["storage_generation"])
        generations = tuple(
            self._generation(row, active=int(row["storage_generation"]) == active_generation)
            for row in self._history_rows(connection)
        )
        return ObjectStorageConfigSummary(
            configured=True,
            provider=str(current["provider"]),
            endpoint=str(current["endpoint"]),
            bucket=str(current["bucket"]),
            region=str(current["region"]),
            access_key_hint=str(current["access_key_hint"]),
            storage_generation=active_generation,
            status="healthy",
            updated_at=int(current["updated_at"]),
            generations=generations,
        )

    def summary(self) -> ObjectStorageConfigSummary:
        with self._lock, closing(self._connect()) as connection:
            return self._summary(connection)

    def current_generation(self) -> int:
        with self._lock, closing(self._connect()) as connection:
            current = self._current_row(connection)
        return 1 if current is None else int(current["storage_generation"])

    def load_credentials(self) -> ObjectStorageCredentials | None:
        with self._lock, closing(self._connect()) as connection:
            row = self._current_row(connection)
        if row is None:
            return None
        secret_id, secret_key = self._decrypt(row)
        return ObjectStorageCredentials(
            endpoint=str(row["endpoint"]),
            bucket=str(row["bucket"]),
            region=str(row["region"]),
            secret_id=secret_id,
            secret_key=secret_key,
            storage_generation=int(row["storage_generation"]),
        )

    def load_generation_credentials(self, storage_generation: int) -> ObjectStorageCredentials:
        """Decrypt a historical generation so the caller can verify before switching."""
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM object_storage_config_history WHERE storage_generation = ?",
                (storage_generation,),
            ).fetchone()
        if row is None:
            raise ObjectStorageGenerationNotFoundError("storage generation is unknown")
        secret_id, secret_key = self._decrypt(row)
        return ObjectStorageCredentials(
            endpoint=str(row["endpoint"]),
            bucket=str(row["bucket"]),
            region=str(row["region"]),
            secret_id=secret_id,
            secret_key=secret_key,
            storage_generation=int(row["storage_generation"]),
        )

    def next_generation_for(self, *, endpoint: str, bucket: str, region: str) -> int:
        """Advance only when the attachments would land somewhere new."""
        with self._lock, closing(self._connect()) as connection:
            current = self._current_row(connection)
            if current is None:
                return 1
            active = int(current["storage_generation"])
            if (
                str(current["endpoint"]) == endpoint
                and str(current["bucket"]) == bucket
                and str(current["region"]) == region
            ):
                return active
            highest = connection.execute(
                "SELECT MAX(storage_generation) AS peak FROM object_storage_config_history"
            ).fetchone()
        peak = active if highest is None or highest["peak"] is None else int(highest["peak"])
        return max(active, peak) + 1

    def save(
        self,
        *,
        endpoint: str,
        bucket: str,
        region: str,
        secret_id: str,
        secret_key: str,
        provider: str = PROVIDER,
    ) -> ObjectStorageConfigSummary:
        validate_configuration(
            endpoint=endpoint,
            bucket=bucket,
            region=region,
            secret_id=secret_id,
            secret_key=secret_key,
        )
        if provider not in (PROVIDER, "s3_compatible", "minio"):
            raise ObjectStorageConfigurationError("object storage provider is invalid")
        nonce, ciphertext = self._encrypt(secret_id, secret_key)
        hint = access_key_hint(secret_id)
        timestamp = self._now()
        common: tuple[object, ...] = (
            provider,
            endpoint,
            bucket,
            region,
            hint,
        )
        sealed: tuple[object, ...] = (_KEY_VERSION, nonce, ciphertext, timestamp)
        # The generation is chosen under the same re-entrant lock that guards
        # the write. Reading it first and writing after would let two
        # concurrent saves pick the same number and have the second silently
        # overwrite the first generation's history row.
        with self._lock, closing(self._connect()) as connection:
            generation = self.next_generation_for(
                endpoint=endpoint,
                bucket=bucket,
                region=region,
            )
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO object_storage_config(
                        id, provider, endpoint, bucket, region, access_key_hint,
                        storage_generation, key_version, nonce, ciphertext, updated_at
                    ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        provider = excluded.provider,
                        endpoint = excluded.endpoint,
                        bucket = excluded.bucket,
                        region = excluded.region,
                        access_key_hint = excluded.access_key_hint,
                        storage_generation = excluded.storage_generation,
                        key_version = excluded.key_version,
                        nonce = excluded.nonce,
                        ciphertext = excluded.ciphertext,
                        updated_at = excluded.updated_at
                    """,
                    (*common, generation, *sealed),
                )
                connection.execute(
                    """
                    INSERT INTO object_storage_config_history(
                        storage_generation, provider, endpoint, bucket, region,
                        access_key_hint, key_version, nonce, ciphertext, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(storage_generation) DO UPDATE SET
                        provider = excluded.provider,
                        endpoint = excluded.endpoint,
                        bucket = excluded.bucket,
                        region = excluded.region,
                        access_key_hint = excluded.access_key_hint,
                        key_version = excluded.key_version,
                        nonce = excluded.nonce,
                        ciphertext = excluded.ciphertext,
                        updated_at = excluded.updated_at
                    """,
                    (generation, *common, *sealed),
                )
                summary = self._summary(connection)
                connection.commit()
                return summary
            except BaseException:
                connection.rollback()
                raise

    def activate_generation(self, storage_generation: int) -> ObjectStorageConfigSummary:
        """Point the current configuration back at an already-stored generation."""
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM object_storage_config_history WHERE storage_generation = ?",
                    (storage_generation,),
                ).fetchone()
                if row is None:
                    raise ObjectStorageGenerationNotFoundError("storage generation is unknown")
                connection.execute(
                    """
                    INSERT INTO object_storage_config(
                        id, provider, endpoint, bucket, region, access_key_hint,
                        storage_generation, key_version, nonce, ciphertext, updated_at
                    ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        provider = excluded.provider,
                        endpoint = excluded.endpoint,
                        bucket = excluded.bucket,
                        region = excluded.region,
                        access_key_hint = excluded.access_key_hint,
                        storage_generation = excluded.storage_generation,
                        key_version = excluded.key_version,
                        nonce = excluded.nonce,
                        ciphertext = excluded.ciphertext,
                        updated_at = excluded.updated_at
                    """,
                    (
                        str(row["provider"]),
                        str(row["endpoint"]),
                        str(row["bucket"]),
                        str(row["region"]),
                        str(row["access_key_hint"]),
                        int(row["storage_generation"]),
                        int(row["key_version"]),
                        bytes(row["nonce"]),
                        bytes(row["ciphertext"]),
                        self._now(),
                    ),
                )
                summary = self._summary(connection)
                connection.commit()
                return summary
            except BaseException:
                connection.rollback()
                raise


class ConfigurableObjectStore:
    """Thread-safe ObjectStore proxy whose backing store is swapped atomically.

    Every delegating method resolves the backing store under the lock and then
    calls it outside of it, so a save that lands mid-download cannot tear a
    request in half: the request finishes against the store it started with.
    Unconfigured is a first-class state — it raises a stable
    ``ObjectStoreNotConfiguredError`` rather than pretending to be degraded.
    """

    def __init__(
        self,
        initial: ObjectStore | None = None,
        *,
        storage_generation: int = 1,
    ) -> None:
        self._lock = threading.RLock()
        self._store = initial
        self._storage_generation = storage_generation

    def current_store(self) -> ObjectStore | None:
        with self._lock:
            return self._store

    def is_configured(self) -> bool:
        with self._lock:
            return self._store is not None

    def storage_generation(self) -> int:
        with self._lock:
            return self._storage_generation

    def snapshot(self) -> tuple[ObjectStore, int]:
        """Capture the concrete store and its generation under one lock."""
        with self._lock:
            if self._store is None:
                raise ObjectStoreNotConfiguredError("object storage is not configured")
            return self._store, self._storage_generation

    def replace(self, candidate: ObjectStore, *, storage_generation: int) -> None:
        with self._lock:
            self._store = candidate
            self._storage_generation = storage_generation

    def replace_after_verification(
        self,
        factory: Callable[[], ObjectStore],
        *,
        storage_generation: int = 1,
    ) -> ObjectStore:
        """Build and verify the candidate first; keep the old store on failure."""
        candidate = factory()
        self.replace(candidate, storage_generation=storage_generation)
        return candidate

    def _require(self) -> ObjectStore:
        with self._lock:
            if self._store is None:
                raise ObjectStoreNotConfiguredError("object storage is not configured")
            return self._store

    def probe_capabilities(self) -> StoreCapabilities:
        return self._require().probe_capabilities()

    def cached_capabilities(self) -> StoreCapabilities:
        """Forwarded, so replacing the bucket also replaces the cached answer.

        The cache lives on the concrete store, and a configuration change
        builds a new one, so there is nothing here to invalidate. A backend
        that does not cache is asked afresh — correct, just not cheap.
        """
        concrete = self._require()
        if isinstance(concrete, CapabilityCache):
            return concrete.cached_capabilities()
        return concrete.probe_capabilities()

    def put_object(
        self,
        archive_id: str,
        object_key: str,
        data: bytes,
        *,
        sha256: str,
    ) -> ObjectAttributes:
        return self._require().put_object(archive_id, object_key, data, sha256=sha256)

    def copy_object(
        self,
        source_scope: str,
        source_key: str,
        target_scope: str,
        target_key: str,
        *,
        sha256: str,
        size: int,
    ) -> ObjectAttributes:
        return self._require().copy_object(
            source_scope, source_key, target_scope, target_key, sha256=sha256, size=size
        )

    def plan_upload(
        self,
        archive_id: str,
        *,
        total_size: int,
        sha256: str,
        part_size: int,
        object_key: str | None = None,
    ) -> UploadPlan:
        return self._require().plan_upload(
            archive_id,
            total_size=total_size,
            sha256=sha256,
            part_size=part_size,
            object_key=object_key,
        )

    def list_uploaded_parts(self, plan: UploadPlan) -> tuple[CompletedPart, ...]:
        return self._require().list_uploaded_parts(plan)

    def upload_part(self, plan: UploadPlan, part_number: int, data: bytes) -> CompletedPart:
        return self._require().upload_part(plan, part_number, data)

    def complete_upload(
        self,
        plan: UploadPlan,
        parts: Sequence[CompletedPart],
    ) -> ObjectAttributes:
        return self._require().complete_upload(plan, parts)

    def abort_upload(self, plan: UploadPlan) -> None:
        self._require().abort_upload(plan)

    def download_object(
        self,
        archive_id: str,
        object_key: str,
        sink: BinaryIO,
    ) -> ObjectAttributes:
        return self._require().download_object(archive_id, object_key, sink)

    def get_object(self, archive_id: str, object_key: str) -> bytes:
        return self._require().get_object(archive_id, object_key)

    def presign_get(
        self,
        archive_id: str,
        object_key: str,
        *,
        expires_in_seconds: int,
    ) -> PresignedRequest:
        return self._require().presign_get(
            archive_id,
            object_key,
            expires_in_seconds=expires_in_seconds,
        )

    def presign_put(
        self,
        archive_id: str,
        object_key: str,
        *,
        size: int,
        sha256: str,
        expires_in_seconds: int,
        md5: str | None = None,
    ) -> PresignedRequest:
        return self._require().presign_put(
            archive_id,
            object_key,
            size=size,
            sha256=sha256,
            expires_in_seconds=expires_in_seconds,
            md5=md5,
        )

    def head_objects(
        self,
        archive_id: str,
        object_keys: Sequence[str],
    ) -> Mapping[str, ObjectHead]:
        concrete = self._require()
        if not isinstance(concrete, ObjectHeadReader):
            raise ObjectStoreCapabilityError("this backend cannot answer a HEAD")
        return concrete.head_objects(archive_id, object_keys)

    def describe_objects(
        self,
        archive_id: str,
        object_keys: Sequence[str],
    ) -> Mapping[str, ObjectAttributes]:
        return self._require().describe_objects(archive_id, object_keys)

    def list_object_checksums(self, archive_id: str) -> Mapping[str, str]:
        return self._require().list_object_checksums(archive_id)

    def list_object_sizes(self, archive_id: str) -> Mapping[str, int]:
        return self._require().list_object_sizes(archive_id)

    def reconcile(
        self,
        archive_id: str,
        expected: Mapping[str, str],
    ) -> ObjectReconciliation:
        return self._require().reconcile(archive_id, expected)

    def delete_object(self, archive_id: str, object_key: str) -> None:
        self._require().delete_object(archive_id, object_key)


def verify_object_store(
    store: ObjectStore,
    archive_scope: str = VERIFICATION_SCOPE,
) -> ObjectStorageVerification:
    """Round-trip a synthetic object: put, read back, hash, presign, delete.

    ``ListBucket`` succeeding proves nothing about whether attachments can
    actually be written and read, so the check writes real bytes under a
    reserved prefix with a random key and removes them again. The caller sees
    only a structured verdict — never the key, the URL, or the provider's own
    error text.
    """
    capabilities = store.probe_capabilities()
    payload = secrets.token_bytes(32)
    object_key = f"a/{archive_scope}/{secrets.token_hex(16)}"
    digest = hashlib.sha256(payload).hexdigest()
    try:
        attributes = store.put_object(archive_scope, object_key, payload, sha256=digest)
        if attributes.sha256 != digest or attributes.size != len(payload):
            raise ObjectStoreError("verification object did not match what was written")
        downloaded = store.get_object(archive_scope, object_key)
        if hashlib.sha256(downloaded).hexdigest() != digest:
            raise ObjectStoreError("verification object did not read back intact")
        if capabilities.presigned_urls:
            # Only exercised against a backend that claims it: a store that
            # declares the gap up front is reported degraded below, while one
            # that claims presigning and then cannot do it fails outright.
            store.presign_get(archive_scope, object_key, expires_in_seconds=300)
    finally:
        try:
            store.delete_object(archive_scope, object_key)
        except (ObjectStoreError, ValueError):
            # The probe object is disposable; a failed cleanup must not mask
            # the verification verdict the caller is waiting on.
            pass
    missing = capabilities.missing()
    # 问不到的那些单独一列，不并进 missing（PSU-2.57）：并进去等于把「这把密钥
    # 没有读桶生命周期配置的权限」说成「这个桶没有清理规则」，而后者是故障、
    # 前者不是。也**不咽掉**——问不到就说问不到。
    return ObjectStorageVerification(
        verified=True,
        status="degraded" if missing else "healthy",
        capabilities={"missing": list(missing), "unknown": list(capabilities.unknown())},
    )


def presigned_origin(store: ObjectStore) -> str | None:
    """The scheme+host this store's signed URLs point at, or None if it cannot sign.

    Read off a signature computed and thrown away — signing is local, nothing
    is sent. Not derived from the configured endpoint, because the two differ
    whenever the store addresses by bucket host: on Tencent COS the endpoint is
    ``cos.<region>.myqcloud.com`` and every URL is on
    ``<bucket>.cos.<region>.myqcloud.com`` (PSU-2.129). A page policy built
    from the endpoint would admit a host no attachment is ever served from.
    """
    try:
        probe = store.presign_get(
            VERIFICATION_SCOPE,
            f"a/{VERIFICATION_SCOPE}/{'0' * 32}",
            expires_in_seconds=60,
        )
    except (ObjectStoreError, ValueError):
        return None
    split = urlsplit(probe.url)
    if not split.scheme or not split.netloc:
        return None
    return f"{split.scheme}://{split.netloc}"


__all__ = [
    "PROVIDER",
    "ConfigurableObjectStore",
    "ObjectStorageConfigSummary",
    "ObjectStorageConfigurationError",
    "ObjectStorageCredentials",
    "ObjectStorageGeneration",
    "ObjectStorageGenerationNotFoundError",
    "ObjectStorageVerification",
    "ObjectStoreNotConfiguredError",
    "SqliteObjectStorageConfigStore",
    "access_key_hint",
    "presigned_origin",
    "validate_configuration",
    "verify_object_store",
]
