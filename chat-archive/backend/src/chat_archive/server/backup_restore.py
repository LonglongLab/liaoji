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
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from chat_archive.server.backup_archive import (
    BACKUP_SCHEMA_VERSION,
    BACKUP_SCHEMA_VERSION_V2,
    ENCRYPTED_RESTORE_NOTE,
    MANIFEST_NAME,
    MAX_DECOMPRESSED_DB_BYTES,
    RESTORE_NOTES,
    TIER_DAILY,
    BackupArchiveError,
    BackupFileRef,
    BackupManifest,
    is_backup_id,
    object_key,
    parse_manifest,
    read_manifest,
    safe_workdir_path,
    unpack_backup_item,
)
from chat_archive.server.backup_crypto import (
    BACKUP_ENC_ALGORITHM,
    DEFAULT_ENC_CHUNK_SIZE,
    BackupCryptoError,
    decrypt_file_stream,
    key_fingerprint,
)
from chat_archive.server.backup_snapshot import (
    REQUIRED_DATABASES,
    SNAPSHOT_DATABASES,
    BackupSnapshotError,
    SnapshotStats,
    read_stats,
    stats_document,
    verify_snapshot,
)
from chat_archive.server.backup_store import BackupStore, md5_base64
from chat_archive.server.routes import RECORD_MODEL_VERSION
from chat_archive.version import SERVER_VERSION

_DECOMPRESS_CHUNK = 1024 * 1024
_DB_NAMES = tuple(SNAPSHOT_DATABASES)
_DB_NAME_PATTERN = "|".join(re.escape(name) for name in _DB_NAMES)
_PART_FILE_RE = re.compile(
    rf"^(?:(?P<backup_id>\d{{8}}T\d{{6}}Z)-)?(?P<name>{_DB_NAME_PATTERN})(?P<ext>\.gz\.enc|\.gz)$"
)
_MANIFEST_FILE_RE = re.compile(rf"^(?P<backup_id>\d{{8}}T\d{{6}}Z)-{re.escape(MANIFEST_NAME)}$")
_MANIFESTLESS_NOTE = (
    "restored directly from local backup shards without manifest.json;"
    " manifest checksum and count cross-check was skipped"
)


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


def _finalize_restore(
    *,
    target: Path,
    names: Sequence[str],
    manifest: BackupManifest,
    verify_stats: bool,
    extra_notes: Sequence[str] = (),
) -> RestoreResult:
    try:
        verify_snapshot(target, names)
    except BackupSnapshotError as error:
        raise BackupArchiveError(str(error)) from None
    stats = read_stats(target)
    if verify_stats:
        restored_document = stats_document(stats)
        for key, expected in manifest.stats.items():
            if restored_document.get(key) != expected:
                raise BackupArchiveError(f"restored {key} does not match the manifest")
    return RestoreResult(
        target=target,
        manifest=manifest,
        stats=stats,
        notes=(*manifest.restore_notes, *extra_notes),
    )


def restore_backup(
    *,
    store: BackupStore,
    prefix: str,
    backup_id: str,
    target: Path,
    tier: str = TIER_DAILY,
    encryption_key: bytes | None = None,
) -> RestoreResult:
    """下载、解密/解压、逐段核 sha256、整体自检，最后对一遍清单里的计数。"""
    manifest = read_manifest(store, prefix, tier, backup_id)
    _require_empty_target(target)
    for item in manifest.files:
        packed = safe_workdir_path(target, item.object_name)
        store.download(object_key(prefix, tier, backup_id, item.object_name), packed)
        restored = safe_workdir_path(target, item.name)
        try:
            unpack_backup_item(
                packed,
                restored,
                item=item,
                manifest=manifest,
                encryption_key=encryption_key,
            )
        finally:
            packed.unlink(missing_ok=True)
    names = tuple(item.name for item in manifest.files)
    return _finalize_restore(
        target=target,
        names=names,
        manifest=manifest,
        verify_stats=True,
    )


