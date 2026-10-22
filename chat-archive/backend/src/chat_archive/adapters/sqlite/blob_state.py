"""SQLite persistence for self-hosted remote blob manifests and upload state."""

from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Callable, Sequence
from contextlib import closing
from pathlib import Path

from chat_archive.ports.blob_state import (
    BlobManifestExpectation,
    BlobManifestReconciliation,
    BlobManifestRecord,
    BlobPartRecord,
    BlobStateConflictError,
    BlobStateValidationError,
    BlobStorageHealth,
    BlobUploadCreate,
    BlobUploadRecord,
    object_scope_for_archive,
    require_content_digest,
    require_manifest_descriptor,
    require_remote_id,
    require_upload_create,
)
from chat_archive.ports.objectstore import new_object_key, require_archive_key, require_sha256
from chat_archive.adapters.sqlite.guards import (
    BoundedWriterLock,
    StatementGuardConnection,
    install_statement_guard,
)
from chat_archive.server.security import constant_time_equal


def _require_storage_generation(value: int) -> None:
    if isinstance(value, bool) or value < 1:
        raise BlobStateValidationError("storage generation must be a positive integer")


_MD5 = re.compile(r"^[0-9a-f]{32}$")
_ATTESTATIONS = frozenset({"sha256", "md5"})


def _require_md5(value: str) -> None:
    if _MD5.fullmatch(value) is None:
        raise BlobStateValidationError("declared md5 must be 32 lowercase hex characters")


