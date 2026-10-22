"""`python -m chat_archive.server.cli backup ...`（PSU-2.140）。

**凭据只从文件读，绝不从命令行读。** 命令行参数会进 shell 历史、进 `ps`、进容器的
进程列表；`runtime.py` 的 `secret_from_file` 立的是同一条规矩，这里照办。

输出是 JSON，好让运维脚本能接。里面**没有**凭据、没有对象键以外的路径、
没有 provider 原话——失败只报稳定代号。
"""

from __future__ import annotations

import argparse
import json
import secrets
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from chat_archive.server.backup_archive import (
    TIER_DAILY,
    TIERS,
    BackupArchiveError,
    latest_backup,
    lifecycle_covers,
    list_backups,
    run_backup,
    verify_backup,
)
from chat_archive.server.backup_config import (
    BACKUP_DATABASE,
    BackupAlreadyRunningError,
    DEFAULT_DAILY_KEEP,
    DEFAULT_INTERVAL_HOURS,
    DEFAULT_MONTHLY_KEEP,
    DEFAULT_WEEKLY_KEEP,
    RETENTION_LIFECYCLE,
    RETENTION_MODES,
    RUN_FAILED,
    RUN_SUCCESS,
    BackupConfigurationError,
    BackupCredentials,
    BackupPolicy,
    SqliteBackupConfigStore,
)
from chat_archive.server.backup_restore import restore_backup
from chat_archive.server.recovery_verification import (
    RecoveryVerificationRecord,
    write_record as write_recovery_verification,
)
from chat_archive.server.backup_store import BackupStore, BackupStoreError, S3BackupStore
from chat_archive.server.backup_worker import failure_code_for

_MAX_SECRET_BYTES = 4096


def add_parser(commands: Any) -> None:
    backup = commands.add_parser(
        "backup",
        help="configure, run, inspect and restore the object storage backups",
    )
    actions = backup.add_subparsers(dest="backup_command", required=True)

    actions.add_parser("config-show", help="print the backup target without any credential")

    configure = actions.add_parser("config-set", help="set the backup target and policy")
    configure.add_argument("--endpoint", required=True)
    configure.add_argument("--bucket", required=True)
    configure.add_argument("--region", required=True)
    configure.add_argument(
        "--prefix",
        required=True,
        help="object key prefix; must not live under the attachment prefix 'a/'",
    )
    configure.add_argument("--secret-id-file", required=True)
    configure.add_argument("--secret-key-file", required=True)
    configure.add_argument("--interval-hours", type=int, default=DEFAULT_INTERVAL_HOURS)
    configure.add_argument(
        "--retention-mode", choices=sorted(RETENTION_MODES), default=RETENTION_LIFECYCLE
    )
    configure.add_argument("--daily-keep", type=int, default=DEFAULT_DAILY_KEEP)
    configure.add_argument("--weekly-keep", type=int, default=DEFAULT_WEEKLY_KEEP)
    configure.add_argument("--monthly-keep", type=int, default=DEFAULT_MONTHLY_KEEP)

    actions.add_parser("now", help="take one backup immediately")

    listing = actions.add_parser("list", help="list the backups in one tier, newest first")
    listing.add_argument("--tier", choices=sorted(TIERS), default=TIER_DAILY)

    verify = actions.add_parser("verify", help="check one backup; --deep really restores it")
    verify.add_argument("--tier", choices=sorted(TIERS), default=TIER_DAILY)
    verify.add_argument("--backup-id", help="omitted means the newest complete backup")
    verify.add_argument(
        "--deep",
        action="store_true",
        help="download, decompress, open and compare counts (FR-RECOVERY-002)",
    )

    restore = actions.add_parser("restore", help="restore one backup into an empty directory")
    restore.add_argument("--target", required=True, help="absolute path to an empty directory")
    restore.add_argument("--tier", choices=sorted(TIERS), default=TIER_DAILY)
    restore.add_argument("--backup-id", help="omitted means the newest complete backup")


def _read_secret(raw: str) -> str:
    path = Path(raw)
    if not path.is_absolute():
        raise BackupConfigurationError("secret file path must be absolute")
    try:
        payload = path.read_bytes()
    except OSError:
        raise BackupConfigurationError("secret file is unreadable") from None
    if not payload or len(payload) > _MAX_SECRET_BYTES:
        raise BackupConfigurationError("secret file is invalid")
    try:
        value = payload.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise BackupConfigurationError("secret file is invalid") from None
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        raise BackupConfigurationError("secret file is invalid")
    return value