def _select_local_manifest(source_dir: Path, backup_id: str | None) -> BackupManifest | None:
    if backup_id is not None:
        if not is_backup_id(backup_id):
            raise BackupArchiveError("backup id is invalid")
        prefixed = safe_workdir_path(source_dir, f"{backup_id}-{MANIFEST_NAME}")
        if prefixed.is_file():
            manifest = parse_manifest(prefixed.read_bytes())
            if manifest.backup_id != backup_id:
                raise BackupArchiveError("manifest backup_id does not match filename")
            return manifest
        bare = safe_workdir_path(source_dir, MANIFEST_NAME)
        if bare.is_file():
            manifest = parse_manifest(bare.read_bytes())
            if manifest.backup_id != backup_id:
                raise BackupArchiveError("source directory manifest belongs to a different backup_id")
            return manifest
        return None

    candidates: list[BackupManifest] = []
    seen_ids: set[str] = set()
    for entry in sorted(source_dir.iterdir(), key=lambda p: p.name):
        if entry.is_symlink() or not entry.is_file():
            continue
        if entry.name == MANIFEST_NAME or _MANIFEST_FILE_RE.fullmatch(entry.name) is not None:
            manifest = parse_manifest(entry.read_bytes())
            if manifest.backup_id not in seen_ids:
                seen_ids.add(manifest.backup_id)
                candidates.append(manifest)
    if len(candidates) > 1:
        raise BackupArchiveError(
            "source directory contains multiple backups; specify --backup-id"
        )
    return candidates[0] if candidates else None


def _resolve_manifest_part_path(source_dir: Path, backup_id: str, object_name: str) -> Path:
    prefixed = safe_workdir_path(source_dir, f"{backup_id}-{object_name}")
    if prefixed.is_file():
        return prefixed
    bare = safe_workdir_path(source_dir, object_name)
    if bare.is_file():
        return bare
    raise BackupArchiveError(f"backup member {object_name} is missing from source directory")


def _gunzip_bounded(source: Path, destination: Path, name: str) -> tuple[int, str]:
    decompressed = 0
    digest = hashlib.sha256()
    try:
        with gzip.open(source, "rb") as reader, destination.open("wb") as writer:
            while chunk := reader.read(_DECOMPRESS_CHUNK):
                decompressed += len(chunk)
                if decompressed > MAX_DECOMPRESSED_DB_BYTES:
                    raise BackupArchiveError(f"{name} exceeds maximum decompressed size")
                digest.update(chunk)
                writer.write(chunk)
    except (OSError, EOFError):
        destination.unlink(missing_ok=True)
        raise BackupArchiveError(f"{name} could not be decompressed") from None
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return decompressed, digest.hexdigest()


