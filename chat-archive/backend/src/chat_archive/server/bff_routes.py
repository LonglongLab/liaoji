"""同源 BFF：远程端拿浏览器 Cookie 回答本地 API 那几个读接口。

形状不是「像」本机应用上的 `/api/v1/remote/*`，是**同一段映射**——两边都过
`RemoteArchiveFacade`，只是本机那边的 transport 走 HTTPS 打到这台服务器，
这边的 transport 直接读本进程的元数据库（`inprocess_archive.py`）。于是
`contracts/openapi.yaml` 那几个 `Remote*` schema 只有一份实现，前端零改动。

浏览器登录是**部署级**的（一个口令），读接口是**归档级**的。中间那一环由
`auth_store.list_archives()` 补：取最早 provision 的那个。自托管单节点上这就是
唯一一个；多归档的浏览器切换不属本单元，所以这里不猜、不给切换入口。
"""

# pyright: reportUnusedFunction=false

from __future__ import annotations

from chat_archive.server.workspace import RemoteWorkspace

from collections.abc import Callable
from dataclasses import asdict
from typing import Annotated, cast

from fastapi import FastAPI, Path as PathParam, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import StringConstraints

from chat_archive.domain.errors import ArchiveError, ErrorCode
from chat_archive.ports.browser_auth import BrowserSessionContext
from chat_archive.ports.device_auth import DeviceAuthStore
from chat_archive.ports.metadata import MetadataStore
from chat_archive.remote_services import (
    MAX_REMOTE_FILTER_VALUES,
    RemoteAppServices,
    RemoteArchiveFacade,
    share_view_payload,
    unique_filter_values,
)
from chat_archive.server.backup_config import (
    SqliteBackupConfigStore,
    backup_status_document,
)
from chat_archive.server.backup_routes import RECENT_RUN_LIMIT
from chat_archive.server.browser_routes import BrowserGate
from chat_archive.server.share_service import ShareService
from chat_archive.http_body import read_bounded_body, read_draft_json
from chat_archive.share_draft import MAX_OBJECT_BYTES, ShareDraftError
from chat_archive.server.inprocess_archive import (
    InProcessRemoteArchiveTransport,
    PresignAttachment,
    SessionCredentialStore,
)

# 归属表里本端**确实做得到**的那几项。做不到的一律不提——「端没提这项能力」
# 就是 unsupported，界面与路由都不出现（PSU-2.28 定的规矩）。
#
# archive-status 自 PSU-2.30 起认领：远程记录不再只有正文，
# 「哪些字段已经落到这个远端」现在是有答案的——`/api/v1/health` 的
# `record_model_version` 就是那份答案，会话列表的 display_name 为 null 也是。
#
# 这几项要有归档才谈得上：没有 provision 过任何归档时，读接口与设备列表
# 都只能答「找不到」。那时如实报 not_configured——界面照规矩仍然出现并标明
# 未配置，而不是摆一个点开就报错的入口。
_NEEDS_AN_ARCHIVE = (
    "remote-conversations",
    "remote-search",
    "device-revocation",
    "archive-status",
)
# 这几项与归档无关：登录、会话、凭据保管在空服务器上照样成立。
_ALWAYS_AVAILABLE = (
    "password-login",
    "browser-session",
    "object-storage-credentials",
)
# 附件那两项（PSU-2.31）要的不是归档，是**对象存储**：归档配好了但没配桶，
# 点开图片仍然只有一个错误码。三态在这里才有意义——available / not_configured
# 都要如实报，界面据此出现入口或出现配置指引，而不是把入口整个藏起来让人
# 以为这一端做不到。
_NEEDS_OBJECT_STORAGE = (
    "attachment-view",
    "presigned-url",
)
# 备份近况在此复用；PSU-2.219 的浏览器管理在 management_routes 注册，
# 配置变更需要本会话的新鲜口令/TOTP确认，整库下载仍仅给已接入电脑。
_BACKUP_CAPABILITY = "remote-backup"