def config_store_for(data_dir: Path) -> SqliteBackupConfigStore:
    return SqliteBackupConfigStore(data_dir / BACKUP_DATABASE, data_dir)


def build_backup_store(credentials: BackupCredentials) -> BackupStore:
    return S3BackupStore(
        endpoint_url=credentials.endpoint,
        access_key=credentials.secret_id,
        secret_key=credentials.secret_key,
        bucket=credentials.bucket,
        region=credentials.region,
    )


def _emit(output: TextIO, document: object) -> None:
    output.write(json.dumps(document, ensure_ascii=False, sort_keys=True))
    output.write("\n")
    output.flush()


def _config_show(store: SqliteBackupConfigStore, output: TextIO) -> int:
    summary = store.summary()
    last = store.last_success()
    _emit(
        output,
        {
            "configured": summary.configured,
            "endpoint": summary.endpoint,
            "bucket": summary.bucket,
            "region": summary.region,
            "prefix": summary.prefix,
            "access_key_hint": summary.access_key_hint,
            "credentials_readable": summary.credentials_readable,
            "updated_at": summary.updated_at,
            "interval_hours": summary.policy.interval_hours,
            "retention_mode": summary.policy.retention_mode,
            "daily_keep": summary.policy.daily_keep,
            "weekly_keep": summary.policy.weekly_keep,
            "monthly_keep": summary.policy.monthly_keep,
            "last_success_at": None if last is None else last.finished_at,
            "last_backup_id": None if last is None else last.backup_id,
        },
    )
    return 0


def _resolve_backup_id(
    store: BackupStore, prefix: str, tier: str, requested: str | None
) -> str:
    if requested is not None:
        return requested
    newest = latest_backup(store, prefix, tier)
    if newest is None:
        raise BackupArchiveError("this tier holds no complete backup")
    return newest.backup_id


