"""把数据卷里该备的那几个库照成一致的一套（PSU-2.140）。

## 备哪几个

`search.sqlite` **不备**。它是线上最大的一个文件（2026-09-17 实测 2.43 GiB，
占备份体积的 70%），而 `remote_search.py` 写得很清楚：索引由归档库喂出来、
`upsert_many` 幂等、掉了的窗口由 `SearchBackfillWorker` 的水位回填补齐。
备它等于每天多传一份能免费重算的东西。代价是恢复后搜索降级到回填追平为止。

`*-master.key` **不备**。`secret_box.py` 的模块文档原话是
"a 32-byte AES-GCM key the server mints for itself and nobody ever backs up"——
把密钥和它守护的密文放进同一个桶等于没锁。后果是恢复后要重填对象存储凭据、
重绑浏览器两步验证，这写在清单的 `restore_notes` 里。

`object-storage.sqlite` 与 `backup.sqlite` **要备**，尽管没有主密钥就解不开凭据：
它们的 `endpoint`/`bucket`/`region` 是**明文列**，而
`object_storage_config.next_generation_for` 只在三者全同时复用当前 storage
generation。备着它，恢复后原样填回同一个桶就还是原来那一代，存量清单继续有效；
填错一个字就升代号，全部存量报 `OBJECT_STORAGE_GENERATION_MISMATCH`。

## 顺序是有讲究的：后照的必须是超集

几个库不是同一时刻照的，所以顺序决定会不会留下悬空引用：

1. `metadata.sqlite` 引用 blob manifest、引用 device、引用 archive
2. `blob-state.sqlite` 比 1 新 → 清单是超集 → 不会有"消息指向不存在的清单"
3. `auth.sqlite` 比 1 新 → 设备与归档名单是超集 → `device_cursors` 不悬空

多出来的清单/设备是无害的——那本来就是"已上传但还没被引用"的正常态。少一条才是坏账。
**把顺序倒过来，用例会红**（`test_backup_snapshot.py`）。

## 用页级拷贝，不用 VACUUM INTO

计划里原本写的是 `VACUUM INTO`。改了，理由是量出来的：线上 `metadata.sqlite`
的 `freelist_count = 0`，VACUUM 一个字节也压不掉，却要重建每一个 btree 和索引；
而 `Connection.backup()` 是页级拷贝，在那台 2 核机器上便宜得多。
`control_snapshot.py` 用的也是它，形状一致。

`pages=-1`（默认）一次拷完，全程持一个读事务，所以拷出来的是一致快照，
不会出现"拷到一半被写"的撕裂；代价是这段时间 WAL 不能 checkpoint、会涨一点。
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

# 顺序即语义：后面的必须是前面的超集。别按字母重排。
SNAPSHOT_DATABASES: tuple[str, ...] = (
    "metadata.sqlite",
    "blob-state.sqlite",
    "auth.sqlite",
    "object-storage.sqlite",
    "backup.sqlite",
    # 在线分享（PSU-2.180）。不备的话，恢复之后「我的分享」一行不剩，而对象存储里
    # 那些副本就成了没人记得、也永远不会被清掉的对象。它不引用前面几个库里的任何行。
    "shares.sqlite",
)

# 少了这几个的备份不值得存：没有 metadata 就没有消息，没有 blob-state 就
# 认不出对象存储里那十几万个对象，没有 auth 就连"这台部署托管着哪些归档"
# 都不知道（`list_archives` 的唯一来源）。
REQUIRED_DATABASES: frozenset[str] = frozenset(
    {"metadata.sqlite", "blob-state.sqlite", "auth.sqlite"}
)

# 快照要占的临时空间按源文件总和的这个倍数估，再加一个固定余量。
# 页级拷贝的产物与源同量级，倍数是给 WAL 里那些还没 checkpoint 的页留的。
SNAPSHOT_SIZE_FACTOR = 1.2
SNAPSHOT_SIZE_HEADROOM = 512 * 1024 * 1024

_CHUNK = 1024 * 1024


class DiskUsage(Protocol):
    """`shutil.disk_usage` 回的那个具名元组里，这里只用得到一个字段。"""

    @property
    def free(self) -> int: ...


class BackupSnapshotError(RuntimeError):
    """快照失败。消息只含结构性事实，不含路径以外的任何内容。"""


class BackupInsufficientDiskError(BackupSnapshotError):
    """盘不够，一个字节都还没写就停下了。"""


@dataclass(frozen=True)
class SnapshotFile:
    name: str
    path: Path
    size: int
    sha256: str


@dataclass(frozen=True)
class SnapshotStats:
    """清单里那些"这份备份里有多少东西"的数。没有正文，没有标识以外的字符串。"""

    archives: tuple[str, ...]
    message_count: int
    conversation_count: int
    last_server_sequence: int
    blob_manifest_count: int
    device_count: int


@dataclass(frozen=True)
class SnapshotSet:
    directory: Path
    files: tuple[SnapshotFile, ...]
    stats: SnapshotStats

    @property
    def total_bytes(self) -> int:
        return sum(item.size for item in self.files)


def present_databases(data_dir: Path) -> tuple[str, ...]:
    """按固定顺序列出这台部署真有的那几个库。"""
    present: list[str] = []
    for name in SNAPSHOT_DATABASES:
        candidate = data_dir / name
        if candidate.is_file():
            present.append(name)
    missing = REQUIRED_DATABASES - set(present)
    if missing:
        raise BackupSnapshotError(
            f"data directory is missing {len(missing)} required database(s)"
        )
    return tuple(present)


def estimate_required_bytes(data_dir: Path) -> int:
    total = 0
    for name in present_databases(data_dir):
        total += (data_dir / name).stat().st_size
        for suffix in ("-wal", "-shm"):
            sidecar = data_dir / f"{name}{suffix}"
            if sidecar.is_file():
                total += sidecar.stat().st_size
    return int(total * SNAPSHOT_SIZE_FACTOR) + SNAPSHOT_SIZE_HEADROOM


def preflight_disk(
    data_dir: Path,
    staging_parent: Path,
    *,
    disk_usage: Callable[[Path], DiskUsage] = shutil.disk_usage,
) -> int:
    """盘不够就在动第一个字节之前失败。

    线上系统盘 2026-09-17 只剩 6.0 G，而一次快照要 ~1 GB 临时空间。这条不是装饰：
    写到一半没空间会既没有备份、又把服务器推到磁盘满。
    """
    required = estimate_required_bytes(data_dir)
    free = int(disk_usage(staging_parent).free)
    if free < required:
        raise BackupInsufficientDiskError(
            f"backup needs {required} bytes of staging space, {free} available"
        )
    return required


def _copy_database(source: Path, destination: Path) -> None:
    """页级在线拷贝。源只读打开。

    这里**不**自检：`create_snapshot` 拷完整套之后由 `verify_snapshot` 跑一遍完整的
    `integrity_check`，它包含 `quick_check` 的全部检查。此前这里多跑一次 `quick_check`，
    等于把 1 GB 的 metadata.sqlite 在一台 2 核机器上白读一遍。
    """
    source_uri = f"{source.as_uri()}?mode=ro"
    with (
        closing(sqlite3.connect(source_uri, uri=True)) as source_connection,
        closing(sqlite3.connect(destination)) as destination_connection,
    ):
        source_connection.execute("PRAGMA query_only = ON")
        try:
            source_connection.backup(destination_connection)
        except sqlite3.Error:
            raise BackupSnapshotError(f"{source.name} could not be copied") from None


def _digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(_CHUNK):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _scalar(connection: sqlite3.Connection, statement: str) -> int:
    row = connection.execute(statement).fetchone()
    if row is None or row[0] is None:
        return 0
    return int(row[0])


def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def read_stats(directory: Path) -> SnapshotStats:
    """从快照自己身上读计数——读源库会得到一个和快照对不上的数。"""
    archives: list[str] = []
    messages = 0
    conversations = 0
    sequence = 0
    metadata_path = directory / "metadata.sqlite"
    if metadata_path.is_file():
        with closing(sqlite3.connect(f"{metadata_path.as_uri()}?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            if _has_table(connection, "archive_stats"):
                for row in connection.execute(
                    "SELECT archive_id, message_count, conversation_count,"
                    " last_server_sequence FROM archive_stats ORDER BY archive_id"
                ):
                    archives.append(str(row["archive_id"]))
                    messages += int(row["message_count"])
                    conversations += int(row["conversation_count"])
                    sequence = max(sequence, int(row["last_server_sequence"]))
    manifests = 0
    blob_path = directory / "blob-state.sqlite"
    if blob_path.is_file():
        with closing(sqlite3.connect(f"{blob_path.as_uri()}?mode=ro", uri=True)) as connection:
            if _has_table(connection, "server_blob_manifests"):
                manifests = _scalar(connection, "SELECT COUNT(*) FROM server_blob_manifests")
    devices = 0
    auth_path = directory / "auth.sqlite"
    if auth_path.is_file():
        with closing(sqlite3.connect(f"{auth_path.as_uri()}?mode=ro", uri=True)) as connection:
            if _has_table(connection, "devices"):
                devices = _scalar(connection, "SELECT COUNT(*) FROM devices")
    return SnapshotStats(
        archives=tuple(archives),
        message_count=messages,
        conversation_count=conversations,
        last_server_sequence=sequence,
        blob_manifest_count=manifests,
        device_count=devices,
    )


def verify_snapshot(directory: Path, names: Sequence[str] | None = None) -> None:
    """对着快照目录跑完整自检。恢复路径与备份路径用的是**同一个**函数。"""
    candidates = tuple(names) if names is not None else tuple(
        name for name in SNAPSHOT_DATABASES if (directory / name).is_file()
    )
    missing = REQUIRED_DATABASES - set(candidates)
    if missing:
        raise BackupSnapshotError(f"snapshot is missing {len(missing)} required database(s)")
    for name in candidates:
        path = directory / name
        if not path.is_file():
            raise BackupSnapshotError(f"{name} is missing from the snapshot")
        # 坏得够彻底时 SQLite 自己就抛（`database disk image is malformed`），
        # 而不是让 `integrity_check` 返回一行说明。两条路都得收成同一个异常：
        # 漏掉那一条，worker 的 `except BackupSnapshotError` 接不住，
        # 账本上那一行就会永远停在 `running`。
        try:
            with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as connection:
                integrity = connection.execute("PRAGMA integrity_check").fetchone()
                if integrity != ("ok",):
                    raise BackupSnapshotError(f"{name} failed integrity_check")
                violations = connection.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    raise BackupSnapshotError(
                        f"{name} has {len(violations)} foreign key violation(s)"
                    )
        except sqlite3.Error:
            raise BackupSnapshotError(f"{name} is not a readable database") from None


def create_snapshot(
    data_dir: Path,
    staging_dir: Path,
    *,
    disk_usage: Callable[[Path], DiskUsage] = shutil.disk_usage,
) -> SnapshotSet:
    """把该备的库按既定顺序照进 `staging_dir`，校验，返回摘要。

    `staging_dir` 必须不存在——建出来是这个函数的事，这样"目录在 = 有人在照"
    是个可靠的判据，也不会误用一个装着别的东西的目录。
    """
    if staging_dir.exists():
        raise BackupSnapshotError("staging directory already exists")
    names = present_databases(data_dir)
    staging_dir.parent.mkdir(parents=True, exist_ok=True)
    preflight_disk(data_dir, staging_dir.parent, disk_usage=disk_usage)
    staging_dir.mkdir(parents=True)
    files: list[SnapshotFile] = []
    try:
        for name in names:
            destination = staging_dir / name
            _copy_database(data_dir / name, destination)
            size, digest = _digest(destination)
            files.append(SnapshotFile(name=name, path=destination, size=size, sha256=digest))
        verify_snapshot(staging_dir, names)
        stats = read_stats(staging_dir)
    except BaseException:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise
    return SnapshotSet(directory=staging_dir, files=tuple(files), stats=stats)


def stats_document(stats: SnapshotStats) -> Mapping[str, object]:
    return {
        "archives": list(stats.archives),
        "message_count": stats.message_count,
        "conversation_count": stats.conversation_count,
        "last_server_sequence": stats.last_server_sequence,
        "blob_manifest_count": stats.blob_manifest_count,
        "device_count": stats.device_count,
    }


__all__ = [
    "REQUIRED_DATABASES",
    "SNAPSHOT_DATABASES",
    "DiskUsage",
    "BackupInsufficientDiskError",
    "BackupSnapshotError",
    "SnapshotFile",
    "SnapshotSet",
    "SnapshotStats",
    "create_snapshot",
    "estimate_required_bytes",
    "preflight_disk",
    "present_databases",
    "read_stats",
    "stats_document",
    "verify_snapshot",
]
