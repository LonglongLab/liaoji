"""PSU-2.219: authenticated self-hosted browser management; no backup download."""
# pyright: reportUnusedFunction=false
# FastAPI registers the decorated functions.
from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import Response

from chat_archive.ports.browser_auth import BrowserAuthThrottled
from chat_archive.ports.device_auth import BrowserPairingStore, DeviceAuthStore, PairingThrottled
from chat_archive.server.backup_routes import BackupActions, BackupConfigRequest
from chat_archive.server.browser_management import BrowserManagement
from chat_archive.server.browser_routes import BrowserGate, browser_throttled_response
from chat_archive.server.http import RemoteServerError
from chat_archive.server.object_storage_config import SqliteObjectStorageConfigStore
from chat_archive.server.object_storage_requests import ObjectStorageConfigRequest


class SharedBackupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prefix: str = Field(default="backup", min_length=1, max_length=128)
    interval_hours: int = Field(default=24, ge=1, le=720)


class BackupPolicyRequest(SharedBackupRequest):
    endpoint: str = Field(min_length=8, max_length=512)
    bucket: str = Field(min_length=3, max_length=128)
    region: str = Field(min_length=2, max_length=64)


def register_management_routes(
    app: FastAPI, gate: BrowserGate, auth_store: DeviceAuthStore, *,
    storage: SqliteObjectStorageConfigStore | None,
    storage_read: Callable[[], dict[str, object]],
    storage_save: Callable[[ObjectStorageConfigRequest], Awaitable[dict[str, object]]],
    storage_test: Callable[[ObjectStorageConfigRequest], Awaitable[dict[str, object]]],
    backup: BackupActions | None,
) -> Callable[[str], None]:
    management = BrowserManagement(gate)
    management.register(app)

    def setup_session(session_id: str) -> None:
        if isinstance(auth_store, BrowserPairingStore):
            auth_store.ensure_browser_archive()
        management.grant(session_id, seconds=900, setup=True)

    if isinstance(auth_store, BrowserPairingStore):
        @app.post("/api/v1/remote/pairing-codes", status_code=201, response_model=None)
        def issue_browser_code(request: Request) -> dict[str, object] | Response:
            management.require(request)
            context = gate.require_fresh(request)
            try:
                issued = auth_store.issue_browser_pairing_code(context.session_id)
            except PairingThrottled:
                return browser_throttled_response(BrowserAuthThrottled(300))
            return {"pairing_code": issued.pairing_code, "expires_at": issued.expires_at}

    if storage is not None:
        @app.get("/api/v1/remote/object-storage/config")
        def read_storage(request: Request) -> dict[str, object]:
            gate.require(request)
            return storage_read()

        @app.put("/api/v1/remote/object-storage/config")
        async def save_storage(request: Request, body: ObjectStorageConfigRequest) -> dict[str, object]:
            management.require(request)
            return await storage_save(body)

        @app.post("/api/v1/remote/object-storage/config/test")
        async def test_storage(request: Request, body: ObjectStorageConfigRequest) -> dict[str, object]:
            management.require(request)
            return await storage_test(body)

    if backup is not None:
        @app.get("/api/v1/remote/backup/config")
        def read_backup(request: Request) -> dict[str, object]:
            gate.require(request)
            return backup.get_config()

        @app.put("/api/v1/remote/backup/config")
        async def save_backup(request: Request, body: BackupConfigRequest) -> dict[str, object]:
            management.require(request)
            return await backup.put_config(body)

        @app.post("/api/v1/remote/backup/config/test")
        async def test_backup(request: Request, body: BackupConfigRequest) -> dict[str, object]:
            management.require(request)
            return await backup.test_config(body)

        @app.post("/api/v1/remote/backup/run-now", status_code=202)
        def run_backup(request: Request) -> dict[str, object]:
            gate.guard_origin(request)
            gate.require_fresh(request)
            return backup.run()

        @app.put("/api/v1/remote/backup/config/policy")
        async def update_policy(request: Request, body: BackupPolicyRequest) -> dict[str, object]:
            management.require(request)
            saved = backup.credentials()
            if saved is None or (body.endpoint, body.bucket, body.region) != (saved.endpoint, saved.bucket, saved.region):
                raise RemoteServerError("BACKUP_CONFIGURATION_INVALID", "更换备份目标时请重新填写密钥。")
            current = backup.get_config()
            candidate: dict[str, object] = {"endpoint": saved.endpoint, "bucket": saved.bucket, "region": saved.region,
                         "secret_id": saved.secret_id, "secret_key": saved.secret_key,
                         "prefix": body.prefix, "interval_hours": body.interval_hours}
            candidate.update({key: current[key] for key in ("retention_mode", "daily_keep", "weekly_keep", "monthly_keep")})
            return await backup.put_config(BackupConfigRequest.model_validate(candidate))

        @app.post("/api/v1/remote/backup/disable")
        def disable_backup(request: Request) -> dict[str, object]:
            management.require(request)
            backup.disable()
            return backup.get_config()

        @app.post("/api/v1/remote/backup/use-object-storage")
        async def share_storage(request: Request, body: SharedBackupRequest) -> dict[str, object]:
            management.require(request)
            credentials = None if storage is None else storage.load_credentials()
            if credentials is None:
                raise RemoteServerError("BACKUP_NOT_CONFIGURED", "请先配置附件存储。")
            return await backup.put_config(BackupConfigRequest(
                endpoint=credentials.endpoint, bucket=credentials.bucket, region=credentials.region,
                secret_id=credentials.secret_id, secret_key=credentials.secret_key,
                prefix=body.prefix, interval_hours=body.interval_hours,
            ))

    return setup_session
