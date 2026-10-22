"""对账：已落地的清单与对象存储里真实存在的东西，双向比一遍。

**为什么是一份文件而不是一张表。** 对账是例行检查，必须能在 API 跑着的时候
执行；而写 SQLite 要取 `SingleWriterLease`，那等于要求停服。所以对账进程对
`blob-state.sqlite` 只读（`SqliteBlobStateStore(..., read_only=True)`，走
`mode=ro`，连建表迁移都不跑），把结论写成数据目录下一份小工件，API 端只读它。
单写者不变量因此一个字都不用动。

**为什么默认不比摘要。** `list_object_checksums` 是**每个对象一次 HEAD**——
真机上 79,061 次串行请求。而每条清单落地时都逐条验过摘要（`verified_at` 非空），
例行检查要答的是「东西还在不在、字节数对不对」。要全查一遍时用 `--deep`。

**「干净」必须意味着真的问过对象存储。** 前缀是从 `archive_id` 直接算出来的，
不是从清单里凑出来的——否则一个零清单的归档会让那趟 LIST 一次都不发生，
三个桶自然全空，于是「从没对过账」被写成「刚刚对过、完全干净」。这盏灯此前
的毛病就是亮得没有依据，换一种方式再犯一次没有意义。
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from chat_archive.ports.blob_state import (
    BlobStateStore,
    BlobStateValidationError,
    object_scope_for_archive,
    require_remote_id,
)
from chat_archive.ports.objectstore import ObjectStore, ObjectStoreError

RECONCILIATION_DIRECTORY = "reconciliation"
RECONCILIATION_SCHEMA_VERSION = "chat-archive.reconciliation.v1"

_REPORT_MODE_LIST = "list"
_REPORT_MODE_DEEP = "deep"
_STATE_REMOTE_READY = "remote_ready"


class ReconciliationError(RuntimeError):
    """对账没能完成——与「对账完成了、发现差异」是两件事。"""


@dataclass(frozen=True)
class ReconciliationResult:
    """一次对账的三个桶，外加这次比了多少条、又有多少条没法比。

    三个桶都空才算干净。名字与 `ObjectReconciliation` 对齐，只是中间那个在
    `list` 模式下量的是字节数、在 `deep` 模式下才是摘要——所以它叫
    `mismatches` 而不是 `checksum_mismatches`，免得报出来的差异被读成
    「摘要不符」这种更吓人的结论。
    """

    blob_count: int
    mode: str
    missing_in_store: tuple[str, ...]
    mismatches: tuple[str, ...]
    orphaned_in_store: tuple[str, ...]
    # 存放在**别代**对象存储里的清单条数。换过 bucket/region 会推进
    # `storage_generation`，老对象留在老桶里（`blob_routes.py` 已经为这种附件
    # 单独报「存放在第 N 代对象存储中」）。拿当前这个桶去对它们，每一条都会
    # 报成缺失，这盏灯从此永远点不亮。所以它们不参与比对，但**必须数出来**：
    # 「干净」在这种部署上是「能查的都对得上」，不是「全都查过了」。
    other_generation_count: int = 0

    @property
    def clean(self) -> bool:
        return not (self.missing_in_store or self.mismatches or self.orphaned_in_store)

    def issue_counts(self) -> dict[str, int]:
        return {
            "missing_in_store": len(self.missing_in_store),
            "mismatches": len(self.mismatches),
            "orphaned_in_store": len(self.orphaned_in_store),
        }


@dataclass(frozen=True)
class ReconciliationRecord:
    """「上次干净跑通是什么时候」。只有干净的那一次才写得进来。"""

    archive_id: str
    verified_at: int
    blob_count: int
    mode: str
    other_generation_count: int = 0


def reconcile_archive(
    blob_state_store: BlobStateStore,
    object_store: ObjectStore,
    archive_id: str,
    *,
    storage_generation: int,
    deep: bool = False,
) -> ReconciliationResult:
    """比一遍，返回三个桶。**不写任何东西**——记录与否是调用方的判断。

    `storage_generation` 是**当前**对象存储的代次，必须由调用方给：这个函数
    只拿得到一个 `ObjectStore`，而端口上没有「你是第几代」这个问题。给错了
    等于拿这个桶去对另一个桶的清单。
    """
    require_remote_id(archive_id, field_name="archive_id")
    manifests = blob_state_store.list_manifests(archive_id)
    # 认领名单取**全部状态**：字节先进桶、后确认成 `remote_ready`，中间那个
    # 窗口里的对象是有主的。只认 `remote_ready` 会把每一次正常上传都报成孤儿。
    claimed = {manifest.object_key for manifest in manifests}
    here = tuple(
        manifest
        for manifest in manifests
        if manifest.storage_generation == storage_generation
    )
    ready = tuple(manifest for manifest in here if manifest.state == _STATE_REMOTE_READY)
    # 前缀从 archive_id 算，不从清单凑：零清单也要真的发一趟 LIST。
    scope = object_scope_for_archive(archive_id)
    expected: Mapping[str, object]
    stored: Mapping[str, object]
    if deep:
        expected = {manifest.object_key: manifest.sha256 for manifest in ready}
        stored = _stored_checksums(object_store, scope)
        mode = _REPORT_MODE_DEEP
    else:
        expected = {manifest.object_key: manifest.size for manifest in ready}
        stored = _stored_sizes(object_store, scope)
        mode = _REPORT_MODE_LIST
    return ReconciliationResult(
        blob_count=len(ready),
        mode=mode,
        missing_in_store=tuple(sorted(set(expected) - set(stored))),
        mismatches=tuple(
            sorted(
                object_key
                for object_key in set(expected) & set(stored)
                if expected[object_key] != stored[object_key]
            )
        ),
        orphaned_in_store=tuple(sorted(set(stored) - claimed)),
        other_generation_count=len(manifests) - len(here),
    )


def _stored_sizes(object_store: ObjectStore, scope: str) -> Mapping[str, int]:
    """列一遍，列不动就是**对账没做成**，不是「对账做完了、桶是空的」。

    对象存储没配、凭据过期、网络断了都会走到这里。把它当成空桶，结论会是
    「所有清单都缺失」或者（零清单时）「完全干净」——两个都是编出来的答案。
    """
    try:
        return object_store.list_object_sizes(scope)
    except ObjectStoreError as error:
        raise ReconciliationError("object store could not be listed") from error


def _stored_checksums(object_store: ObjectStore, scope: str) -> Mapping[str, str]:
    try:
        return object_store.list_object_checksums(scope)
    except ObjectStoreError as error:
        raise ReconciliationError("object store could not be listed") from error


def record_path(data_dir: Path, archive_id: str) -> Path:
    require_remote_id(archive_id, field_name="archive_id")
    return data_dir / RECONCILIATION_DIRECTORY / f"{archive_id}.json"


def write_record(data_dir: Path, record: ReconciliationRecord) -> None:
    """把一次干净的对账落到数据卷上，0600，整份替换。

    先写临时文件再 `replace`：一份被写坏一半的记录会让健康页读到半句话，而
    健康页读不出来时的正确行为是「当作从没对过账」，不是 500——两条都在
    `read_record` 那边兜着，这里只保证不制造那种局面。
    """
    destination = record_path(data_dir, record.archive_id)
    directory = destination.parent
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": RECONCILIATION_SCHEMA_VERSION,
        "archive_id": record.archive_id,
        "verified_at": record.verified_at,
        "blob_count": record.blob_count,
        "mode": record.mode,
        "other_generation_count": record.other_generation_count,
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


def read_record(data_dir: Path, archive_id: str) -> ReconciliationRecord | None:
    """读回上次的对账，读不出来一律 `None`。

    坏掉的记录**不抛异常**：这是一份运维工件，它损坏了唯一诚实的结论是「没有
    对过账」，而不是让 `sync-health` 整个 500。归档 ID 不合法同理——那更像是
    调用方出了问题，但健康页不是发现它的地方。
    """
    try:
        raw = record_path(data_dir, archive_id).read_text(encoding="utf-8")
    except (BlobStateValidationError, OSError, ValueError):
        return None
    try:
        parsed: object = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    document = cast(dict[str, object], parsed)
    if document.get("schema_version") != RECONCILIATION_SCHEMA_VERSION:
        return None
    verified_at = document.get("verified_at")
    blob_count = document.get("blob_count")
    other_generation_count = document.get("other_generation_count")
    mode = document.get("mode")
    if (
        document.get("archive_id") != archive_id
        or not isinstance(mode, str)
        or not isinstance(verified_at, int)
        or isinstance(verified_at, bool)
        or verified_at < 0
        or not isinstance(blob_count, int)
        or isinstance(blob_count, bool)
        or blob_count < 0
        or not isinstance(other_generation_count, int)
        or isinstance(other_generation_count, bool)
        or other_generation_count < 0
    ):
        return None
    return ReconciliationRecord(
        archive_id=archive_id,
        verified_at=verified_at,
        blob_count=blob_count,
        mode=mode,
        other_generation_count=other_generation_count,
    )


def last_reconciled_at_reader(data_dir: Path) -> Callable[[str], int | None]:
    """给 `create_server_app` 的那个可调用对象，绑定到这份数据目录。"""

    def _reader(archive_id: str) -> int | None:
        record = read_record(data_dir, archive_id)
        return None if record is None else record.verified_at

    return _reader


__all__ = [
    "RECONCILIATION_DIRECTORY",
    "RECONCILIATION_SCHEMA_VERSION",
    "ReconciliationError",
    "ReconciliationRecord",
    "ReconciliationResult",
    "last_reconciled_at_reader",
    "read_record",
    "reconcile_archive",
    "record_path",
    "write_record",
]
