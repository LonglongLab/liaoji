"""一次备份从头到尾：压、传、封清单、提升档位、清历史（PSU-2.140）。

## 对象布局

    <前缀>/<档>/<备份号>/manifest.json
    <前缀>/<档>/<备份号>/metadata.sqlite.gz
    <前缀>/<档>/<备份号>/...

`<档>` 是 `daily` / `weekly` / `monthly`，`<备份号>` 是 `YYYYMMDDTHHMMSSZ`。
键里带时间戳是**故意偏离** `ports/objectstore.py` 的不透明键规则：那条规则是给附件的
（`FR-BLOB-002`，因为附件键会签成 URL 交给浏览器），而备份键从不签发、从不出服务器，
保留与"取最新一份"却需要可排序的键。键里没有任何个人信息。

**键里没有 archive_id**，因为备份是**整套部署**级别的——`metadata.sqlite` 一个库服务
这台部署上的全部归档。要分开就用不同的前缀。

## 清单最后写

`manifest.json` 是完整性标记：所有数据对象传完并逐个 HEAD 核过之后才写它。
因此"有 manifest = 这份备份完整"是可信的判据，半路失败留下的碎片永远不会被
`list_backups` 当成一份备份，也永远不会被保留策略算进份数。

传到一半失败留下的碎片**不会主动删**——推荐配置里那把子账号密钥没有
`DeleteObject`（这是刻意的：服务器被打穿也删不掉自己的备份）。碎片由桶上的
生命周期规则过期，这也是 `retention_mode=lifecycle` 是默认值的原因之一。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from chat_archive.canonical_json import canonical_json_text
from chat_archive.server.backup_config import (
    MINIMUM_RETAINED,
    RETENTION_APPLICATION,
    BackupPolicy,
)
from chat_archive.server.backup_crypto import (
    BACKUP_ENC_ALGORITHM,
    DEFAULT_ENC_CHUNK_SIZE,
    MAX_ENC_CHUNK_SIZE,
    MIN_ENC_CHUNK_SIZE,
    BackupCryptoError,
    decrypt_file_stream,
    encrypt_file_stream,
    key_fingerprint,
)
from chat_archive.server.backup_snapshot import (
    SNAPSHOT_DATABASES,
    BackupSnapshotError,
    SnapshotSet,
    create_snapshot,
    read_stats,
    stats_document,
    verify_snapshot,
)
from chat_archive.server.backup_store import (
    BackupStore,
    file_md5_base64,
    md5_base64,
)
from chat_archive.server.routes import RECORD_MODEL_VERSION
from chat_archive.version import SERVER_VERSION

BACKUP_SCHEMA_VERSION = "chat-archive.backup.v1"
BACKUP_SCHEMA_VERSION_V2 = "chat-archive.backup.v2"
SUPPORTED_BACKUP_SCHEMAS: frozenset[str] = frozenset(
    {BACKUP_SCHEMA_VERSION, BACKUP_SCHEMA_VERSION_V2}
)
MANIFEST_NAME = "manifest.json"
MAX_DECOMPRESSED_DB_BYTES = 16 * 1024 * 1024 * 1024

TIER_DAILY = "daily"
TIER_WEEKLY = "weekly"
TIER_MONTHLY = "monthly"
TIERS: tuple[str, ...] = (TIER_DAILY, TIER_WEEKLY, TIER_MONTHLY)

_BACKUP_ID = re.compile(r"^\d{8}T\d{6}Z$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_KEY_FP_RE = re.compile(r"^[0-9a-f]{16}$")
_ALLOWED_DB_NAMES: frozenset[str] = frozenset(SNAPSHOT_DATABASES)
# 比这更新的碎片可能是另一个进程（CLI `backup now`）正在传的那一份，不碰。
FRAGMENT_GRACE = timedelta(days=1)
_GZIP_LEVEL = 6
_CHUNK = 1024 * 1024
ENCRYPTED_RESTORE_NOTE = (
    "this backup is client-encrypted (v2); provide the operator encryption key"
    " via --encryption-key-file or CHAT_ARCHIVE_BACKUP_ENCRYPTION_KEY_FILE when verifying or restoring"
)

# 恢复时人要知道、而代码替不了的那几件事。跟着清单走，因为出事那天
# 手上大概只有这个桶。
RESTORE_NOTES: tuple[str, ...] = (
    "master key files are deliberately not in this backup; object storage"
    " credentials must be re-entered and browser TOTP re-enrolled",
    "re-enter the exact same object storage endpoint, bucket and region:"
    " any difference advances the storage generation and orphans every"
    " existing blob manifest",
    "search.sqlite is not in this backup; it is rebuilt in the background"
    " and search stays degraded until the backfill catches up",
    "the backup target's own credentials are locked on the restored machine"
    " (backup-master.key is not in the backup); re-enter them or the restored"
    " deployment takes no backups of its own",
    "AI authorization scopes and connection tokens are not part of this backup;"
    " authorize AI connections again after restoring into a new directory",
)


class BackupArchiveError(RuntimeError):
    """一次备份或一次校验失败。"""


@dataclass(frozen=True)
class BackupFileRef:
    """清单里的一段。`sha256` 是**解压后**的原库摘要，恢复时按它核。"""

    name: str
    object_name: str
    size: int
    sha256: str
    stored_size: int
    stored_md5: str

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "object_name": self.object_name,
            "size": self.size,
            "sha256": self.sha256,
            "stored_size": self.stored_size,
            "stored_md5": self.stored_md5,
        }


@dataclass(frozen=True)
class BackupManifest:
    """一份备份的全部结构性事实。没有正文、没有路径、没有主机名、没有凭据。"""

    schema_version: str
    backup_id: str
    created_at: int
    app_version: str
    record_model_version: int
    source_bytes: int
    stored_bytes: int
    files: tuple[BackupFileRef, ...]
    stats: Mapping[str, object]
    restore_notes: tuple[str, ...] = RESTORE_NOTES
    encryption: Mapping[str, object] | None = None

    def to_dict(self) -> dict[str, object]:
        document: dict[str, object] = {
            "schema_version": self.schema_version,
            "backup_id": self.backup_id,
            "created_at": self.created_at,
            "app_version": self.app_version,
            "record_model_version": self.record_model_version,
            "source_bytes": self.source_bytes,
            "stored_bytes": self.stored_bytes,
            "files": [item.to_dict() for item in self.files],
            "stats": dict(self.stats),
            "restore_notes": list(self.restore_notes),
        }
        if self.encryption is not None:
            document["encryption"] = dict(self.encryption)
        return document

    def canonical_json(self) -> str:
        return canonical_json_text(self.to_dict(), sort_keys=True, ensure_ascii=True)


def _as_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BackupArchiveError("backup manifest is unreadable")
    return value


def _as_str(value: object) -> str:
    if not isinstance(value, str):
        raise BackupArchiveError("backup manifest is unreadable")
    return value


def _validate_encryption_meta(raw: object) -> Mapping[str, object]:
    if not isinstance(raw, dict):
        raise BackupArchiveError("backup manifest encryption metadata is invalid")
    meta = cast(dict[str, object], raw)
    algorithm = _as_str(meta.get("algorithm"))
    fingerprint = _as_str(meta.get("key_fingerprint"))
    chunk_size = _as_int(meta.get("chunk_size"))
    if (
        algorithm != BACKUP_ENC_ALGORITHM
        or _KEY_FP_RE.fullmatch(fingerprint) is None
        or not MIN_ENC_CHUNK_SIZE <= chunk_size <= MAX_ENC_CHUNK_SIZE
    ):
        raise BackupArchiveError("backup manifest encryption metadata is invalid")
    return {
        "algorithm": algorithm,
        "key_fingerprint": fingerprint,
        "chunk_size": chunk_size,
    }


def parse_manifest(payload: bytes) -> BackupManifest:
    try:
        document: object = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise BackupArchiveError("backup manifest is unreadable") from None
    if not isinstance(document, dict):
        raise BackupArchiveError("backup manifest is unreadable")
    fields = cast(dict[str, object], document)
    schema_version = fields.get("schema_version")
    if not isinstance(schema_version, str) or schema_version not in SUPPORTED_BACKUP_SCHEMAS:
        raise BackupArchiveError("backup manifest schema version is unsupported")
    raw_encryption = fields.get("encryption")
    encryption: Mapping[str, object] | None = None
    if schema_version == BACKUP_SCHEMA_VERSION_V2:
        encryption = _validate_encryption_meta(raw_encryption)
    elif raw_encryption is not None:
        raise BackupArchiveError("backup manifest encryption metadata is invalid")
    raw_files = fields.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise BackupArchiveError("backup manifest has no file list")
    files: list[BackupFileRef] = []
    seen_names: set[str] = set()
    suffix = ".gz.enc" if schema_version == BACKUP_SCHEMA_VERSION_V2 else ".gz"
    for entry in cast(list[object], raw_files):
        if not isinstance(entry, dict):
            raise BackupArchiveError("backup manifest file entry is unreadable")
        item = cast(dict[str, object], entry)
        try:
            name = _as_str(item["name"])
            object_name = _as_str(item["object_name"])
            size = _as_int(item["size"])
            sha256 = _as_str(item["sha256"])
            stored_size = _as_int(item["stored_size"])
            stored_md5 = _as_str(item["stored_md5"])
        except KeyError:
            raise BackupArchiveError("backup manifest file entry is unreadable") from None
        if (
            name not in _ALLOWED_DB_NAMES
            or name in seen_names
            or object_name != f"{name}{suffix}"
            or not 0 <= size <= MAX_DECOMPRESSED_DB_BYTES
            or stored_size < 0
            or _SHA256_RE.fullmatch(sha256) is None
        ):
            raise BackupArchiveError("backup manifest file entry is invalid")
        seen_names.add(name)
        files.append(
            BackupFileRef(
                name=name,
                object_name=object_name,
                size=size,
                sha256=sha256,
                stored_size=stored_size,
                stored_md5=stored_md5,
            )
        )
    raw_stats = fields.get("stats")
    stats = cast(dict[str, object], raw_stats) if isinstance(raw_stats, dict) else {}
    raw_notes = fields.get("restore_notes")
    notes = cast(list[object], raw_notes) if isinstance(raw_notes, list) else []
    try:
        backup_id = _as_str(fields["backup_id"])
        created_at = _as_int(fields["created_at"])
        app_version = _as_str(fields["app_version"])
        record_model_version = _as_int(fields["record_model_version"])
        source_bytes = _as_int(fields["source_bytes"])
        stored_bytes = _as_int(fields["stored_bytes"])
    except KeyError:
        raise BackupArchiveError("backup manifest is unreadable") from None
    if not is_backup_id(backup_id) or source_bytes < 0 or stored_bytes < 0:
        raise BackupArchiveError("backup manifest is unreadable")
    return BackupManifest(
        schema_version=schema_version,
        backup_id=backup_id,
        created_at=created_at,
        app_version=app_version,
        record_model_version=record_model_version,
        source_bytes=source_bytes,
        stored_bytes=stored_bytes,
        files=tuple(files),
        stats=stats,
        restore_notes=tuple(_as_str(note) for note in notes),
        encryption=encryption,
    )


@dataclass(frozen=True)
class BackupSummary:
    """列举时看到的一份备份。`complete` 为假就是碎片，不是备份。"""

    tier: str
    backup_id: str
    complete: bool
    object_count: int


@dataclass(frozen=True)
class RetentionOutcome:
    mode: str
    deleted: tuple[str, ...] = ()
    kept: tuple[str, ...] = ()
    lifecycle_prefixes: tuple[str, ...] | None = None


@dataclass(frozen=True)
class BackupRunResult:
    backup_id: str
    source_bytes: int
    stored_bytes: int
    promoted_tiers: tuple[str, ...] = ()
    retention: RetentionOutcome = field(default_factory=lambda: RetentionOutcome(mode=""))


def backup_id_for(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def is_backup_id(value: str) -> bool:
    return _BACKUP_ID.fullmatch(value) is not None


def tier_prefix(prefix: str, tier: str) -> str:
    if tier not in TIERS:
        raise BackupArchiveError("backup tier is invalid")
    return f"{prefix}/{tier}/"


def backup_prefix(prefix: str, tier: str, backup_id: str) -> str:
    if not is_backup_id(backup_id):
        raise BackupArchiveError("backup id is invalid")
    return f"{tier_prefix(prefix, tier)}{backup_id}/"


def object_key(prefix: str, tier: str, backup_id: str, name: str) -> str:
    return f"{backup_prefix(prefix, tier, backup_id)}{name}"


def due_tiers(moment: datetime) -> tuple[str, ...]:
    """这一刻的备份除了 `daily` 还该被提升到哪些档。

    周日提升到 `weekly`，每月 1 号提升到 `monthly`。提升用服务端 CopyObject，
    不重传——跨太平洋只走一趟。
    """
    utc = moment.astimezone(UTC)
    tiers: list[str] = []
    if utc.isoweekday() == 7:
        tiers.append(TIER_WEEKLY)
    if utc.day == 1:
        tiers.append(TIER_MONTHLY)
    return tuple(tiers)


def _compress(source: Path, destination: Path) -> int:
    with source.open("rb") as raw, destination.open("wb") as packed:
        with gzip.GzipFile(fileobj=packed, mode="wb", compresslevel=_GZIP_LEVEL, mtime=0) as stream:
            while chunk := raw.read(_CHUNK):
                stream.write(chunk)
    return destination.stat().st_size


def list_backups(store: BackupStore, prefix: str, tier: str) -> tuple[BackupSummary, ...]:
    """列出一个档里的备份，最新的在前。碎片会被标成 `complete=False`。"""
    root = tier_prefix(prefix, tier)
    grouped: dict[str, list[str]] = {}
    for key in store.list_keys(root):
        remainder = key[len(root) :]
        head, separator, tail = remainder.partition("/")
        if not separator or not is_backup_id(head):
            continue
        grouped.setdefault(head, []).append(tail)
    summaries = [
        BackupSummary(
            tier=tier,
            backup_id=backup_id,
            complete=MANIFEST_NAME in names,
            object_count=len(names),
        )
        for backup_id, names in grouped.items()
    ]
    return tuple(sorted(summaries, key=lambda item: item.backup_id, reverse=True))


def read_manifest(store: BackupStore, prefix: str, tier: str, backup_id: str) -> BackupManifest:
    payload = store.get_bytes(object_key(prefix, tier, backup_id, MANIFEST_NAME))
    return parse_manifest(payload)


def latest_backup(store: BackupStore, prefix: str, tier: str = TIER_DAILY) -> BackupSummary | None:
    for summary in list_backups(store, prefix, tier):
        if summary.complete:
            return summary
    return None


def _upload_snapshot(
    store: BackupStore,
    snapshot: SnapshotSet,
    prefix: str,
    backup_id: str,
    *,
    encryption_key: bytes | None = None,
) -> tuple[tuple[BackupFileRef, ...], int]:
    """逐个压、（可选）加密、传、HEAD 核；压完立刻删原件，峰值占盘因此可预测。"""
    refs: list[BackupFileRef] = []
    stored_total = 0
    for item in snapshot.files:
        compressed_name = f"{item.name}.gz"
        compressed = snapshot.directory / compressed_name
        stored_size = _compress(item.path, compressed)
        item.path.unlink()
        if encryption_key is not None:
            object_name = f"{compressed_name}.enc"
            packed = snapshot.directory / object_name
            try:
                stored_size = encrypt_file_stream(
                    compressed,
                    packed,
                    key=encryption_key,
                    backup_id=backup_id,
                    item_name=item.name,
                )
            finally:
                compressed.unlink(missing_ok=True)
        else:
            object_name = compressed_name
            packed = compressed
        digest = file_md5_base64(packed)
        key = object_key(prefix, TIER_DAILY, backup_id, object_name)
        store.put_file(key, packed, content_md5=digest)
        landed = store.head(key)
        if landed.size != stored_size:
            raise BackupArchiveError(f"{item.name} did not land with the size it was sent with")
        packed.unlink()
        stored_total += stored_size
        refs.append(
            BackupFileRef(
                name=item.name,
                object_name=object_name,
                size=item.size,
                sha256=item.sha256,
                stored_size=stored_size,
                stored_md5=digest,
            )
        )
    return tuple(refs), stored_total


def promote(
    store: BackupStore,
    prefix: str,
    backup_id: str,
    manifest: BackupManifest,
    tiers: Sequence[str],
) -> tuple[str, ...]:
    """把已经落地的一份提升到别的档。服务端拷贝，清单仍然最后写。"""
    promoted: list[str] = []
    for tier in tiers:
        for item in manifest.files:
            store.copy(
                object_key(prefix, TIER_DAILY, backup_id, item.object_name),
                object_key(prefix, tier, backup_id, item.object_name),
            )
        store.copy(
            object_key(prefix, TIER_DAILY, backup_id, MANIFEST_NAME),
            object_key(prefix, tier, backup_id, MANIFEST_NAME),
        )
        promoted.append(tier)
    return tuple(promoted)


def lifecycle_covers(prefixes: Sequence[str] | None, prefix: str) -> bool | None:
    """桶上那些过期规则盖没盖住这个备份前缀。

    `None` 进 `None` 出：问不到（最小权限密钥读不了 lifecycle 配置）说明不了
    规则是否存在。空规则集是明确的「没有」。规则前缀与备份前缀互为前缀都算盖住：
    README 推荐的是按档配（`backup/daily/`、`backup/weekly/`……），比 `backup/` 长。
    """
    if prefixes is None:
        return None
    root = f"{prefix}/"
    return any(rule == "" or root.startswith(rule) or rule.startswith(root) for rule in prefixes)


def backup_moment(backup_id: str) -> datetime:
    if not is_backup_id(backup_id):
        raise BackupArchiveError("backup id is invalid")
    return datetime.strptime(backup_id, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)


def apply_retention(
    store: BackupStore,
    prefix: str,
    policy: BackupPolicy,
    *,
    now: datetime | None = None,
) -> RetentionOutcome:
    """按策略清历史。三条硬约束在这里，各有回归用例。

    1. **`lifecycle` 模式一次 delete 都不发**——删除权交给桶上的生命周期规则，
       服务器被打穿也删不掉自己的备份。这是默认。
    2. **永不删最新一份**。
    3. **无论多旧，完整备份的总份数不得低于 `MINIMUM_RETAINED`。**

    只在一次成功并核验过的备份之后调用（`run_backup` 里就是这个顺序）：
    先有新的，再删旧的。

    `application` 模式还顺手清**碎片**（传到一半失败、没有清单的那些）：这个模式
    本来就是给没有生命周期规则的目标用的，不清就永远堆着。只清比 `now` 早一天以上的，
    再新的可能是另一个进程正在传的那一份。`now` 不给就不清碎片。
    """
    if policy.retention_mode != RETENTION_APPLICATION:
        return RetentionOutcome(
            mode=policy.retention_mode,
            lifecycle_prefixes=store.lifecycle_expiration_prefixes(),
        )
    deleted: list[str] = []
    if now is not None:
        cutoff = now.astimezone(UTC) - FRAGMENT_GRACE
        for tier in TIERS:
            for summary in list_backups(store, prefix, tier):
                if summary.complete or backup_moment(summary.backup_id) >= cutoff:
                    continue
                for key in store.list_keys(backup_prefix(prefix, tier, summary.backup_id)):
                    store.delete(key)
                deleted.append(f"{tier}/{summary.backup_id} (fragment)")
    keep_by_tier = {
        TIER_DAILY: policy.daily_keep,
        TIER_WEEKLY: policy.weekly_keep,
        TIER_MONTHLY: policy.monthly_keep,
    }
    complete: dict[str, list[BackupSummary]] = {}
    total_complete = 0
    for tier in TIERS:
        found = [item for item in list_backups(store, prefix, tier) if item.complete]
        complete[tier] = found
        total_complete += len(found)
    kept: list[str] = []
    fragments_deleted = len(deleted)
    for tier in TIERS:
        found = complete[tier]
        keep = keep_by_tier[tier]
        for index, summary in enumerate(found):
            label = f"{tier}/{summary.backup_id}"
            if index < keep:
                kept.append(label)
                continue
            if index == 0:
                # 该档最新的一份永远留着，哪怕 keep 被配成 0。
                kept.append(label)
                continue
            if total_complete - (len(deleted) - fragments_deleted) <= MINIMUM_RETAINED:
                kept.append(label)
                continue
            root = backup_prefix(prefix, tier, summary.backup_id)
            # 先删清单：它一没，这份就不再是"完整备份"，中途失败也不会留下
            # 一个指向半套对象的完整标记。
            store.delete(f"{root}{MANIFEST_NAME}")
            for key in store.list_keys(root):
                store.delete(key)
            deleted.append(label)
    return RetentionOutcome(mode=policy.retention_mode, deleted=tuple(deleted), kept=tuple(kept))


def safe_workdir_path(root: Path, filename: str) -> Path:
    """Resolve a single-segment filename strictly inside ``root``."""
    if not filename or "\x00" in filename or Path(filename).name != filename or filename in {".", ".."}:
        raise BackupArchiveError("backup entry path is invalid")
    root_resolved = root.resolve()
    candidate = (root_resolved / filename).resolve()
    if not candidate.is_relative_to(root_resolved) or candidate == root_resolved:
        raise BackupArchiveError("backup entry path escapes working directory")
    return root / filename


def unpack_backup_item(
    packed: Path,
    destination: Path,
    *,
    item: BackupFileRef,
    manifest: BackupManifest,
    encryption_key: bytes | None = None,
) -> None:
    """Verify stored size, decrypt if v2, enforce decompression bounds, and verify SHA-256."""
    if not packed.is_file() or packed.stat().st_size != item.stored_size:
        raise BackupArchiveError(f"{item.name} downloaded with an unexpected size")
    gzip_source = packed
    decrypted_temp: Path | None = None
    try:
        if manifest.schema_version == BACKUP_SCHEMA_VERSION_V2:
            if encryption_key is None:
                raise BackupArchiveError(
                    "backup encryption key is required to read an encrypted backup"
                )
            if manifest.encryption is None:
                raise BackupArchiveError("backup manifest encryption metadata is invalid")
            try:
                actual_fp = key_fingerprint(encryption_key)
            except BackupCryptoError as error:
                raise BackupArchiveError(str(error)) from None
            if actual_fp != manifest.encryption.get("key_fingerprint"):
                raise BackupArchiveError("backup encryption key does not match manifest fingerprint")
            decrypted_temp = safe_workdir_path(destination.parent, f".{item.name}.gz.dec")
            try:
                decrypt_file_stream(
                    packed,
                    decrypted_temp,
                    key=encryption_key,
                    backup_id=manifest.backup_id,
                    item_name=item.name,
                    max_output_bytes=max(item.stored_size, 0),
                )
            except BackupCryptoError as error:
                raise BackupArchiveError(str(error)) from None
            gzip_source = decrypted_temp
        decompressed = 0
        digest = hashlib.sha256()
        max_bytes = min(item.size, MAX_DECOMPRESSED_DB_BYTES)
        try:
            with gzip.open(gzip_source, "rb") as source, destination.open("wb") as writer:
                while chunk := source.read(_CHUNK):
                    decompressed += len(chunk)
                    if decompressed > max_bytes:
                        raise BackupArchiveError(
                            f"{item.name} exceeds declared decompressed size"
                        )
                    digest.update(chunk)
                    writer.write(chunk)
        except (OSError, EOFError):
            destination.unlink(missing_ok=True)
            raise BackupArchiveError(f"{item.name} could not be decompressed") from None
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        if decompressed != item.size or digest.hexdigest() != item.sha256:
            destination.unlink(missing_ok=True)
            raise BackupArchiveError(f"{item.name} does not match its recorded checksum")
    finally:
        if decrypted_temp is not None:
            decrypted_temp.unlink(missing_ok=True)


def run_backup(
    *,
    data_dir: Path,
    staging_root: Path,
    store: BackupStore,
    prefix: str,
    policy: BackupPolicy,
    moment: datetime,
    encryption_key: bytes | None = None,
) -> BackupRunResult:
    """照一套快照、传上去、封清单、提升、清历史。"""
    backup_id = backup_id_for(moment)
    staging_dir = staging_root / f".backup-{backup_id}"
    snapshot = create_snapshot(data_dir, staging_dir)
    encryption_meta: dict[str, object] | None = None
    schema_version = BACKUP_SCHEMA_VERSION
    notes = RESTORE_NOTES
    if encryption_key is not None:
        encryption_meta = {
            "algorithm": BACKUP_ENC_ALGORITHM,
            "key_fingerprint": key_fingerprint(encryption_key),
            "chunk_size": DEFAULT_ENC_CHUNK_SIZE,
        }
        schema_version = BACKUP_SCHEMA_VERSION_V2
        notes = (*RESTORE_NOTES, ENCRYPTED_RESTORE_NOTE)
    try:
        refs, stored_bytes = _upload_snapshot(
            store, snapshot, prefix, backup_id, encryption_key=encryption_key
        )
        manifest = BackupManifest(
            schema_version=schema_version,
            backup_id=backup_id,
            created_at=int(moment.astimezone(UTC).timestamp()),
            app_version=SERVER_VERSION,
            record_model_version=RECORD_MODEL_VERSION,
            source_bytes=snapshot.total_bytes,
            stored_bytes=stored_bytes,
            files=refs,
            stats=stats_document(snapshot.stats),
            restore_notes=notes,
            encryption=encryption_meta,
        )
        payload = manifest.canonical_json().encode("ascii")
        # 完整性标记，最后一个写。
        store.put_bytes(
            object_key(prefix, TIER_DAILY, backup_id, MANIFEST_NAME),
            payload,
            content_md5=md5_base64(payload),
        )
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)
    promoted = promote(store, prefix, backup_id, manifest, due_tiers(moment))
    retention = apply_retention(store, prefix, policy, now=moment)
    return BackupRunResult(
        backup_id=backup_id,
        source_bytes=snapshot.total_bytes,
        stored_bytes=stored_bytes,
        promoted_tiers=promoted,
        retention=retention,
    )


def verify_backup(
    store: BackupStore,
    prefix: str,
    tier: str,
    backup_id: str,
    *,
    deep: bool = False,
    workdir: Path | None = None,
    encryption_key: bytes | None = None,
) -> BackupManifest:
    """浅校验只问"每一段都在、大小对得上"；深校验真下载、真解压、真打开、核 SHA-256。

    深校验是 `FR-RECOVERY-002` 认的那一层：只生成备份而不验证恢复不算达标。
    """
    manifest = read_manifest(store, prefix, tier, backup_id)
    for item in manifest.files:
        landed = store.head(object_key(prefix, tier, backup_id, item.object_name))
        if landed.size != item.stored_size:
            raise BackupArchiveError(f"{item.name} is stored with an unexpected size")
    if not deep:
        return manifest
    if workdir is None:
        raise BackupArchiveError("deep verification needs a working directory")
    if workdir.exists():
        raise BackupArchiveError("deep verification working directory already exists")
    workdir.mkdir(parents=True)
    try:
        for item in manifest.files:
            packed = safe_workdir_path(workdir, item.object_name)
            store.download(object_key(prefix, tier, backup_id, item.object_name), packed)
            target = safe_workdir_path(workdir, item.name)
            try:
                unpack_backup_item(
                    packed,
                    target,
                    item=item,
                    manifest=manifest,
                    encryption_key=encryption_key,
                )
            finally:
                packed.unlink(missing_ok=True)
        names = tuple(item.name for item in manifest.files)
        try:
            verify_snapshot(workdir, names)
        except BackupSnapshotError as error:
            raise BackupArchiveError(str(error)) from None
        restored = stats_document(read_stats(workdir))
        for key, expected in manifest.stats.items():
            if restored.get(key) != expected:
                raise BackupArchiveError(f"restored {key} does not match the manifest")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return manifest


__all__ = [
    "BACKUP_SCHEMA_VERSION",
    "BACKUP_SCHEMA_VERSION_V2",
    "ENCRYPTED_RESTORE_NOTE",
    "MANIFEST_NAME",
    "MAX_DECOMPRESSED_DB_BYTES",
    "RESTORE_NOTES",
    "SUPPORTED_BACKUP_SCHEMAS",
    "TIERS",
    "TIER_DAILY",
    "TIER_MONTHLY",
    "TIER_WEEKLY",
    "BackupArchiveError",
    "BackupFileRef",
    "BackupManifest",
    "BackupRunResult",
    "BackupSummary",
    "RetentionOutcome",
    "FRAGMENT_GRACE",
    "apply_retention",
    "backup_id_for",
    "backup_moment",
    "backup_prefix",
    "due_tiers",
    "is_backup_id",
    "latest_backup",
    "lifecycle_covers",
    "list_backups",
    "object_key",
    "parse_manifest",
    "promote",
    "read_manifest",
    "run_backup",
    "safe_workdir_path",
    "tier_prefix",
    "unpack_backup_item",
    "verify_backup",
]
