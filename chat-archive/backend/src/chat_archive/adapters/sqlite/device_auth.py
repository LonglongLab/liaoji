"""SQLite 设备鉴权存储；schema 与语义镜像 Worker D1 的 devices/pairing_codes/security_audit。"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

from chat_archive.domain.client_info import SyncClientInfo

from chat_archive.ports.device_auth import (
    ArchiveSummary,
    AuthContext,
    BearerToken,
    DeviceListItem,
    PairedDevice,
    PairingCodeIssued,
    PairingRejected,
)
from chat_archive.server.security import (
    constant_time_equal,
    generate_opaque_id,
    generate_secret_hex,
    sha256_hex,
)


class SqliteDeviceAuthStore:
    def __init__(
        self,
        database_path: Path,
        *,
        now: Callable[[], int] | None = None,
        busy_timeout_ms: int = 5_000,
        pairing_ttl_seconds: int = 600,
        max_failed_attempts: int = 8,
    ) -> None:
        if busy_timeout_ms <= 0 or pairing_ttl_seconds <= 0 or max_failed_attempts <= 0:
            raise ValueError("device auth limits must be positive")
        self._database_path = database_path
        self._now = now if now is not None else (lambda: int(time.time()))
        self._busy_timeout_ms = busy_timeout_ms
        self._pairing_ttl_seconds = pairing_ttl_seconds
        self._max_failed_attempts = max_failed_attempts
        self._writer_lock = threading.Lock()
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=self._busy_timeout_ms / 1_000)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS archives (
                    id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY,
                    archive_id TEXT NOT NULL REFERENCES archives(id),
                    display_name TEXT NOT NULL,
                    secret_hash TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
                    created_at INTEGER NOT NULL,
                    last_seen_at INTEGER,
                    revoked_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS ix_devices_archive ON devices(archive_id);
                CREATE TABLE IF NOT EXISTS device_client_info (
                    device_id TEXT PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
                    computer_name TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    client_name TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pairing_codes (
                    id TEXT PRIMARY KEY,
                    archive_id TEXT NOT NULL REFERENCES archives(id),
                    secret_hash TEXT NOT NULL,
                    created_by_device_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('active', 'used', 'revoked')),
                    max_uses INTEGER NOT NULL,
                    used_count INTEGER NOT NULL,
                    failed_attempts INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_pairing_codes_archive
                    ON pairing_codes(archive_id, status);
                CREATE TABLE IF NOT EXISTS security_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    archive_id TEXT NOT NULL,
                    event TEXT NOT NULL,
                    actor_device_id TEXT,
                    target_id TEXT,
                    created_at INTEGER NOT NULL
                );
                """
            )

    def _audit(
        self,
        connection: sqlite3.Connection,
        archive_id: str,
        event: str,
        actor_device_id: str | None,
        target_id: str | None,
    ) -> None:
        connection.execute(
            """INSERT INTO security_audit (archive_id, event, actor_device_id, target_id, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (archive_id, event, actor_device_id, target_id, self._now()),
        )

    def provision_archive(
        self, archive_id: str, display_name: str, device_name: str
    ) -> PairedDevice:
        device_id = f"dev_{generate_opaque_id()}"
        secret = generate_secret_hex()
        now = self._now()
        with self._writer_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT INTO archives (id, display_name, created_at) VALUES (?, ?, ?)",
                    (archive_id, display_name, now),
                )
                connection.execute(
                    """INSERT INTO devices
                         (id, archive_id, display_name, secret_hash, status, created_at,
                          last_seen_at, revoked_at)
                       VALUES (?, ?, ?, ?, 'active', ?, NULL, NULL)""",
                    (device_id, archive_id, device_name, sha256_hex(secret), now),
                )
                self._audit(connection, archive_id, "archive_provisioned", device_id, archive_id)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return PairedDevice(
            archive_id=archive_id, device_id=device_id, device_token=f"{device_id}.{secret}"
        )

    def ensure_browser_archive(self) -> str:
        with self._writer_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT id FROM archives ORDER BY created_at, id LIMIT 1").fetchone()
            if row is not None:
                connection.commit()
                return str(row[0])
            archive_id = "arch_" + generate_opaque_id()
            connection.execute("INSERT INTO archives (id, display_name, created_at) VALUES (?, ?, ?)",
                               (archive_id, "我的归档", self._now()))
            self._audit(connection, archive_id, "browser_archive_initialized", None, archive_id)
            connection.commit()
            return archive_id

    def issue_browser_pairing_code(self, session_id: str) -> PairingCodeIssued:
        from chat_archive.ports.device_auth import PairingThrottled

        archive_id = self.ensure_browser_archive()
        actor = "browser:" + session_id
        now = self._now()
        code_id, secret = generate_opaque_id(), generate_secret_hex()
        with self._writer_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            count = connection.execute(
                "SELECT COUNT(*) FROM security_audit WHERE event = 'browser_pairing_code_created' "
                "AND created_at > ? AND actor_device_id = ?", (now - 300, actor),
            ).fetchone()
            if count is not None and int(count[0]) >= 5:
                connection.rollback()
                raise PairingThrottled()
            connection.execute(
                "INSERT INTO pairing_codes (id, archive_id, secret_hash, created_by_device_id, status, "
                "max_uses, used_count, failed_attempts, expires_at, created_at) VALUES (?, ?, ?, ?, 'active', 1, 0, 0, ?, ?)",
                (code_id, archive_id, sha256_hex(secret), actor, now + self._pairing_ttl_seconds, now),
            )
            self._audit(connection, archive_id, "browser_pairing_code_created", actor, code_id)
            connection.commit()
        return PairingCodeIssued(pairing_code=f"{code_id}.{secret}", pairing_code_id=code_id,
                                 expires_at=now + self._pairing_ttl_seconds)

    def issue_pairing_code(self, archive_id: str, actor_device_id: str) -> PairingCodeIssued:
        code_id = generate_opaque_id()
        secret = generate_secret_hex()
        now = self._now()
        expires_at = now + self._pairing_ttl_seconds
        with self._writer_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """INSERT INTO pairing_codes
                         (id, archive_id, secret_hash, created_by_device_id, status,
                          max_uses, used_count, failed_attempts, expires_at, created_at)
                       VALUES (?, ?, ?, ?, 'active', 1, 0, 0, ?, ?)""",
                    (code_id, archive_id, sha256_hex(secret), actor_device_id, expires_at, now),
                )
                self._audit(
                    connection, archive_id, "pairing_code_created", actor_device_id, code_id
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return PairingCodeIssued(
            pairing_code=f"{code_id}.{secret}", pairing_code_id=code_id, expires_at=expires_at
        )

    def redeem_pairing_code(self, pairing_code: str, device_name: str) -> PairedDevice:
        name = device_name.strip()
        separator = pairing_code.find(".")
        if not name or len(name) > 64 or separator <= 0 or separator == len(pairing_code) - 1:
            raise PairingRejected()
        code_id = pairing_code[:separator]
        secret = pairing_code[separator + 1 :]
        with self._writer_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """SELECT archive_id, secret_hash, status, max_uses, used_count,
                              failed_attempts, expires_at
                       FROM pairing_codes WHERE id = ?""",
                    (code_id,),
                ).fetchone()
                if row is None:
                    connection.rollback()
                    raise PairingRejected()
                matches = constant_time_equal(sha256_hex(secret), row["secret_hash"])
                usable = (
                    row["status"] == "active"
                    and row["expires_at"] > self._now()
                    and row["used_count"] < row["max_uses"]
                    and row["failed_attempts"] < self._max_failed_attempts
                )
                if not matches or not usable:
                    # 错误 secret（且码仍 active）计一次失败并提交，即便本次拒绝
                    if not matches and row["status"] == "active":
                        connection.execute(
                            """UPDATE pairing_codes
                               SET failed_attempts = failed_attempts + 1,
                                   status = CASE WHEN failed_attempts + 1 >= ?
                                                 THEN 'revoked' ELSE status END
                               WHERE id = ?""",
                            (self._max_failed_attempts, code_id),
                        )
                        connection.commit()
                    else:
                        connection.rollback()
                    raise PairingRejected()
                consumed = connection.execute(
                    """UPDATE pairing_codes
                       SET used_count = used_count + 1,
                           status = CASE WHEN used_count + 1 >= max_uses THEN 'used' ELSE status END
                       WHERE id = ? AND status = 'active' AND used_count = ?""",
                    (code_id, row["used_count"]),
                )
                if consumed.rowcount != 1:
                    connection.rollback()
                    raise PairingRejected()
                archive_id = str(row["archive_id"])
                device_id = f"dev_{generate_opaque_id()}"
                secret_out = generate_secret_hex()
                connection.execute(
                    """INSERT INTO devices
                         (id, archive_id, display_name, secret_hash, status, created_at,
                          last_seen_at, revoked_at)
                       VALUES (?, ?, ?, ?, 'active', ?, NULL, NULL)""",
                    (device_id, archive_id, name, sha256_hex(secret_out), self._now()),
                )
                self._audit(connection, archive_id, "device_paired", device_id, code_id)
                connection.commit()
                return PairedDevice(
                    archive_id=archive_id,
                    device_id=device_id,
                    device_token=f"{device_id}.{secret_out}",
                )
            except PairingRejected:
                raise
            except BaseException:
                connection.rollback()
                raise

    def authenticate(self, token: BearerToken) -> AuthContext | None:
        with self._writer_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT archive_id, secret_hash, status FROM devices WHERE id = ?",
                    (token.device_id,),
                ).fetchone()
                if (
                    row is None
                    or row["status"] != "active"
                    or not constant_time_equal(sha256_hex(token.secret), row["secret_hash"])
                ):
                    connection.rollback()
                    return None
                connection.execute(
                    "UPDATE devices SET last_seen_at = ? WHERE id = ?",
                    (self._now(), token.device_id),
                )
                connection.commit()
                return AuthContext(device_id=token.device_id, archive_id=str(row["archive_id"]))
            except BaseException:
                connection.rollback()
                raise

    def revoke_device(
        self, archive_id: str, actor_device_id: str, target_device_id: str
    ) -> bool:
        with self._writer_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                target = connection.execute(
                    "SELECT id FROM devices WHERE id = ? AND archive_id = ?",
                    (target_device_id, archive_id),
                ).fetchone()
                if target is None:
                    connection.rollback()
                    return False
                connection.execute(
                    """UPDATE devices SET status = 'revoked', revoked_at = ?
                       WHERE id = ? AND archive_id = ?""",
                    (self._now(), target_device_id, archive_id),
                )
                self._audit(
                    connection, archive_id, "device_revoked", actor_device_id, target_device_id
                )
                connection.commit()
                return True
            except BaseException:
                connection.rollback()
                raise

    def revoke_all(self, archive_id: str, actor_device_id: str) -> int:
        with self._writer_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """UPDATE devices SET status = 'revoked', revoked_at = ?
                       WHERE archive_id = ? AND status = 'active'""",
                    (self._now(), archive_id),
                )
                revoked = cursor.rowcount
                connection.execute(
                    "UPDATE pairing_codes SET status = 'revoked' WHERE archive_id = ? AND status = 'active'",
                    (archive_id,),
                )
                self._audit(connection, archive_id, "devices_revoked_all", actor_device_id, None)
                connection.commit()
                return revoked
            except BaseException:
                connection.rollback()
                raise

    def rotate_token(self, archive_id: str, device_id: str) -> str:
        secret = generate_secret_hex()
        with self._writer_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    """UPDATE devices SET secret_hash = ?
                       WHERE id = ? AND archive_id = ? AND status = 'active'""",
                    (sha256_hex(secret), device_id, archive_id),
                )
                if cursor.rowcount != 1:
                    connection.rollback()
                    raise PairingRejected()
                self._audit(connection, archive_id, "token_rotated", device_id, device_id)
                connection.commit()
            except PairingRejected:
                raise
            except BaseException:
                connection.rollback()
                raise
        return f"{device_id}.{secret}"

    def list_archives(self) -> list[ArchiveSummary]:
        """本库里的归档，按 provision 顺序。

        浏览器登录是**部署级**的（一个口令），而读接口是**归档级**的，
        中间缺的这一环由本方法补：BFF 取最早 provision 的那个。
        自托管单节点上这就是唯一一个；真出现第二个时，选择是确定的，
        而不是随 SQLite 的行序漂。多归档的浏览器切换不属本单元。
        """
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT id, display_name, created_at
                   FROM archives ORDER BY created_at ASC, id ASC"""
            ).fetchall()
        return [
            ArchiveSummary(
                id=str(row["id"]),
                display_name=str(row["display_name"]),
                created_at=int(row["created_at"]),
            )
            for row in rows
        ]

    def list_devices(self, archive_id: str) -> list[DeviceListItem]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT d.id, d.display_name, d.status, d.created_at, d.last_seen_at, d.revoked_at,
                          c.computer_name, c.platform, c.client_name
                   FROM devices d LEFT JOIN device_client_info c ON c.device_id = d.id
                   WHERE d.archive_id = ? ORDER BY d.created_at ASC, d.id ASC""",
                (archive_id,),
            ).fetchall()
        return [
            DeviceListItem(
                id=row["id"],
                display_name=row["display_name"],
                status=row["status"],
                created_at=row["created_at"],
                last_seen_at=row["last_seen_at"],
                revoked_at=row["revoked_at"],
                last_local_sequence=0,
                client_info=None if row["computer_name"] is None else SyncClientInfo(
                    computer_name=row["computer_name"], platform=row["platform"],
                    client_name=row["client_name"],
                ),
            )
            for row in rows
        ]

    def update_client_info(self, context: AuthContext, info: SyncClientInfo) -> None:
        with self._writer_lock, closing(self._connect()) as connection:
            connection.execute(
                """INSERT INTO device_client_info (device_id, computer_name, platform, client_name)
                   SELECT id, ?, ?, ? FROM devices
                   WHERE id = ? AND archive_id = ? AND status = 'active'
                   ON CONFLICT(device_id) DO UPDATE SET computer_name = excluded.computer_name,
                     platform = excluded.platform, client_name = excluded.client_name
                   WHERE device_client_info.computer_name != excluded.computer_name
                      OR device_client_info.platform != excluded.platform
                      OR device_client_info.client_name != excluded.client_name""",
                (info.computer_name, info.platform, info.client_name, context.device_id, context.archive_id),
            )
            connection.commit()
