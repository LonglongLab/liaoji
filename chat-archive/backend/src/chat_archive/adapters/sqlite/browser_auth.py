"""浏览器登录状态（仅自托管远程端，PSU-2.29）。

与 `SqliteDeviceAuthStore` 同住 `auth.sqlite`，但表是分开的：设备令牌那一套服务
同步客户端，这一套服务人。两者唯一的共同点是单写者约束——同一个 API 进程持有
`SingleWriterLease`，所以两个 store 各开各的连接不会互相踩到（`BEGIN IMMEDIATE`
+ busy_timeout 兜住短暂重叠）。

落盘的东西：口令只有 scrypt 摘要与 salt；会话只有 sha256；恢复码只有共用 salt 下的
scrypt 摘要；TOTP 密钥必须可逆，所以用数据目录里**自己那把**主密钥 AES-GCM 封起来
（不和对象存储凭据共用一把——泄一把不该读到另一把）。退避的来源也只存 sha256：
分桶只要求「同一个来源落同一个桶」，不要求认得出那是谁。
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from chat_archive.domain.client_info import BrowserClientInfo, DeviceKind

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from chat_archive.ports.browser_auth import (
    BootstrapToken,
    BrowserAuthRejected,
    BrowserAuthState,
    BrowserAuthStorageError,
    BrowserAuthThrottled,
    BrowserPasswordRejected,
    BrowserSessionContext,
    BrowserSessionSummary,
    BrowserSessionToken,
    BrowserSetupClosed,
    TotpEnrollment,
)
from chat_archive.adapters.sqlite.guards import (
    BoundedWriterLock,
    StatementGuardConnection,
    install_statement_guard,
)
from chat_archive.server import passwords, totp
from chat_archive.server.secret_box import MasterKey
from chat_archive.server.security import (
    constant_time_equal,
    generate_opaque_id,
    generate_secret_hex,
    sha256_hex,
)

MASTER_KEY_NAME = "browser-auth-master.key"
_MASTER_KEY_VERSION = b"v1\0"
_KEY_VERSION = 1
_AEAD_CONTEXT = b"chat-archive-browser-totp-v1"
_CURSOR_DOMAIN = "chat-archive-browser-cursor-v1\0"

DEFAULT_IDLE_SECONDS = 30 * 24 * 60 * 60
DEFAULT_ABSOLUTE_SECONDS = 180 * 24 * 60 * 60
# 1/2/4/8/16/30/30…：第 n 次连续失败之后要等 min(2^(n-1), 30) 秒。
_MAX_BACKOFF_SECONDS = 30
# 一天没再失败过的来源没有保留价值，顺手清掉，免得这张表随扫描无限长大。
_ATTEMPT_RETENTION_SECONDS = 24 * 60 * 60
_MAX_SOURCE_LENGTH = 256
_MAX_SECOND_FACTOR_LENGTH = 64
_MAX_BOOTSTRAP_TOKEN_LENGTH = 256


@dataclass(frozen=True)
class _FactorMatch:
    """第二因子命中了什么：某一步 TOTP，或某一行恢复码。"""

    kind: Literal["totp", "recovery"]
    value: int


def backoff_seconds(failure_count: int) -> int:
    """连续失败 n 次后的等待秒数；封顶 30 秒，永不变成锁定。"""
    if failure_count <= 0:
        return 0
    if failure_count >= _MAX_BACKOFF_SECONDS.bit_length() + 4:
        return _MAX_BACKOFF_SECONDS
    return min(2 ** (failure_count - 1), _MAX_BACKOFF_SECONDS)


class SqliteBrowserAuthStore:
    """本部署唯一的一份浏览器身份：一个口令、可选一个 TOTP、若干会话。"""

    def __init__(
        self,
        database_path: Path,
        data_dir: Path | None = None,
        *,
        now: Callable[[], int] | None = None,
        busy_timeout_ms: int = 5_000,
        idle_seconds: int = DEFAULT_IDLE_SECONDS,
        absolute_seconds: int = DEFAULT_ABSOLUTE_SECONDS,
        issuer: str = "聊记归档",
        account: str = "browser",
    ) -> None:
        if busy_timeout_ms <= 0 or idle_seconds <= 0 or absolute_seconds <= 0:
            raise ValueError("browser auth limits must be positive")
        if absolute_seconds < idle_seconds:
            raise ValueError("absolute session lifetime must not be shorter than the idle one")
        self._database_path = database_path
        self._data_dir = database_path.parent if data_dir is None else data_dir
        self._now = now if now is not None else (lambda: int(time.time()))
        self._busy_timeout_ms = busy_timeout_ms
        self._idle_seconds = idle_seconds
        self._absolute_seconds = absolute_seconds
        self._issuer = issuer
        self._account = account
        # 同一把有界写锁（PSU-2.98）：11 个写入口，第三次抄写同一个裸锁模式。
        self._writer_lock = BoundedWriterLock(name="browser-auth")
        self._data_dir.mkdir(parents=True, exist_ok=True)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._master_key = MasterKey(
            self._data_dir / MASTER_KEY_NAME,
            version=_MASTER_KEY_VERSION,
            error=BrowserAuthStorageError,
        )
        self._initialize()

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
        install_statement_guard(connection, name="browser-auth")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS browser_identity (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    password_salt BLOB NOT NULL,
                    password_digest BLOB NOT NULL,
                    kdf TEXT NOT NULL,
                    totp_enabled INTEGER NOT NULL CHECK (totp_enabled IN (0, 1)),
                    totp_key_version INTEGER,
                    totp_nonce BLOB,
                    totp_ciphertext BLOB,
                    totp_consumed_step INTEGER,
                    recovery_salt BLOB,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS browser_bootstrap_tokens (
                    id TEXT PRIMARY KEY,
                    secret_hash TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('active', 'used')),
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS browser_sessions (
                    id TEXT PRIMARY KEY,
                    secret_hash TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    last_seen_at INTEGER NOT NULL,
                    absolute_expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS browser_login_attempts (
                    source_hash TEXT PRIMARY KEY,
                    failure_count INTEGER NOT NULL,
                    last_failure_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS browser_session_clients (
                    session_id TEXT PRIMARY KEY REFERENCES browser_sessions(id) ON DELETE CASCADE,
                    model TEXT,
                    platform TEXT,
                    browser TEXT,
                    device_type TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS browser_recovery_codes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    digest BLOB NOT NULL,
                    consumed_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS browser_totp_enrollment (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    session_id TEXT NOT NULL,
                    key_version INTEGER NOT NULL,
                    nonce BLOB NOT NULL,
                    ciphertext BLOB NOT NULL,
                    created_at INTEGER NOT NULL
                );
                -- 与 device_auth.py 逐字相同的一份。审计写在同一张表上，
                -- 但两个 store 的构造顺序不该成为隐式前提：谁先到谁建。
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
        event: str,
        target_id: str | None,
    ) -> None:
        """写进设备侧那张 `security_audit`，好让两套身份共用一条时间线。

        `archive_id` 记成 `browser`：这一套身份不属于任何一个归档，硬塞一个
        归档 id 进去会让后来读审计的人以为它是某个归档的设备。
        """
        connection.execute(
            """INSERT INTO security_audit
                 (archive_id, event, actor_device_id, target_id, created_at)
               VALUES ('browser', ?, NULL, ?, ?)""",
            (event, target_id, self._now()),
        )

    def _identity(self, connection: sqlite3.Connection) -> sqlite3.Row | None:
        return connection.execute("SELECT * FROM browser_identity WHERE id = 1").fetchone()

    # -----------------------------------------------------------------状态

    def state(self) -> BrowserAuthState:
        with closing(self._connect()) as connection:
            row = self._identity(connection)
        if row is None:
            return BrowserAuthState(initialized=False, totp_enabled=False)
        return BrowserAuthState(initialized=True, totp_enabled=bool(row["totp_enabled"]))

    # --------------------------------------------------------------- 退避

    @staticmethod
    def _source_hash(source: str) -> str:
        normalized = source.strip()[:_MAX_SOURCE_LENGTH] or "unknown"
        return sha256_hex(f"browser-login-source\0{normalized}")

    def _guard_backoff(self, connection: sqlite3.Connection, source_hash: str) -> None:
        row = connection.execute(
            "SELECT failure_count, last_failure_at FROM browser_login_attempts WHERE source_hash = ?",
            (source_hash,),
        ).fetchone()
        if row is None:
            return
        wait = backoff_seconds(int(row["failure_count"]))
        remaining = int(row["last_failure_at"]) + wait - self._now()
        if remaining > 0:
            # 退避窗口内**不做慢哈希**再拒绝。先算后拒等于让任何人都能用一个
            # 错口令换走 32 MiB scrypt——那是自己给自己造的 DoS。代价是窗口
            # 里连正确口令也一并挡住；退避按来源分桶，所以挡住的只是这个来源。
            raise BrowserAuthThrottled(remaining)

    def _record_failure(self, connection: sqlite3.Connection, source_hash: str) -> None:
        now = self._now()
        connection.execute(
            "DELETE FROM browser_login_attempts WHERE last_failure_at < ?",
            (now - _ATTEMPT_RETENTION_SECONDS,),
        )
        connection.execute(
            """INSERT INTO browser_login_attempts (source_hash, failure_count, last_failure_at)
               VALUES (?, 1, ?)
               ON CONFLICT(source_hash) DO UPDATE
                 SET failure_count = failure_count + 1, last_failure_at = excluded.last_failure_at""",
            (source_hash, now),
        )
        # 只记时间与来源摘要，绝不记录尝试内容。
        self._audit(connection, "browser_login_failed", source_hash[:16])

    def _clear_failures(self, connection: sqlite3.Connection, source_hash: str) -> None:
        connection.execute(
            "DELETE FROM browser_login_attempts WHERE source_hash = ?", (source_hash,)
        )

    # ------------------------------------------------------------ 引导令牌

    def issue_bootstrap_token(self) -> BootstrapToken:
        token_id = generate_opaque_id()
        secret = generate_secret_hex()
        with self._writer_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if self._identity(connection) is not None:
                    connection.rollback()
                    raise BrowserSetupClosed()
                # 重启且仍未初始化就换新：旧令牌可能已经躺在某处日志里。
                connection.execute("DELETE FROM browser_bootstrap_tokens")
                connection.execute(
                    """INSERT INTO browser_bootstrap_tokens (id, secret_hash, status, created_at)
                       VALUES (?, ?, 'active', ?)""",
                    (token_id, sha256_hex(secret), self._now()),
                )
                self._audit(connection, "browser_bootstrap_token_issued", token_id)
                connection.commit()
            except BrowserSetupClosed:
                raise
            except BaseException:
                connection.rollback()
                raise
        return BootstrapToken(value=f"{token_id}.{secret}")

    def _redeem_bootstrap_token(
        self, connection: sqlite3.Connection, bootstrap_token: str
    ) -> None:
        if len(bootstrap_token) > _MAX_BOOTSTRAP_TOKEN_LENGTH:
            raise BrowserAuthRejected()
        separator = bootstrap_token.find(".")
        if separator <= 0 or separator == len(bootstrap_token) - 1:
            raise BrowserAuthRejected()
        token_id = bootstrap_token[:separator]
        secret = bootstrap_token[separator + 1 :]
        row = connection.execute(
            "SELECT secret_hash, status FROM browser_bootstrap_tokens WHERE id = ?",
            (token_id,),
        ).fetchone()
        if (
            row is None
            or row["status"] != "active"
            or not constant_time_equal(sha256_hex(secret), str(row["secret_hash"]))
        ):
            raise BrowserAuthRejected()
        consumed = connection.execute(
            "UPDATE browser_bootstrap_tokens SET status = 'used' WHERE id = ? AND status = 'active'",
            (token_id,),
        )
        if consumed.rowcount != 1:
            raise BrowserAuthRejected()

    # ------------------------------------------------------------- 首次向导

    def complete_bootstrap(
        self,
        bootstrap_token: str,
        password: str,
        *,
        source: str,
    ) -> BrowserSessionToken:
        source_hash = self._source_hash(source)
        salt = passwords.new_salt()
        with self._writer_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._guard_backoff(connection, source_hash)
                if self._identity(connection) is not None:
                    connection.rollback()
                    raise BrowserSetupClosed()
                try:
                    self._redeem_bootstrap_token(connection, bootstrap_token)
                except BrowserAuthRejected:
                    # 令牌错也计退避：这是这道门上唯一的秘密。
                    self._record_failure(connection, source_hash)
                    connection.commit()
                    raise
                reason = passwords.rejection_reason(password)
                if reason is not None:
                    # 口令是用户自己刚选的，说清楚哪里不合规不构成泄漏；
                    # 但令牌已经作废了，所以这里必须回滚，别让他丢了令牌。
                    connection.rollback()
                    raise BrowserPasswordRejected(reason)
                now = self._now()
                connection.execute(
                    """INSERT INTO browser_identity
                         (id, password_salt, password_digest, kdf, totp_enabled,
                          totp_key_version, totp_nonce, totp_ciphertext, totp_consumed_step,
                          recovery_salt, created_at, updated_at)
                       VALUES (1, ?, ?, ?, 0, NULL, NULL, NULL, NULL, NULL, ?, ?)""",
                    (
                        salt,
                        passwords.derive(password, salt),
                        json.dumps(passwords.scrypt_parameters(), sort_keys=True),
                        now,
                        now,
                    ),
                )
                self._clear_failures(connection, source_hash)
                self._audit(connection, "browser_identity_created", None)
                token = self._start_session(connection)
                connection.commit()
                return token
            except (BrowserAuthRejected, BrowserAuthThrottled, BrowserPasswordRejected):
                raise
            except BrowserSetupClosed:
                raise
            except BaseException:
                connection.rollback()
                raise

    # ----------------------------------------------------------------- 登录

    def login(
        self,
        password: str,
        second_factor: str | None,
        *,
        source: str,
    ) -> BrowserSessionToken:
        source_hash = self._source_hash(source)
        with self._writer_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._guard_backoff(connection, source_hash)
                identity = self._identity(connection)
                if identity is None:
                    connection.rollback()
                    raise BrowserAuthRejected()
                password_ok = passwords.verify(
                    password,
                    bytes(identity["password_salt"]),
                    bytes(identity["password_digest"]),
                )
                match: _FactorMatch | None = None
                if bool(identity["totp_enabled"]):
                    # **口令对不对不影响这里做多少活**。从前是「口令对了才验第二
                    # 因子」，于是一次恢复码形状的提交在口令正确时要多做一次
                    # 32 MiB scrypt——实测 744ms vs 1568ms，毫无重叠。统一的拒绝
                    # 文案挡住了「哪一半错了」，这个时间差却把它原样说了出来。
                    # 现在慢哈希的次数只取决于提交内容的形状（六位数字还是恢复码
                    # 模样），那是攻击者自己写的，不含任何信息。
                    match = self._match_second_factor(connection, identity, second_factor)
                accepted = password_ok and (
                    not bool(identity["totp_enabled"]) or match is not None
                )
                if not accepted:
                    self._record_failure(connection, source_hash)
                    connection.commit()
                    raise BrowserAuthRejected()
                # 副作用只在整体通过之后落。口令错也消费第二因子的话，任何人都
                # 能拿一串错口令把机主的恢复码一个个烧掉。
                self._apply_second_factor(connection, match)
                self._clear_failures(connection, source_hash)
                token = self._start_session(connection)
                connection.commit()
                return token
            except (BrowserAuthRejected, BrowserAuthThrottled):
                raise
            except BaseException:
                connection.rollback()
                raise

    def _match_second_factor(
        self,
        connection: sqlite3.Connection,
        identity: sqlite3.Row,
        second_factor: str | None,
    ) -> _FactorMatch | None:
        """第二因子落在哪儿——**只判定，不写库**。

        判定与副作用分开，是为了让「做多少慢哈希」只取决于提交内容的形状，
        而副作用只在口令也对了之后才落。两件事都做不到的话，要么泄漏口令
        对不对（时间差），要么让错口令也能烧掉恢复码。
        """
        if second_factor is None or len(second_factor) > _MAX_SECOND_FACTOR_LENGTH:
            return None
        secret = self._decrypt_totp(identity)
        step = totp.matching_step(secret, second_factor, self._now())
        if step is not None:
            consumed = identity["totp_consumed_step"]
            # 用过的那一步（以及更早的）一律不认：同一个码 30 秒内本来可以
            # 重放第二次，记下窗口才真的一次一用。
            if consumed is not None and step <= int(consumed):
                return None
            return _FactorMatch(kind="totp", value=step)
        return self._match_recovery_code(connection, identity, second_factor)

    def _match_recovery_code(
        self,
        connection: sqlite3.Connection,
        identity: sqlite3.Row,
        candidate: str,
    ) -> _FactorMatch | None:
        salt = identity["recovery_salt"]
        if salt is None or not totp.looks_like_recovery_code(candidate):
            return None
        # 十个码共用一个 salt，于是一次 scrypt 够了。salt 的作用是挡预计算，
        # 一个就够；逐码一个 salt 会把一次验证变成十次 32 MiB 推导。
        digest = passwords.derive(totp.normalize_recovery_code(candidate), bytes(salt))
        rows = connection.execute(
            "SELECT id, digest FROM browser_recovery_codes WHERE consumed_at IS NULL"
        ).fetchall()
        matched: int | None = None
        for row in rows:
            if constant_time_equal(digest.hex(), bytes(row["digest"]).hex()):
                matched = int(row["id"])
        return None if matched is None else _FactorMatch(kind="recovery", value=matched)

    def _apply_second_factor(
        self,
        connection: sqlite3.Connection,
        match: _FactorMatch | None,
    ) -> None:
        if match is None:
            return
        if match.kind == "totp":
            connection.execute(
                "UPDATE browser_identity SET totp_consumed_step = ?, updated_at = ? WHERE id = 1",
                (match.value, self._now()),
            )
            return
        connection.execute(
            "UPDATE browser_recovery_codes SET consumed_at = ? WHERE id = ?",
            (self._now(), match.value),
        )
        self._audit(connection, "browser_recovery_code_used", None)

    # ----------------------------------------------------------------- 会话

    def _start_session(self, connection: sqlite3.Connection) -> BrowserSessionToken:
        session_id = f"bs_{generate_opaque_id()}"
        secret = generate_secret_hex()
        now = self._now()
        absolute = now + self._absolute_seconds
        connection.execute(
            """INSERT INTO browser_sessions
                 (id, secret_hash, created_at, last_seen_at, absolute_expires_at)
               VALUES (?, ?, ?, ?, ?)""",
            (session_id, sha256_hex(secret), now, now, absolute),
        )
        self._audit(connection, "browser_session_started", session_id)
        return BrowserSessionToken(
            session_id=session_id,
            value=f"{session_id}.{secret}",
            idle_expires_at=now + self._idle_seconds,
            absolute_expires_at=absolute,
        )

    def authenticate(
        self, cookie_value: str, *, touch: bool = True
    ) -> BrowserSessionContext | None:
        """认出会话。`touch=False` 只校验、不顺延空闲期限（PSU-2.213 §4.2）：

        再认证开启而本会话仍锁定时，一个开着的锁定标签页不停轮询，不能把
        30 天空闲期一路续到 180 天绝对期限。
        """
        separator = cookie_value.find(".")
        if separator <= 0 or separator == len(cookie_value) - 1:
            return None
        session_id = cookie_value[:separator]
        secret = cookie_value[separator + 1 :]
        with self._writer_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """SELECT secret_hash, last_seen_at, absolute_expires_at
                       FROM browser_sessions WHERE id = ?""",
                    (session_id,),
                ).fetchone()
                if row is None or not constant_time_equal(
                    sha256_hex(secret), str(row["secret_hash"])
                ):
                    connection.rollback()
                    return None
                now = self._now()
                expired = (
                    now >= int(row["absolute_expires_at"])
                    or now >= int(row["last_seen_at"]) + self._idle_seconds
                )
                if expired:
                    # 过期就地删除：留着只会让「设备与会话」页显示一条永远
                    # 用不了的记录。删了下一次请求就是干净的 401。
                    connection.execute("DELETE FROM browser_sessions WHERE id = ?", (session_id,))
                    connection.commit()
                    return None
                if touch:
                    connection.execute(
                        "UPDATE browser_sessions SET last_seen_at = ? WHERE id = ?",
                        (now, session_id),
                    )
                identity = self._identity(connection)
                connection.commit()
                return BrowserSessionContext(
                    session_id=session_id,
                    cursor_key=sha256_hex(f"{_CURSOR_DOMAIN}{row['secret_hash']}"),
                    totp_enabled=identity is not None and bool(identity["totp_enabled"]),
                )
            except BaseException:
                connection.rollback()
                raise

    def touch(self, session_id: str) -> None:
        """严格门禁通过之后才顺延空闲期限；只动仍在有效期内的那一行。"""
        now = self._now()
        with self._writer_lock, closing(self._connect()) as connection:
            connection.execute(
                """UPDATE browser_sessions SET last_seen_at = ?
                   WHERE id = ? AND absolute_expires_at > ? AND last_seen_at + ? > ?""",
                (now, session_id, now, self._idle_seconds, now),
            )
            connection.commit()

    def confirm_password(self, password: str, *, source: str) -> None:
        """重新确认原账户口令（登记通行密钥前用）；与登录同一套按来源退避。"""
        source_hash = self._source_hash(source)
        with self._writer_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._guard_backoff(connection, source_hash)
                identity = self._identity(connection)
                if identity is None or not passwords.verify(
                    password,
                    bytes(identity["password_salt"]),
                    bytes(identity["password_digest"]),
                ):
                    self._record_failure(connection, source_hash)
                    connection.commit()
                    raise BrowserAuthRejected()
                self._clear_failures(connection, source_hash)
                connection.commit()
            except (BrowserAuthRejected, BrowserAuthThrottled):
                raise
            except BaseException:
                connection.rollback()
                raise

    def confirm_full_mfa(self, password: str, second_factor: str, *, source: str) -> None:
        """在线恢复再认证前的完整 MFA：口令＋TOTP（或一次性恢复码）。

        与登录同一套按来源退避与「慢哈希次数只看提交形状」的写法；第二因子只在
        整体通过后才消费。账户未开启 TOTP 时一律拒绝——单口令不够在线拆锁，
        只能走运维命令（PSU-2.213 §5.3／§14.6）。
        """
        source_hash = self._source_hash(source)
        with self._writer_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._guard_backoff(connection, source_hash)
                identity = self._identity(connection)
                if identity is None or not bool(identity["totp_enabled"]):
                    connection.rollback()
                    raise BrowserAuthRejected()
                password_ok = passwords.verify(
                    password,
                    bytes(identity["password_salt"]),
                    bytes(identity["password_digest"]),
                )
                match = self._match_second_factor(connection, identity, second_factor)
                if not (password_ok and match is not None):
                    self._record_failure(connection, source_hash)
                    connection.commit()
                    raise BrowserAuthRejected()
                self._apply_second_factor(connection, match)
                self._clear_failures(connection, source_hash)
                self._audit(connection, "browser_reauth_recovery_verified", None)
                connection.commit()
            except (BrowserAuthRejected, BrowserAuthThrottled):
                raise
            except BaseException:
                connection.rollback()
                raise

    def revoke_other_sessions(self, keep_session_id: str) -> int:
        with self._writer_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    "DELETE FROM browser_sessions WHERE id <> ?", (keep_session_id,)
                )
                self._audit(connection, "browser_sessions_revoked_others", keep_session_id)
                connection.commit()
                return cursor.rowcount
            except BaseException:
                connection.rollback()
                raise

    def logout(self, session_id: str) -> None:
        self.revoke_session(session_id)

    def list_sessions(self) -> list[BrowserSessionSummary]:
        """还活着的会话。

        已过期的行只在下一次 `authenticate` 时才被清掉，所以列表必须自己过滤：
        把一条下一次请求就会 401 的会话显示成「在用」是谎报，而这一屏正是用来
        回答「现在有谁能进来」的。
        """
        now = self._now()
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT s.id, s.created_at, s.last_seen_at, s.absolute_expires_at,
                          c.session_id AS client_id, c.model, c.platform, c.browser, c.device_type
                   FROM browser_sessions s LEFT JOIN browser_session_clients c ON c.session_id = s.id
                   WHERE s.absolute_expires_at > ? AND s.last_seen_at + ? > ?
                   ORDER BY s.created_at ASC, s.id ASC""",
                (now, self._idle_seconds, now),
            ).fetchall()
        return [
            BrowserSessionSummary(
                id=str(row["id"]),
                created_at=int(row["created_at"]),
                last_seen_at=int(row["last_seen_at"]),
                idle_expires_at=int(row["last_seen_at"]) + self._idle_seconds,
                absolute_expires_at=int(row["absolute_expires_at"]),
                client_info=None if row["client_id"] is None else BrowserClientInfo(
                    model=row["model"], platform=row["platform"], browser=row["browser"],
                    device_type=cast(DeviceKind, row["device_type"]),
                ),
            )
            for row in rows
        ]

    def update_client_info(self, session_id: str, info: BrowserClientInfo) -> None:
        now = self._now()
        with self._writer_lock, closing(self._connect()) as connection:
            connection.execute(
                """INSERT INTO browser_session_clients (session_id, model, platform, browser, device_type)
                   SELECT id, ?, ?, ?, ? FROM browser_sessions
                   WHERE id = ? AND absolute_expires_at > ? AND last_seen_at + ? > ?
                   ON CONFLICT(session_id) DO UPDATE SET
                     model = COALESCE(excluded.model, browser_session_clients.model),
                     platform = COALESCE(excluded.platform, browser_session_clients.platform),
                     browser = COALESCE(excluded.browser, browser_session_clients.browser),
                     device_type = CASE WHEN excluded.device_type = 'unknown'
                       THEN browser_session_clients.device_type ELSE excluded.device_type END""",
                (info.model, info.platform, info.browser, info.device_type,
                 session_id, now, self._idle_seconds, now),
            )
            connection.commit()

    def revoke_session(self, session_id: str) -> bool:
        with self._writer_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    "DELETE FROM browser_sessions WHERE id = ?", (session_id,)
                )
                if cursor.rowcount == 1:
                    self._audit(connection, "browser_session_revoked", session_id)
                connection.commit()
                return cursor.rowcount == 1
            except BaseException:
                connection.rollback()
                raise

    def revoke_all_sessions(self) -> int:
        with self._writer_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute("DELETE FROM browser_sessions")
                self._audit(connection, "browser_sessions_revoked_all", None)
                connection.commit()
                return cursor.rowcount
            except BaseException:
                connection.rollback()
                raise

    # ----------------------------------------------------------------- TOTP

    def _encrypt_totp(self, secret: str) -> tuple[bytes, bytes]:
        key = self._master_key.load_or_create()
        nonce = secrets.token_bytes(12)
        return nonce, AESGCM(key).encrypt(nonce, secret.encode("ascii"), _AEAD_CONTEXT)

    def _decrypt_blob(self, key_version: object, nonce: object, ciphertext: object) -> str:
        # sqlite3 的 BLOB 读出来是 bytes；列可能是 NULL（没绑过），也可能是
        # 将来某个版本写的。两种都不是「密钥错了」，而是「这行读不出来」——
        # 都收进同一个存储错误，免得界面把它说成口令不对让人反复重试。
        if (
            not isinstance(key_version, int)
            or key_version != _KEY_VERSION
            or not isinstance(nonce, bytes)
            or not isinstance(ciphertext, bytes)
        ):
            raise BrowserAuthStorageError("stored secret is unusable")
        key = self._master_key.load_existing()
        try:
            payload = AESGCM(key).decrypt(nonce, ciphertext, _AEAD_CONTEXT)
        except InvalidTag:
            raise BrowserAuthStorageError("master key cannot decrypt the stored secret") from None
        return payload.decode("ascii")

    def _decrypt_totp(self, identity: sqlite3.Row) -> str:
        return self._decrypt_blob(
            identity["totp_key_version"],
            identity["totp_nonce"],
            identity["totp_ciphertext"],
        )

    def begin_totp_enrollment(
        self,
        session_id: str,
        password: str,
        *,
        source: str,
    ) -> TotpEnrollment:
        """绑定两步验证要口令，和解绑一样。

        只凭一条会话就能绑上**攻击者自己的**验证器：机主此后拿着正确口令也进不来，
        因为他没有那个验证码——一次会话窃取就变成一次需要碰服务器才能解开的永久锁死。
        开与关两头都要口令，这条路才不通。
        """
        source_hash = self._source_hash(source)
        secret = totp.generate_secret()
        nonce, ciphertext = self._encrypt_totp(secret)
        with self._writer_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._guard_backoff(connection, source_hash)
                identity = self._identity(connection)
                if identity is None or bool(identity["totp_enabled"]):
                    connection.rollback()
                    raise BrowserAuthRejected()
                if not passwords.verify(
                    password,
                    bytes(identity["password_salt"]),
                    bytes(identity["password_digest"]),
                ):
                    self._record_failure(connection, source_hash)
                    connection.commit()
                    raise BrowserAuthRejected()
                self._clear_failures(connection, source_hash)
                connection.execute("DELETE FROM browser_totp_enrollment")
                connection.execute(
                    """INSERT INTO browser_totp_enrollment
                         (id, session_id, key_version, nonce, ciphertext, created_at)
                       VALUES (1, ?, ?, ?, ?, ?)""",
                    (session_id, _KEY_VERSION, nonce, ciphertext, self._now()),
                )
                connection.commit()
            except (BrowserAuthRejected, BrowserAuthThrottled):
                raise
            except BaseException:
                connection.rollback()
                raise
        return TotpEnrollment(
            secret=secret,
            otpauth_uri=totp.otpauth_uri(secret, account=self._account, issuer=self._issuer),
        )

    def enable_totp(self, session_id: str, code: str) -> tuple[str, ...]:
        recovery_codes = totp.generate_recovery_codes()
        recovery_salt = passwords.new_salt()
        with self._writer_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                identity = self._identity(connection)
                pending = connection.execute(
                    "SELECT * FROM browser_totp_enrollment WHERE id = 1"
                ).fetchone()
                if (
                    identity is None
                    or bool(identity["totp_enabled"])
                    or pending is None
                    or str(pending["session_id"]) != session_id
                ):
                    connection.rollback()
                    raise BrowserAuthRejected()
                secret = self._decrypt_blob(
                    pending["key_version"], pending["nonce"], pending["ciphertext"]
                )
                step = totp.matching_step(secret, code, self._now())
                if step is None:
                    # 回填不上就不开启。扫码失败却已开启 = 把自己锁在门外。
                    connection.rollback()
                    raise BrowserAuthRejected()
                now = self._now()
                connection.execute(
                    """UPDATE browser_identity
                         SET totp_enabled = 1, totp_key_version = ?, totp_nonce = ?,
                             totp_ciphertext = ?, totp_consumed_step = ?, recovery_salt = ?,
                             updated_at = ?
                       WHERE id = 1""",
                    (
                        int(pending["key_version"]),
                        bytes(pending["nonce"]),
                        bytes(pending["ciphertext"]),
                        step,
                        recovery_salt,
                        now,
                    ),
                )
                connection.execute("DELETE FROM browser_totp_enrollment")
                connection.execute("DELETE FROM browser_recovery_codes")
                connection.executemany(
                    "INSERT INTO browser_recovery_codes (digest, consumed_at) VALUES (?, NULL)",
                    [
                        (passwords.derive(totp.normalize_recovery_code(item), recovery_salt),)
                        for item in recovery_codes
                    ],
                )
                self._audit(connection, "browser_totp_enabled", None)
                connection.commit()
                return recovery_codes
            except BrowserAuthRejected:
                raise
            except BaseException:
                connection.rollback()
                raise

    def disable_totp(self, session_id: str, password: str, *, source: str) -> None:
        del session_id
        source_hash = self._source_hash(source)
        with self._writer_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._guard_backoff(connection, source_hash)
                identity = self._identity(connection)
                if identity is None or not passwords.verify(
                    password,
                    bytes(identity["password_salt"]),
                    bytes(identity["password_digest"]),
                ):
                    self._record_failure(connection, source_hash)
                    connection.commit()
                    raise BrowserAuthRejected()
                self._clear_failures(connection, source_hash)
                self._clear_totp(connection)
                connection.commit()
            except (BrowserAuthRejected, BrowserAuthThrottled):
                raise
            except BaseException:
                connection.rollback()
                raise

    def _clear_totp(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """UPDATE browser_identity
                 SET totp_enabled = 0, totp_key_version = NULL, totp_nonce = NULL,
                     totp_ciphertext = NULL, totp_consumed_step = NULL,
                     recovery_salt = NULL, updated_at = ?
               WHERE id = 1""",
            (self._now(),),
        )
        connection.execute("DELETE FROM browser_recovery_codes")
        connection.execute("DELETE FROM browser_totp_enrollment")
        self._audit(connection, "browser_totp_disabled", None)

    # ------------------------------------------------------------ 运维兜底

    def clear_totp(self) -> bool:
        """解绑 TOTP，保留口令与会话。仅本机运维命令调用。"""
        with self._writer_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if self._identity(connection) is None:
                    connection.rollback()
                    return False
                self._clear_totp(connection)
                connection.commit()
                return True
            except BaseException:
                connection.rollback()
                raise

    def reset_to_bootstrap(self) -> BootstrapToken:
        """丢掉口令、TOTP 与全部会话，重开首次向导。仅本机运维命令调用。

        与首次部署共用同一条防抢注路径：重置之后能进来的仍然只有拿到新令牌的人。
        """
        with self._writer_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("DELETE FROM browser_identity")
                connection.execute("DELETE FROM browser_sessions")
                connection.execute("DELETE FROM browser_recovery_codes")
                connection.execute("DELETE FROM browser_totp_enrollment")
                connection.execute("DELETE FROM browser_login_attempts")
                connection.execute("DELETE FROM browser_bootstrap_tokens")
                # 再认证状态（PSU-2.213）属于被重置的这份身份：口令都重来了，
                # 旧通行密钥与已开启的策略不能留下来把新机主锁在门外。
                for table in (
                    "browser_reauth_grants",
                    "browser_webauthn_challenges",
                    "browser_reauth_sessions",
                    "browser_passkeys",
                    "browser_reauth_policy",
                ):
                    present = connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                        (table,),
                    ).fetchone()
                    if present is not None:
                        connection.execute(f"DELETE FROM {table}")
                self._audit(connection, "browser_identity_reset", None)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return self.issue_bootstrap_token()


__all__ = ["MASTER_KEY_NAME", "SqliteBrowserAuthStore", "backoff_seconds"]
