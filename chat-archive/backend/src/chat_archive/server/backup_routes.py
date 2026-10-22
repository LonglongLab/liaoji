"""备份这件事对外说得出来的那几条路由（PSU-2.141）。

**只有自建 SQLite 这一支注册它们。** postgres-s3 那一支的消息在 PostgreSQL 里，
它的灾备是 pgBackRest 的 recovery point（PSU-2.6b）；Worker 那一支既没有文件系统
也没有长任务。不属于本端的能力**不注册路由**——按边界文档，那是 404，不是 403。

响应里没有凭据、没有对象键、没有路径、没有 provider 原话。失败只报稳定代号。
"""

# pyright: reportUnusedFunction=false
# FastAPI route functions are registered through decorators.

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from dataclasses import asdict
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field

from chat_archive.ports.device_auth import DeviceAuthStore
from chat_archive.server.auth import authenticate_request
from chat_archive.server.backup_archive import (
    BackupArchiveError,
    latest_backup,
    object_key,
    read_manifest,
)
from chat_archive.server.backup_config import (
    DEFAULT_DAILY_KEEP,
    DEFAULT_INTERVAL_HOURS,
    DEFAULT_MONTHLY_KEEP,
    DEFAULT_WEEKLY_KEEP,
    MINIMUM_RETAINED,
    RETENTION_APPLICATION,
    RETENTION_LIFECYCLE,
    RUN_RUNNING,
    BackupConfigSummary,
    BackupConfigurationError,
    BackupCredentials,
    BackupPolicy,
    BackupRun,
    SqliteBackupConfigStore,
    backup_status_document,
    validate_policy,
    validate_target,
)
from chat_archive.server.backup_store import (
    BackupStore,
    BackupStoreError,
    S3BackupStore,
    verify_backup_store,
)
from chat_archive.server.http import RemoteServerError

# 签名链接活多久。够把 400 MB 拉下来，又短到捡到也没用。
DOWNLOAD_EXPIRES_SECONDS = 900
RECENT_RUN_LIMIT = 20


class BackupConfigRequest(BaseModel):
    """凭据只在进来的方向上出现，没有任何一条路径把它送回去。"""

    model_config = ConfigDict(extra="forbid")

    endpoint: str = Field(min_length=8, max_length=512)
    bucket: str = Field(min_length=3, max_length=128)
    region: str = Field(min_length=2, max_length=64)
    prefix: str = Field(min_length=1, max_length=128)
    secret_id: str = Field(min_length=8, max_length=256, repr=False)
    secret_key: str = Field(min_length=8, max_length=512, repr=False)
    interval_hours: int = Field(default=DEFAULT_INTERVAL_HOURS, ge=1, le=720)
    retention_mode: Literal["lifecycle", "application"] = RETENTION_LIFECYCLE
    daily_keep: int = Field(default=DEFAULT_DAILY_KEEP, ge=MINIMUM_RETAINED, le=3650)
    weekly_keep: int = Field(default=DEFAULT_WEEKLY_KEEP, ge=0, le=3650)
    monthly_keep: int = Field(default=DEFAULT_MONTHLY_KEEP, ge=0, le=3650)

    def policy(self) -> BackupPolicy:
        return BackupPolicy(
            interval_hours=self.interval_hours,
            retention_mode=self.retention_mode,
            daily_keep=self.daily_keep,
            weekly_keep=self.weekly_keep,
            monthly_keep=self.monthly_keep,
        )

    def credentials(self) -> BackupCredentials:
        return BackupCredentials(
            endpoint=self.endpoint,
            bucket=self.bucket,
            region=self.region,
            prefix=self.prefix,
            secret_id=self.secret_id,
            secret_key=self.secret_key,
        )


def _config_response(summary: BackupConfigSummary) -> dict[str, object]:
    return {
        "configured": summary.configured,
        "endpoint": summary.endpoint,
        "bucket": summary.bucket,
        "region": summary.region,
        "prefix": summary.prefix,
        "access_key_hint": summary.access_key_hint,
        # 恢复之后是 False：主密钥不进备份。界面据此说「凭据要重填」，
        # 而不是显示一个永远不跑的「已配置」。
        "credentials_readable": summary.credentials_readable,
        "updated_at": summary.updated_at,
        "interval_hours": summary.policy.interval_hours,
        "retention_mode": summary.policy.retention_mode,
        "daily_keep": summary.policy.daily_keep,
        "weekly_keep": summary.policy.weekly_keep,
        "monthly_keep": summary.policy.monthly_keep,
    }


def _run_response(run: BackupRun) -> dict[str, object]:
    return asdict(run)


