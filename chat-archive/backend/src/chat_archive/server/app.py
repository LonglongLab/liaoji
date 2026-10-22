"""自建远程服务 ASGI 应用工厂。"""

# pyright: reportUnusedFunction=false
# FastAPI 路由/异常处理函数经装饰器注册，pyright 不把装饰器调用视为访问。

from __future__ import annotations

from chat_archive.server.workspace import RemoteWorkspace
from chat_archive.version import SERVER_VERSION

from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import asynccontextmanager
import weakref
from urllib.parse import urlsplit
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.types import Lifespan

from chat_archive.adapters.s3.objectstore import S3CompatibleObjectStore
from chat_archive.ports.blob_state import (
    BlobStateConflictError,
    BlobStateStore,
    BlobStateValidationError,
)
from chat_archive.ports.browser_auth import BrowserAuthStore
from chat_archive.ports.browser_reauth import BrowserReauthStore
from chat_archive.server.webauthn_service import WebAuthnConfig, WebAuthnService
from chat_archive.ports.device_auth import BrowserPairingStore, DeviceAuthStore
from chat_archive.ports.metadata import (
    MergeAwareMetadataStore,
    MetadataBusyError,
    MetadataConflictError,
    MetadataStore,
    MetadataUnavailableError,
    MetadataValidationError,
    RecordRefsReadableStore,
    UploadCursorObservableStore,
    WriterLockObservableStore,
)
from chat_archive.ports.remote_archive import RemotePresignedAttachment
from chat_archive.ports.objectstore import (
    ObjectImmutabilityError,
    ObjectIntegrityError,
    ObjectNotFoundError,
    ObjectStore,
    ObjectStoreError,
    ObjectStoreValidationError,
)
from chat_archive.domain.errors import ErrorCode
from chat_archive.server.auth import authenticate_request
from chat_archive.server.backup_config import (
    BackupCredentials,
    SqliteBackupConfigStore,
    backup_status_document,
)
from chat_archive.server.backup_routes import BackupActions, register_backup_routes
from chat_archive.server.object_storage_requests import ObjectStorageConfigRequest
from chat_archive.server.backup_store import BackupStore
from chat_archive.server.share_service import ArchiveContentCopier, ShareService
from chat_archive.server.shares import SqliteShareStore
from chat_archive.server.health_capabilities import HealthUploadModes
from chat_archive.server.error_contract import transport_error_response
from chat_archive.server.http import (
    RemoteServerError,
    apply_security_headers,
    remote_error_response,
)
from chat_archive.server.object_storage_config import (
    ConfigurableObjectStore,
    ObjectStorageConfigSummary,
    ObjectStorageConfigurationError,
    ObjectStorageGenerationNotFoundError,
    ObjectStoreNotConfiguredError,
    SqliteObjectStorageConfigStore,
    presigned_origin,
    validate_configuration,
    verify_object_store,
)
from chat_archive.server.routes import (
    CONTRACT_VERSION,
    SCHEMA_VERSION,
    record_model_version_of,
)


class ObjectStorageRestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    storage_generation: int = Field(ge=1)