def restore_backup_from_dir(
    *,
    source_dir: Path,
    target: Path,
    backup_id: str | None = None,
    encryption_key: bytes | None = None,
) -> RestoreResult:
    """Restore a v1 or encrypted v2 backup from a local directory without contacting S3."""
    if not source_dir.is_dir():
        raise BackupArchiveError("backup source directory does not exist")
    manifest = _select_local_manifest(source_dir, backup_id)
    if manifest is not None:
        pairs = [
            (item, _resolve_manifest_part_path(source_dir, manifest.backup_id, item.object_name))
            for item in manifest.files
        ]
        _require_empty_target(target)
        for item, packed in pairs:
            unpack_backup_item(
                packed,
                safe_workdir_path(target, item.name),
                item=item,
                manifest=manifest,
                encryption_key=encryption_key,
            )
        return _finalize_restore(
            target=target,
            names=tuple(item.name for item in manifest.files),
            manifest=manifest,
            verify_stats=True,
        )

    matched_by_id: dict[str, dict[str, tuple[Path, str]]] = {}
    bare_parts: dict[str, tuple[Path, str]] = {}
    for entry in sorted(source_dir.iterdir(), key=lambda p: p.name):
        if entry.is_symlink() or not entry.is_file():
            continue
        match = _PART_FILE_RE.fullmatch(entry.name)
        if match is None:
            continue
        found_id = match.group("backup_id")
        db_name = match.group("name")
        ext = match.group("ext")
        if found_id is None:
            if db_name in bare_parts:
                raise BackupArchiveError(f"duplicate backup files for {db_name}")
            bare_parts[db_name] = (entry, ext)
        else:
            if backup_id is not None and found_id != backup_id:
                continue
            bucket = matched_by_id.setdefault(found_id, {})
            if db_name in bucket:
                raise BackupArchiveError(f"duplicate backup files for {db_name}")
            bucket[db_name] = (entry, ext)

    if len(matched_by_id) > 1:
        raise BackupArchiveError(
            "source directory contains multiple backups; specify --backup-id"
        )
    if matched_by_id:
        resolved_backup_id, parts_map = next(iter(matched_by_id.items()))
    elif bare_parts:
        parts_map = bare_parts
        resolved_backup_id = backup_id or ""
    else:
        raise BackupArchiveError("no backup files were found in source directory")

    if REQUIRED_DATABASES - set(parts_map):
        raise BackupArchiveError("source directory is missing required database backup files")
    extensions = {ext for _path, ext in parts_map.values()}
    if len(extensions) != 1:
        raise BackupArchiveError("source directory mixes encrypted and plaintext backup files")
    encrypted = next(iter(extensions)) == ".gz.enc"
    if encrypted:
        if encryption_key is None:
            raise BackupArchiveError(
                "backup encryption key is required to read an encrypted backup"
            )
        if not resolved_backup_id:
            raise BackupArchiveError(
                "backup id is required to decrypt unprefixed v2 backup files; specify --backup-id"
            )
    elif not resolved_backup_id:
        resolved_backup_id = "19700101T000000Z"

    ordered = [
        (name, parts_map[name][0], parts_map[name][1])
        for name in SNAPSHOT_DATABASES
        if name in parts_map
    ]
    _require_empty_target(target)
    refs: list[BackupFileRef] = []
    source_total = 0
    stored_total = 0
    for name, packed, ext in ordered:
        stored_size = packed.stat().st_size
        stored_total += stored_size
        restored = safe_workdir_path(target, name)
        if encrypted:
            if encryption_key is None:
                raise BackupArchiveError(
                    "backup encryption key is required to read an encrypted backup"
                )
            decrypted_temp = safe_workdir_path(target, f".{name}.gz.dec")
            try:
                try:
                    decrypt_file_stream(
                        packed,
                        decrypted_temp,
                        key=encryption_key,
                        backup_id=resolved_backup_id,
                        item_name=name,
                        max_output_bytes=max(stored_size, 0),
                    )
                except BackupCryptoError as error:
                    raise BackupArchiveError(str(error)) from None
                size, sha256_hex = _gunzip_bounded(decrypted_temp, restored, name)
            finally:
                decrypted_temp.unlink(missing_ok=True)
        else:
            size, sha256_hex = _gunzip_bounded(packed, restored, name)
        source_total += size
        refs.append(
            BackupFileRef(
                name=name,
                object_name=f"{name}{ext}",
                size=size,
                sha256=sha256_hex,
                stored_size=stored_size,
                stored_md5=md5_base64(b""),
            )
        )

    encryption_meta: dict[str, object] | None = None
    notes = RESTORE_NOTES
    schema_version = BACKUP_SCHEMA_VERSION
    if encrypted and encryption_key is not None:
        schema_version = BACKUP_SCHEMA_VERSION_V2
        encryption_meta = {
            "algorithm": BACKUP_ENC_ALGORITHM,
            "key_fingerprint": key_fingerprint(encryption_key),
            "chunk_size": DEFAULT_ENC_CHUNK_SIZE,
        }
        notes = (*RESTORE_NOTES, ENCRYPTED_RESTORE_NOTE)
    synthetic = BackupManifest(
        schema_version=schema_version,
        backup_id=resolved_backup_id,
        created_at=1,
        app_version=SERVER_VERSION,
        record_model_version=RECORD_MODEL_VERSION,
        source_bytes=source_total,
        stored_bytes=stored_total,
        files=tuple(refs),
        stats={},
        restore_notes=notes,
        encryption=encryption_meta,
    )
    return _finalize_restore(
        target=target,
        names=tuple(name for name, _p, _e in ordered),
        manifest=synthetic,
        verify_stats=False,
        extra_notes=(_MANIFESTLESS_NOTE,),
    )


__all__ = ["RestoreResult", "restore_backup", "restore_backup_from_dir"]