class SqliteBlobStateStore:
    def __init__(
        self,
        database_path: Path,
        *,
        now: Callable[[], int] | None = None,
        busy_timeout_ms: int = 5_000,
        read_only: bool = False,
    ) -> None:
        """``read_only`` 是给对账那类旁观者的（PSU-2.58）。

        平常这个构造函数是**写**操作：它建目录、`PRAGMA journal_mode = WAL`、
        `CREATE TABLE IF NOT EXISTS`，还会跑 `storage_generation` 那次
        `ALTER TABLE`。对账进程要能在 API 跑着的时候执行，而写 SQLite 的资格
        由 `SingleWriterLease` 独占——所以它必须走 `mode=ro`：不建库、不建表、
        不迁移，库不存在就直接失败，而不是凭空造一个空的出来。
        """
        if busy_timeout_ms <= 0:
            raise ValueError("blob state busy timeout must be positive")
        self._database_path = database_path
        self._now = now if now is not None else (lambda: int(time.time()))
        self._busy_timeout_ms = busy_timeout_ms
        self._read_only = read_only
        # 同一把有界写锁（PSU-2.98）。这里的 9 个写入口与 metadata 那 6 个是
        # 同一个裸锁模式的第二次抄写，所以修的是模式，不是一个点。
        self._writer_lock = BoundedWriterLock(name="blob-state")
        if read_only:
            return
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        if self._read_only:
            connection = sqlite3.connect(
                f"file:{self._database_path.as_posix()}?mode=ro",
                timeout=self._busy_timeout_ms / 1_000,
                uri=True,
                factory=StatementGuardConnection,
            )
        else:
            connection = sqlite3.connect(
                self._database_path,
                timeout=self._busy_timeout_ms / 1_000,
                factory=StatementGuardConnection,
            )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        install_statement_guard(connection, name="blob-state")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS server_blob_manifests (
                    archive_id TEXT NOT NULL,
                    blob_id TEXT NOT NULL,
                    size INTEGER NOT NULL CHECK (size >= 0),
                    sha256 TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN ('pending_upload', 'uploading_parts', 'remote_ready')
                    ),
                    object_scope TEXT NOT NULL,
                    object_key TEXT NOT NULL UNIQUE,
                    object_version TEXT,
                    verified_at INTEGER,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (archive_id, blob_id)
                );
                CREATE INDEX IF NOT EXISTS ix_server_blob_manifests_archive
                    ON server_blob_manifests(archive_id, blob_id);
                CREATE INDEX IF NOT EXISTS ix_server_blob_manifests_content
                    ON server_blob_manifests(archive_id, sha256, state);
                CREATE TABLE IF NOT EXISTS server_blob_uploads (
                    archive_id TEXT NOT NULL,
                    blob_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    client_upload_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    provider_upload_id TEXT NOT NULL,
                    object_scope TEXT NOT NULL,
                    object_key TEXT NOT NULL,
                    total_size INTEGER NOT NULL CHECK (total_size > 0),
                    part_size INTEGER NOT NULL CHECK (part_size > 0),
                    part_count INTEGER NOT NULL CHECK (part_count > 0),
                    sha256 TEXT NOT NULL,
                    plan_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN (
                            'planned', 'uploading_parts', 'completing', 'remote_ready',
                            'aborted', 'integrity_failed'
                        )
                    ),
                    object_version TEXT,
                    verified_at INTEGER,
                    finalizer_id TEXT,
                    finalizer_expires_at INTEGER,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (archive_id, blob_id, session_id),
                    UNIQUE (archive_id, device_id, client_upload_id),
                    FOREIGN KEY (archive_id, blob_id)
                        REFERENCES server_blob_manifests(archive_id, blob_id)
                );
                CREATE TABLE IF NOT EXISTS server_blob_upload_parts (
                    archive_id TEXT NOT NULL,
                    blob_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    part_number INTEGER NOT NULL CHECK (part_number > 0),
                    expected_size INTEGER NOT NULL CHECK (expected_size > 0),
                    expected_sha256 TEXT NOT NULL,
                    etag TEXT,
                    completed_at INTEGER,
                    capability_hash TEXT,
                    capability_expires_at INTEGER,
                    PRIMARY KEY (archive_id, blob_id, session_id, part_number),
                    FOREIGN KEY (archive_id, blob_id, session_id)
                        REFERENCES server_blob_uploads(archive_id, blob_id, session_id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS ix_server_blob_uploads_device_client
                    ON server_blob_uploads(archive_id, device_id, client_upload_id);
                """
            )
            self._migrate_storage_generation(connection)
            self._migrate_md5_attestation(connection)

    @staticmethod
    def _migrate_md5_attestation(connection: sqlite3.Connection) -> None:
        """Add the two nullable PSU-2.131 columns; existing rows stay NULL.

        NULL is the truthful value for every row already here: none of them
        declared an MD5, and all of them were vouched for by SHA-256.
        """
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(server_blob_manifests)")
        }
        for column in ("md5", "attestation"):
            if column not in columns:
                connection.execute(
                    f"ALTER TABLE server_blob_manifests ADD COLUMN {column} TEXT"  # noqa: S608 - fixed names
                )
        connection.commit()

    @staticmethod
    def _migrate_storage_generation(connection: sqlite3.Connection) -> None:
        """Add ``storage_generation`` and backfill existing rows to 1.

        Anything already stored was written before buckets could change, so it
        belongs to the first generation by definition. SQLite applies the
        constant DEFAULT to existing rows during ADD COLUMN, so the backfill is
        the migration — there is no second pass that could be interrupted.
        """
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(server_blob_manifests)")
        }
        if "storage_generation" in columns:
            return
        connection.execute(
            """
            ALTER TABLE server_blob_manifests
            ADD COLUMN storage_generation INTEGER NOT NULL DEFAULT 1
            """
        )
        connection.commit()

    @staticmethod
    def _manifest(row: sqlite3.Row) -> BlobManifestRecord:
        raw_version = row["object_version"]
        raw_verified_at = row["verified_at"]
        return BlobManifestRecord(
            archive_id=str(row["archive_id"]),
            blob_id=str(row["blob_id"]),
            size=int(row["size"]),
            sha256=str(row["sha256"]),
            state=str(row["state"]),
            object_scope=str(row["object_scope"]),
            object_key=str(row["object_key"]),
            object_version=None if raw_version is None else str(raw_version),
            verified_at=None if raw_verified_at is None else int(raw_verified_at),
            storage_generation=int(row["storage_generation"]),
            md5=None if row["md5"] is None else str(row["md5"]),
            attestation=None if row["attestation"] is None else str(row["attestation"]),
        )

    @staticmethod
    def _select_manifest(
        connection: sqlite3.Connection,
        archive_id: str,
        blob_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT archive_id, blob_id, size, sha256, state, object_scope,
                   object_key, object_version, verified_at, storage_generation,
                   md5, attestation
            FROM server_blob_manifests
            WHERE archive_id = ? AND blob_id = ?
            """,
            (archive_id, blob_id),
        ).fetchone()

    @staticmethod
    def _select_upload(
        connection: sqlite3.Connection,
        archive_id: str,
        blob_id: str,
        session_id: str,
        device_id: str | None = None,
    ) -> sqlite3.Row | None:
        device_clause = "" if device_id is None else " AND device_id = ?"
        parameters: tuple[object, ...] = (
            (archive_id, blob_id, session_id)
            if device_id is None
            else (archive_id, blob_id, session_id, device_id)
        )
        return connection.execute(
            f"""
            SELECT archive_id, blob_id, device_id, client_upload_id, session_id,
                   provider_upload_id, object_scope, object_key, total_size,
                   part_size, sha256, plan_sha256, state, object_version, verified_at,
                   finalizer_id, finalizer_expires_at
            FROM server_blob_uploads
            WHERE archive_id = ? AND blob_id = ? AND session_id = ?{device_clause}
            """,
            parameters,
        ).fetchone()

    @staticmethod
    def _select_upload_by_client(
        connection: sqlite3.Connection,
        archive_id: str,
        device_id: str,
        client_upload_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT archive_id, blob_id, device_id, client_upload_id, session_id,
                   provider_upload_id, object_scope, object_key, total_size,
                   part_size, sha256, plan_sha256, state, object_version, verified_at,
                   finalizer_id, finalizer_expires_at
            FROM server_blob_uploads
            WHERE archive_id = ? AND device_id = ? AND client_upload_id = ?
            """,
            (archive_id, device_id, client_upload_id),
        ).fetchone()

    @staticmethod
    def _part_rows(
        connection: sqlite3.Connection,
        archive_id: str,
        blob_id: str,
        session_id: str,
    ) -> list[sqlite3.Row]:
        return connection.execute(
            """
            SELECT part_number, expected_size, expected_sha256, etag,
                   completed_at, capability_hash, capability_expires_at
            FROM server_blob_upload_parts
            WHERE archive_id = ? AND blob_id = ? AND session_id = ?
            ORDER BY part_number
            """,
            (archive_id, blob_id, session_id),
        ).fetchall()

    @staticmethod
    def _part(row: sqlite3.Row) -> BlobPartRecord:
        raw_etag = row["etag"]
        raw_completed_at = row["completed_at"]
        raw_capability_hash = row["capability_hash"]
        raw_capability_expires_at = row["capability_expires_at"]
        return BlobPartRecord(
            part_number=int(row["part_number"]),
            size=int(row["expected_size"]),
            sha256=str(row["expected_sha256"]),
            etag=None if raw_etag is None else str(raw_etag),
            completed_at=None if raw_completed_at is None else int(raw_completed_at),
            capability_hash=(
                None if raw_capability_hash is None else str(raw_capability_hash)
            ),
            capability_expires_at=(
                None
                if raw_capability_expires_at is None
                else int(raw_capability_expires_at)
            ),
        )

    def _upload(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> BlobUploadRecord:
        raw_version = row["object_version"]
        raw_verified_at = row["verified_at"]
        return BlobUploadRecord(
            archive_id=str(row["archive_id"]),
            blob_id=str(row["blob_id"]),
            device_id=str(row["device_id"]),
            client_upload_id=str(row["client_upload_id"]),
            session_id=str(row["session_id"]),
            provider_upload_id=str(row["provider_upload_id"]),
            object_scope=str(row["object_scope"]),
            object_key=str(row["object_key"]),
            total_size=int(row["total_size"]),
            part_size=int(row["part_size"]),
            sha256=str(row["sha256"]),
            plan_sha256=str(row["plan_sha256"]),
            state=str(row["state"]),
            object_version=None if raw_version is None else str(raw_version),
            verified_at=None if raw_verified_at is None else int(raw_verified_at),
            parts=tuple(
                self._part(part)
                for part in self._part_rows(
                    connection,
                    str(row["archive_id"]),
                    str(row["blob_id"]),
                    str(row["session_id"]),
                )
            ),
        )

    def storage_health(self, archive_id: str) -> BlobStorageHealth:
        """这个归档的 blob 账，从**真的有人写**的那张表上聚合（PSU-2.57）。

        `pending_blob_count` 数的是尚未 `remote_ready` 的清单行，也就是「说好要
        传、还没落地」的那些。它必须来自这里：健康页从前问 metadata store 的
        同名方法，而那张 `blob_manifests` 自 blob 状态搬家之后没有任何写者。
        """
        require_remote_id(archive_id, field_name="archive_id")
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT COALESCE(SUM(size), 0) AS used_bytes,
                       COUNT(*) AS blob_count,
                       COALESCE(SUM(CASE WHEN state = 'remote_ready' THEN 0 ELSE 1 END), 0)
                           AS pending_blob_count
                FROM server_blob_manifests WHERE archive_id = ?
                """,
                (archive_id,),
            ).fetchone()
        assert row is not None
        return BlobStorageHealth(
            used_bytes=int(row["used_bytes"]),
            blob_count=int(row["blob_count"]),
            pending_blob_count=int(row["pending_blob_count"]),
        )

    def list_manifests(self, archive_id: str) -> tuple[BlobManifestRecord, ...]:
        require_remote_id(archive_id, field_name="archive_id")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT archive_id, blob_id, size, sha256, state, object_scope,
                       object_key, object_version, verified_at, storage_generation,
                       md5, attestation
                FROM server_blob_manifests
                WHERE archive_id = ?
                ORDER BY blob_id
                """,
                (archive_id,),
            ).fetchall()
        return tuple(self._manifest(row) for row in rows)

    def list_archive_ids(self) -> tuple[str, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT DISTINCT archive_id FROM server_blob_manifests ORDER BY archive_id"
            ).fetchall()
        return tuple(str(row["archive_id"]) for row in rows)

    def put_manifest(
        self,
        archive_id: str,
        blob_id: str,
        *,
        size: int,
        sha256: str,
        storage_generation: int = 1,
        md5: str | None = None,
    ) -> BlobManifestRecord:
        require_remote_id(archive_id, field_name="archive_id")
        require_manifest_descriptor(
            blob_id,
            size=size,
            sha256=sha256,
            state="pending_upload",
        )
        _require_storage_generation(storage_generation)
        if md5 is not None:
            _require_md5(md5)
        with self._writer_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._select_manifest(connection, archive_id, blob_id)
                if row is None:
                    object_scope = object_scope_for_archive(archive_id)
                    object_key = new_object_key(object_scope)
                    connection.execute(
                        """
                        INSERT INTO server_blob_manifests(
                            archive_id, blob_id, size, sha256, state,
                            object_scope, object_key, object_version,
                            verified_at, updated_at, storage_generation, md5
                        ) VALUES (?, ?, ?, ?, 'pending_upload', ?, ?, NULL, NULL, ?, ?, ?)
                        """,
                        (
                            archive_id,
                            blob_id,
                            size,
                            sha256,
                            object_scope,
                            object_key,
                            self._now(),
                            storage_generation,
                            md5,
                        ),
                    )
                else:
                    prior = self._manifest(row)
                    same_descriptor = prior.size == size and prior.sha256 == sha256
                    if (
                        same_descriptor
                        and md5 is not None
                        and prior.md5 is not None
                        and prior.md5 != md5
                    ):
                        # Same bytes by SHA-256, different bytes by MD5: one of
                        # the two declarations is wrong, and signing either would
                        # let the store refuse the other's upload for a reason
                        # nobody could see (PSU-2.131).
                        raise BlobStateConflictError("declared md5 disagrees with the manifest")
                    if (
                        same_descriptor
                        and md5 is not None
                        and prior.md5 is None
                        and prior.state != "remote_ready"
                    ):
                        # An older client registered this row without an MD5; a
                        # newer one declaring it is the only way that row ever
                        # gets MD5 attestation. A ready row is never rewritten.
                        connection.execute(
                            """
                            UPDATE server_blob_manifests SET md5 = ?, updated_at = ?
                            WHERE archive_id = ? AND blob_id = ? AND md5 IS NULL
                              AND state <> 'remote_ready'
                            """,
                            (md5, self._now(), archive_id, blob_id),
                        )
                    if prior.state == "remote_ready":
                        if not same_descriptor:
                            raise BlobStateConflictError(
                                "verified blob manifest is immutable"
                            )
                    elif prior.state == "uploading_parts":
                        if not same_descriptor:
                            raise BlobStateConflictError(
                                "active blob upload descriptor is immutable"
                            )
                    elif not same_descriptor:
                        object_key = new_object_key(prior.object_scope)
                        connection.execute(
                            """
                            UPDATE server_blob_manifests
                            SET size = ?, sha256 = ?, object_key = ?,
                                object_version = NULL, verified_at = NULL,
                                updated_at = ?, storage_generation = ?, md5 = ?
                            WHERE archive_id = ? AND blob_id = ?
                              AND state = 'pending_upload'
                            """,
                            (
                                size,
                                sha256,
                                object_key,
                                self._now(),
                                storage_generation,
                                md5,
                                archive_id,
                                blob_id,
                            ),
                        )
                persisted = self._select_manifest(connection, archive_id, blob_id)
                if persisted is None:
                    raise BlobStateConflictError("blob manifest was not persisted")
                connection.commit()
                return self._manifest(persisted)
            except BaseException:
                connection.rollback()
                raise

    def get_manifest(self, archive_id: str, blob_id: str) -> BlobManifestRecord | None:
        require_remote_id(archive_id, field_name="archive_id")
        require_remote_id(blob_id, field_name="blob_id")
        with closing(self._connect()) as connection:
            row = self._select_manifest(connection, archive_id, blob_id)
        return None if row is None else self._manifest(row)

    def get_manifests(self, archive_id: str, blob_ids: Sequence[str]) -> dict[str, BlobManifestRecord]:
        """Read bounded batches in one connection, always scoped to one archive."""
        require_remote_id(archive_id, field_name="archive_id")
        wanted = tuple(dict.fromkeys(blob_ids))
        for blob_id in wanted:
            require_remote_id(blob_id, field_name="blob_id")
        if not wanted:
            return {}
        result: dict[str, BlobManifestRecord] = {}
        with closing(self._connect()) as connection:
            for start in range(0, len(wanted), 500):
                batch = wanted[start:start + 500]
                placeholders = ",".join("?" for _ in batch)
                rows = connection.execute(
                    f"SELECT * FROM server_blob_manifests WHERE archive_id=? AND blob_id IN ({placeholders})",
                    (archive_id, *batch),
                ).fetchall()
                result.update((str(row["blob_id"]), self._manifest(row)) for row in rows)
        return result

    def forward_media_manifests(self, archive_id: str, blob_ids: Sequence[str]):
        """Verified descriptors for forwarded-media publication, scoped to one archive."""
        from chat_archive.ports.metadata import BlobManifest

        require_remote_id(archive_id, field_name="archive_id")
        result: dict[str, BlobManifest] = {}
        wanted = tuple(dict.fromkeys(blob_ids))
        with closing(self._connect()) as connection:
            for start in range(0, len(wanted), 500):
                chunk = wanted[start:start + 500]
                for blob_id in chunk:
                    require_remote_id(blob_id, field_name="blob_id")
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"SELECT blob_id, size, sha256, state FROM server_blob_manifests WHERE archive_id=? AND state='remote_ready' AND blob_id IN ({placeholders})",  # noqa: S608
                    (archive_id, *chunk),
                ).fetchall()
                for row in rows:
                    blob_id = str(row["blob_id"])
                    result[blob_id] = BlobManifest(blob_id, int(row["size"]), str(row["sha256"]), str(row["state"]))
        return result

    def ready_manifest_sizes(
        self,
        archive_id: str,
        blob_ids: Sequence[str],
    ) -> dict[str, int]:
        """Size of each named manifest that is ``remote_ready`` (PSU-2.143).

        What merging two single-ref records compares when both reach an
        original: the larger one wins. Missing or unverified ids are absent.
        """
        require_remote_id(archive_id, field_name="archive_id")
        wanted = tuple(dict.fromkeys(blob_ids))
        found: dict[str, int] = {}
        if not wanted:
            return found
        with closing(self._connect()) as connection:
            for start in range(0, len(wanted), 500):
                chunk = wanted[start : start + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"""
                    SELECT blob_id, size FROM server_blob_manifests
                    WHERE archive_id = ? AND state = 'remote_ready'
                      AND blob_id IN ({placeholders})
                    """,  # noqa: S608 - placeholders are generated, values are bound
                    (archive_id, *chunk),
                ).fetchall()
                for row in rows:
                    found[str(row["blob_id"])] = int(row["size"])
        return found

    def manifest_states(
        self,
        archive_id: str,
        blob_ids: Sequence[str],
    ) -> dict[str, str]:
        """State of each named manifest, missing ids simply absent.

        Batched because its one caller asks about a whole sync batch at once
        (PSU-2.82): merging ``attachment_refs`` needs to know which of the two
        candidate ref lists can actually open something, and a query per blob
        id would put a few hundred round trips inside a commit.
        """
        require_remote_id(archive_id, field_name="archive_id")
        wanted = tuple(dict.fromkeys(blob_ids))
        if not wanted:
            return {}
        found: dict[str, str] = {}
        with closing(self._connect()) as connection:
            for start in range(0, len(wanted), 500):
                chunk = wanted[start : start + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"""
                    SELECT blob_id, state FROM server_blob_manifests
                    WHERE archive_id = ? AND blob_id IN ({placeholders})
                    """,  # noqa: S608 - placeholders are generated, values are bound
                    (archive_id, *chunk),
                ).fetchall()
                for row in rows:
                    found[str(row["blob_id"])] = str(row["state"])
        return found

    def find_ready_manifest_by_content(
        self,
        archive_id: str,
        *,
        sha256: str,
        size: int,
        storage_generation: int,
    ) -> BlobManifestRecord | None:
        """The verified manifest already holding these bytes, if there is one.

        The one lookup that makes cross-machine attachment dedupe possible
        (PSU-2.82): two machines derive different ``blob_id`` for the same
        picture — the id is salted by the account, which is salted by the
        capture machine — but ``batch-presign`` is handed the content hash in
        the same request, so the second machine can be told "that is already
        here, point at it" instead of sending the bytes again.

        **Locked inside one ``archive_id``**, and that is a security boundary
        rather than a convenience: an archive belongs to one person, so a
        content-addressed lookup within it answers nothing its owner does not
        already know. Across archives the non-correlation argument in
        :func:`~chat_archive.domain.identity.remote_blob_id` still holds — the
        ids stay salted and this query cannot see past the scope.

        ``size`` is compared as well as the digest. A pair that agrees on the
        hash but not the length means one of the two ends computed something
        wrong, and the safe answer there is to upload rather than to claim.

        ``storage_generation`` is not optional either: after a bucket change
        the previous generation's rows are still in this table, and claiming
        one would hand out a reference to an object that is not in the current
        bucket at all.

        Ordered so that several verified manifests for the same bytes — the
        single-machine duplicates that predate this unit — resolve to the same
        one on every call, whoever asks.
        """
        require_remote_id(archive_id, field_name="archive_id")
        require_content_digest(sha256)
        if isinstance(size, bool) or size < 0:
            raise BlobStateValidationError("blob manifest size must be non-negative")
        _require_storage_generation(storage_generation)
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT archive_id, blob_id, size, sha256, state, object_scope,
                       object_key, object_version, verified_at, storage_generation,
                       md5, attestation
                FROM server_blob_manifests
                WHERE archive_id = ? AND sha256 = ? AND state = 'remote_ready'
                  AND size = ? AND storage_generation = ?
                ORDER BY verified_at, blob_id
                LIMIT 1
                """,
                (archive_id, sha256, size, storage_generation),
            ).fetchone()
        return None if row is None else self._manifest(row)

    def mark_manifest_ready(
        self,
        archive_id: str,
        blob_id: str,
        *,
        size: int,
        sha256: str,
        object_key: str,
        object_version: str,
        verified_at: int,
        storage_generation: int = 1,
        attestation: str | None = None,
    ) -> BlobManifestRecord:
        require_remote_id(archive_id, field_name="archive_id")
        require_manifest_descriptor(
            blob_id,
            size=size,
            sha256=sha256,
            state="remote_ready",
        )
        _require_storage_generation(storage_generation)
        if attestation is not None and attestation not in _ATTESTATIONS:
            raise BlobStateValidationError("attestation must be sha256 or md5")
        if not object_version or isinstance(verified_at, bool) or verified_at <= 0:
            raise BlobStateValidationError("verified object metadata is invalid")
        with self._writer_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._select_manifest(connection, archive_id, blob_id)
                if row is None:
                    raise BlobStateConflictError("blob manifest does not exist")
                prior = self._manifest(row)
                require_archive_key(prior.object_scope, object_key)
                if prior.size != size or prior.sha256 != sha256 or prior.object_key != object_key:
                    raise BlobStateConflictError("blob manifest changed during upload")
                if prior.state != "remote_ready":
                    changed = connection.execute(
                        """
                        UPDATE server_blob_manifests
                        SET state = 'remote_ready', object_version = ?,
                            verified_at = ?, updated_at = ?,
                            storage_generation = ?, attestation = ?
                        WHERE archive_id = ? AND blob_id = ?
                          AND size = ? AND sha256 = ?
                          AND object_key = ?
                          AND state IN ('pending_upload', 'uploading_parts')
                        """,
                        (
                            object_version,
                            verified_at,
                            self._now(),
                            storage_generation,
                            attestation,
                            archive_id,
                            blob_id,
                            size,
                            sha256,
                            object_key,
                        ),
                    )
                    if changed.rowcount != 1:
                        raise BlobStateConflictError("blob manifest state changed")
                persisted = self._select_manifest(connection, archive_id, blob_id)
                if persisted is None:
                    raise BlobStateConflictError("blob manifest was not persisted")
                connection.commit()
                return self._manifest(persisted)
            except BaseException:
                connection.rollback()
                raise

    def reconcile_manifests(
        self,
        archive_id: str,
        expected: tuple[BlobManifestExpectation, ...],
    ) -> BlobManifestReconciliation:
        require_remote_id(archive_id, field_name="archive_id")
        expected_by_id: dict[str, BlobManifestExpectation] = {}
        for manifest in expected:
            require_manifest_descriptor(
                manifest.blob_id,
                size=manifest.size,
                sha256=manifest.sha256,
                state=manifest.state,
            )
            if manifest.blob_id in expected_by_id:
                raise BlobStateValidationError(
                    "expected manifest contains duplicate blob IDs"
                )
            expected_by_id[manifest.blob_id] = manifest
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT blob_id, sha256
                FROM server_blob_manifests
                WHERE archive_id = ?
                """,
                (archive_id,),
            ).fetchall()
        stored = {str(row["blob_id"]): str(row["sha256"]) for row in rows}
        return BlobManifestReconciliation(
            missing_in_store=tuple(sorted(set(expected_by_id) - set(stored))),
            checksum_mismatches=tuple(
                sorted(
                    blob_id
                    for blob_id in set(expected_by_id) & set(stored)
                    if expected_by_id[blob_id].sha256 != stored[blob_id]
                )
            ),
            orphaned_in_store=tuple(sorted(set(stored) - set(expected_by_id))),
        )

    def find_upload_by_client(
        self,
        archive_id: str,
        device_id: str,
        client_upload_id: str,
    ) -> BlobUploadRecord | None:
        require_remote_id(archive_id, field_name="archive_id")
        require_remote_id(device_id, field_name="device_id")
        require_remote_id(client_upload_id, field_name="client_upload_id")
        with closing(self._connect()) as connection:
            row = self._select_upload_by_client(
                connection,
                archive_id,
                device_id,
                client_upload_id,
            )
            return None if row is None else self._upload(connection, row)

    def create_upload(self, upload: BlobUploadCreate) -> BlobUploadRecord:
        require_upload_create(upload)
        require_archive_key(upload.object_scope, upload.object_key)
        with self._writer_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                manifest_row = self._select_manifest(
                    connection,
                    upload.archive_id,
                    upload.blob_id,
                )
                if manifest_row is None:
                    raise BlobStateConflictError("blob manifest does not exist")
                manifest = self._manifest(manifest_row)
                if (
                    manifest.state != "pending_upload"
                    or manifest.size != upload.total_size
                    or manifest.sha256 != upload.sha256
                    or manifest.object_scope != upload.object_scope
                    or manifest.object_key != upload.object_key
                ):
                    raise BlobStateConflictError(
                        "blob manifest is not available for this upload"
                    )
                if (
                    self._select_upload_by_client(
                        connection,
                        upload.archive_id,
                        upload.device_id,
                        upload.client_upload_id,
                    )
                    is not None
                ):
                    raise BlobStateConflictError("client upload ID already exists")
                now = self._now()
                try:
                    connection.execute(
                        """
                        INSERT INTO server_blob_uploads(
                            archive_id, blob_id, device_id, client_upload_id,
                            session_id, provider_upload_id, object_scope, object_key,
                            total_size, part_size, part_count, sha256, plan_sha256,
                            state, object_version, verified_at, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                  'planned', NULL, NULL, ?, ?)
                        """,
                        (
                            upload.archive_id,
                            upload.blob_id,
                            upload.device_id,
                            upload.client_upload_id,
                            upload.session_id,
                            upload.provider_upload_id,
                            upload.object_scope,
                            upload.object_key,
                            upload.total_size,
                            upload.part_size,
                            len(upload.parts),
                            upload.sha256,
                            upload.plan_sha256,
                            now,
                            now,
                        ),
                    )
                    connection.executemany(
                        """
                        INSERT INTO server_blob_upload_parts(
                            archive_id, blob_id, session_id, part_number,
                            expected_size, expected_sha256
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            (
                                upload.archive_id,
                                upload.blob_id,
                                upload.session_id,
                                part.part_number,
                                part.size,
                                part.sha256,
                            )
                            for part in upload.parts
                        ),
                    )
                except sqlite3.IntegrityError:
                    raise BlobStateConflictError(
                        "multipart upload state already exists"
                    ) from None
                changed = connection.execute(
                    """
                    UPDATE server_blob_manifests
                    SET state = 'uploading_parts', updated_at = ?
                    WHERE archive_id = ? AND blob_id = ?
                      AND state = 'pending_upload'
                      AND size = ? AND sha256 = ? AND object_key = ?
                    """,
                    (
                        now,
                        upload.archive_id,
                        upload.blob_id,
                        upload.total_size,
                        upload.sha256,
                        upload.object_key,
                    ),
                )
                if changed.rowcount != 1:
                    raise BlobStateConflictError("blob manifest changed during planning")
                row = self._select_upload(
                    connection,
                    upload.archive_id,
                    upload.blob_id,
                    upload.session_id,
                )
                if row is None:
                    raise BlobStateConflictError("multipart upload was not persisted")
                persisted = self._upload(connection, row)
                connection.commit()
                return persisted
            except BaseException:
                connection.rollback()
                raise

    def get_upload(
        self,
        archive_id: str,
        blob_id: str,
        session_id: str,
        device_id: str,
    ) -> BlobUploadRecord | None:
        require_remote_id(archive_id, field_name="archive_id")
        require_remote_id(blob_id, field_name="blob_id")
        require_remote_id(device_id, field_name="device_id")
        with closing(self._connect()) as connection:
            row = self._select_upload(
                connection,
                archive_id,
                blob_id,
                session_id,
                device_id,
            )
            return None if row is None else self._upload(connection, row)

    def set_part_capability(
        self,
        archive_id: str,
        blob_id: str,
        session_id: str,
        part_number: int,
        *,
        capability_hash: str,
        expires_at: int,
    ) -> None:
        require_sha256(capability_hash)
        if part_number <= 0 or expires_at <= 0:
            raise BlobStateValidationError("part capability metadata is invalid")
        with self._writer_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                upload_row = self._select_upload(
                    connection,
                    archive_id,
                    blob_id,
                    session_id,
                )
                if upload_row is None or str(upload_row["state"]) not in (
                    "planned",
                    "uploading_parts",
                ):
                    raise BlobStateConflictError("multipart upload is not active")
                changed = connection.execute(
                    """
                    UPDATE server_blob_upload_parts
                    SET capability_hash = ?, capability_expires_at = ?
                    WHERE archive_id = ? AND blob_id = ? AND session_id = ?
                      AND part_number = ? AND etag IS NULL
                    """,
                    (
                        capability_hash,
                        expires_at,
                        archive_id,
                        blob_id,
                        session_id,
                        part_number,
                    ),
                )
                if changed.rowcount != 1:
                    raise BlobStateConflictError("multipart part is not pending")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def authorize_part(
        self,
        archive_id: str,
        blob_id: str,
        session_id: str,
        part_number: int,
        *,
        capability_hash: str,
        now: int,
    ) -> tuple[BlobUploadRecord, BlobPartRecord] | None:
        with closing(self._connect()) as connection:
            upload_row = self._select_upload(
                connection,
                archive_id,
                blob_id,
                session_id,
            )
            if upload_row is None or str(upload_row["state"]) not in (
                "planned",
                "uploading_parts",
            ):
                return None
            part_row = connection.execute(
                """
                SELECT part_number, expected_size, expected_sha256, etag,
                       completed_at, capability_hash, capability_expires_at
                FROM server_blob_upload_parts
                WHERE archive_id = ? AND blob_id = ? AND session_id = ?
                  AND part_number = ?
                """,
                (archive_id, blob_id, session_id, part_number),
            ).fetchone()
            if part_row is None:
                return None
            part = self._part(part_row)
            if (
                part.etag is not None
                or part.capability_hash is None
                or part.capability_expires_at is None
                or part.capability_expires_at < now
                or not constant_time_equal(part.capability_hash, capability_hash)
            ):
                return None
            return self._upload(connection, upload_row), part

    def mark_part_completed(
        self,
        archive_id: str,
        blob_id: str,
        session_id: str,
        part_number: int,
        *,
        etag: str,
        completed_at: int,
        expected_provider_upload_id: str | None = None,
    ) -> BlobUploadRecord:
        if not etag or completed_at <= 0:
            raise BlobStateValidationError("completed part metadata is invalid")
        with self._writer_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                upload_row = self._select_upload(
                    connection,
                    archive_id,
                    blob_id,
                    session_id,
                )
                if upload_row is None or str(upload_row["state"]) not in (
                    "planned",
                    "uploading_parts",
                ):
                    raise BlobStateConflictError("multipart upload is not active")
                if (expected_provider_upload_id is not None
                    and upload_row["provider_upload_id"] != expected_provider_upload_id):
                    raise BlobStateConflictError("multipart provider session changed")
                part_row = connection.execute(
                    """
                    SELECT part_number, expected_size, expected_sha256, etag,
                           completed_at, capability_hash, capability_expires_at
                    FROM server_blob_upload_parts
                    WHERE archive_id = ? AND blob_id = ? AND session_id = ?
                      AND part_number = ?
                    """,
                    (archive_id, blob_id, session_id, part_number),
                ).fetchone()
                if part_row is None:
                    raise BlobStateConflictError("multipart part does not exist")
                prior = self._part(part_row)
                if prior.etag is not None and prior.etag != etag:
                    raise BlobStateConflictError("multipart part ETag changed")
                connection.execute(
                    """
                    UPDATE server_blob_upload_parts
                    SET etag = ?, completed_at = ?,
                        capability_hash = NULL, capability_expires_at = NULL
                    WHERE archive_id = ? AND blob_id = ? AND session_id = ?
                      AND part_number = ?
                    """,
                    (
                        etag,
                        completed_at,
                        archive_id,
                        blob_id,
                        session_id,
                        part_number,
                    ),
                )
                connection.execute(
                    """
                    UPDATE server_blob_uploads
                    SET state = 'uploading_parts', updated_at = ?
                    WHERE archive_id = ? AND blob_id = ? AND session_id = ?
                    """,
                    (completed_at, archive_id, blob_id, session_id),
                )
                persisted = self._select_upload(
                    connection,
                    archive_id,
                    blob_id,
                    session_id,
                )
                if persisted is None:
                    raise BlobStateConflictError("multipart upload disappeared")
                result = self._upload(connection, persisted)
                connection.commit()
                return result
            except BaseException:
                connection.rollback()
                raise

    def sync_completed_parts(
        self,
        archive_id: str,
        blob_id: str,
        session_id: str,
        parts: tuple[tuple[int, str], ...],
        *,
        completed_at: int,
        expected_provider_upload_id: str | None = None,
    ) -> BlobUploadRecord:
        with self._writer_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                upload_row = self._select_upload(
                    connection,
                    archive_id,
                    blob_id,
                    session_id,
                )
                if upload_row is None:
                    raise BlobStateConflictError("multipart upload does not exist")
                if (expected_provider_upload_id is not None
                    and upload_row["provider_upload_id"] != expected_provider_upload_id):
                    raise BlobStateConflictError("multipart provider session changed")
                if str(upload_row["state"]) in ("aborted", "integrity_failed"):
                    raise BlobStateConflictError("multipart upload has ended")
                known_parts = {
                    part.part_number: part
                    for part in (
                        self._part(row)
                        for row in self._part_rows(
                            connection,
                            archive_id,
                            blob_id,
                            session_id,
                        )
                    )
                }
                for part_number, etag in parts:
                    known = known_parts.get(part_number)
                    if known is None or not etag:
                        raise BlobStateConflictError("provider returned an unknown part")
                    if known.etag is not None and known.etag != etag:
                        raise BlobStateConflictError("provider part ETag changed")
                    connection.execute(
                        """
                        UPDATE server_blob_upload_parts
                        SET etag = ?, completed_at = COALESCE(completed_at, ?),
                            capability_hash = NULL, capability_expires_at = NULL
                        WHERE archive_id = ? AND blob_id = ? AND session_id = ?
                          AND part_number = ?
                        """,
                        (
                            etag,
                            completed_at,
                            archive_id,
                            blob_id,
                            session_id,
                            part_number,
                        ),
                    )
                if parts and str(upload_row["state"]) == "planned":
                    connection.execute(
                        """
                        UPDATE server_blob_uploads
                        SET state = 'uploading_parts', updated_at = ?
                        WHERE archive_id = ? AND blob_id = ? AND session_id = ?
                        """,
                        (completed_at, archive_id, blob_id, session_id),
                    )
                persisted = self._select_upload(
                    connection,
                    archive_id,
                    blob_id,
                    session_id,
                )
                if persisted is None:
                    raise BlobStateConflictError("multipart upload disappeared")
                result = self._upload(connection, persisted)
                connection.commit()
                return result
            except BaseException:
                connection.rollback()
                raise

    def replace_missing_provider_upload(
        self, upload: BlobUploadRecord, provider_upload_id: str,
    ) -> BlobUploadRecord | None:
        """CAS an unverified session without a live finalizer; revoke old capabilities."""
        if not provider_upload_id or provider_upload_id == upload.provider_upload_id:
            raise BlobStateValidationError("replacement provider identity is invalid")
        with self._writer_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._select_upload(connection, upload.archive_id, upload.blob_id, upload.session_id)
            if row is None:
                return None
            current = self._upload(connection, row)
            manifest = self._select_manifest(connection, upload.archive_id, upload.blob_id)
            now = self._now()
            expiry = row["finalizer_expires_at"]
            expired_completion = (
                current.state == "completing"
                and expiry is not None and int(expiry) <= now
            )
            if (current.provider_upload_id != upload.provider_upload_id
                or current.plan_sha256 != upload.plan_sha256
                or current.device_id != upload.device_id
                or (current.state not in ("planned", "uploading_parts") and not expired_completion)
                or manifest is None or manifest["state"] == "remote_ready"):
                return None
            connection.execute(
                "UPDATE server_blob_uploads SET provider_upload_id=?,state='planned',updated_at=?,"
                "finalizer_id=NULL,finalizer_expires_at=NULL "
                "WHERE archive_id=? AND blob_id=? AND session_id=?",
                (provider_upload_id, now, upload.archive_id, upload.blob_id, upload.session_id),
            )
            connection.execute(
                "UPDATE server_blob_upload_parts SET etag=NULL,completed_at=NULL,"
                "capability_hash=NULL,capability_expires_at=NULL "
                "WHERE archive_id=? AND blob_id=? AND session_id=?",
                (upload.archive_id, upload.blob_id, upload.session_id),
            )
            persisted = self._select_upload(connection, upload.archive_id, upload.blob_id, upload.session_id)
            assert persisted is not None
            result = self._upload(connection, persisted)
            connection.commit()
            return result

    def claim_upload_completion(
        self,
        archive_id: str,
        blob_id: str,
        session_id: str,
        *,
        finalizer_id: str,
        now: int,
        lease_seconds: int,
    ) -> BlobUploadRecord:
        require_remote_id(finalizer_id, field_name="finalizer_id")
        if now <= 0 or lease_seconds <= 0:
            raise BlobStateValidationError("completion lease is invalid")
        with self._writer_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                upload_row = self._select_upload(
                    connection,
                    archive_id,
                    blob_id,
                    session_id,
                )
                if upload_row is None:
                    raise BlobStateConflictError("multipart upload does not exist")
                upload = self._upload(connection, upload_row)
                if upload.state in ("remote_ready", "aborted", "integrity_failed"):
                    raise BlobStateConflictError("multipart upload has ended")
                if any(part.etag is None for part in upload.parts):
                    raise BlobStateConflictError("multipart upload has missing parts")
                raw_expiry = upload_row["finalizer_expires_at"]
                if (
                    upload.state == "completing"
                    and raw_expiry is not None
                    and int(raw_expiry) > now
                ):
                    raise BlobStateConflictError("multipart completion is already active")
                if upload.state not in ("planned", "uploading_parts", "completing"):
                    raise BlobStateConflictError("multipart upload cannot be completed")
                expires_at = now + lease_seconds
                claimed = connection.execute(
                    """
                    UPDATE server_blob_uploads
                    SET state = 'completing', finalizer_id = ?,
                        finalizer_expires_at = ?, updated_at = ?
                    WHERE archive_id = ? AND blob_id = ? AND session_id = ?
                    """,
                    (
                        finalizer_id,
                        expires_at,
                        now,
                        archive_id,
                        blob_id,
                        session_id,
                    ),
                )
                if claimed.rowcount != 1:
                    raise BlobStateConflictError(
                        "multipart completion claim changed"
                    )
                connection.execute(
                    """
                    UPDATE server_blob_upload_parts
                    SET capability_hash = NULL, capability_expires_at = NULL
                    WHERE archive_id = ? AND blob_id = ? AND session_id = ?
                    """,
                    (archive_id, blob_id, session_id),
                )
                persisted = self._select_upload(
                    connection,
                    archive_id,
                    blob_id,
                    session_id,
                )
                if persisted is None:
                    raise BlobStateConflictError("multipart upload disappeared")
                result = self._upload(connection, persisted)
                connection.commit()
                return result
            except BaseException:
                connection.rollback()
                raise

    def mark_upload_ready(
        self,
        archive_id: str,
        blob_id: str,
        session_id: str,
        *,
        finalizer_id: str,
        object_version: str,
        verified_at: int,
        storage_generation: int = 1,
    ) -> BlobUploadRecord:
        require_remote_id(finalizer_id, field_name="finalizer_id")
        _require_storage_generation(storage_generation)
        if not object_version or verified_at <= 0:
            raise BlobStateValidationError("verified upload metadata is invalid")
        with self._writer_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                upload_row = self._select_upload(
                    connection,
                    archive_id,
                    blob_id,
                    session_id,
                )
                if upload_row is None:
                    raise BlobStateConflictError("multipart upload does not exist")
                upload = self._upload(connection, upload_row)
                if upload.state == "remote_ready":
                    connection.commit()
                    return upload
                if (
                    upload.state != "completing"
                    or upload_row["finalizer_id"] is None
                    or str(upload_row["finalizer_id"]) != finalizer_id
                ):
                    raise BlobStateConflictError(
                        "multipart completion claim is stale"
                    )
                if any(part.etag is None for part in upload.parts):
                    raise BlobStateConflictError("multipart upload has missing parts")
                manifest_row = self._select_manifest(connection, archive_id, blob_id)
                if manifest_row is None:
                    raise BlobStateConflictError("blob manifest does not exist")
                manifest = self._manifest(manifest_row)
                if (
                    manifest.size != upload.total_size
                    or manifest.sha256 != upload.sha256
                    or manifest.object_key != upload.object_key
                ):
                    raise BlobStateConflictError("blob manifest changed during upload")
                connection.execute(
                    """
                    UPDATE server_blob_manifests
                    SET state = 'remote_ready', object_version = ?,
                        verified_at = ?, updated_at = ?,
                        storage_generation = ?
                    WHERE archive_id = ? AND blob_id = ?
                    """,
                    (
                        object_version,
                        verified_at,
                        verified_at,
                        storage_generation,
                        archive_id,
                        blob_id,
                    ),
                )
                ready_changed = connection.execute(
                    """
                    UPDATE server_blob_uploads
                    SET state = 'remote_ready', object_version = ?,
                        verified_at = ?, finalizer_id = NULL,
                        finalizer_expires_at = NULL, updated_at = ?
                    WHERE archive_id = ? AND blob_id = ? AND session_id = ?
                      AND state = 'completing' AND finalizer_id = ?
                    """,
                    (
                        object_version,
                        verified_at,
                        verified_at,
                        archive_id,
                        blob_id,
                        session_id,
                        finalizer_id,
                    ),
                )
                if ready_changed.rowcount != 1:
                    raise BlobStateConflictError(
                        "multipart completion claim changed"
                    )
                persisted = self._select_upload(
                    connection,
                    archive_id,
                    blob_id,
                    session_id,
                )
                if persisted is None:
                    raise BlobStateConflictError("multipart upload disappeared")
                result = self._upload(connection, persisted)
                connection.commit()
                return result
            except BaseException:
                connection.rollback()
                raise

    def mark_upload_integrity_failed(
        self,
        archive_id: str,
        blob_id: str,
        session_id: str,
        *,
        finalizer_id: str | None = None,
    ) -> BlobUploadRecord:
        return self._mark_upload_ended(
            archive_id,
            blob_id,
            session_id,
            state="integrity_failed",
            reset_manifest=False,
            finalizer_id=finalizer_id,
        )

    def mark_upload_aborted(
        self,
        archive_id: str,
        blob_id: str,
        session_id: str,
        *,
        now: int,
    ) -> BlobUploadRecord:
        return self._mark_upload_ended(
            archive_id,
            blob_id,
            session_id,
            state="aborted",
            reset_manifest=True,
            now=now,
        )

    def _mark_upload_ended(
        self,
        archive_id: str,
        blob_id: str,
        session_id: str,
        *,
        state: str,
        reset_manifest: bool,
        finalizer_id: str | None = None,
        now: int | None = None,
    ) -> BlobUploadRecord:
        if finalizer_id is not None:
            require_remote_id(finalizer_id, field_name="finalizer_id")
        if state == "aborted" and (now is None or now <= 0):
            raise BlobStateValidationError("abort time is invalid")
        with self._writer_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                upload_row = self._select_upload(
                    connection,
                    archive_id,
                    blob_id,
                    session_id,
                )
                if upload_row is None:
                    raise BlobStateConflictError("multipart upload does not exist")
                upload = self._upload(connection, upload_row)
                if upload.state == "remote_ready":
                    raise BlobStateConflictError("verified upload cannot be changed")
                if upload.state == state:
                    connection.commit()
                    return upload
                if upload.state == "aborted":
                    raise BlobStateConflictError("aborted upload cannot be changed")
                if upload.state == "integrity_failed" and state != "aborted":
                    raise BlobStateConflictError(
                        "integrity-failed upload cannot be changed"
                    )
                if upload.state == "completing":
                    if state == "integrity_failed":
                        if (
                            finalizer_id is None
                            or upload_row["finalizer_id"] is None
                            or str(upload_row["finalizer_id"]) != finalizer_id
                        ):
                            raise BlobStateConflictError(
                                "multipart completion claim is stale"
                            )
                    else:
                        raw_expiry = upload_row["finalizer_expires_at"]
                        if raw_expiry is None or now is None or int(raw_expiry) > now:
                            raise BlobStateConflictError(
                                "active multipart completion cannot be aborted"
                            )
                changed_at = self._now() if now is None else now
                connection.execute(
                    """
                    UPDATE server_blob_uploads
                    SET state = ?, finalizer_id = NULL,
                        finalizer_expires_at = NULL, updated_at = ?
                    WHERE archive_id = ? AND blob_id = ? AND session_id = ?
                    """,
                    (state, changed_at, archive_id, blob_id, session_id),
                )
                connection.execute(
                    """
                    UPDATE server_blob_upload_parts
                    SET capability_hash = NULL, capability_expires_at = NULL
                    WHERE archive_id = ? AND blob_id = ? AND session_id = ?
                    """,
                    (archive_id, blob_id, session_id),
                )
                if reset_manifest:
                    replacement_key = new_object_key(upload.object_scope)
                    connection.execute(
                        """
                        UPDATE server_blob_manifests
                        SET state = 'pending_upload', object_key = ?,
                            object_version = NULL, verified_at = NULL,
                            updated_at = ?
                        WHERE archive_id = ? AND blob_id = ?
                          AND state <> 'remote_ready' AND object_key = ?
                        """,
                        (
                            replacement_key,
                            changed_at,
                            archive_id,
                            blob_id,
                            upload.object_key,
                        ),
                    )
                persisted = self._select_upload(
                    connection,
                    archive_id,
                    blob_id,
                    session_id,
                )
                if persisted is None:
                    raise BlobStateConflictError("multipart upload disappeared")
                result = self._upload(connection, persisted)
                connection.commit()
                return result
            except BaseException:
                connection.rollback()
                raise
