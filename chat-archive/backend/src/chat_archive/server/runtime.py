"""Production runtime wiring for the single-node self-hosted server."""

from __future__ import annotations

from chat_archive.server.workspace import RemoteWorkspace

import os
import re
import secrets
import sqlite3
import sys
from collections.abc import AsyncGenerator, Collection, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import TextIO
from urllib.parse import urlsplit

from fastapi import FastAPI

from chat_archive.adapters.s3.objectstore import S3CompatibleObjectStore
from chat_archive.adapters.sqlite.blob_state import SqliteBlobStateStore
from chat_archive.adapters.sqlite.browser_auth import SqliteBrowserAuthStore
from chat_archive.adapters.sqlite.browser_reauth import SqliteBrowserReauthStore
from chat_archive.adapters.sqlite.device_auth import SqliteDeviceAuthStore
from chat_archive.adapters.sqlite.metadata import SQLiteMetadataStore
from chat_archive.conversation_merge import ConversationMergeWorker
from chat_archive.ports.metadata import ConversationMergeStep
from chat_archive.remote_search import SearchBackfillWorker
from chat_archive.server.app import create_server_app
from chat_archive.server.webauthn_service import WebAuthnConfig
from chat_archive.server.backup_cli import build_backup_store, config_store_for
from chat_archive.server.backup_worker import BackupWorker
from chat_archive.server.object_storage_config import (
    ConfigurableObjectStore,
    ObjectStorageConfigurationError,
    SqliteObjectStorageConfigStore,
)
from chat_archive.server.reconciliation import last_reconciled_at_reader
from chat_archive.server.share_sweeper import ShareSweeper
from chat_archive.server.shares import SHARES_DATABASE, SqliteShareStore
from chat_archive.server.recovery_verification import (
    last_recovery_verified_at_reader,
)

# 在线归并 worker 一批搬多少行。缺省的 2,000 行（`_ACCOUNT_MERGE_CHUNK_ROWS`，
# 也是 `settle-merges` 停机清账用的量）在真机上量过：一批连索引重排要 4～4.7 秒，
# 而写锁的 `acquire_timeout` 是 5 秒——落在批头的同步写请求只差零点几秒就吃
# REMOTE_BUSY（2026-09-08 部署当天实测：identity 发布等了 1.9 秒）。降到 500，
# 每次持锁约 1 秒出头；duty cycle 不变，总时长不变，变的只是让路的粒度。
_MERGE_WORKER_BATCH_ROWS = 500

OBJECT_STORAGE_DATABASE = "object-storage.sqlite"
# 引导令牌的明文落点。数据卷在容器里是唯一可写的地方（根文件系统只读），
# 而运维要有一条不必去翻 docker 日志的取法。初始化完成即删除。
BOOTSTRAP_TOKEN_FILE = "browser-bootstrap-token"

_BUCKET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_MAX_SECRET_BYTES = 4096


class RuntimeConfigurationError(RuntimeError):
    """Stable, privacy-safe startup failure."""


def _required(environment: Mapping[str, str], name: str) -> str:
    value = environment.get(name)
    if value is None or not value.strip():
        raise RuntimeConfigurationError(f"{name} is required")
    return value.strip()


def _absolute_path(environment: Mapping[str, str], name: str) -> Path:
    raw = _required(environment, name)
    path = Path(raw)
    if not path.is_absolute():
        raise RuntimeConfigurationError(f"{name} must be an absolute path")
    return path


def secret_from_file(environment: Mapping[str, str], name: str) -> str:
    """Read a small single-line secret from an absolute mounted file."""

    path = _absolute_path(environment, name)
    try:
        payload = path.read_bytes()
    except OSError:
        raise RuntimeConfigurationError(f"{name} is unreadable") from None
    if not payload or len(payload) > _MAX_SECRET_BYTES:
        raise RuntimeConfigurationError(f"{name} is invalid")
    try:
        value = payload.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise RuntimeConfigurationError(f"{name} is invalid") from None
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        raise RuntimeConfigurationError(f"{name} is invalid")
    return value


def _optional_absolute_path(environment: Mapping[str, str], name: str) -> Path | None:
    """未设即 None：没有前端产物时服务器照常只提供 API。"""
    raw = environment.get(name)
    if raw is None or not raw.strip():
        return None
    path = Path(raw.strip())
    if not path.is_absolute():
        raise RuntimeConfigurationError(f"{name} must be an absolute path")
    return path


