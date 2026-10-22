"""深度恢复验证的那份运维工件：`backup verify --deep` 写它，API 读它。

**为什么这盏灯以前恒亮。** `create_server_app` 的 `last_recovery_verified_at`
默认 `None`，而自建部署的装配从来不传它——于是
`RECOVERY_VERIFICATION_REQUIRED` 与「有没有验证过恢复」毫无关系。那个默认值
在当时是诚实的：SQLite+S3 这支既没有演练工具也没有 runbook。PSU-2.140 的
`backup verify --deep` 补上了前者（真下载、真解压、真打开、真比 stats），
这个模块只做一件事：把那一次成功留在数据卷上，让服务读得到。

**为什么是一份文件而不是一张表**——同 `reconciliation.py`：深校验是在 API
跑着的时候执行的运维命令，写 SQLite 要抢 `SingleWriterLease`，那等于要求停服。

**为什么不分归档。** 深校验校的是整份数据卷的快照（五个库一起），不是某一个
归档的一部分；`create_server_app` 的那个参数也一直是单个值。
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

RECOVERY_VERIFICATION_FILENAME = "recovery-verification.json"
RECOVERY_VERIFICATION_SCHEMA_VERSION = "chat-archive.recovery-verification.v1"


@dataclass(frozen=True)
class RecoveryVerificationRecord:
    """一次深校验：什么时候、验的是哪一个备份。

    `tier`/`backup_id` 不进健康页，但进这份工件——运维要答得出「那次到底验的
    是哪一份」，而账本在远端的 `backup.sqlite` 里、恢复之后可能已经不在手上。
    """

    verified_at: int
    tier: str
    backup_id: str


def record_path(data_dir: Path) -> Path:
    return data_dir / RECOVERY_VERIFICATION_FILENAME


def write_record(data_dir: Path, record: RecoveryVerificationRecord) -> None:
    """落盘，0600，整份替换（先写临时文件再 `replace`）。

    写坏一半的记录会让健康页读到半句话，而那时唯一诚实的结论是「没验证过」，
    不是 500——那条由 `read_record` 兜着，这里只保证不制造那种局面。
    """
    destination = record_path(data_dir)
    directory = destination.parent
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": RECOVERY_VERIFICATION_SCHEMA_VERSION,
        "verified_at": record.verified_at,
        "tier": record.tier,
        "backup_id": record.backup_id,
    }
    handle, temporary = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary, destination)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def read_record(data_dir: Path) -> RecoveryVerificationRecord | None:
    """读回上次的深校验，读不出来一律 `None`。

    坏掉的、缺字段的、版本不认的，全都是「没验证过」。**不许把读不懂的工件
    读成已验证**：这盏灯的毛病本来就是「亮得没有依据」，反过来犯一次
    「灭得没有依据」要糟得多——它是「安全卸载本地媒体缓存」的阻断项之一。
    """
    try:
        raw = record_path(data_dir).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    try:
        parsed: object = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    document = cast(dict[str, object], parsed)
    if document.get("schema_version") != RECOVERY_VERIFICATION_SCHEMA_VERSION:
        return None
    verified_at = document.get("verified_at")
    tier = document.get("tier")
    backup_id = document.get("backup_id")
    if (
        not isinstance(verified_at, int)
        or isinstance(verified_at, bool)
        or verified_at <= 0
        or not isinstance(tier, str)
        or not tier
        or not isinstance(backup_id, str)
        or not backup_id
    ):
        return None
    return RecoveryVerificationRecord(
        verified_at=verified_at, tier=tier, backup_id=backup_id
    )


def last_recovery_verified_at_reader(data_dir: Path) -> Callable[[], int | None]:
    """给 `create_server_app` 的那个可调用对象，绑定到这份数据目录。

    是可调用对象而不是常数，理由同 `last_reconciled_at`：深校验在这个进程之外
    跑，值会在进程活着的时候变。**这里不判过期**——工件说的是「上次验证通过
    的时刻」，那个时刻是多久以前算不算达标，由要下结论的那一层按自己的时钟判
    （`operational_health.py`）。
    """

    def _reader() -> int | None:
        record = read_record(data_dir)
        return None if record is None else record.verified_at

    return _reader


__all__ = [
    "RECOVERY_VERIFICATION_FILENAME",
    "RECOVERY_VERIFICATION_SCHEMA_VERSION",
    "RecoveryVerificationRecord",
    "last_recovery_verified_at_reader",
    "read_record",
    "record_path",
    "write_record",
]
