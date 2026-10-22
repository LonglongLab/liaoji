"""备份目标的配置与运行账本（PSU-2.140）。

形状照抄 `object_storage_config.py`：凭据是 AES-256-GCM 密文，密钥是服务器自己
在数据目录里铸的 `0600` 文件，摘要类型**没有放秘密的字段**，所以回显不是"避免了"
而是结构上不可能。

**为什么不复用附件那套凭据。** 附件那条路被打穿就够得到备份，备份就不再是备份。
所以这里是独立的一份配置、独立的一把主密钥、并且建议指向独立的桶和一个
只有 Put/Get/Head、**没有 Delete** 的子账号。允许指到同一个桶的另一个前缀——
代码不在乎，但那是用户的选择，不是默认。

**前缀不许落在 `a/` 下面。** `S3CompatibleObjectStore.list_object_checksums` 按
`Prefix="a/<archive_id>/"` 列桶来做清单对账，落进去的备份对象会让**每一次
`reconcile` 都把它报成 orphaned**。反过来说，别的前缀对 reconcile 是隐形的，
所以"同桶不同前缀"在技术上是安全的。
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from chat_archive.server.object_storage_config import (
    ObjectStorageConfigurationError,
    access_key_hint,
    validate_configuration,
)
from chat_archive.server.secret_box import MasterKey

BACKUP_DATABASE = "backup.sqlite"

_MASTER_KEY_NAME = "backup-master.key"
_MASTER_KEY_VERSION = b"v1\0"
_KEY_VERSION = 1
_AEAD_CONTEXT = b"chat-archive-backup-v1"

RETENTION_LIFECYCLE = "lifecycle"
RETENTION_APPLICATION = "application"
RETENTION_MODES = (RETENTION_LIFECYCLE, RETENTION_APPLICATION)

# 无论多旧，份数不得低于这个数。保留策略的三条硬约束之一——另外两条
# （先验证新的再删旧的、永不删最新一份）在 `backup_archive.apply_retention` 里。
MINIMUM_RETAINED = 3

DEFAULT_INTERVAL_HOURS = 24
DEFAULT_DAILY_KEEP = 7
DEFAULT_WEEKLY_KEEP = 4
DEFAULT_MONTHLY_KEEP = 12

_MAX_INTERVAL_HOURS = 24 * 30
# 段是 `[a-z0-9_-]`，段间一个斜杠，不许前导/尾随/连续斜杠。
_PREFIX_PATTERN = re.compile(r"^[a-z0-9_-]{1,32}(?:/[a-z0-9_-]{1,32}){0,3}$")

# 超过这么多个周期还没成功过，就是「备份悄悄停了」。两个周期而不是一个：
# 一轮晚了几小时是正常的（机器重启、上一轮跑久了），连着两轮没有才是坏了。
_OVERDUE_INTERVALS = 2

RUN_RUNNING = "running"
RUN_SUCCESS = "success"
RUN_FAILED = "failed"
# 进程没了、线程随之没了的那一轮，启动时被收尾成这个代号。
RUN_INTERRUPTED_CODE = "BACKUP_INTERRUPTED"
# `running` 这一行多久之后不再算"有人在跑"。真机上一趟 1.04 GiB 的快照连传带核
# 4 分 21 秒（2026-09-18 实测），所以六小时是留给"慢得离谱但仍在跑"的余量，
# 而不是给被 kill 掉的进程留的坟——那种由 `abandon_running` 在 API 启动时收尾。
RUN_STALE_AFTER_SECONDS = 6 * 60 * 60


class BackupConfigurationError(RuntimeError):
    """备份目标配置非法，或存着的那份解不开。"""


class BackupAlreadyRunningError(BackupConfigurationError):
    """另一趟备份正在跑——不是失败，是这一趟不该开始。"""


class BackupNotConfiguredError(BackupConfigurationError):
    """还没有人配置过备份目标。"""


@dataclass(frozen=True)
class BackupCredentials:
    """解密后的凭据。永不序列化、永不进日志。"""

    endpoint: str
    bucket: str
    region: str
    prefix: str
    secret_id: str
    secret_key: str


@dataclass(frozen=True)
class BackupPolicy:
    """多久备一次、谁负责删旧的。没有秘密。"""

    interval_hours: int = DEFAULT_INTERVAL_HOURS
    retention_mode: str = RETENTION_LIFECYCLE
    daily_keep: int = DEFAULT_DAILY_KEEP
    weekly_keep: int = DEFAULT_WEEKLY_KEEP
    monthly_keep: int = DEFAULT_MONTHLY_KEEP


@dataclass(frozen=True)
class BackupRun:
    """账本上的一行。`failure_code` 是稳定代号，从来不是 provider 原话。"""

    id: int
    started_at: int
    finished_at: int | None
    status: str
    backup_id: str | None
    source_bytes: int | None
    stored_bytes: int | None
    failure_code: str | None
    verified: bool
    # 这一轮之后桶上的生命周期规则是否盖住了备份前缀。`None` = 没问（application
    # 模式）或问不到（密钥读不了 lifecycle 配置）。只有 `False` 是「规则被删了」。
    lifecycle_covered: bool | None = None


@dataclass(frozen=True)
class FailureStreak:
    """上次成功之后的连败。`count == 0` 就没有退避可言。"""

    count: int
    last_finished_at: int | None


@dataclass(frozen=True)
class BackupConfigSummary:
    """界面能看到的全部。刻意没有放秘密的字段。"""

    configured: bool
    endpoint: str | None = None
    bucket: str | None = None
    region: str | None = None
    prefix: str | None = None
    access_key_hint: str | None = None
    updated_at: int | None = None
    policy: BackupPolicy = BackupPolicy()
    # 存着的凭据这台机器解不解得开。恢复之后必然是 False：`backup-master.key`
    # 故意不进备份。此时 `configured` 仍为 True——端点/桶/前缀是明文列，能预填——
    # 但 worker 每一轮都会跳过。不把它说出来，界面显示「已配置」而备份永远不跑，
    # 要等两个周期后 `backup_overdue` 才响。
    credentials_readable: bool = True


def validate_prefix(prefix: str) -> None:
    """前缀必须是良形的，而且不能落在附件那块地盘上。"""
    if _PREFIX_PATTERN.fullmatch(prefix) is None:
        raise BackupConfigurationError("backup prefix is invalid")
    if prefix == "a" or prefix.startswith("a/"):
        # 落在 `a/` 下会让每一次 reconcile 都把备份报成 orphaned。
        raise BackupConfigurationError("backup prefix must not live under the attachment prefix")


def validate_policy(policy: BackupPolicy) -> None:
    if not 1 <= policy.interval_hours <= _MAX_INTERVAL_HOURS:
        raise BackupConfigurationError("backup interval is invalid")
    if policy.retention_mode not in RETENTION_MODES:
        raise BackupConfigurationError("backup retention mode is invalid")
    if policy.daily_keep < MINIMUM_RETAINED:
        raise BackupConfigurationError(
            f"backup retention must keep at least {MINIMUM_RETAINED} daily copies"
        )
    if policy.weekly_keep < 0 or policy.monthly_keep < 0:
        raise BackupConfigurationError("backup retention counts must not be negative")


def validate_target(
    *,
    endpoint: str,
    bucket: str,
    region: str,
    prefix: str,
    secret_id: str,
    secret_key: str,
) -> None:
    """端点/桶/区域/凭据的规则与附件那套**同一份**，不另立一套会烂掉的副本。"""
    try:
        validate_configuration(
            endpoint=endpoint,
            bucket=bucket,
            region=region,
            secret_id=secret_id,
            secret_key=secret_key,
        )
    except ObjectStorageConfigurationError as error:
        raise BackupConfigurationError(str(error)) from None
    validate_prefix(prefix)


class SqliteBackupConfigStore:
    """备份目标配置 + 运行账本，住在 `backup.sqlite`。"""

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
            error=BackupConfigurationError,
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
                CREATE TABLE IF NOT EXISTS backup_config (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    endpoint TEXT NOT NULL,
                    bucket TEXT NOT NULL,
                    region TEXT NOT NULL,
                    prefix TEXT NOT NULL,
                    access_key_hint TEXT NOT NULL,
                    interval_hours INTEGER NOT NULL CHECK (interval_hours >= 1),
                    retention_mode TEXT NOT NULL,
                    daily_keep INTEGER NOT NULL CHECK (daily_keep >= 3),
                    weekly_keep INTEGER NOT NULL CHECK (weekly_keep >= 0),
                    monthly_keep INTEGER NOT NULL CHECK (monthly_keep >= 0),
                    key_version INTEGER NOT NULL,
                    nonce BLOB NOT NULL,
                    ciphertext BLOB NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS backup_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at INTEGER NOT NULL,
                    finished_at INTEGER,
                    status TEXT NOT NULL,
                    backup_id TEXT,
                    source_bytes INTEGER,
                    stored_bytes INTEGER,
                    failure_code TEXT,
                    verified INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS backup_runs_started_at
                    ON backup_runs (started_at DESC);
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(backup_runs)").fetchall()
            }
            if "lifecycle_covered" not in columns:
                # 评审后加的列（2026-09-17）。这个库随 PSU-2.140 一起上过线的话
                # 已经有 `backup_runs` 表，CREATE IF NOT EXISTS 不会补列。
                connection.execute("ALTER TABLE backup_runs ADD COLUMN lifecycle_covered INTEGER")
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
            raise BackupConfigurationError("master key version is unsupported")
        key = self._master_key.load_existing()
        try:
            payload = AESGCM(key).decrypt(
                bytes(row["nonce"]), bytes(row["ciphertext"]), _AEAD_CONTEXT
            )
        except InvalidTag:
            raise BackupConfigurationError(
                "master key cannot decrypt the stored backup configuration"
            ) from None
        try:
            value: object = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise BackupConfigurationError("stored backup configuration is unreadable") from None
        if not isinstance(value, dict):
            raise BackupConfigurationError("stored backup configuration is unreadable")
        decoded = cast(dict[str, object], value)
        secret_id = decoded.get("secret_id")
        secret_key = decoded.get("secret_key")
        if not isinstance(secret_id, str) or not isinstance(secret_key, str):
            raise BackupConfigurationError("stored backup configuration is unreadable")
        return secret_id, secret_key

    def _current_row(self, connection: sqlite3.Connection) -> sqlite3.Row | None:
        return connection.execute("SELECT * FROM backup_config WHERE id = 1").fetchone()

    @staticmethod
    def _policy(row: sqlite3.Row) -> BackupPolicy:
        return BackupPolicy(
            interval_hours=int(row["interval_hours"]),
            retention_mode=str(row["retention_mode"]),
            daily_keep=int(row["daily_keep"]),
            weekly_keep=int(row["weekly_keep"]),
            monthly_keep=int(row["monthly_keep"]),
        )

    def save(
        self,
        *,
        endpoint: str,
        bucket: str,
        region: str,
        prefix: str,
        secret_id: str,
        secret_key: str,
        policy: BackupPolicy = BackupPolicy(),
    ) -> BackupConfigSummary:
        validate_target(
            endpoint=endpoint,
            bucket=bucket,
            region=region,
            prefix=prefix,
            secret_id=secret_id,
            secret_key=secret_key,
        )
        validate_policy(policy)
        nonce, ciphertext = self._encrypt(secret_id, secret_key)
        updated_at = self._now()
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO backup_config (
                    id, endpoint, bucket, region, prefix, access_key_hint,
                    interval_hours, retention_mode, daily_keep, weekly_keep,
                    monthly_keep, key_version, nonce, ciphertext, updated_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    endpoint = excluded.endpoint,
                    bucket = excluded.bucket,
                    region = excluded.region,
                    prefix = excluded.prefix,
                    access_key_hint = excluded.access_key_hint,
                    interval_hours = excluded.interval_hours,
                    retention_mode = excluded.retention_mode,
                    daily_keep = excluded.daily_keep,
                    weekly_keep = excluded.weekly_keep,
                    monthly_keep = excluded.monthly_keep,
                    key_version = excluded.key_version,
                    nonce = excluded.nonce,
                    ciphertext = excluded.ciphertext,
                    updated_at = excluded.updated_at
                """,
                (
                    endpoint,
                    bucket,
                    region,
                    prefix,
                    access_key_hint(secret_id),
                    policy.interval_hours,
                    policy.retention_mode,
                    policy.daily_keep,
                    policy.weekly_keep,
                    policy.monthly_keep,
                    _KEY_VERSION,
                    nonce,
                    ciphertext,
                    updated_at,
                ),
            )
            connection.commit()
        return self.summary()

    def summary(self) -> BackupConfigSummary:
        with self._lock, closing(self._connect()) as connection:
            row = self._current_row(connection)
        if row is None:
            return BackupConfigSummary(configured=False)
        try:
            self._decrypt(row)
            readable = True
        except BackupConfigurationError:
            readable = False
        return BackupConfigSummary(
            configured=True,
            endpoint=str(row["endpoint"]),
            bucket=str(row["bucket"]),
            region=str(row["region"]),
            prefix=str(row["prefix"]),
            access_key_hint=str(row["access_key_hint"]),
            updated_at=int(row["updated_at"]),
            policy=self._policy(row),
            credentials_readable=readable,
        )

    def policy(self) -> BackupPolicy | None:
        with self._lock, closing(self._connect()) as connection:
            row = self._current_row(connection)
        return None if row is None else self._policy(row)

    def disable(self) -> None:
        """Stop future runs and forget credentials; retain ledger and all remote objects."""
        with self._lock, closing(self._connect()) as connection:
            connection.execute("DELETE FROM backup_config WHERE id = 1")
            connection.commit()

    def load_credentials(self) -> BackupCredentials | None:
        """解不开就抛；没配过返回 None。两者对调用方意味着不同的处置。"""
        with self._lock, closing(self._connect()) as connection:
            row = self._current_row(connection)
        if row is None:
            return None
        secret_id, secret_key = self._decrypt(row)
        return BackupCredentials(
            endpoint=str(row["endpoint"]),
            bucket=str(row["bucket"]),
            region=str(row["region"]),
            prefix=str(row["prefix"]),
            secret_id=secret_id,
            secret_key=secret_key,
        )

    def require_credentials(self) -> BackupCredentials:
        credentials = self.load_credentials()
        if credentials is None:
            raise BackupNotConfiguredError("backup target is not configured")
        return credentials

    def begin_run(self) -> int:
        """开一行账，**同时挡住第二个并发的快照**（PSU-2.142）。

        `backup now` 跑在运维自己的进程里，定时线程跑在 API 进程里——两者没有
        任何共享的内存锁。2026-09-18 上线时这两个就撞上了：手动那一趟刚开始
        37 秒后定时线程也判到点了，于是一台 2 核机器同时做两份 1 GB 快照、
        往桶里传两份 330 MB。两趟都成功，所以这不是数据事故；但活儿翻倍、
        桶里多一份当天的备份，而且手动那一趟看起来像卡住了。

        账本就是两个进程唯一的共同状态，所以闸设在这里：`running` 那一行
        **在事务里**看一眼。**只挡年轻的那种**——被 kill 掉的进程留下的
        `running` 会一直在那儿（要等 API 重启时 `abandon_running` 收尾），
        拿它无限期地挡住以后每一次备份，那是把一次崩溃变成永久停摆。
        """
        with self._lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = self._now()
            running = connection.execute(
                "SELECT id, started_at FROM backup_runs WHERE status = ? "
                "ORDER BY started_at DESC LIMIT 1",
                (RUN_RUNNING,),
            ).fetchone()
            if running is not None and now - int(running["started_at"]) < RUN_STALE_AFTER_SECONDS:
                connection.rollback()
                raise BackupAlreadyRunningError(
                    f"backup run {int(running['id'])} is already running"
                )
            cursor = connection.execute(
                "INSERT INTO backup_runs (started_at, status) VALUES (?, ?)",
                (now, RUN_RUNNING),
            )
            connection.commit()
        return int(cursor.lastrowid or 0)

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        backup_id: str | None = None,
        source_bytes: int | None = None,
        stored_bytes: int | None = None,
        failure_code: str | None = None,
        verified: bool = False,
        lifecycle_covered: bool | None = None,
    ) -> None:
        if status not in (RUN_SUCCESS, RUN_FAILED):
            raise BackupConfigurationError("backup run status is invalid")
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE backup_runs
                   SET finished_at = ?, status = ?, backup_id = ?,
                       source_bytes = ?, stored_bytes = ?, failure_code = ?,
                       verified = ?, lifecycle_covered = ?
                 WHERE id = ?
                """,
                (
                    self._now(),
                    status,
                    backup_id,
                    source_bytes,
                    stored_bytes,
                    failure_code,
                    1 if verified else 0,
                    None if lifecycle_covered is None else (1 if lifecycle_covered else 0),
                    run_id,
                ),
            )
            connection.commit()

    @staticmethod
    def _run(row: sqlite3.Row) -> BackupRun:
        finished = row["finished_at"]
        return BackupRun(
            id=int(row["id"]),
            started_at=int(row["started_at"]),
            finished_at=None if finished is None else int(finished),
            status=str(row["status"]),
            backup_id=None if row["backup_id"] is None else str(row["backup_id"]),
            source_bytes=None if row["source_bytes"] is None else int(row["source_bytes"]),
            stored_bytes=None if row["stored_bytes"] is None else int(row["stored_bytes"]),
            failure_code=None if row["failure_code"] is None else str(row["failure_code"]),
            verified=bool(row["verified"]),
            lifecycle_covered=(
                None if row["lifecycle_covered"] is None else bool(row["lifecycle_covered"])
            ),
        )

    def recent_runs(self, limit: int = 20) -> Sequence[BackupRun]:
        bounded = max(1, min(limit, 200))
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM backup_runs ORDER BY started_at DESC, id DESC LIMIT ?",
                (bounded,),
            ).fetchall()
        return tuple(self._run(row) for row in rows)

    def last_success(self) -> BackupRun | None:
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM backup_runs
                 WHERE status = ?
                 ORDER BY finished_at DESC, id DESC
                 LIMIT 1
                """,
                (RUN_SUCCESS,),
            ).fetchone()
        return None if row is None else self._run(row)

    def failure_streak(self) -> FailureStreak:
        """上一次成功之后连着失败了几次、最后一次是什么时候。

        worker 的退避按它算。`BACKUP_INTERRUPTED` 不计：那是进程没了，不是备份
        本身失败，而且 README 承诺过「错过的窗口在启动五分钟后补跑」。
        """
        with self._lock, closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count, MAX(finished_at) AS last_finished_at
                  FROM backup_runs
                 WHERE status = ?
                   AND (failure_code IS NULL OR failure_code != ?)
                   AND id > COALESCE(
                         (SELECT MAX(id) FROM backup_runs WHERE status = ?), 0)
                """,
                (RUN_FAILED, RUN_INTERRUPTED_CODE, RUN_SUCCESS),
            ).fetchone()
        count = 0 if row is None else int(row["count"])
        last = None if row is None or row["last_finished_at"] is None else int(row["last_finished_at"])
        return FailureStreak(count=count, last_finished_at=last)

    def abandon_running(self) -> int:
        """启动时把上次进程留下的 `running` 收尾成失败。

        它们的线程随进程一起没了，留着会让"上次备份"永远显示在跑。
        """
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE backup_runs
                   SET status = ?, finished_at = ?, failure_code = ?
                 WHERE status = ?
                """,
                (RUN_FAILED, self._now(), RUN_INTERRUPTED_CODE, RUN_RUNNING),
            )
            connection.commit()
        return cursor.rowcount