def _optional_positive_int(environment: Mapping[str, str], name: str) -> int | None:
    """未设即 None，也就是「这套部署没说过它的额度」（PSU-2.139）。

    不接受 0 和负数：那不是「没有额度」，那是一个打错的数，而一个打错的额度会
    让服务器对每一次附件上行说「放不下」——正是本单元要修掉的那种沉默失败。
    """
    raw = environment.get(name)
    if raw is None or not raw.strip():
        return None
    try:
        value = int(raw.strip(), 10)
    except ValueError:
        raise RuntimeConfigurationError(f"{name} must be a positive integer") from None
    if value <= 0:
        raise RuntimeConfigurationError(f"{name} must be a positive integer")
    return value


def _strict_bool(environment: Mapping[str, str], name: str, *, default: bool) -> bool:
    raw = environment.get(name)
    if raw is None:
        return default
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise RuntimeConfigurationError(f"{name} must be true or false")


def _validate_endpoint(endpoint: str, *, allow_insecure: bool) -> str:
    parsed = urlsplit(endpoint)
    allowed_scheme = parsed.scheme == "https" or (
        allow_insecure and parsed.scheme == "http"
    )
    if (
        not allowed_scheme
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise RuntimeConfigurationError("CHAT_ARCHIVE_S3_ENDPOINT is invalid")
    return endpoint.rstrip("/")


def runtime_data_dir(environment: Mapping[str, str] | None = None) -> Path:
    """Load only the non-secret data directory used by operator commands."""

    source = os.environ if environment is None else environment
    return _absolute_path(source, "CHAT_ARCHIVE_DATA_DIR")


def build_object_storage(
    data_dir: Path,
) -> tuple[SqliteObjectStorageConfigStore, ConfigurableObjectStore]:
    """Wire the page-managed object storage without contacting any provider.

    Startup must succeed with no object storage at all: the archive still
    serves messages, and attachments report ``not_configured`` until someone
    submits credentials in the UI. A stored configuration that cannot be
    decrypted — master key lost with the database restored, say — also starts,
    unconfigured, because refusing to boot would take the readable messages
    down with the unreadable attachments.
    """
    config_store = SqliteObjectStorageConfigStore(
        data_dir / OBJECT_STORAGE_DATABASE,
        data_dir,
    )
    try:
        credentials = config_store.load_credentials()
    except ObjectStorageConfigurationError:
        credentials = None
    if credentials is None:
        return config_store, ConfigurableObjectStore()
    initial = S3CompatibleObjectStore(
        endpoint_url=credentials.endpoint,
        access_key=credentials.secret_id,
        secret_key=credentials.secret_key,
        bucket=credentials.bucket,
        region=credentials.region,
    )
    return config_store, ConfigurableObjectStore(
        initial,
        storage_generation=credentials.storage_generation,
    )


def _webauthn_config(source: Mapping[str, str]) -> WebAuthnConfig | None:
    rp_id = source.get("CHAT_ARCHIVE_WEBAUTHN_RP_ID", "").strip()
    origins = source.get("CHAT_ARCHIVE_WEBAUTHN_ORIGINS", "").strip()
    if not rp_id and not origins:
        return None
    if not rp_id or not origins:
        raise RuntimeConfigurationError(
            "CHAT_ARCHIVE_WEBAUTHN_RP_ID and CHAT_ARCHIVE_WEBAUTHN_ORIGINS must be set together"
        )
    # 生产只接受 https；`localhost` 只在测试里直接构造 WebAuthnConfig 时放开。
    config = WebAuthnConfig.parse(rp_id, origins, allow_localhost=False)
    if config is None:
        raise RuntimeConfigurationError("CHAT_ARCHIVE_WEBAUTHN_* is invalid")
    return config


@dataclass(frozen=True)
class RuntimeConfig:
    """Validated self-hosted runtime configuration.

    Credential fields are deliberately excluded from ``repr``. Production
    startup accepts credentials only from mounted files and never falls back
    to plaintext environment variables.

    The ``s3_*`` fields are validated when present but no longer build the
    object store: object storage is configured from the settings page and kept
    encrypted in the data directory. They are retained only so a leftover
    environment fails loudly instead of being silently half-read; there is
    deliberately no import path from them into the stored configuration.
    """

    data_dir: Path
    frontend_dist: Path | None = None
    # 这套部署给附件留了多少字节（`CHAT_ARCHIVE_STORAGE_BUDGET_BYTES`，PSU-2.139）。
    # **不推断**，和 `frontend_dist` 同一条理由：附件的字节落在配置好的对象存储里，
    # 而 S3/COS 没有「还剩多少」这个问题的 API，服务端猜不出来。没说就是没说，
    # 下游把 `None` 读成「不知道」并一路放行。
    storage_budget_bytes: int | None = None
    # 可选日常再认证的 RP（PSU-2.213）。`CHAT_ARCHIVE_WEBAUTHN_RP_ID` 与
    # `CHAT_ARCHIVE_WEBAUTHN_ORIGINS`（逗号分隔的 https Origin）要么都不给、要么都给；
    # 只给一半或不合法直接启动失败。不给 = 再认证不可用，绝不从 Host 推导。
    webauthn: WebAuthnConfig | None = None
    s3_endpoint: str | None = None
    s3_bucket: str | None = None
    s3_region: str | None = None
    s3_access_key: str | None = field(default=None, repr=False)
    s3_secret_key: str | None = field(default=None, repr=False)
    s3_allow_insecure: bool = False

    @classmethod
    def from_environ(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> RuntimeConfig:
        source = os.environ if environment is None else environment
        data_dir = runtime_data_dir(source)
        frontend_dist = _optional_absolute_path(source, "CHAT_ARCHIVE_FRONTEND_DIST")
        storage_budget_bytes = _optional_positive_int(
            source, "CHAT_ARCHIVE_STORAGE_BUDGET_BYTES"
        )
        webauthn = _webauthn_config(source)
        has_s3_environment = any(name.startswith("CHAT_ARCHIVE_S3_") for name in source)
        if not has_s3_environment:
            return cls(
                data_dir=data_dir,
                frontend_dist=frontend_dist,
                storage_budget_bytes=storage_budget_bytes,
                webauthn=webauthn,
            )
        allow_insecure = _strict_bool(source, "CHAT_ARCHIVE_S3_ALLOW_INSECURE", default=False)
        endpoint = _validate_endpoint(_required(source, "CHAT_ARCHIVE_S3_ENDPOINT"), allow_insecure=allow_insecure)
        bucket = _required(source, "CHAT_ARCHIVE_S3_BUCKET")
        if _BUCKET_PATTERN.fullmatch(bucket) is None or ".." in bucket:
            raise RuntimeConfigurationError("CHAT_ARCHIVE_S3_BUCKET is invalid")
        region = _required(source, "CHAT_ARCHIVE_S3_REGION")
        return cls(
            data_dir=runtime_data_dir(source),
            frontend_dist=frontend_dist,
            storage_budget_bytes=storage_budget_bytes,
            webauthn=webauthn,
            s3_endpoint=endpoint,
            s3_bucket=bucket,
            s3_region=region,
            s3_access_key=secret_from_file(
                source,
                "CHAT_ARCHIVE_S3_ACCESS_KEY_FILE",
            ),
            s3_secret_key=secret_from_file(
                source,
                "CHAT_ARCHIVE_S3_SECRET_KEY_FILE",
            ),
            s3_allow_insecure=allow_insecure,
        )


class SingleWriterLease:
    """Process-lifetime exclusive lease for the single-node SQLite runtime."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection: sqlite3.Connection | None = connection

    @classmethod
    def acquire(cls, data_dir: Path) -> SingleWriterLease:
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                data_dir / ".single-writer.sqlite",
                timeout=0,
                isolation_level=None,
                check_same_thread=False,
            )
        except OSError:
            raise RuntimeConfigurationError("runtime data directory is unavailable") from None
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS runtime_lease (id INTEGER PRIMARY KEY)"
            )
            connection.execute("PRAGMA busy_timeout = 0")
            connection.execute("BEGIN EXCLUSIVE")
        except sqlite3.Error:
            connection.close()
            raise RuntimeConfigurationError(
                "another self-hosted API instance is already active"
            ) from None
        return cls(connection)

    def close(self) -> None:
        connection = self._connection
        if connection is None:
            return
        self._connection = None
        try:
            connection.rollback()
        finally:
            connection.close()

    def __enter__(self) -> SingleWriterLease:
        return self

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()


def publish_bootstrap_token(
    store: SqliteBrowserAuthStore,
    data_dir: Path,
    *,
    stdout: TextIO | None = None,
) -> str | None:
    """未初始化就换发一个一次性引导令牌，并让运维拿得到它。

    两条取法，因为 `docker compose up -d` 之后没人看得见 stdout：
    - 打印一行到 stdout（进 docker 日志）；
    - 写进数据卷里一个 `0600` 文件，`docker compose exec api cat …` 取。

    完成初始化之后文件立即删除。令牌明文**不进镜像、不进 Git**，也永远不写
    进任何一份配置——它只在这两处存在，且用一次就作废。
    """
    token_path = data_dir / BOOTSTRAP_TOKEN_FILE
    if store.state().initialized:
        token_path.unlink(missing_ok=True)
        return None
    token = store.issue_bootstrap_token()
    staging = token_path.with_name(f".{token_path.name}.{secrets.token_hex(8)}.tmp")
    try:
        descriptor = os.open(
            staging,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        try:
            os.write(descriptor, f"{token.value}\n".encode("ascii"))
        finally:
            os.close(descriptor)
        os.replace(staging, token_path)
        try:
            os.chmod(token_path, 0o600)
        except OSError:
            pass
    except OSError:
        # 文件写不成不该把服务顶掉——stdout 那条路还在，而没有令牌就意味着
        # 谁也进不去，那才是真的把人锁在门外。
        staging.unlink(missing_ok=True)
    target = sys.stdout if stdout is None else stdout
    target.write(
        f"chat-archive: browser setup is not complete. one-time bootstrap token: {token.value}\n"
    )
    target.flush()
    return token.value




def create_runtime_app(config: RuntimeConfig | None = None) -> FastAPI:
    """Build the production ASGI app without contacting object storage."""

    settings = RuntimeConfig.from_environ() if config is None else config
    lease = SingleWriterLease.acquire(settings.data_dir)
    # 存量记录的索引回填（PSU-2.45）。在这里建、在 lifespan 里起停：一条只写
    # `search.sqlite` 的后台线程，不占启动路径，也不与同步或附件长传抢任何东西。
    backfill: SearchBackfillWorker | None = None
    # 会话归并（PSU-2.98）。它**曾经**跑在每一轮同步的第一个写请求里，于是
    # 2026-09-07 那 216,282 行把所有设备的写入口一起瘫掉一小时。挪到这里：
    # 同一条起停的壳，一批一放锁，搬多久让多久。
    merges: ConversationMergeWorker | None = None
    # 定时全量备份（PSU-2.140）。同一条起停的壳：快照是读，不与同步争写锁。
    # 没配置过备份目标时它照样起，只是每一轮都立刻返回——「未配置」不是失败。
    backups: BackupWorker | None = None
    # 过期分享的清理（PSU-2.180）。同一条起停的壳，一小时一醒。
    share_sweeper: ShareSweeper | None = None

    workspace: RemoteWorkspace | None = None

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        if backfill is not None:
            backfill.start()
        if merges is not None:
            merges.start()
        if backups is not None:
            backups.start()
        if share_sweeper is not None:
            share_sweeper.start()
        try:
            yield
        finally:
            if workspace is not None:
                workspace.close()
            if share_sweeper is not None:
                share_sweeper.stop()
            if backups is not None:
                backups.stop()
            if merges is not None:
                merges.stop()
            if backfill is not None:
                backfill.stop()
            lease.close()

    try:
        auth_store = SqliteDeviceAuthStore(settings.data_dir / "auth.sqlite")
        blob_state_store = SqliteBlobStateStore(
            settings.data_dir / "blob-state.sqlite"
        )
        metadata_store = SQLiteMetadataStore(
            settings.data_dir / "metadata.sqlite",
            # 合并 `attachment_refs` 时要问「这个 blob 落地了没有」（PSU-2.82）。
            # 两张表住在两个库、由两个组件拥有，所以在装配根这一层把它们接起来，
            # 而不是让记录存储自己去找 blob 状态库——建在它前面就是为了这一步。
            blob_manifest_states=blob_state_store.manifest_states,
            # 两台机器都有原图时留大的那份（PSU-2.143）。
            blob_manifest_sizes=blob_state_store.ready_manifest_sizes,
            forward_media_manifests=blob_state_store.forward_media_manifests,
        )
        config_store, object_store = build_object_storage(settings.data_dir)
        browser_auth_store = SqliteBrowserAuthStore(
            settings.data_dir / "auth.sqlite",
            settings.data_dir,
        )
        publish_bootstrap_token(browser_auth_store, settings.data_dir)
        # 再认证表与登录会话同住 auth.sqlite（外键级联到 browser_sessions），
        # 必须在登录 store 建好表之后再建。策略默认关闭。
        browser_reauth_store = SqliteBrowserReauthStore(settings.data_dir / "auth.sqlite")
        token_path = settings.data_dir / BOOTSTRAP_TOKEN_FILE

        def _forget_bootstrap_token() -> None:
            # 向导一跑完就删。令牌本身已经作废，但明文留在卷里没有任何用处，
            # 而 README 承诺的正是「完成即删」。
            token_path.unlink(missing_ok=True)

        backfill = SearchBackfillWorker(
            archives=lambda: [item.id for item in auth_store.list_archives()],
            backfill=lambda archive_id, chunk: metadata_store.backfill_search_chunk(
                archive_id, chunk_size=chunk
            ),
        )
        def _merge_step(
            archive_id: str, *, skip: Collection[str] = ()
        ) -> ConversationMergeStep:
            return metadata_store.settle_conversation_merge_step(
                archive_id, max_rows=_MERGE_WORKER_BATCH_ROWS, skip=skip
            )

        merges = ConversationMergeWorker(
            archives=lambda: [item.id for item in auth_store.list_archives()],
            step=_merge_step,
            refresh_pending_rows=(
                metadata_store.refresh_conversation_merge_pending_rows
            ),
        )
        # 账号归并把索引写留在账本上（PSU-2.101）；归并一结束就叫醒 worker。
        metadata_store.on_search_rehome_pending = merges.poke
        backup_config_store = config_store_for(settings.data_dir)
        # 上一个进程留下的 `running` 收尾成失败：它们的线程随进程没了，
        # 留着会让健康页永远显示"正在备份"。
        backup_config_store.abandon_running()
        backups = BackupWorker(
            data_dir=settings.data_dir,
            config_store=backup_config_store,
            store_factory=build_backup_store,
        )
        share_store = SqliteShareStore(settings.data_dir / SHARES_DATABASE)
        share_sweeper = ShareSweeper(store=share_store, objects=object_store)
        workspace = RemoteWorkspace(settings.data_dir / "metadata.sqlite",
                                    settings.data_dir / "workspace", blob_state_store, object_store)
        app = create_server_app(
            auth_store,
            metadata_store,
            blob_state_store,
            object_store,
            lifespan=lifespan,
            object_storage_config_store=config_store,
            browser_auth_store=browser_auth_store,
            browser_reauth_store=browser_reauth_store,
            webauthn_config=settings.webauthn,
            frontend_dist=settings.frontend_dist,
            storage_budget_bytes=settings.storage_budget_bytes,
            # 对账结论是数据卷上的一份运维工件，由 `chat-archive-server reconcile`
            # 写、这里读（PSU-2.58）。绑数据目录这件事属于这一层。
            last_reconciled_at=last_reconciled_at_reader(settings.data_dir),
            # 深度恢复验证同理（PSU-2.142）：`backup verify --deep` 写数据卷上
            # 一份工件，这里读。此前这个参数一个都没传，于是
            # `RECOVERY_VERIFICATION_REQUIRED` 恒亮，与验证做没做过无关。
            last_recovery_verified_at=last_recovery_verified_at_reader(
                settings.data_dir
            ),
            backup_config_store=backup_config_store,
            request_backup=backups.poke,
            share_store=share_store,
            on_browser_setup_complete=_forget_bootstrap_token,
            workspace=workspace,
        )
    except BaseException:
        lease.close()
        raise

    # Keep the lease alive for the process lifetime and release it during a
    # graceful ASGI shutdown. The attribute name is private by convention so
    # it cannot accidentally enter API serialization.
    app.state._single_writer_lease = lease
    return app
