"""设备生命周期端点；语义镜像 Worker `deviceRoutes.ts`。"""

# pyright: reportUnusedFunction=false

from __future__ import annotations

from typing import cast

from fastapi import FastAPI, Request
from starlette.concurrency import run_in_threadpool

from chat_archive.ports.device_auth import DeviceAuthStore, PairingRejected
from chat_archive.server.auth import authenticate_request
from chat_archive.server.http import RemoteServerError


_MAX_DEVICE_NAME_LENGTH = 64
# 配对码固定为 32 位 id + "." + 64 位 secret（97 字符），128 留出余量。
_MAX_PAIRING_CODE_LENGTH = 128


def register_device_routes(app: FastAPI, store: DeviceAuthStore) -> None:
    @app.post("/api/v1/pair")
    async def pair(request: Request) -> dict[str, str]:
        try:
            payload = await request.json()
        except ValueError:
            raise RemoteServerError("QUERY_INVALID", "请求体必须是合法 JSON。") from None
        if not isinstance(payload, dict):
            raise RemoteServerError("QUERY_INVALID", "pairing_code 与 device_name 是必填字段。")
        body = cast(dict[str, object], payload)
        pairing_code = body.get("pairing_code")
        device_name = body.get("device_name")
        if (
            not isinstance(pairing_code, str)
            or not pairing_code
            or not isinstance(device_name, str)
            or not device_name.strip()
        ):
            raise RemoteServerError("QUERY_INVALID", "pairing_code 与 device_name 是必填字段。")
        if (
            len(device_name.strip()) > _MAX_DEVICE_NAME_LENGTH
            or len(pairing_code) > _MAX_PAIRING_CODE_LENGTH
        ):
            raise RemoteServerError("QUERY_INVALID", "pairing_code 与 device_name 是必填字段。")
        try:
            # redeem_pairing_code 是同步阻塞的 SQLite 写事务；这是唯一一个没有
            # 走 run_in_threadpool 的 async handler，配对请求会卡住共享事件循环。
            paired = await run_in_threadpool(store.redeem_pairing_code, pairing_code, device_name)
        except PairingRejected:
            raise RemoteServerError("UNAUTHORIZED", "配对码无效或已过期，请重新获取。") from None
        return {
            "archive_id": paired.archive_id,
            "device_id": paired.device_id,
            "device_token": paired.device_token,
        }

    @app.post("/api/v1/archives/{archive_id}/pairing-codes")
    def create_pairing_code(archive_id: str, request: Request) -> dict[str, object]:
        auth = authenticate_request(request, archive_id, store)
        issued = store.issue_pairing_code(archive_id, auth.device_id)
        return {
            "pairing_code": issued.pairing_code,
            "pairing_code_id": issued.pairing_code_id,
            "expires_at": issued.expires_at,
        }

    @app.post("/api/v1/archives/{archive_id}/devices/revoke-all")
    def revoke_all(archive_id: str, request: Request) -> dict[str, int]:
        auth = authenticate_request(request, archive_id, store)
        return {"revoked_devices": store.revoke_all(archive_id, auth.device_id)}

    @app.post("/api/v1/archives/{archive_id}/devices/self/rotate")
    def rotate(archive_id: str, request: Request) -> dict[str, str]:
        auth = authenticate_request(request, archive_id, store)
        try:
            # 防御性竞态保护：设备在 authenticate_request 通过后、rotate_token 执行前被并发吊销。
            # 单线程测试不可达，因此不额外补测试。
            token = store.rotate_token(archive_id, auth.device_id)
        except PairingRejected:
            raise RemoteServerError("UNAUTHORIZED", "设备令牌缺失或无效，请重新配对设备。") from None
        return {"device_id": auth.device_id, "device_token": token}

    @app.post("/api/v1/archives/{archive_id}/devices/{device_id}/revoke")
    def revoke_device(archive_id: str, device_id: str, request: Request) -> dict[str, str]:
        auth = authenticate_request(request, archive_id, store)
        if not store.revoke_device(archive_id, auth.device_id, device_id):
            raise RemoteServerError("NOT_FOUND", "资源不存在。")
        return {"device_id": device_id, "status": "revoked"}
