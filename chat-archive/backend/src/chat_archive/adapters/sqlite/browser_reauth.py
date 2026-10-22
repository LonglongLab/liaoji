"""可选日常再认证的持久状态（仅自托管远程端，PSU-2.213；PSU-2.221 每台设备各自开启）。

与 `SqliteBrowserAuthStore` 同住 `auth.sqlite`，表是分开的，并且都以外键挂在
`browser_sessions` / `browser_devices` 上：会话被登出、撤销、过期清理时，它的挑战、
锁定版本、设备绑定与解锁授权**随行删除**，不需要哪一条业务路径记得顺手清。

设备（PSU-2.221）：每台浏览器一行 `browser_devices`，长期 Cookie 只存摘要。应用锁
开关、策略版本、WebAuthn `user.id` 与通行密钥都挂在设备上；登录会话经
`browser_session_devices` 绑定到设备，**锁定判断只看会话绑定的设备**。登出删掉
会话与绑定，设备行保留——同一浏览器再登录仍绑回原设备，仍然锁着。

每次私人请求都读这里的持久事实，不在进程里缓存「已解锁」——多进程、重启、
锁定提交之后的下一条请求看到的是同一份答案。
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Literal, cast

from chat_archive.adapters.sqlite.browser_auth import backoff_seconds
from chat_archive.adapters.sqlite.guards import (
    BoundedWriterLock,
    StatementGuardConnection,
    install_statement_guard,
)
from chat_archive.ports.browser_auth import BrowserAuthThrottled
from chat_archive.ports.browser_reauth import (
    BrowserReauthConflict,
    BrowserReauthFreshRequired,
    BrowserReauthRejected,
    DeviceCookie,
    PasskeySummary,
    ReauthChallenge,
    ReauthGrantToken,
    ReauthPolicy,
    ReauthPurpose,
    ReauthSessionState,
    StoredPasskey,
)
from chat_archive.server.security import constant_time_equal, generate_opaque_id, sha256_hex

# 以下数值是**实施默认值**：解锁授权的绝对上限、挑战有效期与安全管理的新鲜窗口。
# 均可经构造参数覆盖。
DEFAULT_GRANT_SECONDS = 12 * 60 * 60
DEFAULT_CHALLENGE_SECONDS = 120
DEFAULT_FRESH_SECONDS = 5 * 60
# 设备 Cookie 有效期取 Chrome 上限 400 天，每次绑定顺延；180 天没有任何会话的设备清理。
DEFAULT_DEVICE_COOKIE_SECONDS = 400 * 24 * 60 * 60
DEFAULT_DEVICE_STALE_SECONDS = 180 * 24 * 60 * 60
# 系统时间明显回退（恢复旧备份、校时错误）时，旧授权不能因此「重新年轻」。
_CLOCK_ROLLBACK_TOLERANCE_SECONDS = 60
_MAX_GRANT_LENGTH = 256
_MAX_DEVICE_COOKIE_LENGTH = 256
_MAX_LABEL_LENGTH = 40
_MAX_TRANSPORTS = 8
_GRANT_DOMAIN = "chat-archive-browser-reauth-grant-v1\0"
_DEVICE_DOMAIN = "chat-archive-browser-device-v1\0"

UnlockMethod = Literal["passkey", "password"]

REAUTH_TABLES = (
    "browser_reauth_grants",
    "browser_webauthn_challenges",
    "browser_reauth_sessions",
    "browser_reauth_notices",
    "browser_passkeys",
    "browser_session_devices",
    "browser_devices",
)


def _grant_digest(value: str) -> str:
    return sha256_hex(f"{_GRANT_DOMAIN}{value}")


def _device_digest(value: str) -> str:
    return sha256_hex(f"{_DEVICE_DOMAIN}{value}")


def clean_label(label: str | None) -> str:
    """凭据显示名：去掉控制字符并截断；空了就给一个中性的名字。"""
    if label is None:
        return "系统验证"
    cleaned = "".join(char for char in label if char.isprintable()).strip()
    return cleaned[:_MAX_LABEL_LENGTH] or "系统验证"


def _columns(connection: sqlite3.Connection, table: str) -> set[str] | None:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    if exists is None:
        return None
    return {str(row["name"]) for row in connection.execute(f"PRAGMA table_info({table})")}


_DEVICE_SESSIONS = "SELECT session_id FROM browser_session_devices WHERE device_id = ?"

_DEVICES_DDL = """
CREATE TABLE IF NOT EXISTS browser_devices (
    id TEXT PRIMARY KEY,
    cookie_digest TEXT NOT NULL UNIQUE,
    user_handle BLOB NOT NULL,
    lock_enabled INTEGER NOT NULL DEFAULT 0 CHECK (lock_enabled IN (0, 1)),
    policy_version INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL
)
"""


class SqliteBrowserReauthStore:
    def __init__(
        self,
        database_path: Path,
        *,
        now: Callable[[], int] | None = None,
        busy_timeout_ms: int = 5_000,
        grant_seconds: int = DEFAULT_GRANT_SECONDS,
        challenge_seconds: int = DEFAULT_CHALLENGE_SECONDS,
        fresh_seconds: int = DEFAULT_FRESH_SECONDS,
        device_cookie_seconds: int = DEFAULT_DEVICE_COOKIE_SECONDS,
        device_stale_seconds: int = DEFAULT_DEVICE_STALE_SECONDS,
    ) -> None:
        if (
            min(
                busy_timeout_ms,
                grant_seconds,
                challenge_seconds,
                fresh_seconds,
                device_cookie_seconds,
                device_stale_seconds,
            )
            <= 0
        ):
            raise ValueError("browser reauth limits must be positive")
        self._database_path = database_path
        self._now = now if now is not None else (lambda: int(time.time()))
        self._busy_timeout_ms = busy_timeout_ms
        self._grant_seconds = grant_seconds
        self._challenge_seconds = challenge_seconds
        self._fresh_seconds = fresh_seconds
        self._device_cookie_seconds = device_cookie_seconds
        self._device_stale_seconds = device_stale_seconds
        self._writer_lock = BoundedWriterLock(name="browser-reauth")
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @property
    def challenge_seconds(self) -> int:
        return self._challenge_seconds

    @property
    def device_cookie_seconds(self) -> int:
        return self._device_cookie_seconds

    # ---------------------------------------------------------------- storage

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._database_path,
            timeout=self._busy_timeout_ms / 1_000,
            factory=StatementGuardConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        install_statement_guard(connection, name="browser-reauth")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            # 设备表先建：迁移给旧 `browser_passkeys` 补的外键列要指向它。
            connection.execute(_DEVICES_DDL)
            self._migrate_account_level(connection)
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS browser_session_devices (
                    session_id TEXT PRIMARY KEY
                        REFERENCES browser_sessions(id) ON DELETE CASCADE,
                    device_id TEXT NOT NULL
                        REFERENCES browser_devices(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS browser_session_devices_device
                    ON browser_session_devices(device_id);
                CREATE TABLE IF NOT EXISTS browser_passkeys (
                    id TEXT PRIMARY KEY,
                    credential_id BLOB NOT NULL UNIQUE,
                    public_key BLOB NOT NULL,
                    sign_count INTEGER NOT NULL,
                    transports TEXT NOT NULL,
                    backed_up INTEGER NOT NULL CHECK (backed_up IN (0, 1)),
                    label TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    last_used_at INTEGER,
                    device_id TEXT REFERENCES browser_devices(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS browser_reauth_sessions (
                    session_id TEXT PRIMARY KEY
                        REFERENCES browser_sessions(id) ON DELETE CASCADE,
                    lock_version INTEGER NOT NULL,
                    fresh_until INTEGER,
                    fresh_lock_version INTEGER,
                    fresh_passkey_id TEXT,
                    failure_count INTEGER NOT NULL DEFAULT 0,
                    last_failure_at INTEGER,
                    fresh_method TEXT
                );
                CREATE TABLE IF NOT EXISTS browser_webauthn_challenges (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL
                        REFERENCES browser_sessions(id) ON DELETE CASCADE,
                    purpose TEXT NOT NULL CHECK (purpose IN ('register', 'unlock', 'manage')),
                    challenge BLOB NOT NULL,
                    policy_version INTEGER NOT NULL,
                    lock_version INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS browser_reauth_grants (
                    digest TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL
                        REFERENCES browser_sessions(id) ON DELETE CASCADE,
                    device_id TEXT NOT NULL
                        REFERENCES browser_devices(id) ON DELETE CASCADE,
                    method TEXT NOT NULL CHECK (method IN ('passkey', 'password')),
                    passkey_id TEXT
                        REFERENCES browser_passkeys(id) ON DELETE CASCADE,
                    policy_version INTEGER NOT NULL,
                    lock_version INTEGER NOT NULL,
                    issued_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    CHECK ((method = 'passkey') = (passkey_id IS NOT NULL))
                );
                CREATE TABLE IF NOT EXISTS browser_reauth_notices (
                    session_id TEXT PRIMARY KEY
                        REFERENCES browser_sessions(id) ON DELETE CASCADE,
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS browser_reauth_grants_session
                    ON browser_reauth_grants(session_id);
                CREATE INDEX IF NOT EXISTS browser_webauthn_challenges_session
                    ON browser_webauthn_challenges(session_id);
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

    def _migrate_account_level(self, connection: sqlite3.Connection) -> None:
        """PSU-2.213 的账户级应用锁 → PSU-2.221 每台设备各自开启。

        旧凭据没有设备归属，不能安全地分给某一台设备，所以一律清掉；旧锁是开着的，
        就给当时所有会话留一次提示，请用户在需要的设备上重新开启。只跑一次：
        旧策略行删掉、旧表结构补齐之后，再启动就什么也不做。旧表 `browser_reauth_policy`
        保留（空）一个版本。
        """
        connection.execute("BEGIN IMMEDIATE")
        try:
            passkeys = _columns(connection, "browser_passkeys")
            sessions = _columns(connection, "browser_reauth_sessions")
            grants = _columns(connection, "browser_reauth_grants")
            policy = _columns(connection, "browser_reauth_policy")
            legacy_enabled = False
            migrated = False
            if policy is not None:
                row = connection.execute(
                    "SELECT enabled FROM browser_reauth_policy WHERE id = 1"
                ).fetchone()
                if row is not None:
                    legacy_enabled = bool(row["enabled"])
                    connection.execute("DELETE FROM browser_reauth_policy")
                    migrated = True
            if grants is not None and "method" not in grants:
                connection.execute("DROP TABLE browser_reauth_grants")
                migrated = True
            if sessions is not None and "fresh_method" not in sessions:
                connection.execute("ALTER TABLE browser_reauth_sessions ADD COLUMN fresh_method TEXT")
                migrated = True
            if passkeys is not None and "device_id" not in passkeys:
                connection.execute("DELETE FROM browser_passkeys")
                connection.execute(
                    "ALTER TABLE browser_passkeys ADD COLUMN device_id TEXT"
                    " REFERENCES browser_devices(id) ON DELETE CASCADE"
                )
                migrated = True
            if migrated:
                connection.execute("DELETE FROM browser_passkeys WHERE device_id IS NULL")
                if _columns(connection, "browser_webauthn_challenges") is not None:
                    connection.execute("DELETE FROM browser_webauthn_challenges")
                if sessions is not None:
                    connection.execute(
                        """UPDATE browser_reauth_sessions
                             SET fresh_until = NULL, fresh_lock_version = NULL,
                                 fresh_passkey_id = NULL, fresh_method = NULL"""
                    )
                if legacy_enabled and _columns(connection, "browser_sessions") is not None:
                    connection.execute(
                        """CREATE TABLE IF NOT EXISTS browser_reauth_notices (
                               session_id TEXT PRIMARY KEY
                                   REFERENCES browser_sessions(id) ON DELETE CASCADE,
                               created_at INTEGER NOT NULL
                           )"""
                    )
                    connection.execute(
                        """INSERT OR IGNORE INTO browser_reauth_notices (session_id, created_at)
                           SELECT id, ? FROM browser_sessions""",
                        (self._now(),),
                    )
                if _columns(connection, "security_audit") is not None:
                    self._audit(connection, "browser_reauth_migrated_per_device", None)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    def _audit(self, connection: sqlite3.Connection, event: str, target_id: str | None) -> None:
        # 只记事件类型与不透明 id；不记凭据、挑战、assertion、口令或授权明文。
        connection.execute(
            """INSERT INTO security_audit
                 (archive_id, event, actor_device_id, target_id, created_at)
               VALUES ('browser', ?, NULL, ?, ?)""",
            (event, target_id, self._now()),
        )

    def _write(self) -> sqlite3.Connection:
        connection = self._connect()
        connection.execute("BEGIN IMMEDIATE")
        return connection

    # ---------------------------------------------------------------- devices

    @staticmethod
    def _device_row(connection: sqlite3.Connection, session_id: str) -> sqlite3.Row | None:
        return connection.execute(
            """SELECT d.* FROM browser_session_devices s
               JOIN browser_devices d ON d.id = s.device_id
               WHERE s.session_id = ?""",
            (session_id,),
        ).fetchone()

    def _require_device(self, connection: sqlite3.Connection, session_id: str) -> sqlite3.Row:
        row = self._device_row(connection, session_id)
        if row is None:
            raise BrowserReauthRejected()
        return row

    def _policy_of(self, connection: sqlite3.Connection, session_id: str) -> ReauthPolicy:
        row = self._device_row(connection, session_id)
        if row is None:
            return ReauthPolicy(enabled=False, policy_version=0, user_handle=b"")
        return ReauthPolicy(
            enabled=bool(row["lock_enabled"]),
            policy_version=int(row["policy_version"]),
            user_handle=bytes(row["user_handle"]),
        )

    @staticmethod
    def _passkey_count(connection: sqlite3.Connection, device_id: str | None) -> int:
        if device_id is None:
            return 0
        return int(
            connection.execute(
                "SELECT COUNT(*) AS n FROM browser_passkeys WHERE device_id = ?", (device_id,)
            ).fetchone()["n"]
        )

    def bind_device(self, session_id: str, cookie_value: str | None) -> DeviceCookie | None:
        now = self._now()
        presented = (
            cookie_value
            if cookie_value and len(cookie_value) <= _MAX_DEVICE_COOKIE_LENGTH
            else None
        )
        with self._writer_lock, closing(self._write()) as connection:
            try:
                exists = connection.execute(
                    "SELECT 1 FROM browser_sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if exists is None:
                    connection.rollback()
                    return None
                # 长期没有任何会话的设备连同它的通行密钥一起清理。
                connection.execute(
                    """DELETE FROM browser_devices
                       WHERE last_seen_at < ?
                         AND id NOT IN (SELECT device_id FROM browser_session_devices)""",
                    (now - self._device_stale_seconds,),
                )
                presented_id: str | None = None
                if presented is not None:
                    digest = _device_digest(presented)
                    row = connection.execute(
                        "SELECT id, cookie_digest FROM browser_devices WHERE cookie_digest = ?",
                        (digest,),
                    ).fetchone()
                    if row is not None and constant_time_equal(str(row["cookie_digest"]), digest):
                        presented_id = str(row["id"])
                bound = connection.execute(
                    "SELECT device_id FROM browser_session_devices WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                result: DeviceCookie | None
                if bound is not None:
                    # 已绑定的会话从不改绑：换设备 Cookie 绕不开本机的锁。
                    device_id = str(bound["device_id"])
                    result = (
                        DeviceCookie(value=cast(str, presented), created=False)
                        if presented_id == device_id
                        else None
                    )
                else:
                    if presented_id is not None:
                        device_id = presented_id
                        result = DeviceCookie(value=cast(str, presented), created=False)
                    else:
                        device_id = f"bd_{generate_opaque_id()}"
                        value = secrets.token_urlsafe(32)
                        # 每台设备一个随机不透明 user.id：同步型通行密钥提供方按
                        # (rpId, user.id) 存凭据，共用一个会让 B 设备登记时覆盖 A 的。
                        connection.execute(
                            """INSERT INTO browser_devices
                                 (id, cookie_digest, user_handle, lock_enabled, policy_version,
                                  created_at, last_seen_at)
                               VALUES (?, ?, ?, 0, 0, ?, ?)""",
                            (device_id, _device_digest(value), secrets.token_bytes(32), now, now),
                        )
                        self._audit(connection, "browser_device_created", device_id)
                        result = DeviceCookie(value=value, created=True)
                    connection.execute(
                        "INSERT INTO browser_session_devices (session_id, device_id) VALUES (?, ?)",
                        (session_id, device_id),
                    )
                connection.execute(
                    "UPDATE browser_devices SET last_seen_at = ? WHERE id = ?", (now, device_id)
                )
                connection.commit()
                return result
            except BaseException:
                connection.rollback()
                raise

    # ------------------------------------------------------------------ reads

    def lock_enabled(self, session_id: str) -> bool:
        with closing(self._connect()) as connection:
            row = self._device_row(connection, session_id)
            return row is not None and bool(row["lock_enabled"])

    def policy(self, session_id: str) -> ReauthPolicy:
        with closing(self._connect()) as connection:
            return self._policy_of(connection, session_id)

    def user_handle(self, session_id: str) -> bytes:
        with closing(self._connect()) as connection:
            return bytes(self._require_device(connection, session_id)["user_handle"])

    @staticmethod
    def _lock_version(connection: sqlite3.Connection, session_id: str) -> int:
        row = connection.execute(
            "SELECT lock_version FROM browser_reauth_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return 0 if row is None else int(row["lock_version"])

    @staticmethod
    def _ensure_session(connection: sqlite3.Connection, session_id: str) -> None:
        exists = connection.execute(
            "SELECT 1 FROM browser_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if exists is None:
            raise BrowserReauthRejected()
        connection.execute(
            """INSERT INTO browser_reauth_sessions (session_id, lock_version)
               VALUES (?, 0) ON CONFLICT(session_id) DO NOTHING""",
            (session_id,),
        )

    def _grant_expiry(
        self, connection: sqlite3.Connection, session_id: str, grant_value: str | None
    ) -> int | None:
        if not grant_value or len(grant_value) > _MAX_GRANT_LENGTH:
            return None
        digest = _grant_digest(grant_value)
        # 授权只在：会话仍绑定签发时的设备、设备的锁仍开着且版本没变、会话没有
        # 再锁过、签发它的凭据（口令解锁除外）还在本设备上——全部成立时有效。
        row = connection.execute(
            """SELECT g.digest, g.issued_at, g.expires_at
               FROM browser_reauth_grants g
               JOIN browser_session_devices sd
                 ON sd.session_id = g.session_id AND sd.device_id = g.device_id
               JOIN browser_devices d
                 ON d.id = g.device_id AND d.lock_enabled = 1
                AND d.policy_version = g.policy_version
               JOIN browser_reauth_sessions r
                 ON r.session_id = g.session_id AND r.lock_version = g.lock_version
               JOIN browser_sessions s ON s.id = g.session_id
               LEFT JOIN browser_passkeys k
                 ON k.id = g.passkey_id AND k.device_id = g.device_id
               WHERE g.digest = ? AND g.session_id = ?
                 AND (g.method = 'password' OR k.id IS NOT NULL)""",
            (digest, session_id),
        ).fetchone()
        if row is None or not constant_time_equal(str(row["digest"]), digest):
            return None
        now = self._now()
        if now >= int(row["expires_at"]):
            return None
        if now < int(row["issued_at"]) - _CLOCK_ROLLBACK_TOLERANCE_SECONDS:
            return None
        return int(row["expires_at"])

    def grant_valid(self, session_id: str, grant_value: str | None) -> bool:
        with closing(self._connect()) as connection:
            return self._grant_expiry(connection, session_id, grant_value) is not None

    def _fresh_until(self, connection: sqlite3.Connection, session_id: str) -> int | None:
        row = connection.execute(
            """SELECT r.fresh_until, r.fresh_lock_version, r.lock_version, r.fresh_method,
                      k.id AS passkey_id
               FROM browser_reauth_sessions r
               LEFT JOIN browser_session_devices sd ON sd.session_id = r.session_id
               LEFT JOIN browser_passkeys k
                 ON k.id = r.fresh_passkey_id AND k.device_id = sd.device_id
               WHERE r.session_id = ?""",
            (session_id,),
        ).fetchone()
        if row is None or row["fresh_until"] is None or row["fresh_lock_version"] is None:
            return None
        if row["fresh_method"] != "password" and row["passkey_id"] is None:
            return None
        if int(row["fresh_lock_version"]) != int(row["lock_version"]):
            return None
        until = int(row["fresh_until"])
        now = self._now()
        if now >= until or now < until - self._fresh_seconds - _CLOCK_ROLLBACK_TOLERANCE_SECONDS:
            return None
        return until

    def has_fresh(self, session_id: str) -> bool:
        with closing(self._connect()) as connection:
            return self._fresh_until(connection, session_id) is not None

    def session_state(self, session_id: str, grant_value: str | None) -> ReauthSessionState:
        with closing(self._connect()) as connection:
            device = self._device_row(connection, session_id)
            policy = self._policy_of(connection, session_id)
            expiry = self._grant_expiry(connection, session_id, grant_value)
            notice = connection.execute(
                "SELECT 1 FROM browser_reauth_notices WHERE session_id = ?", (session_id,)
            ).fetchone()
            return ReauthSessionState(
                enabled=policy.enabled,
                policy_version=policy.policy_version,
                unlocked=expiry is not None,
                unlocked_until=expiry,
                fresh_until=self._fresh_until(connection, session_id),
                passkey_count=self._passkey_count(
                    connection, None if device is None else str(device["id"])
                ),
                migration_notice=notice is not None,
            )

    def list_passkeys(self, session_id: str) -> list[StoredPasskey]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT k.* FROM browser_passkeys k
                   JOIN browser_session_devices s ON s.device_id = k.device_id
                   WHERE s.session_id = ?
                   ORDER BY k.created_at ASC, k.id ASC""",
                (session_id,),
            ).fetchall()
        return [
            StoredPasskey(
                id=str(row["id"]),
                credential_id=bytes(row["credential_id"]),
                public_key=bytes(row["public_key"]),
                sign_count=int(row["sign_count"]),
                transports=tuple(str(item) for item in json.loads(str(row["transports"]))),
                backed_up=bool(row["backed_up"]),
                label=str(row["label"]),
                created_at=int(row["created_at"]),
                last_used_at=None if row["last_used_at"] is None else int(row["last_used_at"]),
            )
            for row in rows
        ]

    # ------------------------------------------------------------- challenges

    def create_challenge(
        self, session_id: str, purpose: ReauthPurpose, challenge: bytes
    ) -> ReauthChallenge:
        if len(challenge) < 32:
            raise ValueError("challenge must carry at least 32 random bytes")
        now = self._now()
        with self._writer_lock, closing(self._write()) as connection:
            try:
                self._ensure_session(connection, session_id)
                self._require_device(connection, session_id)
                policy = self._policy_of(connection, session_id)
                connection.execute(
                    "DELETE FROM browser_webauthn_challenges WHERE expires_at <= ?", (now,)
                )
                # 每条会话、每种用途只保留最新一个挑战：旧挑战没有继续存在的理由。
                connection.execute(
                    "DELETE FROM browser_webauthn_challenges WHERE session_id = ? AND purpose = ?",
                    (session_id, purpose),
                )
                record = ReauthChallenge(
                    id=f"wc_{generate_opaque_id()}",
                    purpose=purpose,
                    challenge=challenge,
                    policy_version=policy.policy_version,
                    lock_version=self._lock_version(connection, session_id),
                    expires_at=now + self._challenge_seconds,
                )
                connection.execute(
                    """INSERT INTO browser_webauthn_challenges
                         (id, session_id, purpose, challenge, policy_version, lock_version, expires_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        record.id,
                        session_id,
                        purpose,
                        challenge,
                        record.policy_version,
                        record.lock_version,
                        record.expires_at,
                    ),
                )
                connection.commit()
                return record
            except BaseException:
                connection.rollback()
                raise

    def consume_challenge(
        self, session_id: str, challenge_id: str, purposes: tuple[ReauthPurpose, ...]
    ) -> ReauthChallenge:
        """取出并**作废**挑战；无论之后验签成败，同一挑战都不能再用第二次。"""
        if not challenge_id or len(challenge_id) > 128:
            raise BrowserReauthRejected()
        with self._writer_lock, closing(self._write()) as connection:
            try:
                row = connection.execute(
                    "SELECT * FROM browser_webauthn_challenges WHERE id = ? AND session_id = ?",
                    (challenge_id, session_id),
                ).fetchone()
                if row is None:
                    connection.rollback()
                    raise BrowserReauthRejected()
                connection.execute(
                    "DELETE FROM browser_webauthn_challenges WHERE id = ?", (challenge_id,)
                )
                connection.commit()
            except BrowserReauthRejected:
                raise
            except BaseException:
                connection.rollback()
                raise
        record = ReauthChallenge(
            id=str(row["id"]),
            purpose=cast(ReauthPurpose, str(row["purpose"])),
            challenge=bytes(row["challenge"]),
            policy_version=int(row["policy_version"]),
            lock_version=int(row["lock_version"]),
            expires_at=int(row["expires_at"]),
        )
        if record.purpose not in purposes or self._now() >= record.expires_at:
            raise BrowserReauthRejected()
        return record

    def _assert_versions(
        self, connection: sqlite3.Connection, session_id: str, challenge: ReauthChallenge
    ) -> ReauthPolicy:
        self._require_device(connection, session_id)
        policy = self._policy_of(connection, session_id)
        if (
            policy.policy_version != challenge.policy_version
            or self._lock_version(connection, session_id) != challenge.lock_version
        ):
            # 在旧版本上发起、锁定或改开关之后才回来的验证：不能重新打开。
            raise BrowserReauthRejected()
        return policy

    # ---------------------------------------------------------------- backoff

    def guard_backoff(self, session_id: str) -> None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT failure_count, last_failure_at FROM browser_reauth_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None or row["last_failure_at"] is None:
            return
        remaining = int(row["last_failure_at"]) + backoff_seconds(int(row["failure_count"])) - self._now()
        if remaining > 0:
            raise BrowserAuthThrottled(remaining)

    def record_failure(self, session_id: str) -> None:
        """系统验证与口令解锁的失败共用一个按会话计数。"""
        now = self._now()
        with self._writer_lock, closing(self._write()) as connection:
            try:
                connection.execute(
                    """INSERT INTO browser_reauth_sessions
                         (session_id, lock_version, failure_count, last_failure_at)
                       SELECT id, 0, 1, ? FROM browser_sessions WHERE id = ?
                       ON CONFLICT(session_id) DO UPDATE
                         SET failure_count = failure_count + 1, last_failure_at = excluded.last_failure_at""",
                    (now, session_id),
                )
                self._audit(connection, "browser_reauth_failed", session_id)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @staticmethod
    def _clear_failures(connection: sqlite3.Connection, session_id: str) -> None:
        connection.execute(
            """UPDATE browser_reauth_sessions SET failure_count = 0, last_failure_at = NULL
               WHERE session_id = ?""",
            (session_id,),
        )

    def _open_fresh(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        *,
        method: UnlockMethod,
        passkey_id: str | None,
    ) -> None:
        # 窗口绑定当前锁定版本，锁定即作废。
        connection.execute(
            """UPDATE browser_reauth_sessions
                 SET fresh_until = ?, fresh_lock_version = lock_version,
                     fresh_passkey_id = ?, fresh_method = ?
               WHERE session_id = ?""",
            (self._now() + self._fresh_seconds, passkey_id, method, session_id),
        )

    # ---------------------------------------------------------------- writes

    def add_passkey(
        self,
        session_id: str,
        challenge: ReauthChallenge,
        *,
        credential_id: bytes,
        public_key: bytes,
        sign_count: int,
        transports: tuple[str, ...],
        backed_up: bool,
        label: str,
    ) -> PasskeySummary:
        if challenge.purpose != "register":
            raise BrowserReauthRejected()
        now = self._now()
        passkey_id = f"pk_{generate_opaque_id()}"
        clean = clean_label(label)
        kept_transports = [item[:32] for item in transports[:_MAX_TRANSPORTS]]
        with self._writer_lock, closing(self._write()) as connection:
            try:
                self._ensure_session(connection, session_id)
                device = self._require_device(connection, session_id)
                self._assert_versions(connection, session_id, challenge)
                duplicate = connection.execute(
                    "SELECT 1 FROM browser_passkeys WHERE credential_id = ?", (credential_id,)
                ).fetchone()
                if duplicate is not None:
                    raise BrowserReauthRejected()
                connection.execute(
                    """INSERT INTO browser_passkeys
                         (id, credential_id, public_key, sign_count, transports, backed_up,
                          label, created_at, last_used_at, device_id)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
                    (
                        passkey_id,
                        credential_id,
                        public_key,
                        max(0, sign_count),
                        json.dumps(kept_transports),
                        1 if backed_up else 0,
                        clean,
                        now,
                        str(device["id"]),
                    ),
                )
                self._clear_failures(connection, session_id)
                # 登记本身就是一次「用户验证 required」的系统验证，而且证明这枚新密钥此刻
                # 真的能用：记作对它的一次新鲜验证。这样「登记 + 开启」只弹一次系统框。
                self._open_fresh(connection, session_id, method="passkey", passkey_id=passkey_id)
                self._audit(connection, "browser_passkey_registered", passkey_id)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return PasskeySummary(
            id=passkey_id, label=clean, created_at=now, last_used_at=None, backed_up=backed_up
        )

    def _issue_grant(
        self,
        connection: sqlite3.Connection,
        session_id: str,
        policy: ReauthPolicy,
        *,
        method: UnlockMethod,
        passkey_id: str | None,
    ) -> ReauthGrantToken:
        device = self._require_device(connection, session_id)
        now = self._now()
        value = secrets.token_hex(32)
        expires_at = now + self._grant_seconds
        # 每次解锁都换一个新值；本会话的旧授权一并作废（权限变化后轮换凭据）。
        connection.execute("DELETE FROM browser_reauth_grants WHERE session_id = ?", (session_id,))
        connection.execute(
            """INSERT INTO browser_reauth_grants
                 (digest, session_id, device_id, method, passkey_id, policy_version,
                  lock_version, issued_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                _grant_digest(value),
                session_id,
                str(device["id"]),
                method,
                passkey_id if method == "passkey" else None,
                policy.policy_version,
                self._lock_version(connection, session_id),
                now,
                expires_at,
            ),
        )
        return ReauthGrantToken(value=value, expires_at=expires_at)

    def complete_verification(
        self,
        session_id: str,
        challenge: ReauthChallenge,
        *,
        passkey_id: str,
        new_sign_count: int,
        backed_up: bool,
    ) -> ReauthGrantToken | None:
        """验签通过之后落账：解锁签发授权；安全管理只打开一个短的新鲜窗口。"""
        now = self._now()
        with self._writer_lock, closing(self._write()) as connection:
            try:
                self._ensure_session(connection, session_id)
                device = self._require_device(connection, session_id)
                policy = self._assert_versions(connection, session_id, challenge)
                # 只认本设备的凭据：别的设备同步过来的那枚在这里不算数。
                updated = connection.execute(
                    """UPDATE browser_passkeys
                         SET sign_count = MAX(sign_count, ?), backed_up = ?, last_used_at = ?
                       WHERE id = ? AND device_id = ?""",
                    (
                        max(0, new_sign_count),
                        1 if backed_up else 0,
                        now,
                        passkey_id,
                        str(device["id"]),
                    ),
                )
                if updated.rowcount != 1:
                    raise BrowserReauthRejected()
                self._clear_failures(connection, session_id)
                token: ReauthGrantToken | None = None
                if challenge.purpose == "unlock":
                    if not policy.enabled:
                        raise BrowserReauthConflict()
                    token = self._issue_grant(
                        connection, session_id, policy, method="passkey", passkey_id=passkey_id
                    )
                    self._audit(connection, "browser_reauth_unlocked", session_id)
                elif challenge.purpose == "manage":
                    self._open_fresh(connection, session_id, method="passkey", passkey_id=passkey_id)
                    self._audit(connection, "browser_reauth_fresh", session_id)
                else:
                    raise BrowserReauthRejected()
                connection.commit()
                return token
            except BaseException:
                connection.rollback()
                raise

    def unlock_with_password(self, session_id: str) -> ReauthGrantToken:
        with self._writer_lock, closing(self._write()) as connection:
            try:
                self._ensure_session(connection, session_id)
                policy = self._policy_of(connection, session_id)
                if not policy.enabled:
                    raise BrowserReauthConflict()
                self._clear_failures(connection, session_id)
                token = self._issue_grant(
                    connection, session_id, policy, method="password", passkey_id=None
                )
                # 口令解锁之后紧接着关锁或重新录入不必再验证一次。
                self._open_fresh(connection, session_id, method="password", passkey_id=None)
                self._audit(connection, "browser_reauth_unlocked_password", session_id)
                connection.commit()
                return token
            except BaseException:
                connection.rollback()
                raise

    def mark_password_fresh(self, session_id: str) -> None:
        with self._writer_lock, closing(self._write()) as connection:
            try:
                self._ensure_session(connection, session_id)
                self._clear_failures(connection, session_id)
                self._open_fresh(connection, session_id, method="password", passkey_id=None)
                self._audit(connection, "browser_reauth_fresh_password", session_id)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def lock(self, session_id: str) -> None:
        with self._writer_lock, closing(self._write()) as connection:
            try:
                exists = connection.execute(
                    "SELECT 1 FROM browser_sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if exists is not None:
                    self._ensure_session(connection, session_id)
                    connection.execute(
                        """UPDATE browser_reauth_sessions
                             SET lock_version = lock_version + 1, fresh_until = NULL,
                                 fresh_lock_version = NULL, fresh_passkey_id = NULL,
                                 fresh_method = NULL
                           WHERE session_id = ?""",
                        (session_id,),
                    )
                    connection.execute(
                        "DELETE FROM browser_reauth_grants WHERE session_id = ?", (session_id,)
                    )
                    connection.execute(
                        "DELETE FROM browser_webauthn_challenges WHERE session_id = ?",
                        (session_id,),
                    )
                    self._audit(connection, "browser_reauth_locked", session_id)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def set_policy(
        self, session_id: str, *, enabled: bool, expected_version: int
    ) -> tuple[ReauthPolicy, ReauthGrantToken | None]:
        with self._writer_lock, closing(self._write()) as connection:
            try:
                if self._fresh_until(connection, session_id) is None:
                    raise BrowserReauthFreshRequired()
                device = self._device_row(connection, session_id)
                if device is None:
                    raise BrowserReauthConflict()
                device_id = str(device["id"])
                if int(device["policy_version"]) != expected_version:
                    raise BrowserReauthConflict()
                if enabled and self._passkey_count(connection, device_id) == 0:
                    raise BrowserReauthConflict()
                connection.execute(
                    """UPDATE browser_devices
                         SET lock_enabled = ?, policy_version = policy_version + 1
                       WHERE id = ?""",
                    (1 if enabled else 0, device_id),
                )
                # 改本机开关：本机所有旧授权一律失效；其他设备不受影响。
                connection.execute(
                    "DELETE FROM browser_reauth_grants WHERE device_id = ?", (device_id,)
                )
                if not enabled:
                    # 关闭即清除本机登记；保持递增版本，阻止旧挑战或旧 CAS 复用。
                    connection.execute(
                        f"DELETE FROM browser_webauthn_challenges WHERE session_id IN ({_DEVICE_SESSIONS})",
                        (device_id,),
                    )
                    connection.execute(
                        f"DELETE FROM browser_reauth_sessions WHERE session_id IN ({_DEVICE_SESSIONS})",
                        (device_id,),
                    )
                    connection.execute(
                        "DELETE FROM browser_passkeys WHERE device_id = ?", (device_id,)
                    )
                policy = self._policy_of(connection, session_id)
                token: ReauthGrantToken | None = None
                if enabled:
                    # 刚刚完成新鲜验证的这一条会话直接解锁，不让用户开完立刻被锁在门外。
                    fresh = connection.execute(
                        """SELECT fresh_passkey_id, fresh_method
                           FROM browser_reauth_sessions WHERE session_id = ?""",
                        (session_id,),
                    ).fetchone()
                    method: UnlockMethod = (
                        "password" if fresh["fresh_method"] == "password" else "passkey"
                    )
                    token = self._issue_grant(
                        connection,
                        session_id,
                        policy,
                        method=method,
                        passkey_id=None if method == "password" else str(fresh["fresh_passkey_id"]),
                    )
                    connection.execute(
                        f"DELETE FROM browser_reauth_notices WHERE session_id IN ({_DEVICE_SESSIONS})",
                        (device_id,),
                    )
                self._audit(
                    connection,
                    "browser_reauth_enabled" if enabled else "browser_reauth_disabled",
                    device_id,
                )
                connection.commit()
                return policy, token
            except BaseException:
                connection.rollback()
                raise

    def delete_passkey(self, session_id: str, passkey_id: str) -> bool:
        with self._writer_lock, closing(self._write()) as connection:
            try:
                if self._fresh_until(connection, session_id) is None:
                    raise BrowserReauthFreshRequired()
                device = self._device_row(connection, session_id)
                if device is None:
                    connection.rollback()
                    return False
                device_id = str(device["id"])
                target = connection.execute(
                    "SELECT 1 FROM browser_passkeys WHERE id = ? AND device_id = ?",
                    (passkey_id, device_id),
                ).fetchone()
                if target is None:
                    connection.rollback()
                    return False
                if bool(device["lock_enabled"]) and self._passkey_count(connection, device_id) <= 1:
                    # 删最后一枚 = 只剩口令能解；先关锁或先重新录入。
                    raise BrowserReauthConflict()
                # 外键级联：由这枚凭据签出的授权随之失效。
                connection.execute("DELETE FROM browser_passkeys WHERE id = ?", (passkey_id,))
                self._audit(connection, "browser_passkey_deleted", passkey_id)
                connection.commit()
                return True
            except BaseException:
                connection.rollback()
                raise

    def dismiss_notice(self, session_id: str) -> None:
        with self._writer_lock, closing(self._write()) as connection:
            try:
                connection.execute(
                    "DELETE FROM browser_reauth_notices WHERE session_id = ?", (session_id,)
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    # ------------------------------------------------------------ 运维兜底

    def reset(self) -> None:
        """服务器所有者的受控恢复：关闭所有设备的应用锁，删除全部通行密钥、挑战与授权。

        设备行与会话绑定保留（版本递增），同一浏览器下次登录仍认得出来。只由本机
        运维命令调用；不提供任何「持旧 Cookie 一键关闭」的网页入口。
        """
        with self._writer_lock, closing(self._write()) as connection:
            try:
                for table in (
                    "browser_reauth_grants",
                    "browser_webauthn_challenges",
                    "browser_reauth_sessions",
                    "browser_reauth_notices",
                    "browser_passkeys",
                ):
                    connection.execute(f"DELETE FROM {table}")
                connection.execute(
                    "UPDATE browser_devices SET lock_enabled = 0, policy_version = policy_version + 1"
                )
                self._audit(connection, "browser_reauth_reset", None)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise


__all__ = [
    "DEFAULT_CHALLENGE_SECONDS",
    "DEFAULT_DEVICE_COOKIE_SECONDS",
    "DEFAULT_FRESH_SECONDS",
    "DEFAULT_GRANT_SECONDS",
    "REAUTH_TABLES",
    "SqliteBrowserReauthStore",
    "clean_label",
]