def backup_status_document(
    store: SqliteBackupConfigStore,
    *,
    now: Callable[[], int] | None = None,
) -> dict[str, object]:
    """健康页要的那几个键。

    `last_backup_status` 有五种值，刻意分得比"成功/失败"细：
    `not_configured` 还没开始、`never` 配了但一次都没跑过、`running` 正在跑、
    `success` / `failed` 是最近一轮的结论。把前两种混成 `failed` 会让人去查一个
    不存在的故障；混成 `success` 则是撒谎。

    `backup_overdue` **在这一端算**，因为只有这一端知道自己的周期。让客户端拿
    `last_backup_at` 去猜一个阈值，就是让它据一个它没有的数下结论——超过两个
    周期还没成功，才是"备份悄悄停了"这件事的定义，而周期写在这台部署的策略里。

    第六种值 `locked`（评审后加）：配了、但这台机器解不开存着的凭据——恢复之后
    必然如此，因为主密钥故意不进备份。它压过最近一轮的结论：账本里那些成功是
    另一台机器的，这台一轮都跑不了，说「上一轮成功」是撒谎。

    `backup_lifecycle_covered`（评审后加）：最近一次成功之后桶上的过期规则是否
    盖住了备份前缀。`None` = 不适用或问不到；只有 `False` 是「规则被删了、备份在
    闷头堆积」——lifecycle 模式的静默失败就是这一种，所以它要进健康页而不是只在
    配置那一刻看得到。
    """
    summary = store.summary()
    if not summary.configured:
        return {
            "last_backup_at": None,
            "last_backup_bytes": None,
            "last_backup_status": "not_configured",
            "backup_retention_mode": None,
            # 没配置不是"过期"，那是另一条 issue。混成一个会让人去修一个
            # 还不存在的东西。
            "backup_overdue": False,
            "backup_lifecycle_covered": None,
        }
    recent = store.recent_runs(1)
    last = store.last_success()
    moment = (lambda: max(1, int(time.time()))) if now is None else now
    window = summary.policy.interval_hours * 3600 * _OVERDUE_INTERVALS
    if last is None or last.finished_at is None:
        # 配了却一次都没成功过。刚配完的那一刻也落在这里，所以给一个周期的宽限：
        # 第一轮还没到点就报"过期"，只会教人忽略这个告警。
        reference = summary.updated_at
        overdue = reference is not None and moment() - reference > window
    else:
        overdue = moment() - last.finished_at > window
    if not summary.credentials_readable:
        status = "locked"
    else:
        status = recent[0].status if recent else "never"
    covered: bool | None = None
    if summary.policy.retention_mode == RETENTION_LIFECYCLE and last is not None:
        covered = last.lifecycle_covered
    return {
        "last_backup_at": None if last is None else last.finished_at,
        "last_backup_bytes": None if last is None else last.stored_bytes,
        "last_backup_status": status,
        "backup_retention_mode": summary.policy.retention_mode,
        "backup_overdue": overdue,
        "backup_lifecycle_covered": covered,
    }


__all__ = [
    "BACKUP_DATABASE",
    "DEFAULT_DAILY_KEEP",
    "DEFAULT_INTERVAL_HOURS",
    "DEFAULT_MONTHLY_KEEP",
    "DEFAULT_WEEKLY_KEEP",
    "MINIMUM_RETAINED",
    "RETENTION_APPLICATION",
    "RETENTION_LIFECYCLE",
    "RETENTION_MODES",
    "RUN_FAILED",
    "RUN_INTERRUPTED_CODE",
    "RUN_RUNNING",
    "RUN_STALE_AFTER_SECONDS",
    "RUN_SUCCESS",
    "BackupAlreadyRunningError",
    "BackupConfigSummary",
    "BackupConfigurationError",
    "BackupCredentials",
    "BackupNotConfiguredError",
    "BackupPolicy",
    "BackupRun",
    "FailureStreak",
    "SqliteBackupConfigStore",
    "backup_status_document",
    "validate_policy",
    "validate_prefix",
    "validate_target",
]
