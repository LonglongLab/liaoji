"""Operator-only commands for the self-hosted server."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from collections.abc import Mapping, Sequence
from contextlib import closing
from pathlib import Path
from typing import TextIO

from chat_archive.adapters.sqlite.blob_state import SqliteBlobStateStore
from chat_archive.adapters.sqlite.browser_auth import SqliteBrowserAuthStore
from chat_archive.adapters.sqlite.browser_reauth import SqliteBrowserReauthStore
from chat_archive.adapters.sqlite.device_auth import SqliteDeviceAuthStore
from chat_archive.adapters.sqlite.metadata import SQLiteMetadataStore
from chat_archive.server import backup_cli
from chat_archive.server.backup_archive import BackupArchiveError
from chat_archive.server.backup_config import BackupConfigurationError
from chat_archive.server.backup_store import BackupStoreError
from chat_archive.server.object_storage_config import ObjectStoreNotConfiguredError
from chat_archive.server.reconciliation import (
    ReconciliationError,
    ReconciliationRecord,
    reconcile_archive,
    write_record,
)
from chat_archive.server.shares import SHARE_OBJECT_SCOPE
from chat_archive.server.runtime import (
    build_object_storage,
    RuntimeConfigurationError,
    SingleWriterLease,
    publish_bootstrap_token,
    runtime_data_dir,
)

_REMOTE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# `settle-merges` 一个归档最多推多少步。每步 2,000 行，所以这是 4,000 万行的
# 余量——真机上的存量是 216,282 行。它存在的理由不是限量，是**不许死循环**：
# 这条命令跑在停机窗口里，没人守着它。
_SETTLE_STEP_LIMIT = 20_000


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chat-archive-server")
    commands = parser.add_subparsers(dest="command", required=True)
    provision = commands.add_parser(
        "provision",
        help="create an archive and print its first device token exactly once",
    )
    provision.add_argument("--archive-id", required=True)
    provision.add_argument("--archive-name", required=True)
    provision.add_argument("--device-name", required=True)
    commands.add_parser(
        "reset-browser-login",
        help=(
            "forget the browser password, TOTP and every browser session, then "
            "print a fresh one-time bootstrap token"
        ),
    )
    commands.add_parser(
        "disable-browser-totp",
        help="unbind two-factor authentication, keeping the password and sessions",
    )
    commands.add_parser(
        "reset-browser-reauth",
        help=(
            "turn the app lock off on every device, forget every passkey and unlock "
            "grant, and sign out every browser session"
        ),
    )
    reconcile = commands.add_parser(
        "reconcile",
        help=(
            "compare every landed blob manifest against the object store and, "
            "when they agree, record when that last held"
        ),
    )
    reconcile.add_argument(
        "--archive-id",
        help="only this archive; omitted means every archive on this deployment",
    )
    reconcile.add_argument(
        "--deep",
        action="store_true",
        help=(
            "compare declared checksums instead of byte counts; one HEAD per "
            "object, so minutes rather than seconds on a real archive"
        ),
    )
    settle = commands.add_parser(
        "settle-merges",
        help=(
            "move every record still filed under a merged-away conversation, "
            "in one sitting; stop the service first"
        ),
    )
    settle.add_argument(
        "--archive-id",
        help="only this archive; omitted means every archive on this deployment",
    )
    backup_cli.add_parser(commands)
    return parser


def _valid_name(value: str, *, maximum: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError("invalid display name")
    return normalized


def _provision(
    *,
    data_dir: Path,
    archive_id: str,
    archive_name: str,
    device_name: str,
) -> dict[str, str]:
    if _REMOTE_ID.fullmatch(archive_id) is None:
        raise ValueError("invalid archive id")
    if archive_id == SHARE_OBJECT_SCOPE:
        # 这个前缀归分享副本所有（PSU-2.180）；一个同名归档会让 reconcile 把
        # 全部分享副本当成它的孤儿对象。
        raise ValueError("invalid archive id")
    normalized_archive_name = _valid_name(archive_name, maximum=128)
    normalized_device_name = _valid_name(device_name, maximum=64)
    with SingleWriterLease.acquire(data_dir):
        paired = SqliteDeviceAuthStore(data_dir / "auth.sqlite").provision_archive(
            archive_id,
            normalized_archive_name,
            normalized_device_name,
        )
    return {
        "archive_id": paired.archive_id,
        "device_id": paired.device_id,
        "device_token": paired.device_token,
    }


def _reconcile(
    *,
    data_dir: Path,
    archive_id: str | None,
    deep: bool,
    output: TextIO,
    errors: TextIO,
) -> int:
    """对账，并且**只有干净的那一次**才记录时间。

    不取 `SingleWriterLease`：对账是例行检查，要能在 API 跑着的时候做。它对
    `blob-state.sqlite` 真的只读（`read_only=True` → `mode=ro`，连 `WAL` 那句
    pragma 和建表迁移都不跑），结论写成数据卷上的一份工件，所以单写者不变量
    不受影响。

    也**不新建任何库**：数据目录里没有 `auth.sqlite`/`blob-state.sqlite` 时，
    这里不是一个已部署的归档，正确反应是拒绝，而不是凭空造两个空库、然后
    对着空气报告「完全干净」。
    """
    # 参数先验，再碰盘：非法的归档 ID 不该留下任何痕迹。
    if archive_id is not None and _REMOTE_ID.fullmatch(archive_id) is None:
        raise ValueError("invalid archive id")
    auth_path = data_dir / "auth.sqlite"
    blob_state_path = data_dir / "blob-state.sqlite"
    for required in (auth_path, blob_state_path):
        if not required.is_file():
            raise ReconciliationError(f"{required.name} is not present in the data dir")
    blob_state_store = SqliteBlobStateStore(blob_state_path, read_only=True)
    _config_store, object_store = build_object_storage(data_dir)
    generation = object_store.storage_generation()
    if archive_id is not None:
        archives = [archive_id]
    else:
        # 两个来源取并集：`auth.sqlite` 是这台部署托管着哪些归档的权威名单，
        # 而清单表才是对账真正要查的东西。只问前者会漏掉「有附件、没有设备
        # 配对」的归档；只问后者会让一个还没传过附件的归档永远没有记录。
        archives = sorted(
            {item.id for item in SqliteDeviceAuthStore(auth_path).list_archives()}
            | set(blob_state_store.list_archive_ids())
        )
        if not archives:
            # 一次「什么都没对」的成功退出，和真的对完了长得一模一样。
            raise ReconciliationError("this deployment hosts no archive to reconcile")
    exit_code = 0
    for archive in archives:
        result = reconcile_archive(
            blob_state_store,
            object_store,
            archive,
            storage_generation=generation,
            deep=deep,
        )
        summary: dict[str, object] = {
            "archive_id": archive,
            "mode": result.mode,
            "blob_count": result.blob_count,
            "storage_generation": generation,
            "other_generation_count": result.other_generation_count,
            **result.issue_counts(),
        }
        if result.clean:
            record = ReconciliationRecord(
                archive_id=archive,
                verified_at=int(time.time()),
                blob_count=result.blob_count,
                mode=result.mode,
                other_generation_count=result.other_generation_count,
            )
            write_record(data_dir, record)
            summary["verified_at"] = record.verified_at
        else:
            # 一次发现差异的对账不能把灯点亮，也不该覆盖上一次干净的记录。
            exit_code = 1
        output.write(
            json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        )
        for label, keys in (
            ("missing_in_store", result.missing_in_store),
            ("mismatches", result.mismatches),
            ("orphaned_in_store", result.orphaned_in_store),
        ):
            # 差异的样本走 stderr：stdout 那一行是给机器读的。
            for key in keys[:5]:
                errors.write(f"{label}: {key}\n")
    return exit_code


def _settle_merges(
    *,
    data_dir: Path,
    archive_id: str | None,
    output: TextIO,
    errors: TextIO,
) -> int:
    """把欠着的会话搬家一次性清完（PSU-2.98 的 S-D）。

    在线上那条后台 worker 是**会让路**的：搬多久让多久，好让同步的写请求总有
    窗口挤进来。真机上的存量是 216,282 行、约 108 批——指望它边服务边磨完，
    用户要等的是小时级。所以清账走这里：停机窗口内不让路地跑完，再起服务。

    取 `SingleWriterLease`（进程级独占），所以**服务必须先停**。这不是额外
    约束，它与「停机窗口」是同一件事。

    单条语句的墙钟守卫在这里**关掉**：这条命令自己就是那条长语句，守卫防的是
    请求路径上的跑飞。

    可中断、可续跑：欠不欠搬按行判，杀掉重跑就从剩下的接着搬。
    """
    if archive_id is not None and _REMOTE_ID.fullmatch(archive_id) is None:
        raise ValueError("invalid archive id")
    auth_path = data_dir / "auth.sqlite"
    metadata_path = data_dir / "metadata.sqlite"
    for required in (auth_path, metadata_path):
        if not required.is_file():
            raise ValueError(f"{required.name} is not present in the data dir")
    with SingleWriterLease.acquire(data_dir):
        store = SQLiteMetadataStore(metadata_path, statement_abort_seconds=None)
        if archive_id is not None:
            archives = [archive_id]
        else:
            archives = sorted(
                item.id for item in SqliteDeviceAuthStore(auth_path).list_archives()
            )
            if not archives:
                raise ValueError("this deployment hosts no archive to settle")
        exit_code = 0
        for archive in archives:
            owed_rows = store.refresh_conversation_merge_pending_rows(archive)
            moved = 0
            settled = 0
            # 一条搬完仍然判成欠搬的链接（四张表任意一张有残留）不该把整条命令
            # 拖住：放它一边，把其余的清完，最后如实说剩下哪几条。
            stuck: set[str] = set()
            contended = 0
            # 有上限。`while True` 在一条清不掉的链接面前就是死循环，而这条命令
            # 跑在停机窗口里——没人守着它。上限按「每 2,000 行一步」给足余量。
            for _step in range(_SETTLE_STEP_LIMIT):
                step = store.settle_conversation_merge_step(archive, skip=stuck)
                moved += step.moved_rows
                settled += step.settled_pairs
                if step.done or (
                    step.conversation_id is None
                    and step.replayed_rows == 0
                    and not step.contended
                ):
                    break
                if step.contended:
                    # 搜索库被别人握着。服务本该已经停了，所以这不该发生——
                    # 但一把没释放的锁不该让这条命令原地转到天亮。
                    contended += 1
                    if contended <= 10:
                        continue
                    errors.write(
                        f"settle-merges: {archive} could not get the search index\n"
                    )
                    exit_code = 1
                    break
                if step.stuck and step.conversation_id is not None:
                    stuck.add(step.conversation_id)
                    errors.write(
                        f"settle-merges: {archive} conversation "
                        f"{step.conversation_id} still owes a move\n"
                    )
                if step.moved_rows:
                    # 一批一行进度：108 批的活，没有输出的十分钟看起来和挂死一样。
                    output.write(
                        f"settle-merges: {archive} moved {moved}/{owed_rows} rows, "
                        f"{step.pending_pairs} conversations still owe a move\n"
                    )
                    output.flush()
            if stuck:
                errors.write(
                    f"settle-merges: {archive} left {len(stuck)} conversation(s) "
                    "unsettled\n"
                )
                exit_code = 1
            # 收尾做一次整账号重建。归并本身不再整账号重建（那正是写瘫的一半），
            # 于是「每次归并顺手校正全部状态行」这层保险没了；在停机窗口里补上，
            # 那里没有请求在等这把锁。
            for account_id in _archive_account_ids(metadata_path, archive):
                store.rebuild_account_state(archive, account_id)
            remaining = store.refresh_conversation_merge_pending_rows(archive)
            output.write(
                json.dumps(
                    {
                        "archive_id": archive,
                        "moved_rows": moved,
                        "settled_conversations": settled,
                        "pending_rows": remaining,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )
            if remaining:
                exit_code = 1
    return exit_code


def _archive_account_ids(metadata_path: Path, archive_id: str) -> tuple[str, ...]:
    """这个归档里有哪些账号。读一次，不经 store——它没有这个面，而收尾重建
    要按账号逐个跑。"""
    with closing(
        sqlite3.connect(f"file:{metadata_path.as_posix()}?mode=ro", uri=True)
    ) as db:
        return tuple(
            str(row[0])
            for row in db.execute(
                "SELECT DISTINCT COALESCE(source_account_id, '') "
                "FROM archive_conversation_state WHERE archive_id = ?",
                (archive_id,),
            )
        )


def _browser_store(data_dir: Path) -> SqliteBrowserAuthStore:
    return SqliteBrowserAuthStore(data_dir / "auth.sqlite", data_dir)


def _reset_browser_login(data_dir: Path, output: TextIO) -> int:
    """重置 = 回到未初始化状态并换发引导令牌。

    **不打印、也不接受口令**：一个能设口令的运维命令迟早会出现在部署脚本与
    shell 历史里。重置之后能进来的仍然只有拿到新令牌的人，与首次部署同一条路径。
    """
    with SingleWriterLease.acquire(data_dir):
        store = _browser_store(data_dir)
        store.reset_to_bootstrap()
        # reset_to_bootstrap 已经换发过一次；这里让文件与 stdout 两条取法与
        # 正常启动完全一致，顺便把令牌落到数据卷里那个 0600 文件。
        publish_bootstrap_token(store, data_dir, stdout=output)
    return 0


def _reset_browser_reauth(data_dir: Path) -> int:
    """服务器所有者的受控恢复（PSU-2.213 §5.3；PSU-2.221 起按设备）。

    关闭所有设备的应用锁、删除全部通行密钥、挑战与解锁授权，并登出所有浏览器
    会话；口令与 TOTP 不动。日常忘了指纹用登录口令就能解锁，这条只用于连口令
    都要重置之类的运维场景。
    """
    with SingleWriterLease.acquire(data_dir):
        browser = _browser_store(data_dir)
        SqliteBrowserReauthStore(data_dir / "auth.sqlite").reset()
        browser.revoke_all_sessions()
    return 0


def _disable_browser_totp(data_dir: Path, errors: TextIO) -> int:
    with SingleWriterLease.acquire(data_dir):
        if not _browser_store(data_dir).clear_totp():
            errors.write("browser login is not set up yet\n")
            return 2
    return 0


def main(
    arguments: Sequence[str] | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run an operator command and return a process exit code."""

    output = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr
    source = os.environ if environment is None else environment
    parsed = _parser().parse_args(arguments)
    if parsed.command not in {
        "provision",
        "reset-browser-login",
        "disable-browser-totp",
        "reset-browser-reauth",
        "reconcile",
        "settle-merges",
        "backup",
    }:
        errors.write("unsupported operator command\n")
        return 2
    if parsed.command == "backup":
        try:
            return backup_cli.run(
                parsed,
                data_dir=runtime_data_dir(source),
                output=output,
                errors=errors,
            )
        except (
            RuntimeConfigurationError,
            BackupConfigurationError,
            BackupArchiveError,
            BackupStoreError,
            ValueError,
            sqlite3.Error,
            OSError,
        ) as error:
            # 稳定代号与结构性事实由被调方决定；这里只保证进程有个退出码，
            # 而且不把 provider 原话或凭据带出来。
            errors.write(f"{error}\n")
            return 2
    if parsed.command == "settle-merges":
        try:
            return _settle_merges(
                data_dir=runtime_data_dir(source),
                archive_id=(
                    None if parsed.archive_id is None else str(parsed.archive_id)
                ),
                output=output,
                errors=errors,
            )
        except (RuntimeConfigurationError, ValueError, sqlite3.Error, OSError):
            errors.write("conversation merges could not be settled\n")
            return 2
    if parsed.command == "reconcile":
        try:
            return _reconcile(
                data_dir=runtime_data_dir(source),
                archive_id=(
                    None if parsed.archive_id is None else str(parsed.archive_id)
                ),
                deep=bool(parsed.deep),
                output=output,
                errors=errors,
            )
        except (
            RuntimeConfigurationError,
            ReconciliationError,
            ObjectStoreNotConfiguredError,
            ValueError,
            sqlite3.Error,
            OSError,
        ):
            errors.write("reconciliation could not be completed\n")
            return 2
    if parsed.command == "reset-browser-login":
        try:
            return _reset_browser_login(runtime_data_dir(source), output)
        except (RuntimeConfigurationError, ValueError, sqlite3.Error, OSError):
            errors.write("browser login could not be reset\n")
            return 2
    if parsed.command == "disable-browser-totp":
        try:
            return _disable_browser_totp(runtime_data_dir(source), errors)
        except (RuntimeConfigurationError, ValueError, sqlite3.Error, OSError):
            errors.write("two-factor authentication could not be unbound\n")
            return 2
    if parsed.command == "reset-browser-reauth":
        try:
            return _reset_browser_reauth(runtime_data_dir(source))
        except (RuntimeConfigurationError, ValueError, sqlite3.Error, OSError):
            errors.write("daily re-authentication could not be reset\n")
            return 2
    try:
        payload = _provision(
            data_dir=runtime_data_dir(source),
            archive_id=str(parsed.archive_id),
            archive_name=str(parsed.archive_name),
            device_name=str(parsed.device_name),
        )
    except (RuntimeConfigurationError, ValueError, sqlite3.Error):
        errors.write("archive could not be provisioned\n")
        return 2
    output.write(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
