"""把一份备份还原成一个能直接当数据卷用的目录（PSU-2.140）。

还原**只写空目录**。覆盖一个正在服务的数据卷是这类命令最容易造成的、也是最不可逆的
事故，所以这里没有 `--force`：要覆盖就自己把旧目录挪走，那一步应该由人决定。

还原出来的目录里**没有 `*-master.key`**（备份里本来就没有）。这是刻意的，后果写在
`RESTORE_NOTES` 里，命令会把它们打出来：对象存储凭据要重填、浏览器两步验证要重绑。
`build_object_storage` 早就承诺过这种情形照常启动、只是报 `not_configured`——
消息可读这件事不会被解不开的附件配置拖下水。
"""

from __future__ import annotations

import gzip
import hashlib
from dataclasses import dataclass
from pathlib import Path

from chat_archive.server.backup_archive import (
    TIER_DAILY,
    BackupArchiveError,
    BackupManifest,
    object_key,
    read_manifest,
)
from chat_archive.server.backup_snapshot import (
    BackupSnapshotError,
    SnapshotStats,
    read_stats,
    stats_document,
    verify_snapshot,
)
from chat_archive.server.backup_store import BackupStore

_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class RestoreResult:
    target: Path
    manifest: BackupManifest
    stats: SnapshotStats
    notes: tuple[str, ...]


def _require_empty_target(target: Path) -> None:
    if target.exists():
        if not target.is_dir():
            raise BackupArchiveError("restore target must be a directory")
        if any(target.iterdir()):
            raise BackupArchiveError("restore target must be empty")
        return
    target.mkdir(parents=True)


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def restore_backup(
    *,
    store: BackupStore,
    prefix: str,
    backup_id: str,
    target: Path,
    tier: str = TIER_DAILY,
) -> RestoreResult:
    """下载、解压、逐段核 sha256、整体自检，最后对一遍清单里的计数。"""
    manifest = read_manifest(store, prefix, tier, backup_id)
    _require_empty_target(target)
    for item in manifest.files:
        packed = target / item.object_name
        store.download(object_key(prefix, tier, backup_id, item.object_name), packed)
        if packed.stat().st_size != item.stored_size:
            raise BackupArchiveError(f"{item.name} downloaded with an unexpected size")
        restored = target / item.name
        with gzip.open(packed, "rb") as source, restored.open("wb") as destination:
            while chunk := source.read(_CHUNK):
                destination.write(chunk)
        packed.unlink()
        if _digest(restored) != item.sha256:
            # 这里失败意味着传输或存储把字节改了。留着这个文件没有意义，
            # 而留着会让下一步"目录非空"的守卫把人挡在外面。
            restored.unlink()
            raise BackupArchiveError(f"{item.name} does not match its recorded checksum")
    names = tuple(item.name for item in manifest.files)
    try:
        verify_snapshot(target, names)
    except BackupSnapshotError as error:
        raise BackupArchiveError(str(error)) from None
    stats = read_stats(target)
    restored_document = stats_document(stats)
    for key, expected in manifest.stats.items():
        if restored_document.get(key) != expected:
            raise BackupArchiveError(f"restored {key} does not match the manifest")
    return RestoreResult(
        target=target,
        manifest=manifest,
        stats=stats,
        notes=tuple(manifest.restore_notes),
    )


__all__ = ["RestoreResult", "restore_backup"]