#: 多值过滤的**单个值**，与本机 `http_api.py` 的 `_FilterValue` 同形。约束必须
#: 落在元素类型上：`list[str]` 配 `Query(min_length=…)` 在 Pydantic v2 下约束
#: 的是列表长度，`?conversation_id=` 会解析成 `[""]`——而空过滤拼出的游标
#: 作用域与不过滤相同，游标能跨作用域重放。
_FilterValue = Annotated[str, StringConstraints(min_length=1, max_length=512)]




def register_bff_routes(
    app: FastAPI,
    gate: BrowserGate,
    auth_store: DeviceAuthStore,
    metadata_store: MetadataStore,
    presign_attachment: PresignAttachment | None = None,
    object_storage_ready: Callable[[], bool] = lambda: False,
    backup_config_store: SqliteBackupConfigStore | None = None,
    share_service: ShareService | None = None,
    workspace: RemoteWorkspace | None = None,
    storage_management: bool = False,
    pairing_management: bool = False,
) -> None:
    def _archive_id() -> str:
        archives = auth_store.list_archives()
        if not archives:
            raise ArchiveError(ErrorCode.REMOTE_ARCHIVE_NOT_FOUND)
        return archives[0].id

    def _facade(context: BrowserSessionContext) -> RemoteArchiveFacade:
        return RemoteArchiveFacade(
            RemoteAppServices(
                credentials=SessionCredentialStore(
                    _archive_id(),
                    context.session_id,
                    context.cursor_key,
                ),
                client_factory=lambda _profile, _token: InProcessRemoteArchiveTransport(
                    metadata_store,
                    presign_attachment,
                    share_service,
                ),
            )
        )

    @app.get("/api/v1/capabilities")
    def capabilities(request: Request) -> dict[str, object]:
        state = gate.store.state()
        if not state.initialized:
            # 首次向导要先知道自己该出现。这一句只泄漏「还没人设过口令」——
            # 而防抢注靠的是引导令牌，不是藏着这个事实。
            return {"deployment": "remote", "auth_state": "uninitialized"}
        context = gate.session(request)
        if context is None:
            return {"deployment": "remote", "auth_state": "anonymous"}
        if not gate.is_unlocked(request, context):
            # 已登录但未解锁（PSU-2.213）：锁定页只需要知道「该去解锁了」，
            # 不列任何私人能力，也不透露是否已有归档。细节走 reauth/state。
            return {"deployment": "remote", "auth_state": "reauth_required"}
        has_archive = bool(auth_store.list_archives())
        claimed = [{"id": item, "state": "available"} for item in _ALWAYS_AVAILABLE]
        claimed.extend(
            {"id": item, "state": "available" if has_archive else "not_configured"}
            for item in _NEEDS_AN_ARCHIVE
        )
        storage_ready = (
            has_archive and presign_attachment is not None and object_storage_ready()
        )
        claimed.extend(
            {"id": item, "state": "available" if storage_ready else "not_configured"}
            for item in _NEEDS_OBJECT_STORAGE
        )
        claimed.append(
            {"id": "totp", "state": "available" if context.totp_enabled else "not_configured"}
        )
        if storage_management:
            claimed.append({"id": "object-storage-config", "state": "available"})
        if pairing_management:
            claimed.append({"id": "pairing-code", "state": "available"})
        if backup_config_store is not None:
            claimed.append({"id": "backup-management", "state": "available"})
            # 这一端做得到备份这件事，只是可能还没配。做不到的一律不提。
            claimed.append(
                {
                    "id": _BACKUP_CAPABILITY,
                    "state": (
                        "available"
                        if backup_config_store.summary().configured
                        else "not_configured"
                    ),
                }
            )
        if share_service is not None:
            # 在线分享（PSU-2.182）：只有自建 SQLite 给了 share_service。要有归档才谈得上。
            claimed.extend(
                {"id": item, "state": "available" if has_archive else "not_configured"}
                for item in ("share", "remote-shares")
            )
        if workspace is not None:
            claimed.extend({"id": item, "state": "available" if has_archive else "not_configured"}
                           for item in ("remote-stats", "remote-export"))
        return {
            "deployment": "remote",
            "auth_state": "authenticated",
            "capabilities": claimed,
        }

    if workspace is not None:
        from chat_archive.server.workspace_routes import register_workspace_routes
        register_workspace_routes(app, gate, workspace, _archive_id)

    if backup_config_store is not None:
        store = backup_config_store

        @app.get("/api/v1/remote/backup/runs")
        def remote_backup_runs(request: Request) -> dict[str, object]:
            gate.require(request)
            payload = backup_status_document(store)
            payload["runs"] = [asdict(item) for item in store.recent_runs(RECENT_RUN_LIMIT)]
            return payload

    @app.get("/api/v1/remote/status")
    def remote_status(request: Request) -> dict[str, object]:
        gate.require(request)
        archives = auth_store.list_archives()
        # 远程端就是那个远端，所以「配没配」问的是它自己托管着归档没有。
        return {
            "configured": bool(archives),
            "archive_id": archives[0].id if archives else None,
        }

    @app.get("/api/v1/remote/accounts")
    def remote_accounts(request: Request) -> dict[str, object]:
        context = gate.require(request)
        return asdict(_facade(context).list_accounts())

    @app.get("/api/v1/remote/conversations")
    def remote_conversations(
        request: Request,
        cursor: str | None = Query(default=None, min_length=1, max_length=4096),
        q: str | None = Query(default=None, min_length=1, max_length=512),
        source_account_id: str | None = Query(default=None, min_length=1, max_length=128),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, object]:
        context = gate.require(request)
        return asdict(
            _facade(context).list_conversations(
                cursor=cursor,
                limit=limit,
                source_account_id=source_account_id,
                query=q,
            )
        )

    @app.get("/api/v1/remote/conversations/{conversation_id}")
    def remote_conversation(
        request: Request,
        conversation_id: str = PathParam(min_length=1, max_length=512),
        source_account_id: str | None = Query(default=None, min_length=1, max_length=128),
    ) -> dict[str, object]:
        context = gate.require(request)
        return asdict(
            _facade(context).get_conversation(
                conversation_id,
                source_account_id=source_account_id,
            )
        )

    @app.get("/api/v1/remote/conversations/{conversation_id}/members")
    def remote_conversation_members(
        request: Request,
        conversation_id: str = PathParam(min_length=1, max_length=512),
        cursor: str | None = Query(default=None, min_length=1, max_length=4096),
        source_account_id: str | None = Query(default=None, min_length=1, max_length=128),
        limit: int = Query(default=200, ge=1, le=500),
    ) -> dict[str, object]:
        context = gate.require(request)
        return asdict(
            _facade(context).list_conversation_members(
                conversation_id,
                cursor=cursor,
                limit=limit,
                source_account_id=source_account_id,
            )
        )

    @app.get("/api/v1/remote/conversations/{conversation_id}/messages")
    def remote_messages(
        request: Request,
        conversation_id: str = PathParam(min_length=1, max_length=512),
        cursor: str | None = Query(default=None, min_length=1, max_length=4096),
        source_account_id: str | None = Query(default=None, min_length=1, max_length=128),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, object]:
        context = gate.require(request)
        return asdict(
            _facade(context).list_conversation_messages(
                conversation_id,
                cursor=cursor,
                limit=limit,
                source_account_id=source_account_id,
            )
        )

    @app.get("/api/v1/remote/conversations/{conversation_id}/messages/window")
    def remote_message_window(
        request: Request,
        conversation_id: str = PathParam(min_length=1, max_length=512),
        anchor_message_id: str = Query(min_length=1, max_length=512),
        before: int = Query(default=20, ge=0, le=200),
        after: int = Query(default=20, ge=0, le=200),
        source_account_id: str | None = Query(default=None, min_length=1, max_length=128),
    ) -> dict[str, object]:
        context = gate.require(request)
        return asdict(
            _facade(context).message_window(
                conversation_id,
                anchor_message_id=anchor_message_id,
                before=before,
                after=after,
                source_account_id=source_account_id,
            )
        )

    @app.get("/api/v1/remote/attachments/{blob_id}/url")
    def remote_attachment_url(
        request: Request,
        blob_id: str = PathParam(min_length=1, max_length=128),
    ) -> dict[str, object]:
        context = gate.require(request)
        return asdict(_facade(context).attachment_url(blob_id))

    @app.get("/api/v1/remote/attachments/{blob_id}/thumbnail-url")
    def remote_attachment_thumbnail_url(
        request: Request,
        blob_id: str = PathParam(min_length=1, max_length=128),
    ) -> dict[str, object]:
        context = gate.require(request)
        return asdict(_facade(context).thumbnail_url(blob_id))

    @app.get("/api/v1/remote/search")
    def remote_search(
        request: Request,
        q: str | None = Query(default=None, min_length=1, max_length=512),
        source_account_id: str | None = Query(default=None, min_length=1, max_length=128),
        conversation_id: list[_FilterValue] | None = Query(
            default=None, min_length=1, max_length=MAX_REMOTE_FILTER_VALUES
        ),
        sender_id: list[_FilterValue] | None = Query(
            default=None, min_length=1, max_length=MAX_REMOTE_FILTER_VALUES
        ),
        message_types: list[str] | None = Query(
            default=None, alias="type", min_length=1, max_length=11
        ),
        has_attachment: bool | None = Query(default=None),
        date_from: int | None = Query(default=None, ge=0),
        date_to: int | None = Query(default=None, ge=0),
        cursor: str | None = Query(default=None, min_length=1, max_length=4096),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, object]:
        context = gate.require(request)
        return asdict(
            _facade(context).search(
                q,
                limit=limit,
                source_account_id=source_account_id,
                conversation_ids=unique_filter_values(conversation_id),
                sender_uids=unique_filter_values(sender_id),
                message_types=unique_filter_values(message_types) or None,
                has_attachment=has_attachment,
                date_from=date_from,
                date_to=date_to,
                cursor=cursor,
            )
        )

    @app.get("/api/v1/remote/sessions")
    def list_sessions(request: Request) -> dict[str, object]:
        context = gate.require(request)
        return {
            "items": [
                {
                    "id": item.id,
                    "created_at": item.created_at,
                    "last_seen_at": item.last_seen_at,
                    "idle_expires_at": item.idle_expires_at,
                    "absolute_expires_at": item.absolute_expires_at,
                    "current": item.id == context.session_id,
                    "client_info": None if item.client_info is None else asdict(item.client_info),
                }
                for item in gate.store.list_sessions()
            ]
        }

    if share_service is not None:
        _register_share_routes(app, gate, _facade)

    @app.post("/api/v1/remote/sessions/{session_id}/revoke")
    def revoke_session(
        request: Request,
        session_id: str = PathParam(min_length=1, max_length=128),
    ) -> dict[str, object]:
        gate.guard_origin(request)
        gate.require(request)
        # 撤销是幂等的意愿：目标本就不在了（自己过期、别处已撤）也算达成。
        # 从前这里抛 SOURCE_NOT_FOUND，那个码的公开文案是「未找到微信数据源」——
        # 在一台远程服务器的会话列表上，那句话没有任何意义。
        gate.store.revoke_session(session_id)
        return {"session_id": session_id, "status": "revoked"}

    @app.post("/api/v1/remote/sessions/revoke-all")
    def revoke_all_sessions(request: Request) -> dict[str, object]:
        gate.guard_origin(request)
        gate.require(request)
        # 包括当前这一条。「退出所有设备」把自己留下就不叫所有。
        return {"revoked_sessions": gate.store.revoke_all_sessions()}

    @app.get("/api/v1/remote/devices")
    def list_devices(request: Request) -> dict[str, object]:
        gate.require(request)
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
                }
                for item in auth_store.list_devices(_archive_id())
            ]
        }

    @app.post("/api/v1/remote/devices/{device_id}/revoke")
    def revoke_device(
        request: Request,
        device_id: str = PathParam(min_length=1, max_length=128),
    ) -> dict[str, object]:
        gate.guard_origin(request)
        context = gate.require(request)
        archive_id = _archive_id()
        # 撤销的执行者记成这条浏览器会话，好让审计能分辨「谁撤的」——
        # 复用的是设备侧那条既有的 revoke，不另起一套。
        auth_store.revoke_device(archive_id, context.session_id, device_id)
        return {"device_id": device_id, "status": "revoked"}