def run(
    parsed: argparse.Namespace,
    *,
    data_dir: Path,
    output: TextIO,
    errors: TextIO,
) -> int:
    command = str(parsed.backup_command)
    config = config_store_for(data_dir)
    if command == "config-show":
        return _config_show(config, output)
    if command == "config-set":
        config.save(
            endpoint=str(parsed.endpoint),
            bucket=str(parsed.bucket),
            region=str(parsed.region),
            prefix=str(parsed.prefix),
            secret_id=_read_secret(str(parsed.secret_id_file)),
            secret_key=_read_secret(str(parsed.secret_key_file)),
            policy=BackupPolicy(
                interval_hours=int(parsed.interval_hours),
                retention_mode=str(parsed.retention_mode),
                daily_keep=int(parsed.daily_keep),
                weekly_keep=int(parsed.weekly_keep),
                monthly_keep=int(parsed.monthly_keep),
            ),
        )
        return _config_show(config, output)

    credentials = config.load_credentials()
    if credentials is None:
        errors.write("backup target is not configured\n")
        return 2
    store = build_backup_store(credentials)
    prefix = credentials.prefix

    if command == "now":
        policy = config.policy()
        if policy is None:
            errors.write("backup target is not configured\n")
            return 2
        try:
            run_id = config.begin_run()
        except BackupAlreadyRunningError:
            # API 进程里的定时线程已经在跑这一轮了（PSU-2.142）。两趟一起跑在
            # 2 核机器上等于把活儿翻倍，还会在桶里留下第二份当天的备份。
            errors.write("another backup is already running\n")
            return 2
        try:
            result = run_backup(
                data_dir=data_dir,
                staging_root=data_dir,
                store=store,
                prefix=prefix,
                policy=policy,
                moment=datetime.now(timezone.utc),
            )
        except BaseException as error:
            # 失败也要收口：否则这一行停在 `running`，健康页会一直显示「正在备份」，
            # 直到下一次 API 重启才被 `abandon_running` 收尾。
            config.finish_run(run_id, status=RUN_FAILED, failure_code=failure_code_for(error))
            raise
        config.finish_run(
            run_id,
            status=RUN_SUCCESS,
            backup_id=result.backup_id,
            source_bytes=result.source_bytes,
            stored_bytes=result.stored_bytes,
            verified=True,
            lifecycle_covered=lifecycle_covers(result.retention.lifecycle_prefixes, prefix),
        )
        _emit(
            output,
            {
                "backup_id": result.backup_id,
                "source_bytes": result.source_bytes,
                "stored_bytes": result.stored_bytes,
                "promoted_tiers": list(result.promoted_tiers),
                "retention_mode": result.retention.mode,
                "retention_deleted": list(result.retention.deleted),
                "lifecycle_prefixes": (
                    None
                    if result.retention.lifecycle_prefixes is None
                    else list(result.retention.lifecycle_prefixes)
                ),
            },
        )
        return 0

    if command == "list":
        tier = str(parsed.tier)
        _emit(
            output,
            {
                "tier": tier,
                "backups": [
                    {
                        "backup_id": item.backup_id,
                        "complete": item.complete,
                        "object_count": item.object_count,
                    }
                    for item in list_backups(store, prefix, tier)
                ],
            },
        )
        return 0

    if command == "verify":
        tier = str(parsed.tier)
        backup_id = _resolve_backup_id(
            store, prefix, tier, None if parsed.backup_id is None else str(parsed.backup_id)
        )
        deep = bool(parsed.deep)
        manifest = verify_backup(
            store,
            prefix,
            tier,
            backup_id,
            deep=deep,
            # 名字带一段随机：`verify_backup` 坚持目录必须不存在（对的，它不该
            # 往别人的目录里解压），但一次被 kill 的深校验若留下固定名字的残骸，
            # 就会把**以后每一次**深校验都挡在门外。
            workdir=(data_dir / f".backup-verify-{secrets.token_hex(6)}") if deep else None,
        )
        # 深校验通过 = `FR-RECOVERY-002` 认的那一层真的跑过一次。把它落到数据卷上，
        # 让服务端的 `last_recovery_verified_at` 有据可读（PSU-2.142）。**只有 deep
        # 写**：浅校验只问「每一段都在、大小对得上」，那证明不了这份备份能打开。
        recovery_verified_at: int | None = None
        if deep:
            recovery_verified_at = int(datetime.now(timezone.utc).timestamp())
            write_recovery_verification(
                data_dir,
                RecoveryVerificationRecord(
                    verified_at=recovery_verified_at,
                    tier=tier,
                    backup_id=manifest.backup_id,
                ),
            )
        _emit(
            output,
            {
                "tier": tier,
                "backup_id": manifest.backup_id,
                "deep": deep,
                "recovery_verified_at": recovery_verified_at,
                "app_version": manifest.app_version,
                "record_model_version": manifest.record_model_version,
                "source_bytes": manifest.source_bytes,
                "stored_bytes": manifest.stored_bytes,
                "stats": dict(manifest.stats),
            },
        )
        return 0

    if command == "restore":
        target = Path(str(parsed.target))
        if not target.is_absolute():
            errors.write("restore target must be absolute\n")
            return 2
        tier = str(parsed.tier)
        backup_id = _resolve_backup_id(
            store, prefix, tier, None if parsed.backup_id is None else str(parsed.backup_id)
        )
        result = restore_backup(
            store=store, prefix=prefix, backup_id=backup_id, target=target, tier=tier
        )
        _emit(
            output,
            {
                "backup_id": result.manifest.backup_id,
                "tier": tier,
                "app_version": result.manifest.app_version,
                "record_model_version": result.manifest.record_model_version,
                "stats": dict(result.manifest.stats),
                "restore_notes": list(result.notes),
            },
        )
        return 0

    errors.write("unsupported backup command\n")
    return 2


def main(
    arguments: Sequence[str] | None = None,
    *,
    data_dir: Path,
    output: TextIO,
    errors: TextIO,
) -> int:
    """独立入口，只给测试用；生产走 `chat_archive.server.cli backup ...`。"""
    parser = argparse.ArgumentParser(prog="chat-archive-server")
    add_parser(parser.add_subparsers(dest="command", required=True))
    parsed = parser.parse_args(arguments)
    try:
        return run(parsed, data_dir=data_dir, output=output, errors=errors)
    except (BackupConfigurationError, BackupArchiveError, BackupStoreError) as error:
        errors.write(f"{error}\n")
        return 2


__all__ = ["add_parser", "build_backup_store", "config_store_for", "main", "run"]