def create_server_app(
    auth_store: DeviceAuthStore,
    metadata_store: MetadataStore,
    blob_state_store: BlobStateStore,
    object_store: ObjectStore,
    *,
    lifespan: Lifespan[FastAPI] | None = None,
    backend_type: str | None = None,
    # 上次对账干净跑通的时刻，按归档问（PSU-2.58）。注入一个可调用对象而不是
    # 一个常数：对账是在这个进程之外跑的运维命令，值会在进程活着的时候变；
    # 也不让这一层自己去读文件——`available_bytes` 的注释已经说过为什么。
    last_reconciled_at: Callable[[str], int | None] = lambda _archive_id: None,
    # 上次深度恢复验证通过的时刻（PSU-2.142）。与 `last_reconciled_at` 同形，
    # 理由也同：`backup verify --deep` 是在这个进程之外跑的运维命令，值会在进程
    # 活着的时候变。此前它是个恒 `None` 的常数，那在当时是诚实的——SQLite+S3 这支
    # 既没有演练工具也没有 runbook，而 postgres-s3 与 D1+R2 的演练走的是另外两份
    # runbook、不写这个工件。**默认值仍然是「从没验证过」**：那两支不许被一个
    # 默认值说成已验证。写者见 `server/recovery_verification.py`。
    last_recovery_verified_at: Callable[[], int | None] = lambda: None,
    object_storage_config_store: SqliteObjectStorageConfigStore | None = None,
    browser_auth_store: BrowserAuthStore | None = None,
    # 可选日常再认证（PSU-2.213）。给了 store 才有这件事；策略默认关闭，关闭时
    # 所有旧行为不变。`webauthn_config` 缺失 = 不可用（不能开启；已开启则 fail closed）。
    browser_reauth_store: BrowserReauthStore | None = None,
    webauthn_config: WebAuthnConfig | None = None,
    frontend_dist: Path | None = None,
    # 这套部署给附件留了多少字节，由部署方说出来（PSU-2.139）。`None` = 没说过。
    # 见 `_sync_health` 里那一段：**服务端猜不出这个数**，而猜错的代价是把真数据
    # 挡在门外。
    storage_budget_bytes: int | None = None,
    # 定时备份（PSU-2.140/2.141）。给了这个 store 才有备份这件事：健康页的
    # 四个 `last_backup_*` 键出现、`/backup/*` 那几条路由注册。
    #
    # 不给 = **这套部署答不出备份这件事**（postgres-s3 那一支的灾备是 pgBackRest
    # 的 recovery point，Worker 那一支既没有文件系统也没有长任务）。那时
    # `sync_health` 里一个 `last_backup_*` 键都不出现，路由是 404 而不是 403——
    # 缺键是"还不知道"，`last_backup_at: null` 是"从来没备份成功过"，
    # 两者对操作者意味着完全不同的处置。
    backup_config_store: SqliteBackupConfigStore | None = None,
    # 「立即备份」按下去时叫醒后台线程。没有它，那条路由答 BACKUP_UNAVAILABLE——
    # 配置读得到而线程不在，是运维要知道的事实，不是可以假装成功的事。
    request_backup: Callable[[], None] | None = None,
    # 备份目标的客户端怎么造。生产上就是默认那个 S3 客户端；注入进来是为了
    # 让用例能对着一个不联网的假实现跑完整条路由——与 `object_store` 同一条理由。
    backup_store_factory: object = None,
    # 在线分享（PSU-2.180）。与备份同一条规矩：给了才注册路由，不给＝这套部署
    # 没有分享这件事（postgres-s3 暂不支持、Worker 不支持），那是 404 不是 403。
    share_store: SqliteShareStore | None = None,
    secure_cookie: bool = True,
    on_browser_setup_complete: Callable[[], None] | None = None,
    workspace: RemoteWorkspace | None = None,
) -> FastAPI:
    health_modes = HealthUploadModes(
        object_store, record_claim=isinstance(metadata_store, RecordRefsReadableStore)
    )
    @asynccontextmanager
    async def health_lifespan(
        application: FastAPI,
    ) -> AsyncGenerator[Mapping[str, object] | None, None]:
        try:
            if lifespan is None:
                health_modes.read()
                yield None
            else:
                async with lifespan(application) as state:
                    health_modes.read()
                    yield state
        finally:
            health_modes.close()

    app = FastAPI(
        title="聊记自建远程服务",
        version=SERVER_VERSION,
        openapi_url=None,
        docs_url=None,
        # Starlette types stateful/stateless hooks as a union of callables.
        # The wrapper forwards either original result without changing its shape.
        lifespan=cast(Lifespan[FastAPI], health_lifespan),
    )
    # Test clients that do not enter lifespan still release their cache on GC.
    weakref.finalize(app, health_modes.close)
    resolved_backend_type = backend_type
    if resolved_backend_type is None:
        adapter_name = type(metadata_store).__name__.lower()
        resolved_backend_type = (
            "self_hosted_postgresql" if "postgres" in adapter_name else "self_hosted_sqlite"
        )

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        # 静态托管那条路径自己带一份能跑起来的 CSP 与缓存策略（`spa.py`）。
        # 这里改成「没设过才设」，于是 API 的 `default-src 'none'` 一个字没动，
        # 而 SPA 的响应不会被它按回一个连脚本都加载不了的头。
        apply_security_headers(response, overwrite=False)
        return response

    @app.exception_handler(RemoteServerError)
    def _remote_error(_request: Request, error: RemoteServerError) -> Response:
        return remote_error_response(error)

    # 「还没进到路由就失败」的三类：参数校验不过、方法不对、路径不存在。
    # 下面这两个处理器按**路径**挑错误契约，因为这个
    # app 上同时住着设备令牌那套三字段与浏览器那套六字段（`error_contract.py`）。
    # 从前一律按三字段回，于是同一次 `limit=0` 在本机 app 上是六字段、在这里是
    # 三字段——「同一份前端零改动」这句话在传输层是不成立的。
    @app.exception_handler(RequestValidationError)
    def _validation(request: Request, _error: RequestValidationError) -> Response:
        return transport_error_response(request.url.path, ErrorCode.QUERY_INVALID)

    @app.exception_handler(MetadataValidationError)
    def _metadata_validation(_request: Request, _error: MetadataValidationError) -> Response:
        return remote_error_response(RemoteServerError("QUERY_INVALID", "请求批次未通过校验。"))

    @app.exception_handler(MetadataConflictError)
    def _metadata_conflict(_request: Request, _error: MetadataConflictError) -> Response:
        # 不写「请重试」（PSU-2.165）：两种成因——同一批次 id 内容不同、本地序号与
        # 服务端记的上传位置接不上——重放同一批都只会得到同一个答复。
        return remote_error_response(
            RemoteServerError(
                "SYNC_CONFLICT",
                "批次与服务端记录的上传位置接不上，重放同一批不会好转。",
            )
        )

    @app.exception_handler(MetadataBusyError)
    def _metadata_busy(_request: Request, _error: MetadataBusyError) -> Response:
        """写锁排不进去 → 503 + Retry-After（PSU-2.98）。

        映射写在这里而**不是** `domain/errors.py` 的 `_HTTP_STATUS`：那张表是
        桌面端 API 的，远端服务器不走它。

        与 `SERVICE_UNAVAILABLE` 分开，而且这个区分**真的到得了客户端**：
        `http_pool.send` 对 503 读一次响应体，认出 `REMOTE_BUSY` 就以本名抛出，
        它最终落进 `ManualSyncResult.stopped_error_code`。动作上两者一样（都是
        「等一会儿再来」，所以 `ErrorCode` 那一层不必长出第四个值），说法上不
        一样——2026-09-07 那一小时里用户看到的是「远程不可用」，而它把「等一
        会儿」说成了「坏了」。
        """
        return remote_error_response(
            RemoteServerError(
                "REMOTE_BUSY",
                "远程正忙于另一次写入，请稍后重试。",
                retryable=True,
            ),
            headers={"Retry-After": "5"},
        )

    @app.exception_handler(MetadataUnavailableError)
    def _metadata_unavailable(
        _request: Request,
        _error: MetadataUnavailableError,
    ) -> Response:
        return remote_error_response(
            RemoteServerError(
                "SERVICE_UNAVAILABLE",
                "元数据服务暂时不可用，请重试。",
                retryable=True,
            )
        )

    @app.exception_handler(BlobStateValidationError)
    def _blob_state_validation(_request: Request, _error: BlobStateValidationError) -> Response:
        return remote_error_response(RemoteServerError("QUERY_INVALID", "附件请求未通过校验。"))

    @app.exception_handler(BlobStateConflictError)
    def _blob_state_conflict(_request: Request, _error: BlobStateConflictError) -> Response:
        return remote_error_response(
            RemoteServerError("BLOB_CONFLICT", "附件与服务端状态冲突。")
        )

    # Registered ahead of the generic ObjectStoreError handler it derives from:
    # Starlette resolves handlers along the MRO, so "not configured" reports
    # itself instead of collapsing into "storage is temporarily unavailable".
    @app.exception_handler(ObjectStoreNotConfiguredError)
    def _object_store_not_configured(
        _request: Request,
        _error: ObjectStoreNotConfiguredError,
    ) -> Response:
        return remote_error_response(
            RemoteServerError(
                "OBJECT_STORAGE_NOT_CONFIGURED",
                "对象存储尚未配置，附件功能不可用；请先在设置中配置腾讯云 COS。",
            )
        )

    @app.exception_handler(ObjectStorageConfigurationError)
    def _object_storage_configuration_invalid(
        _request: Request,
        _error: ObjectStorageConfigurationError,
    ) -> Response:
        return remote_error_response(
            RemoteServerError(
                "OBJECT_STORAGE_CONFIGURATION_INVALID",
                "对象存储配置不合法。",
            )
        )

    @app.exception_handler(ObjectStoreValidationError)
    def _object_validation(_request: Request, _error: ObjectStoreValidationError) -> Response:
        return remote_error_response(RemoteServerError("QUERY_INVALID", "附件对象请求无效。"))

    @app.exception_handler(ObjectNotFoundError)
    def _object_not_found(_request: Request, _error: ObjectNotFoundError) -> Response:
        return remote_error_response(RemoteServerError("NOT_FOUND", "附件内容不存在。"))

    @app.exception_handler(ObjectIntegrityError)
    def _object_integrity(_request: Request, _error: ObjectIntegrityError) -> Response:
        return remote_error_response(
            RemoteServerError("BLOB_INTEGRITY", "附件对象完整性校验失败。")
        )

    @app.exception_handler(ObjectImmutabilityError)
    def _object_immutable(_request: Request, _error: ObjectImmutabilityError) -> Response:
        return remote_error_response(
            RemoteServerError("BLOB_CONFLICT", "附件对象已存在且不可覆盖。")
        )

    @app.exception_handler(ObjectStoreError)
    def _object_store_error(_request: Request, _error: ObjectStoreError) -> Response:
        return remote_error_response(
            RemoteServerError(
                "BLOB_CONFLICT",
                "附件对象存储暂时不可用，请重试。",
                retryable=True,
            )
        )

    @app.exception_handler(HTTPException)
    def _http_error(request: Request, error: HTTPException) -> Response:
        return transport_error_response(
            request.url.path,
            ErrorCode.METHOD_NOT_ALLOWED if error.status_code == 405 else ErrorCode.NOT_FOUND,
        )

    @app.get("/api/v1/health")
    async def health() -> dict[str, object]:
        return {
            "status": "ok",
            "server_version": SERVER_VERSION,
            "runtime_profile": "self_hosted",
            "contract_version": CONTRACT_VERSION,
            "schema_version": SCHEMA_VERSION,
            # Asked of the store that would actually serve the read, not of a
            # build-time constant: a v3 binary running against a store frozen
            # at v1 must say 1, or every client would send it rich records it
            # cannot keep and reading orders it cannot produce.
            "record_model_version": record_model_version_of(metadata_store),
            # Which upload shapes this remote can serve. Asked of the store
            # that would serve them, for the same reason as above: one binary
            # in front of MinIO and the same binary in front of a filesystem
            # are not the same remote. A client that does not find this key
            # assumes the two that predate it, so nothing has to be negotiated.
            "blob_upload_modes": list(health_modes.read()),
        }

    @app.get("/api/v1/archives/{archive_id}/devices")
    def list_devices(archive_id: str, request: Request) -> dict[str, object]:
        authenticate_request(request, archive_id, auth_store)
        items = auth_store.list_devices(archive_id)
        return {
            "items": [
                {
                    "id": item.id,
                    "display_name": item.display_name,
                    "status": item.status,
                    "created_at": item.created_at,
                    "last_seen_at": item.last_seen_at,
                    "revoked_at": item.revoked_at,
                    "client_info": None if item.client_info is None else asdict(item.client_info),
                    "last_local_sequence": item.last_local_sequence,
                }
                for item in items
            ]
        }

    @app.get("/api/v1/archives/{archive_id}/stats")
    def archive_stats(archive_id: str, request: Request) -> dict[str, int]:
        authenticate_request(request, archive_id, auth_store)
        stats = metadata_store.stats(archive_id)
        return {
            "message_count": stats.message_count,
            "conversation_count": stats.conversation_count,
            "last_server_sequence": stats.last_server_sequence,
        }

    @app.get("/api/v1/archives/{archive_id}/sync-health")
    def sync_health(archive_id: str, request: Request) -> dict[str, object]:
        context = authenticate_request(request, archive_id, auth_store)
        stats = metadata_store.stats(archive_id)
        storage = metadata_store.storage_health(archive_id, context.device_id)
        # 字节的账问 blob-state store，不问 metadata store（PSU-2.57）。
        # 后者的 `blob_manifests` 自 blob 状态搬家之后就没有写者了，三个数字
        # 恒为 0：存量看着像空的，而 `pending_blob_count` 恒 0 意味着
        # `ATTACHMENTS_PENDING` 永远不会亮。同一次调用里的
        # `last_uploaded_local_sequence` 与 `conflict_count` 读的是
        # `device_cursors` 与 `record_conflicts`，那两个是活的，照旧从它取。
        blobs = blob_state_store.storage_health(archive_id)
        device = next(
            (
                item
                for item in auth_store.list_devices(archive_id)
                if item.id == context.device_id
            ),
            None,
        )
        # A cache miss may perform cross-cloud probes while holding the provider
        # lock. Share public health's background refresh instead of waiting here.
        object_status = health_modes.read_status()
        # 归并进度只有认得账号维度的后端答得出（PSU-2.86 收尾）。答不上来就
        # **整个不报这四个键**，而不是报零——客户端把"缺键"读成"这个远端不知道"，
        # 把零读成"没有待办"，两者对操作者意味着相反的处置。
        merge: dict[str, object] = {}
        if isinstance(metadata_store, MergeAwareMetadataStore):
            backlog = metadata_store.merge_backlog(archive_id)
            merge = {
                "account_link_count": backlog.account_link_count,
                "account_merge_pending": backlog.account_merge_pending,
                "conversation_link_count": backlog.conversation_link_count,
                "conversation_merge_pending": backlog.conversation_merge_pending,
            }
            # 欠搬的**行**数只在后台 worker 数过之后才有（PSU-2.98）。没数过就
            # 不报这个键——同一条口径：缺键是「还不知道」，零是「没有待办」。
            if backlog.conversation_merge_pending_rows is not None:
                merge["conversation_merge_pending_rows"] = (
                    backlog.conversation_merge_pending_rows
                )
            merge["search_rehome_pending_rows"] = backlog.search_rehome_pending
        # 那把全局写锁此刻的样子。**这是 2026-09-07 缺的那一条**：写瘫的一小时里
        # 这个端点全程 0.3 s / 200，因为读路径根本不走写锁，界面据此说「远程可用」。
        if isinstance(metadata_store, WriterLockObservableStore):
            lock = metadata_store.writer_lock_status()
            merge["writer_lock_held_ms"] = lock.held_ms
            merge["writer_lock_rejections_recent"] = lock.rejections_recent
        # 这台设备每个账号的游标（PSU-2.165）。上面那个 `last_uploaded_local_sequence`
        # 是它们的 MAX，客户端撞上「序号不连续」时要对齐的是其中一个。答不出就不报。
        if isinstance(metadata_store, UploadCursorObservableStore):
            merge["account_upload_cursors"] = metadata_store.device_upload_cursors(
                archive_id, context.device_id
            )
        backup: dict[str, object] = {}
        if backup_config_store is not None:
            backup = dict(backup_status_document(backup_config_store))
        return {
            **merge,
            **backup,
            "archive_id": archive_id,
            "backend_type": resolved_backend_type,
            "device_id": context.device_id,
            "last_uploaded_local_sequence": storage.last_uploaded_local_sequence,
            "last_server_sequence": stats.last_server_sequence,
            "remote_used_bytes": blobs.used_bytes,
            "remote_blob_count": blobs.blob_count,
            "pending_attachment_count": blobs.pending_blob_count,
            "conflict_count": storage.conflict_count,
            "last_success_at": None if device is None else device.last_seen_at,
            "last_reconciled_at": last_reconciled_at(archive_id),
            "last_recovery_verified_at": last_recovery_verified_at(),
            "remote_service_status": "healthy",
            "object_storage_status": object_status,
            # 这套部署给附件留的额度还剩多少。附件上行据此在动第一个字节之前
            # 判断放不放得下。
            #
            # **这个数以前是 `shutil.disk_usage(data_dir).free`，也就是本 API 自己
            # 那块数据卷的剩余空间**，前提是「单机部署上它和对象存储是同一块盘」。
            # 2026-09-15 切到腾讯云 COS 那天前提就没了，而数没跟着改：线上于是
            # 一边报 `remote_available_bytes = 6.39 GiB`、一边报
            # `remote_used_bytes = 34 GB`——用量比可用还大，自证量错了对象。
            # 后果是欠账超过 3.39 GiB 的机器再也传不上一张图（PSU-2.139）。
            #
            # 现在只有部署方说过额度才有这个数，没说就是 `null`。客户端把 `null`
            # 读成「它没说」而不是「它没有了」（`SpaceBudget.unknown`），一路放行；
            # 真写满时上传自己的传输会失败得足够响亮。
            "remote_available_bytes": (
                None
                if storage_budget_bytes is None
                else max(storage_budget_bytes - blobs.used_bytes, 0)
            ),
        }


    def _require_config_store() -> SqliteObjectStorageConfigStore:
        if object_storage_config_store is None:
            # This deployment profile has no page-managed configuration at all,
            # which is a different statement from "configured: false".
            raise RemoteServerError("NOT_FOUND", "该部署未启用页面配置对象存储。")
        return object_storage_config_store

    def _summary_response(summary: ObjectStorageConfigSummary) -> dict[str, object]:
        payload = asdict(summary)
        payload["generations"] = [asdict(item) for item in summary.generations]
        return payload

    def _candidate_store(
        endpoint: str,
        bucket: str,
        region: str,
        secret_id: str,
        secret_key: str,
    ) -> S3CompatibleObjectStore:
        validate_configuration(
            endpoint=endpoint,
            bucket=bucket,
            region=region,
            secret_id=secret_id,
            secret_key=secret_key,
        )
        try:
            return S3CompatibleObjectStore(
                endpoint_url=endpoint,
                access_key=secret_id,
                secret_key=secret_key,
                bucket=bucket,
                region=region,
            )
        except (ObjectStoreValidationError, ValueError):
            raise RemoteServerError(
                "OBJECT_STORAGE_CONFIGURATION_INVALID",
                "对象存储配置不合法。",
            ) from None

    async def _verified(candidate: S3CompatibleObjectStore) -> str:
        """Round-trip the candidate, reducing any failure to one stable code.

        Provider error text can quote the request it rejected, so it never
        reaches the caller — only whether verification passed and which
        capabilities are missing.
        """
        try:
            result = await run_in_threadpool(verify_object_store, candidate)
        except (ObjectStoreError, ObjectStoreValidationError, OSError, ValueError):
            raise RemoteServerError(
                "OBJECT_STORAGE_VERIFICATION_FAILED",
                "对象存储往返验证失败：凭据、桶名或权限不正确。",
            ) from None
        return result.status

    @app.get("/api/v1/archives/{archive_id}/object-storage/config")
    def get_object_storage_config(archive_id: str, request: Request) -> dict[str, object]:
        authenticate_request(request, archive_id, auth_store)
        return _summary_response(_require_config_store().summary())

    async def _test_object_storage(body: ObjectStorageConfigRequest) -> dict[str, object]:
        candidate = _candidate_store(
            body.endpoint,
            body.bucket,
            body.region,
            body.secret_id,
            body.secret_key,
        )
        try:
            result = await run_in_threadpool(verify_object_store, candidate)
        except (ObjectStoreError, ObjectStoreValidationError, OSError, ValueError):
            raise RemoteServerError(
                "OBJECT_STORAGE_VERIFICATION_FAILED",
                "对象存储往返验证失败：凭据、桶名或权限不正确。",
            ) from None
        return asdict(result)

    @app.post("/api/v1/archives/{archive_id}/object-storage/config/test")
    async def test_object_storage_config(archive_id: str, request: Request, body: ObjectStorageConfigRequest) -> dict[str, object]:
        authenticate_request(request, archive_id, auth_store)
        return await _test_object_storage(body)

    async def _save_object_storage(body: ObjectStorageConfigRequest) -> dict[str, object]:
        config_store = _require_config_store()
        candidate = _candidate_store(
            body.endpoint,
            body.bucket,
            body.region,
            body.secret_id,
            body.secret_key,
        )
        # Verify before persisting: a candidate that cannot round-trip must
        # leave the working configuration — and the live store — untouched.
        status = await _verified(candidate)
        saved = await run_in_threadpool(
            config_store.save,
            provider=body.provider,
            endpoint=body.endpoint,
            bucket=body.bucket,
            region=body.region,
            secret_id=body.secret_id,
            secret_key=body.secret_key,
        )
        if isinstance(object_store, ConfigurableObjectStore):
            object_store.replace(
                candidate,
                storage_generation=saved.storage_generation,
            )
        return _summary_response(saved) | {"status": status}

    @app.put("/api/v1/archives/{archive_id}/object-storage/config")
    async def put_object_storage_config(archive_id: str, request: Request, body: ObjectStorageConfigRequest) -> dict[str, object]:
        authenticate_request(request, archive_id, auth_store)
        return await _save_object_storage(body)

    @app.post("/api/v1/archives/{archive_id}/object-storage/config/restore")
    async def restore_object_storage_config(
        archive_id: str,
        request: Request,
        body: ObjectStorageRestoreRequest,
    ) -> dict[str, object]:
        """Switch back to a stored generation so its attachments read again."""
        authenticate_request(request, archive_id, auth_store)
        config_store = _require_config_store()
        try:
            credentials = await run_in_threadpool(
                config_store.load_generation_credentials,
                body.storage_generation,
            )
        except ObjectStorageGenerationNotFoundError:
            raise RemoteServerError(
                "OBJECT_STORAGE_GENERATION_MISMATCH",
                "该对象存储代号不存在。",
            ) from None
        candidate = _candidate_store(
            credentials.endpoint,
            credentials.bucket,
            credentials.region,
            credentials.secret_id,
            credentials.secret_key,
        )
        status = await _verified(candidate)
        restored = await run_in_threadpool(
            config_store.activate_generation,
            credentials.storage_generation,
        )
        if isinstance(object_store, ConfigurableObjectStore):
            object_store.replace(
                candidate,
                storage_generation=restored.storage_generation,
            )
        return _summary_response(restored) | {"status": status}

    backup_actions: BackupActions | None = None
    if backup_config_store is not None:
        backup_actions = register_backup_routes(
            app,
            auth_store,
            backup_config_store,
            store_factory=cast(
                "Callable[[BackupCredentials], BackupStore] | None", backup_store_factory
            ),
            request_backup=request_backup,
        )

    share_service: ShareService | None = None
    if share_store is not None:
        from chat_archive.server.share_routes import (
            register_device_share_routes,
            register_share_routes,
        )

        # 远端已有的字节在对象存储里复制（PSU-2.184），取代数与具体存储的办法与
        # 下面的 `AttachmentPresigner` 相同。
        share_service = ShareService(
            share_store,
            object_store,
            copier=ArchiveContentCopier(
                blob_state_store,
                object_store.snapshot
                if isinstance(object_store, ConfigurableObjectStore)
                else lambda: (object_store, 1),
            ),
        )
        if browser_auth_store is None:
            register_share_routes(app, share_store, object_store)
        # 建与管（PSU-2.181）：设备令牌这一半在这里，浏览器那一半经 BFF 走同一个 service。
        register_device_share_routes(app, auth_store, share_service)

    from chat_archive.server.device_routes import register_device_routes

    register_device_routes(app, auth_store)

    from chat_archive.server.query_routes import register_query_routes

    register_query_routes(app, auth_store, metadata_store)

    from chat_archive.server.sync_routes import register_sync_routes

    register_sync_routes(app, auth_store, metadata_store)

    from chat_archive.server.blob_routes import AttachmentPresigner, register_blob_routes

    presigner = AttachmentPresigner(
        blob_state_store,
        object_store,
        storage_generation=(
            object_store.storage_generation
            if isinstance(object_store, ConfigurableObjectStore)
            else None
        ),
        object_store_snapshot=(
            object_store.snapshot
            if isinstance(object_store, ConfigurableObjectStore)
            else None
        ),
    )
    def _presign_for_browser(archive_id: str, blob_id: str, /) -> RemotePresignedAttachment:
        """浏览器这一半的签名入口，形状与设备令牌那条端点逐字段相同。

        走的是同一个 `AttachmentPresigner`，所以两端要么都签得出来、要么都
        拒绝，不会一端给 URL 另一端报错。
        """
        payload = presigner(archive_id, blob_id)
        return RemotePresignedAttachment(
            url=str(payload["url"]),
            method=str(payload["method"]),
            expires_at=int(cast(int, payload["expires_at"])),
            storage_origin=str(payload["storage_origin"]),
        )

    def _object_storage_ready() -> bool:
        """对象存储配没配好——`attachment-view` 是 available 还是 not_configured。

        问的是配置，不是可达性：`probe_capabilities()` 会真发网络请求，而这是
        每次刷页面都要答的能力清单。桶配了但连不上属于「配了但坏了」，由附件
        自己的稳定错误码说，不该让整个入口在界面上消失。
        """
        if not isinstance(object_store, ConfigurableObjectStore):
            return True
        try:
            object_store.snapshot()
        except ObjectStoreNotConfiguredError:
            return False
        return True

    register_blob_routes(
        app,
        auth_store,
        blob_state_store,
        object_store,
        (
            object_store.storage_generation
            if isinstance(object_store, ConfigurableObjectStore)
            else None
        ),
        (
            object_store.snapshot
            if isinstance(object_store, ConfigurableObjectStore)
            else None
        ),
        record_refs=(
            metadata_store.attachment_refs_for_records
            if isinstance(metadata_store, RecordRefsReadableStore)
            else None
        ),
    )

    if browser_auth_store is not None:
        from chat_archive.server.bff_routes import register_bff_routes
        from chat_archive.server.browser_routes import BrowserGate, register_browser_routes

        webauthn_service = WebAuthnService(webauthn_config)
        gate = BrowserGate(
            browser_auth_store,
            secure_cookie=secure_cookie,
            reauth=browser_reauth_store,
            reauth_ready=webauthn_service.available,
        )
        from chat_archive.server.management_routes import register_management_routes
        setup_session = register_management_routes(
            app, gate, auth_store, storage=object_storage_config_store,
            storage_read=lambda: _summary_response(_require_config_store().summary()),
            storage_save=_save_object_storage, storage_test=_test_object_storage,
            backup=backup_actions,
        )
        register_browser_routes(app, gate, on_initialized=on_browser_setup_complete,
                                on_setup_session=setup_session)
        if browser_reauth_store is not None:
            from chat_archive.server.reauth_routes import register_reauth_routes

            register_reauth_routes(app, gate, webauthn_service)
        register_bff_routes(
            app,
            gate,
            auth_store,
            metadata_store,
            # 浏览器这一半与设备令牌那一半共用同一个 presigner：同一条
            # remote_ready 判定、同一代对象存储、同一份 URL 形状。
            _presign_for_browser,
            _object_storage_ready,
            backup_config_store=backup_config_store,
            share_service=share_service,
            workspace=workspace,
            storage_management=object_storage_config_store is not None,
            pairing_management=isinstance(auth_store, BrowserPairingStore),
        )

        from chat_archive.spa import register_spa_routes

        def _storage_origin() -> str | None:
            """当前对象存储的 scheme+host，没配就是 None。

            CSP 要放行的正是这一个 origin。取自**已保存的配置**而不是某个环境
            变量：用户在设置页换了桶，下一次刷新就该生效，不必重启。

            有生效中的存储就按它**实际签出来的**地址算（PSU-2.129）：腾讯云 COS
            按桶名子域名寻址，签名链接在 `<bucket>.cos.<region>.myqcloud.com`，与
            配置里的端点不是同一个 host。签不出来（没配、或后端不会签）才退回端点。
            """
            if isinstance(object_store, ConfigurableObjectStore):
                concrete = object_store.current_store()
                if concrete is not None:
                    signed = presigned_origin(concrete)
                    if signed is not None:
                        return signed
            if object_storage_config_store is None:
                return None
            endpoint = object_storage_config_store.summary().endpoint
            if not endpoint:
                return None
            parsed = urlsplit(endpoint)
            if not parsed.scheme or not parsed.netloc:
                return None
            return f"{parsed.scheme}://{parsed.netloc}"

        if share_store is not None:
            from chat_archive.server.share_routes import register_share_routes

            register_share_routes(app, share_store, object_store, frontend_dist=frontend_dist,
                                  storage_origin=_storage_origin)

        # 最后一条：它有一个吃掉所有剩余路径的兜底路由（history 模式深链）。
        register_spa_routes(
            app,
            frontend_dist,
            # 远端要按路径在本地契约与远端契约两份信封间挑，那条判定留在 server/。
            not_found=lambda path: transport_error_response(path, ErrorCode.NOT_FOUND),
            storage_origin=_storage_origin,
        )

    return app