def _register_share_routes(
    app: FastAPI,
    gate: BrowserGate,
    facade: Callable[[BrowserSessionContext], RemoteArchiveFacade],
) -> None:
    """在线分享的建与管（PSU-2.181），与本机 `/api/v1/remote/shares*` 逐字同形。

    只有自托管 SQLite 给了 `share_service`；别的部署不注册（404，不是 403）。
    写操作先过来源校验、再要会话，与撤销会话同一个顺序。
    """

    async def _body(request: Request, limit: int) -> bytes:
        try:
            return await read_bounded_body(request, limit)
        except ShareDraftError as error:
            raise ArchiveError(ErrorCode(error.code)) from None

    @app.post("/api/v1/remote/shares")
    async def create_share(request: Request) -> dict[str, object]:
        gate.guard_origin(request)
        context = gate.require(request)
        try:
            payload = await read_draft_json(request)
        except ShareDraftError as error:
            raise ArchiveError(ErrorCode(error.code)) from None
        if not isinstance(payload, dict):
            raise ArchiveError(ErrorCode.QUERY_INVALID)
        view = await run_in_threadpool(
            facade(context).create_share, cast(dict[str, object], payload)
        )
        return share_view_payload(view)

    @app.get("/api/v1/remote/shares")
    def list_shares(request: Request) -> dict[str, object]:
        context = gate.require(request)
        return {"items": [share_view_payload(item) for item in facade(context).list_shares()]}

    @app.put("/api/v1/remote/shares/{token}/objects/{attachment_id}")
    async def upload_share_object(
        request: Request,
        token: str = PathParam(min_length=1, max_length=64),
        attachment_id: str = PathParam(min_length=1, max_length=128),
    ) -> dict[str, object]:
        gate.guard_origin(request)
        context = gate.require(request)
        content = await _body(request, MAX_OBJECT_BYTES)
        await run_in_threadpool(
            facade(context).upload_share_object, token, attachment_id, content
        )
        return {"uploaded": True}

    @app.post("/api/v1/remote/shares/{token}/finalize")
    def finalize_share(
        request: Request,
        token: str = PathParam(min_length=1, max_length=64),
    ) -> dict[str, object]:
        gate.guard_origin(request)
        context = gate.require(request)
        return share_view_payload(facade(context).finalize_share(token))

    @app.post("/api/v1/remote/shares/{token}/extend")
    def extend_share(
        request: Request,
        token: str = PathParam(min_length=1, max_length=64),
    ) -> dict[str, object]:
        gate.guard_origin(request)
        context = gate.require(request)
        return share_view_payload(facade(context).extend_share(token))

    @app.delete("/api/v1/remote/shares/{token}")
    def delete_share(
        request: Request,
        token: str = PathParam(min_length=1, max_length=64),
    ) -> dict[str, object]:
        gate.guard_origin(request)
        context = gate.require(request)
        facade(context).delete_share(token)
        return {"deleted": True}


__all__ = ["register_bff_routes"]
