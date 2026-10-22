"""Bearer 鉴权与逐归档隔离。"""

from __future__ import annotations

import sqlite3

from fastapi import Request

from chat_archive.domain.client_info import SYNC_CLIENT_HEADER, parse_sync_client_info

from chat_archive.ports.device_auth import AuthContext, DeviceAuthStore
from chat_archive.server.http import RemoteServerError
from chat_archive.server.security import parse_bearer_token


def authenticate_request(request: Request, archive_id: str, store: DeviceAuthStore) -> AuthContext:
    token = parse_bearer_token(request.headers.get("Authorization"))
    if token is None:
        raise RemoteServerError("UNAUTHORIZED", "设备令牌缺失或无效，请重新配对设备。")
    context = store.authenticate(token)
    if context is None:
        raise RemoteServerError("UNAUTHORIZED", "设备令牌缺失或无效，请重新配对设备。")
    if context.archive_id != archive_id:
        # 跨归档逐字节相同 404，不泄漏存在性
        raise RemoteServerError("NOT_FOUND", "资源不存在。")
    info = parse_sync_client_info(request.headers.get(SYNC_CLIENT_HEADER))
    if info is not None:
        try:
            store.update_client_info(context, info)
        except (sqlite3.Error, TimeoutError):
            # Optional labels must not reject an already-authenticated sync operation.
            # No values, header bodies or underlying exception text are logged.
            pass
    return context