def default_backup_store(credentials: BackupCredentials) -> BackupStore:
    return S3BackupStore(
        endpoint_url=credentials.endpoint,
        access_key=credentials.secret_id,
        secret_key=credentials.secret_key,
        bucket=credentials.bucket,
        region=credentials.region,
    )


@dataclass(frozen=True)
class BackupActions:
    get_config: Callable[[], dict[str, object]]
    test_config: Callable[[BackupConfigRequest], Awaitable[dict[str, object]]]
    put_config: Callable[[BackupConfigRequest], Awaitable[dict[str, object]]]
    run: Callable[[], dict[str, object]]
    disable: Callable[[], None]
    credentials: Callable[[], BackupCredentials | None]


def register_backup_routes(
    app: FastAPI,
    auth_store: DeviceAuthStore,
    config_store: SqliteBackupConfigStore,
    *,
    store_factory: Callable[[BackupCredentials], BackupStore] | None = None,
    request_backup: Callable[[], None] | None = None,
) -> BackupActions:
    build_store = default_backup_store if store_factory is None else store_factory
    poke = request_backup

    def _validate(body: BackupConfigRequest) -> None:
        # 规则与 CLI、与存储层是**同一份**（`backup_config` 的两个模块级函数），
        # 不在路由层另立一套会烂掉的副本。
        validate_target(
            endpoint=body.endpoint,
            bucket=body.bucket,
            region=body.region,
            prefix=body.prefix,
            secret_id=body.secret_id,
            secret_key=body.secret_key,
        )
        validate_policy(body.policy())

    def _invalid(error: BackupConfigurationError) -> RemoteServerError:
        # 校验消息本身是无害的（"prefix is invalid" 一类），但它由被调方措辞，
        # 这里不再加工，也不附带用户填进来的值。
        return RemoteServerError("BACKUP_CONFIGURATION_INVALID", str(error))

    def _verify(candidate: BackupCredentials) -> dict[str, object]:
        store = build_store(candidate)
        result = verify_backup_store(store, candidate.prefix)
        return {
            "verified": result.verified,
            "can_delete": result.can_delete,
            "lifecycle_prefixes": (
                None if result.lifecycle_prefixes is None else list(result.lifecycle_prefixes)
            ),
        }

    async def _verified(candidate: BackupCredentials) -> dict[str, object]:
        try:
            return await run_in_threadpool(_verify, candidate)
        except (BackupStoreError, OSError, ValueError):
            raise RemoteServerError(
                "BACKUP_VERIFICATION_FAILED",
                "备份目标往返验证失败：端点、桶名、前缀或权限不正确。",
            ) from None

    def _load_credentials() -> BackupCredentials | None:
        """解不开就是一条稳定代号，不是 500。恢复之后必然走到这里。"""
        try:
            return config_store.load_credentials()
        except BackupConfigurationError:
            raise RemoteServerError(
                "BACKUP_CREDENTIALS_UNREADABLE",
                "这台机器解不开存着的备份凭据（恢复后主密钥不在了）；请重新填写一次。",
            ) from None

    def _status_payload() -> dict[str, object]:
        payload = backup_status_document(config_store)
        payload["runs"] = [_run_response(item) for item in config_store.recent_runs(RECENT_RUN_LIMIT)]
        return payload

    @app.get("/api/v1/archives/{archive_id}/backup/config")
    def get_backup_config(archive_id: str, request: Request) -> dict[str, object]:
        authenticate_request(request, archive_id, auth_store)
        return _config_response(config_store.summary())

    async def _test_config(body: BackupConfigRequest) -> dict[str, object]:
        try:
            _validate(body)
        except BackupConfigurationError as error:
            raise _invalid(error) from None
        return await _verified(body.credentials())

    async def _put_config(body: BackupConfigRequest) -> dict[str, object]:
        candidate = body.credentials()
        policy = body.policy()
        try:
            _validate(body)
        except BackupConfigurationError as error:
            raise _invalid(error) from None
        # 先验证、再保存：验不过的候选必须让现有配置原封不动。
        verification = await _verified(candidate)
        if policy.retention_mode == RETENTION_APPLICATION and not verification["can_delete"]:
            # 选了"应用自己删"却删不动，不会报错，只会一路堆到把桶撑满。
            # 这是唯一能在配置那一刻抓住它的地方。
            # 自己的代号，不与「前缀不合规」共用：桌面端只转述代号、不转述文案，
            # 共用一个代号就等于把这句话丢在远端。
            raise RemoteServerError(
                "BACKUP_RETENTION_NEEDS_DELETE",
                "这把密钥没有删除权限，无法用「应用自行清理」的保留方式；"
                "请改用生命周期规则，或换一把有删除权限的密钥。",
            )
        saved = await run_in_threadpool(
            config_store.save,
            endpoint=candidate.endpoint,
            bucket=candidate.bucket,
            region=candidate.region,
            prefix=candidate.prefix,
            secret_id=candidate.secret_id,
            secret_key=candidate.secret_key,
            policy=policy,
        )
        return _config_response(saved)

    @app.post("/api/v1/archives/{archive_id}/backup/config/test")
    async def test_backup_config(archive_id: str, request: Request, body: BackupConfigRequest) -> dict[str, object]:
        authenticate_request(request, archive_id, auth_store)
        return await _test_config(body)

    @app.put("/api/v1/archives/{archive_id}/backup/config")
    async def put_backup_config(archive_id: str, request: Request, body: BackupConfigRequest) -> dict[str, object]:
        authenticate_request(request, archive_id, auth_store)
        return await _put_config(body)

    @app.get("/api/v1/archives/{archive_id}/backup/runs")
    def list_backup_runs(archive_id: str, request: Request) -> dict[str, object]:
        authenticate_request(request, archive_id, auth_store)
        return _status_payload()

    def _run() -> dict[str, object]:
        if _load_credentials() is None:
            raise RemoteServerError(
                "BACKUP_NOT_CONFIGURED", "还没有配置备份目标。"
            )
        if poke is None:
            raise RemoteServerError(
                "BACKUP_UNAVAILABLE", "这套部署没有运行备份线程。"
            )
        recent = config_store.recent_runs(1)
        if recent and recent[0].status == RUN_RUNNING:
            # 正在跑的那一轮就是这次请求要的东西。再 poke 一下只会在它结束后
            # 立刻再来一整轮全量——连点几下就是连着几份 400 MB。
            return _status_payload()
        poke()
        return _status_payload()

    @app.post("/api/v1/archives/{archive_id}/backup/runs", status_code=202)
    def request_backup_run(archive_id: str, request: Request) -> dict[str, object]:
        authenticate_request(request, archive_id, auth_store)
        return _run()

    @app.get("/api/v1/archives/{archive_id}/backup/runs/latest/download")
    async def download_latest_backup(archive_id: str, request: Request) -> dict[str, object]:
        authenticate_request(request, archive_id, auth_store)
        credentials = _load_credentials()
        if credentials is None:
            raise RemoteServerError("BACKUP_NOT_CONFIGURED", "还没有配置备份目标。")

        def _build() -> dict[str, object]:
            store = build_store(credentials)
            newest = latest_backup(store, credentials.prefix)
            if newest is None:
                raise BackupArchiveError("no complete backup is available yet")
            manifest = read_manifest(store, credentials.prefix, newest.tier, newest.backup_id)
            parts = [
                {
                    "name": item.name,
                    "size": item.size,
                    "sha256": item.sha256,
                    "stored_size": item.stored_size,
                    "url": store.presign_get(
                        object_key(
                            credentials.prefix, newest.tier, newest.backup_id, item.object_name
                        ),
                        expires_in=DOWNLOAD_EXPIRES_SECONDS,
                        # 与前端 `backupPartFileName` 同一个形状；跨域下载只认
                        # 服务端签进去的这个名字。
                        filename=f"{manifest.backup_id}-{item.name}.gz",
                    ),
                }
                for item in manifest.files
            ]
            return {
                "backup_id": manifest.backup_id,
                "created_at": manifest.created_at,
                "app_version": manifest.app_version,
                "expires_in": DOWNLOAD_EXPIRES_SECONDS,
                "parts": parts,
                "restore_notes": list(manifest.restore_notes),
            }

        try:
            return await run_in_threadpool(_build)
        except BackupArchiveError:
            # 自己的代号：配置是有的，只是还没有一份完整备份。与「先去配置」
            # 共用 BACKUP_NOT_CONFIGURED 会把人指向一个已经做完的动作。
            raise RemoteServerError(
                "BACKUP_NOT_AVAILABLE", "还没有一份可下载的完整备份。"
            ) from None
        except (BackupStoreError, OSError):
            raise RemoteServerError(
                "BACKUP_VERIFICATION_FAILED", "取备份清单失败：备份目标当前不可达。"
            ) from None

    return BackupActions(lambda: _config_response(config_store.summary()), _test_config, _put_config, _run, config_store.disable, _load_credentials)


__all__ = [
    "DOWNLOAD_EXPIRES_SECONDS",
    "RECENT_RUN_LIMIT",
    "BackupConfigRequest",
    "default_backup_store",
    "register_backup_routes",
]
