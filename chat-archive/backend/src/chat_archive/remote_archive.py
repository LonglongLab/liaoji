"""Strict HTTP client for the remote read-only archive surface."""

from __future__ import annotations

from chat_archive.domain.client_info import SYNC_CLIENT_HEADER, SyncClientInfo
from chat_archive.release_compatibility import CompatibilityError, check_remote_health

import http.client
import json
import logging
import ssl
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from email.message import Message
from typing import IO, cast

from chat_archive.domain.reply_preview import ReplyPreviewRecord

from chat_archive.ports.remote_archive import (
    RemoteSearchPosition,
    RemoteSearchResultPage,
    RemoteAccountsPage,
    RemoteAccountSummary,
    RemoteArchiveError,
    RemoteBackupConfig,
    RemoteBackupDownload,
    RemoteBackupDownloadPart,
    RemoteBackupRun,
    RemoteBackupStatus,
    RemoteBackupVerification,
    RemoteConversationMember,
    RemoteConversationMemberPage,
    RemoteConversationPage,
    RemoteConversationPosition,
    RemoteConversationSummary,
    RemoteHealth,
    RemoteMessage,
    RemoteMessagePage,
    RemoteMessagePosition,
    RemoteMessageWindow,
    RemotePresignedAttachment,
    RemoteObjectStorageConfig,
    RemoteObjectStorageGeneration,
    RemoteObjectStorageVerification,
    RemoteRecord,
    RemoteRecordPage,
    RemoteShare,
    RemoteVoiceTranscript,
    WritableBinaryStream,
    remote_share_from,
)
from chat_archive.remote_url import validate_remote_base_url

_MAX_PAGE_SIZE = 500
_MAX_JSON_RESPONSE_BYTES = 8 * 1024 * 1024
_CHUNK_SIZE = 64 * 1024
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_logger = logging.getLogger(__name__)
Opener = Callable[[urllib.request.Request, float], IO[bytes]]


class _RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Turn every redirect response into an HTTPError at the original request boundary."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> urllib.request.Request | None:
        del req, fp, code, msg, headers, newurl
        return None


class _RemoteReadError(RemoteArchiveError):
    def __init__(self, code: str, *, transient: bool = True) -> None:
        super().__init__(code)
        self.transient = transient


class _DefaultOpener:
    """urllib adapter with timeout passed by keyword and redirects disabled."""

    def __init__(self) -> None:
        self._director = urllib.request.build_opener(_RejectRedirectHandler())

    def __call__(self, request: urllib.request.Request, timeout: float) -> IO[bytes]:
        return cast(IO[bytes], self._director.open(request, timeout=timeout))


class HttpRemoteArchiveClient:
    """Consume remote browse/search/record/blob endpoints without persisting by default."""

    # 扇出搜索可以并发地调这个客户端：`search_page` 每次调用各自组参数、各自
    # 经 `_get_json` 开一条新请求（`_DefaultOpener` 不复用连接），方法本身不碰
    # 共享可变状态；唯一的实例缓存 `_record_model_version` 只在
    # `record_model_version()` 里读写，不在这条并发路径上（2026-08-31 逐行核对）。
    supports_concurrent_requests = True

    def __init__(
        self,
        base_url: str,
        *,
        device_token: str,
        timeout_seconds: float = 30.0,
        opener: Opener | None = None,
        client_info: SyncClientInfo | None = None,
    ) -> None:
        if not device_token:
            raise ValueError("device token must be non-empty")
        if timeout_seconds <= 0:
            raise ValueError("remote archive timeout must be positive")
        self._base_url = validate_remote_base_url(base_url)
        self._device_token = device_token
        self._timeout_seconds = timeout_seconds
        self._opener: Opener = opener if opener is not None else _DefaultOpener()
        self._record_model_version: int | None = None
        self._client_headers = {} if client_info is None else {SYNC_CLIENT_HEADER: client_info.header_value()}

    def get_object_storage_config(self, archive_id: str) -> RemoteObjectStorageConfig:
        payload = self._get_json(
            self._archive_path(archive_id, "object-storage/config"),
            {},
        )
        return self._storage_config(payload)

    def test_object_storage_config(
        self,
        archive_id: str,
        payload: Mapping[str, str],
    ) -> RemoteObjectStorageVerification:
        response = self._json_request(
            "POST",
            self._archive_path(archive_id, "object-storage/config/test"),
            payload,
        )
        return self._storage_verification(response)

    def put_object_storage_config(
        self,
        archive_id: str,
        payload: Mapping[str, str],
    ) -> RemoteObjectStorageConfig:
        response = self._json_request(
            "PUT",
            self._archive_path(archive_id, "object-storage/config"),
            payload,
        )
        return self._storage_config(response)

    def restore_object_storage_generation(
        self,
        archive_id: str,
        storage_generation: int,
    ) -> RemoteObjectStorageConfig:
        response = self._json_request(
            "POST",
            self._archive_path(archive_id, "object-storage/config/restore"),
            {"storage_generation": storage_generation},
        )
        return self._storage_config(response)

    # ------------------------------------------------------------------
    # 定时备份（PSU-2.141）。配置那一半只走设备令牌这套接口——填凭据是已配对
    # 设备的事，浏览器会话只看得到 `/backup/runs` 那一条只读的近况。

    def get_backup_config(self, archive_id: str) -> RemoteBackupConfig:
        payload = self._get_json(self._archive_path(archive_id, "backup/config"), {})
        return self._backup_config(payload)

    def test_backup_config(
        self,
        archive_id: str,
        payload: Mapping[str, object],
    ) -> RemoteBackupVerification:
        response = self._json_request(
            "POST", self._archive_path(archive_id, "backup/config/test"), payload
        )
        return self._backup_verification(response)

    # ------------------------------------------------------------------
    # 在线分享（PSU-2.181）。只有自托管 SQLite 远端注册这组路由；别的远端答 404，
    # 到 facade 那里是「远程档案不支持」而不是「协议坏了」。

    def create_share(self, archive_id: str, payload: Mapping[str, object]) -> RemoteShare:
        return remote_share_from(
            self._json_request("POST", self._archive_path(archive_id, "shares"), payload)
        )

    def upload_share_object(
        self, archive_id: str, token: str, attachment_id: str, content: bytes
    ) -> None:
        self._send(
            "PUT",
            self._share_path(archive_id, token, f"objects/{self._quote_identity(attachment_id)}"),
            content,
            "application/octet-stream",
        )

    def finalize_share(self, archive_id: str, token: str) -> RemoteShare:
        return remote_share_from(
            self._send("POST", self._share_path(archive_id, token, "finalize"), b"", None)
        )

    def list_shares(self, archive_id: str) -> tuple[RemoteShare, ...]:
        response = self._send("GET", self._archive_path(archive_id, "shares"), None, None)
        items = response.get("items")
        if not isinstance(items, list):
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        shares: list[RemoteShare] = []
        for item in cast(list[object], items):
            if not isinstance(item, Mapping):
                raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
            shares.append(remote_share_from(cast(Mapping[str, object], item)))
        return tuple(shares)

    def extend_share(self, archive_id: str, token: str) -> RemoteShare:
        return remote_share_from(
            self._send("POST", self._share_path(archive_id, token, "extend"), b"", None)
        )

    def delete_share(self, archive_id: str, token: str) -> None:
        self._send("DELETE", self._share_path(archive_id, token, ""), None, None)

    def _share_path(self, archive_id: str, token: str, suffix: str) -> str:
        path = self._archive_path(archive_id, f"shares/{self._quote_identity(token)}")
        return f"{path}/{suffix}" if suffix else path

    def _send(
        self,
        method: str,
        path: str,
        data: bytes | None,
        content_type: str | None,
    ) -> Mapping[str, object]:
        """发一个不是 JSON 请求体的请求（原始字节或没有请求体），回 JSON 对象。"""
        headers = {
            **self._client_headers,
            "Accept": "application/json",
            "Authorization": f"Bearer {self._device_token}",
        }
        if content_type is not None:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(
            f"{self._base_url}{path}", method=method, data=data, headers=headers
        )
        try:
            with self._opener(request, self._timeout_seconds) as response:
                body = self._read_limited_json_body(response)
            value: object = json.loads(body)
        except urllib.error.HTTPError as error:
            code = self._stable_error_code(error)
            if code is not None:
                raise RemoteArchiveError(code) from None
            raise self._http_error(error.code) from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
            raise RemoteArchiveError("NETWORK_UNAVAILABLE") from None
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE") from None
        if not isinstance(value, Mapping):
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        return cast(Mapping[str, object], value)

    def put_backup_config(
        self,
        archive_id: str,
        payload: Mapping[str, object],
    ) -> RemoteBackupConfig:
        response = self._json_request(
            "PUT", self._archive_path(archive_id, "backup/config"), payload
        )
        return self._backup_config(response)

    def list_backup_runs(self, archive_id: str) -> RemoteBackupStatus:
        payload = self._get_json(self._archive_path(archive_id, "backup/runs"), {})
        return self._backup_status(payload)

    def request_backup_run(self, archive_id: str) -> RemoteBackupStatus:
        response = self._json_request(
            "POST", self._archive_path(archive_id, "backup/runs"), {}
        )
        return self._backup_status(response)

    def latest_backup_download(self, archive_id: str) -> RemoteBackupDownload:
        payload = self._get_json(
            self._archive_path(archive_id, "backup/runs/latest/download"), {}
        )
        return self._backup_download(payload)

    def forward_media_publication_confirmed(
        self, archive_id: str, record_key: str, publication_sha256: str,
    ) -> bool:
        payload = self._get_json(
            self._archive_path(archive_id, "sync/forward-media-publications/" + urllib.parse.quote(record_key, safe="")),
            {"publication_sha256": publication_sha256},
        )
        confirmed = payload.get("confirmed")
        if type(confirmed) is not bool:
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        return confirmed

    def sync_health(self, archive_id: str) -> RemoteHealth:
        payload = self._get_json(self._archive_path(archive_id, "sync-health"), {})
        optional_timestamps = (
            "last_success_at",
            "last_reconciled_at",
            "last_recovery_verified_at",
        )
        timestamps: dict[str, int | None] = {}
        for name in optional_timestamps:
            value = payload.get(name)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value <= 0
            ):
                raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
            timestamps[name] = value
        service_status = self._string(payload, "remote_service_status")
        object_status = self._string(payload, "object_storage_status")
        if service_status not in {"healthy", "degraded"} or object_status not in {
            "healthy",
            "degraded",
            "unknown",
            "not_configured",
        }:
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        return RemoteHealth(
            archive_id=self._string(payload, "archive_id"),
            backend_type=self._string(payload, "backend_type"),
            device_id=self._string(payload, "device_id"),
            last_uploaded_local_sequence=self._integer(
                payload, "last_uploaded_local_sequence", minimum=0
            ),
            last_server_sequence=self._integer(
                payload, "last_server_sequence", minimum=0
            ),
            remote_used_bytes=self._integer(payload, "remote_used_bytes", minimum=0),
            remote_blob_count=self._integer(payload, "remote_blob_count", minimum=0),
            pending_attachment_count=self._integer(
                payload, "pending_attachment_count", minimum=0
            ),
            conflict_count=self._integer(payload, "conflict_count", minimum=0),
            last_success_at=timestamps["last_success_at"],
            last_reconciled_at=timestamps["last_reconciled_at"],
            last_recovery_verified_at=timestamps["last_recovery_verified_at"],
            # Absent on every remote built before PSU-2.31; absence is
            # "unknown", not "zero", and the pre-flight distinguishes them.
            remote_available_bytes=self._optional_integer(payload, "remote_available_bytes"),
            # 同一条口径：缺键 = 这个远端不知道（本单元之前的服务端，或没有账号
            # 维度的后端），不是零。
            account_link_count=self._optional_integer(payload, "account_link_count"),
            account_merge_pending=self._optional_integer(payload, "account_merge_pending"),
            conversation_link_count=self._optional_integer(
                payload, "conversation_link_count"
            ),
            conversation_merge_pending=self._optional_integer(
                payload, "conversation_merge_pending"
            ),
            conversation_merge_pending_rows=self._optional_integer(
                payload, "conversation_merge_pending_rows"
            ),
            search_rehome_pending_rows=self._optional_integer(
                payload, "search_rehome_pending_rows"
            ),
            writer_lock_held_ms=self._optional_integer(payload, "writer_lock_held_ms"),
            writer_lock_rejections_recent=self._optional_integer(
                payload, "writer_lock_rejections_recent"
            ),
            # 备份（PSU-2.140/2.141）。同一条口径：**缺键 = 这个远端答不出备份
            # 这件事**，不是"没备过"。`last_backup_status` 缺席时其余四个也一律
            # 留 `None`，界面据此整块不出现，而不是画一个"从未备份"的红条。
            last_backup_at=self._optional_integer(payload, "last_backup_at"),
            last_backup_bytes=self._optional_integer(payload, "last_backup_bytes"),
            last_backup_status=self._optional_backup_status(payload),
            backup_retention_mode=self._optional_retention_mode(payload),
            backup_overdue=self._optional_bool(payload, "backup_overdue"),
            backup_lifecycle_covered=self._optional_bool(payload, "backup_lifecycle_covered"),
            remote_service_status=service_status,
            object_storage_status=object_status,
        )

    _BACKUP_STATUSES = frozenset(
        {"not_configured", "never", "running", "success", "failed", "locked"}
    )
    _RETENTION_MODES = frozenset({"lifecycle", "application"})

    def _optional_backup_status(self, payload: Mapping[str, object]) -> str | None:
        value = payload.get("last_backup_status")
        if value is None:
            return None
        if not isinstance(value, str) or value not in self._BACKUP_STATUSES:
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        return value

    def _optional_retention_mode(self, payload: Mapping[str, object]) -> str | None:
        value = payload.get("backup_retention_mode")
        if value is None:
            return None
        if not isinstance(value, str) or value not in self._RETENTION_MODES:
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        return value

    def list_conversations(
        self,
        archive_id: str,
        *,
        after_conversation_id: str | None = None,
        recent: bool = False,
        after_position: RemoteConversationPosition | None = None,
        query: str | None = None,
        limit: int = 100,
        source_account_id: str | None = None,
    ) -> RemoteConversationPage:
        self._validate_limit(limit)
        # `parameters`, not `query`: the filter term owns that name here, the
        # same one the port declares. One interface, one spelling.
        parameters: dict[str, str] = {"limit": str(limit)}
        # 只在真要过滤时携带。老服务端会忽略认不得的参数而**不过滤**，所以
        # 把这个参数发给谁由 facade 按 `record_model_version` 把关，这里只
        # 负责不凭空多发。
        if source_account_id is not None:
            self._validate_account(source_account_id)
            parameters["source_account_id"] = source_account_id
        if recent:
            parameters["order"] = "recent"
            if query:
                parameters["q"] = query
            if after_position is not None:
                # 两列一起发或都不发，理由同阅读顺序的四列键集。
                parameters["after_last_created_at"] = str(after_position.last_created_at)
                parameters["after_conversation_id"] = after_position.conversation_id
        else:
            parameters["after_conversation_id"] = after_conversation_id or ""
        payload = self._get_json(
            self._archive_path(archive_id, "query/conversations"),
            parameters,
        )
        raw_items = self._items(payload)
        conversations = tuple(self._conversation(item) for item in raw_items)
        has_more = payload.get("has_more")
        if not isinstance(has_more, bool):
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        if recent:
            raw_position = payload.get("next_position")
            position = None
            if raw_position is not None:
                item = self._mapping(raw_position)
                position = RemoteConversationPosition(
                    last_created_at=self._integer(item, "last_created_at", minimum=0),
                    conversation_id=self._string(item, "conversation_id"),
                )
            return RemoteConversationPage(
                conversations, None, has_more, next_position=position
            )
        next_id = payload.get("next_conversation_id")
        if next_id is not None and not isinstance(next_id, str):
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        return RemoteConversationPage(conversations, next_id, has_more)

    def get_conversation(
        self,
        archive_id: str,
        conversation_id: str,
        *,
        source_account_id: str | None = None,
    ) -> RemoteConversationSummary | None:
        parameters: dict[str, str] = {}
        if source_account_id is not None:
            self._validate_account(source_account_id)
            parameters["source_account_id"] = source_account_id
        try:
            payload = self._get_json(
                self._archive_path(
                    archive_id,
                    f"query/conversations/{self._quote_identity(conversation_id)}",
                ),
                parameters,
            )
        except RemoteArchiveError as error:
            # 404 收成 None,不让它落进 facade 的通用映射——那条映射会把它说成
            # `REMOTE_ARCHIVE_NOT_FOUND`（「归档不存在」），而这里说的是
            # 「这个归档里没有这条会话」。两句话在界面上是两种处置。
            if error.code == "REMOTE_HTTP_404":
                return None
            raise
        return self._conversation(payload)

    def list_conversation_members(
        self,
        archive_id: str,
        conversation_id: str,
        *,
        source_account_id: str | None = None,
        after_member_uid: str | None = None,
        limit: int = 200,
    ) -> RemoteConversationMemberPage:
        """One page of a conversation's roster (record model v8, PSU-2.47)."""
        self._validate_limit(limit)
        parameters: dict[str, str] = {"limit": str(limit)}
        if source_account_id is not None:
            self._validate_account(source_account_id)
            parameters["source_account_id"] = source_account_id
        if after_member_uid is not None:
            parameters["after_member_uid"] = after_member_uid
        payload = self._get_json(
            self._archive_path(
                archive_id,
                f"query/conversations/{self._quote_identity(conversation_id)}/members",
            ),
            parameters,
        )
        members = tuple(self._member(item) for item in self._items(payload))
        has_more = payload.get("has_more")
        if not isinstance(has_more, bool):
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        next_member_uid = self._optional_string(payload, "next_member_uid")
        if has_more and next_member_uid is None:
            # 说「还有」却不说从哪儿接着走，就是一条走不下去的分页。
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        return RemoteConversationMemberPage(
            members=members,
            next_member_uid=next_member_uid,
            has_more=has_more,
        )

    def search_page(
        self,
        archive_id: str,
        query: str | None,
        *,
        source_account_id: str | None = None,
        conversation_id: str | None = None,
        sender_uid: str | None = None,
        message_types: tuple[str, ...] | None = None,
        has_attachment: bool | None = None,
        date_from: int | None = None,
        date_to: int | None = None,
        after: RemoteSearchPosition | None = None,
        limit: int = 100,
    ) -> RemoteSearchResultPage:
        self._validate_limit(limit)
        parameters: dict[str, str] = {"limit": str(limit), "indexed": "true"}
        if query:
            parameters["q"] = query
        if source_account_id is not None:
            self._validate_account(source_account_id)
            parameters["source_account_id"] = source_account_id
        if conversation_id is not None:
            parameters["conversation_id"] = conversation_id
        if sender_uid is not None:
            parameters["sender_uid"] = sender_uid
        if message_types:
            parameters["message_types"] = ",".join(message_types)
        if has_attachment is not None:
            parameters["has_attachment"] = "true" if has_attachment else "false"
        if date_from is not None:
            parameters["date_from"] = str(date_from)
        if date_to is not None:
            parameters["date_to"] = str(date_to)
        if after is not None:
            # 两列一起发或都不发，理由同阅读顺序的四列键集。
            parameters["after_created_at"] = str(after.created_at)
            parameters["after_record_key"] = after.record_key
        payload = self._get_json(
            self._archive_path(archive_id, "query/search"), parameters
        )
        records = tuple(self._message(item) for item in self._items(payload))
        has_more = payload.get("has_more")
        if not isinstance(has_more, bool):
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        raw_position = payload.get("next_position")
        position = None
        if raw_position is not None:
            item = self._mapping(raw_position)
            position = RemoteSearchPosition(
                created_at=self._integer(item, "created_at", minimum=0),
                record_key=self._string(item, "record_key"),
            )
        status = self._string(payload, "index_status")
        if status not in {"ready", "building", "unavailable"}:
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        return RemoteSearchResultPage(
            records=records,
            next_position=position,
            has_more=has_more,
            total_count=self._integer(payload, "total_count", minimum=0),
            conversation_counts=self._conversation_counts(payload),
            index_status=status,
        )

    @staticmethod
    def _conversation_counts(payload: Mapping[str, object]) -> dict[str, int]:
        raw = payload.get("conversation_counts")
        if not isinstance(raw, Mapping):
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        counts: dict[str, int] = {}
        for key, value in cast(Mapping[str, object], raw).items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
            counts[str(key)] = value
        return counts

    def record_model_version(self) -> int:
        """Which record model this remote keeps; absent in health means 1.

        Cached **for this client instance**, which on the browsing path is one
        operation long — `RemoteArchiveFacade` builds a fresh client per call,
        so the caller that reads this before every page memoizes it itself
        (`remote_services.RemoteArchiveFacade._record_model_version`). This
        cache is the one that pays off on a long-lived client: a sync run.

        A health call that fails raises rather than answering 1 — an
        unreachable remote is not an old remote, and guessing would silently
        page the archive in upload order for the rest of the session.
        """
        if self._record_model_version is not None:
            return self._record_model_version
        payload = self._get_json("/api/v1/health", {})
        try:
            version = check_remote_health(payload, required_record_model=1)
        except CompatibilityError as error:
            raise RemoteArchiveError(error.code) from None
        self._record_model_version = version
        return version

    def list_conversation_messages(
        self,
        archive_id: str,
        conversation_id: str,
        *,
        after_server_sequence: int = 0,
        chronological: bool = False,
        latest: bool = False,
        after: RemoteMessagePosition | None = None,
        limit: int = 100,
        source_account_id: str | None = None,
    ) -> RemoteMessagePage:
        self._validate_limit(limit)
        account_query: dict[str, str] = {}
        if source_account_id is not None:
            self._validate_account(source_account_id)
            account_query["source_account_id"] = source_account_id
        path = self._archive_path(
            archive_id,
            f"query/conversations/{self._quote_identity(conversation_id)}/messages",
        )
        if not chronological and not latest:
            self._validate_cursor(after_server_sequence)
            payload = self._get_json(
                path,
                {
                    "after_server_sequence": str(after_server_sequence),
                    "limit": str(limit),
                    **account_query,
                },
            )
            messages = tuple(self._message(item) for item in self._items(payload))
            next_sequence, has_more = self._page_tail(payload)
            return RemoteMessagePage(
                messages,
                next_sequence,
                has_more,
                read_conversation_id=self._read_conversation_id(payload),
            )
        query = {
            "order": "latest" if latest else "chronological",
            "limit": str(limit),
            **account_query,
        }
        if after is not None:
            # All four or none: a partial keyset selects a different ordering
            # line, and its pages either overlap or skip without erroring.
            query["after_created_at"] = str(after.created_at)
            query["after_source_sequence"] = str(after.source_sequence)
            query["after_source_local_id"] = str(after.source_local_id)
            query["after_record_key"] = after.record_key
        payload = self._get_json(path, query)
        messages = tuple(self._message(item) for item in self._items(payload))
        has_more = payload.get("has_more")
        if not isinstance(has_more, bool):
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        raw_position = payload.get("next_position")
        position = None
        if raw_position is not None:
            item = self._mapping(raw_position)
            position = RemoteMessagePosition(
                created_at=self._integer(item, "created_at", minimum=0),
                source_sequence=self._integer(item, "source_sequence", minimum=0),
                source_local_id=self._integer(item, "source_local_id", minimum=0),
                record_key=self._string(item, "record_key"),
            )
        return RemoteMessagePage(
            messages,
            after_server_sequence,
            has_more,
            next_position=position,
            read_conversation_id=self._read_conversation_id(payload),
        )

    def message_window(
        self,
        archive_id: str,
        conversation_id: str,
        *,
        anchor_record_key: str,
        before: int = 20,
        after: int = 20,
        source_account_id: str | None = None,
    ) -> RemoteMessageWindow:
        if not anchor_record_key:
            raise RemoteArchiveError("REMOTE_INVALID_REQUEST")
        if before < 0 or after < 0:
            raise RemoteArchiveError("REMOTE_INVALID_REQUEST")
        self._validate_limit(before + after + 1)
        query: dict[str, str] = {
            "anchor_record_key": anchor_record_key,
            "before": str(before),
            "after": str(after),
        }
        if source_account_id is not None:
            self._validate_account(source_account_id)
            query["source_account_id"] = source_account_id
        try:
            payload = self._get_json(
                self._archive_path(
                    archive_id,
                    f"query/conversations/{self._quote_identity(conversation_id)}"
                    "/messages/window",
                ),
                query,
            )
        except RemoteArchiveError as error:
            if error.code != "REMOTE_HTTP_404":
                raise
            # 这条路由上的 404 只有一种读法值得转达：这条会话里没有这个锚点。
            # 归档标识来自刚刚协商过的 profile，不是调用方随手拼的。
            raise RemoteArchiveError("NOT_FOUND") from None
        anchor = payload.get("anchor_record_key")
        if not isinstance(anchor, str) or not anchor:
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        records = tuple(self._message(item) for item in self._items(payload))
        if all(record.record_key != anchor for record in records):
            # 窗口里没有锚点，就不是这条消息的上下文。宁可当协议错误，
            # 也不把一段"附近的消息"当成命中交给读者。
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        return RemoteMessageWindow(
            records,
            anchor,
            read_conversation_id=self._read_conversation_id(payload),
        )

    def search(
        self,
        archive_id: str,
        query: str,
        *,
        limit: int = 100,
        source_account_id: str | None = None,
    ) -> tuple[RemoteMessage, ...]:
        self._validate_limit(limit)
        parameters = {"q": query, "limit": str(limit)}
        if source_account_id is not None:
            self._validate_account(source_account_id)
            parameters["source_account_id"] = source_account_id
        payload = self._get_json(
            self._archive_path(archive_id, "query/search"),
            parameters,
        )
        return tuple(self._message(item) for item in self._items(payload))

    def list_accounts(self, archive_id: str) -> RemoteAccountsPage:
        payload = self._get_json(self._archive_path(archive_id, "query/accounts"), {})
        raw_items = self._items(payload)
        items: list[RemoteAccountSummary] = []
        for value in raw_items:
            item = self._mapping(value)
            items.append(
                RemoteAccountSummary(
                    source_account_id=self._string(item, "source_account_id"),
                    record_count=self._integer(item, "record_count", minimum=0),
                    # 键缺席（v4 及更早的远端）与 null（v5 说「还没送过名字」）
                    # 在读侧是同一个结论：只能显示标识。
                    display_name=self._optional_string(item, "display_name"),
                    # 头像同理（v10）：v9 及更早的远端根本不送这个键。
                    avatar_blob_id=self._optional_string(item, "avatar_blob_id"),
                    # 键缺席（v10 及更早的远端）与空数组（v11 说「这条流没有
                    # 同伴」）在读侧是同一个结论：URL 上的 id 不用换。
                    merged_source_account_ids=self._string_tuple(
                        item, "merged_source_account_ids"
                    ),
                )
            )
        return RemoteAccountsPage(
            items=tuple(items),
            unattributed_count=self._integer(payload, "unattributed_count", minimum=0),
        )

    def list_records(
        self,
        archive_id: str,
        *,
        after_server_sequence: int = 0,
        limit: int = 100,
    ) -> RemoteRecordPage:
        self._validate_cursor(after_server_sequence)
        self._validate_limit(limit)
        payload = self._get_json(
            self._archive_path(archive_id, "sync/records"),
            {"after_server_sequence": str(after_server_sequence), "limit": str(limit)},
        )
        records = tuple(self._record(item) for item in self._items(payload))
        next_sequence, has_more = self._page_tail(payload)
        return RemoteRecordPage(records, next_sequence, has_more)

    def presign_attachment(
        self,
        archive_id: str,
        blob_id: str,
    ) -> RemotePresignedAttachment:
        payload = self._get_json(
            self._archive_path(
                archive_id,
                f"blobs/{self._quote_identity(blob_id)}/presigned-url",
            ),
            {},
        )
        url = self._string(payload, "url")
        origin = self._string(payload, "storage_origin")
        # The remote may sit behind a different object-storage origin than this
        # machine can reach, but it must at least agree with itself: a URL and
        # an origin that disagree mean one of the two was rewritten in transit.
        if not url.startswith(f"{origin}/"):
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        return RemotePresignedAttachment(
            url=url,
            method=self._string(payload, "method"),
            expires_at=self._integer(payload, "expires_at", minimum=1),
            storage_origin=origin,
        )

    def download_blob(
        self,
        archive_id: str,
        blob_id: str,
        sink: WritableBinaryStream,
    ) -> None:
        request = self._request(
            self._archive_path(archive_id, f"blobs/{self._quote_identity(blob_id)}/content")
        )
        try:
            with self._opener(request, self._timeout_seconds) as response:
                while chunk := response.read(_CHUNK_SIZE):
                    sink.write(chunk)
        except urllib.error.HTTPError as error:
            raise self._http_error(error.code) from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
            raise RemoteArchiveError("NETWORK_UNAVAILABLE") from None

    # 远端会发、而桌面端要原样转述的代号族。别的代号（UNAUTHORIZED、NOT_FOUND……）
    # 按 HTTP 状态归类就够了；这两族要逐条区分，因为界面上每一条对应不同的处置。
    _RELAYED_CODE_PREFIXES = ("OBJECT_STORAGE_", "BACKUP_", "SHARE_")

    @classmethod
    def _stable_error_code(cls, error: urllib.error.HTTPError) -> str | None:
        """Recover the stable error code, and nothing else, from a failed call.

        The rest of the body may quote the request that was rejected, so only
        the code is read — never the message, and never the payload.
        """
        try:
            body: object = json.loads(error.read().decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            return None
        if not isinstance(body, dict):
            return None
        envelope = cast(dict[str, object], body).get("error")
        if not isinstance(envelope, dict):
            return None
        code = cast(dict[str, object], envelope).get("code")
        if isinstance(code, str) and code.startswith(cls._RELAYED_CODE_PREFIXES):
            return code
        return None

    def _json_request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object],
    ) -> Mapping[str, object]:
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            method=method,
            data=json.dumps(dict(payload), separators=(",", ":")).encode("utf-8"),
            headers={
                **self._client_headers,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._device_token}",
            },
        )
        try:
            with self._opener(request, self._timeout_seconds) as response:
                body = self._read_limited_json_body(response)
            value: object = json.loads(body)
        except urllib.error.HTTPError as error:
            code = self._stable_error_code(error)
            if code is not None:
                raise RemoteArchiveError(code) from None
            raise self._http_error(error.code) from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
            raise RemoteArchiveError("NETWORK_UNAVAILABLE") from None
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE") from None
        if not isinstance(value, Mapping):
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        return cast(Mapping[str, object], value)

    def _get_json(self, path: str, query: Mapping[str, str]) -> Mapping[str, object]:
        attempts = 2 if path.endswith("/sync-health") else 1
        for attempt in range(attempts):
            try:
                return self._get_json_once(path, query, self._timeout_seconds / attempts)
            except RemoteArchiveError as error:
                if (attempt + 1 == attempts or error.code not in {"NETWORK_UNAVAILABLE", "REMOTE_RETRYABLE"}
                    or not getattr(error, "transient", True)):
                    raise
        raise AssertionError("unreachable health retry state")

    def _get_json_once(self, path: str, query: Mapping[str, str], timeout: float) -> Mapping[str, object]:
        request = self._request(path, query)
        try:
            with self._opener(request, timeout) as response:
                response_body = self._read_limited_json_body(response)
            payload = json.loads(response_body)
        except urllib.error.HTTPError as error:
            # GET 也会收到稳定代号：`backup/runs/latest/download` 在还没有一份
            # 完整备份时答 409 `BACKUP_NOT_CONFIGURED`。只按状态归类会把它
            # 变成 `REMOTE_HTTP_409`，再到界面上就是「不兼容的数据」。
            code = self._stable_error_code(error)
            if code is not None:
                raise RemoteArchiveError(code) from None
            raise self._http_error(error.code) from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as error:
            self._log_read_network_failure(path, error)
            reason = getattr(error, "reason", error)
            transient = not isinstance(reason, ssl.SSLError) or isinstance(reason, (ssl.SSLEOFError, ssl.SSLZeroReturnError))
            raise _RemoteReadError("NETWORK_UNAVAILABLE", transient=transient) from None
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE") from None
        if not isinstance(payload, Mapping):
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        return cast(Mapping[str, object], payload)

    def _log_read_network_failure(self, path: str, error: Exception) -> None:
        """Retain error structure and proxy flags without URLs, ids or messages."""
        reason = getattr(error, "reason", error)
        error_number = getattr(reason, "errno", None)
        win_error = getattr(reason, "winerror", None)
        configured: bool | None = None
        bypass: bool | None = None
        loopback: bool | None = None
        try:
            destination = urllib.parse.urlparse(self._base_url)
            proxy = urllib.request.getproxies().get(destination.scheme)
            configured = bool(proxy)
            bypass = urllib.request.proxy_bypass(destination.hostname or "")
            loopback = urllib.parse.urlparse(proxy or "").hostname in ("127.0.0.1", "localhost", "::1")
        except (ValueError, OSError):
            # Evidence collection must not replace the original transport error.
            pass
        kind = "message_window" if path.endswith("/messages/window") else "health" if path.endswith(("/health", "/sync-health")) else "other_get"
        _logger.warning(
            "remote read network failure: kind=%s exception=%s cause=%s errno=%s winerror=%s proxy_configured=%s proxy_bypass=%s proxy_loopback=%s",
            kind, type(error).__name__, type(reason).__name__,
            error_number if isinstance(error_number, int) else None,
            win_error if isinstance(win_error, int) else None,
            configured, bypass, loopback,
        )

    @staticmethod
    def _optional_string(payload: Mapping[str, object], name: str) -> str | None:
        value = payload.get(name)
        if value is None:
            return None
        if not isinstance(value, str):
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        return value

    @staticmethod
    def _string_tuple(payload: Mapping[str, object], name: str) -> tuple[str, ...]:
        """缺席即空。远端答的不是字符串数组才算坏响应——**空数组不是坏响应**，
        它是「这条流没有同伴」这个正常答案（PSU-2.90）。"""
        value = payload.get(name)
        if value is None:
            return ()
        if not isinstance(value, list):
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        for entry in cast(list[object], value):
            if not isinstance(entry, str):
                raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        return tuple(cast(list[str], value))

    @staticmethod
    def _optional_integer(payload: Mapping[str, object], name: str) -> int | None:
        value = payload.get(name)
        if value is None:
            return None
        if not isinstance(value, int) or isinstance(value, bool):
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        return value

    @classmethod
    def _storage_generation(cls, item: object) -> RemoteObjectStorageGeneration:
        if not isinstance(item, Mapping):
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        entry = cast(Mapping[str, object], item)
        active = entry.get("active")
        if not isinstance(active, bool):
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        return RemoteObjectStorageGeneration(
            storage_generation=cls._integer(entry, "storage_generation", minimum=1),
            endpoint=cls._string(entry, "endpoint"),
            bucket=cls._string(entry, "bucket"),
            region=cls._string(entry, "region"),
            access_key_hint=cls._string(entry, "access_key_hint"),
            updated_at=cls._integer(entry, "updated_at", minimum=0),
            active=active,
        )

    @classmethod
    def _storage_config(cls, payload: Mapping[str, object]) -> RemoteObjectStorageConfig:
        configured = payload.get("configured")
        if not isinstance(configured, bool):
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        raw_generations = payload.get("generations", [])
        if not isinstance(raw_generations, list):
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        generations = tuple(
            cls._storage_generation(item) for item in cast(list[object], raw_generations)
        )
        return RemoteObjectStorageConfig(
            configured=configured,
            provider=cls._string(payload, "provider"),
            endpoint=cls._optional_string(payload, "endpoint"),
            bucket=cls._optional_string(payload, "bucket"),
            region=cls._optional_string(payload, "region"),
            access_key_hint=cls._optional_string(payload, "access_key_hint"),
            storage_generation=cls._integer(payload, "storage_generation", minimum=1),
            status=cls._string(payload, "status"),
            updated_at=cls._optional_integer(payload, "updated_at"),
            generations=generations,
        )

    @classmethod
    def _backup_config(cls, payload: Mapping[str, object]) -> RemoteBackupConfig:
        configured = payload.get("configured")
        if not isinstance(configured, bool):
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        readable = payload.get("credentials_readable", True)
        if not isinstance(readable, bool):
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        return RemoteBackupConfig(
            configured=configured,
            credentials_readable=readable,
            endpoint=cls._optional_string(payload, "endpoint"),
            bucket=cls._optional_string(payload, "bucket"),
            region=cls._optional_string(payload, "region"),
            prefix=cls._optional_string(payload, "prefix"),
            access_key_hint=cls._optional_string(payload, "access_key_hint"),
            updated_at=cls._optional_integer(payload, "updated_at"),
            interval_hours=cls._integer(payload, "interval_hours", minimum=1),
            retention_mode=cls._string(payload, "retention_mode"),
            daily_keep=cls._integer(payload, "daily_keep", minimum=0),
            weekly_keep=cls._integer(payload, "weekly_keep", minimum=0),
            monthly_keep=cls._integer(payload, "monthly_keep", minimum=0),
        )

    @classmethod
    def _backup_verification(
        cls, payload: Mapping[str, object]
    ) -> RemoteBackupVerification:
        verified = payload.get("verified")
        can_delete = payload.get("can_delete")
        if not isinstance(verified, bool) or not isinstance(can_delete, bool):
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        raw = payload.get("lifecycle_prefixes")
        if raw is None:
            # 「问不到」。不是"没有规则"——最小权限密钥读不了生命周期配置时
            # 就是这个，把它当成缺口会把一条权限事实报成一个不存在的故障。
            prefixes = None
        elif isinstance(raw, list):
            prefixes = tuple(
                item if isinstance(item, str) else "" for item in cast(list[object], raw)
            )
        else:
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        return RemoteBackupVerification(
            verified=verified, can_delete=can_delete, lifecycle_prefixes=prefixes
        )

    @classmethod
    def _backup_run(cls, payload: Mapping[str, object]) -> RemoteBackupRun:
        verified = payload.get("verified")
        if not isinstance(verified, bool):
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        return RemoteBackupRun(
            id=cls._integer(payload, "id", minimum=1),
            started_at=cls._integer(payload, "started_at", minimum=1),
            finished_at=cls._optional_integer(payload, "finished_at"),
            status=cls._string(payload, "status"),
            backup_id=cls._optional_string(payload, "backup_id"),
            source_bytes=cls._optional_integer(payload, "source_bytes"),
            stored_bytes=cls._optional_integer(payload, "stored_bytes"),
            failure_code=cls._optional_string(payload, "failure_code"),
            verified=verified,
            lifecycle_covered=cls._optional_bool(payload, "lifecycle_covered"),
        )

    @classmethod
    def _backup_status(cls, payload: Mapping[str, object]) -> RemoteBackupStatus:
        raw_runs = payload.get("runs")
        if not isinstance(raw_runs, list):
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        runs: list[RemoteBackupRun] = []
        for item in cast(list[object], raw_runs):
            if not isinstance(item, dict):
                raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
            runs.append(cls._backup_run(cast(dict[str, object], item)))
        overdue = payload.get("backup_overdue")
        if not isinstance(overdue, bool):
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        return RemoteBackupStatus(
            last_backup_at=cls._optional_integer(payload, "last_backup_at"),
            last_backup_bytes=cls._optional_integer(payload, "last_backup_bytes"),
            last_backup_status=cls._string(payload, "last_backup_status"),
            backup_retention_mode=cls._optional_string(payload, "backup_retention_mode"),
            backup_overdue=overdue,
            runs=tuple(runs),
            backup_lifecycle_covered=cls._optional_bool(payload, "backup_lifecycle_covered"),
        )

    @classmethod
    def _backup_download(cls, payload: Mapping[str, object]) -> RemoteBackupDownload:
        raw_parts = payload.get("parts")
        raw_notes = payload.get("restore_notes")
        if not isinstance(raw_parts, list) or not isinstance(raw_notes, list):
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        parts: list[RemoteBackupDownloadPart] = []
        for item in cast(list[object], raw_parts):
            if not isinstance(item, dict):
                raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
            entry = cast(dict[str, object], item)
            parts.append(
                RemoteBackupDownloadPart(
                    name=cls._string(entry, "name"),
                    size=cls._integer(entry, "size", minimum=0),
                    sha256=cls._string(entry, "sha256"),
                    stored_size=cls._integer(entry, "stored_size", minimum=0),
                    url=cls._string(entry, "url"),
                )
            )
        return RemoteBackupDownload(
            backup_id=cls._string(payload, "backup_id"),
            created_at=cls._integer(payload, "created_at", minimum=1),
            app_version=cls._string(payload, "app_version"),
            expires_in=cls._integer(payload, "expires_in", minimum=1),
            parts=tuple(parts),
            restore_notes=tuple(
                item if isinstance(item, str) else "" for item in cast(list[object], raw_notes)
            ),
        )

    @classmethod
    def _storage_verification(
        cls,
        payload: Mapping[str, object],
    ) -> RemoteObjectStorageVerification:
        capabilities = payload.get("capabilities")
        verified = payload.get("verified")
        if not isinstance(capabilities, Mapping) or not isinstance(verified, bool):
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        return RemoteObjectStorageVerification(
            verified=verified,
            status=cls._string(payload, "status"),
            capabilities=cast(Mapping[str, object], capabilities),
        )

    @staticmethod
    def _read_limited_json_body(response: IO[bytes]) -> bytes:
        chunks: list[bytes] = []
        received = 0
        while received <= _MAX_JSON_RESPONSE_BYTES:
            chunk = response.read(min(_CHUNK_SIZE, _MAX_JSON_RESPONSE_BYTES - received + 1))
            if not chunk:
                return b"".join(chunks)
            received += len(chunk)
            if received > _MAX_JSON_RESPONSE_BYTES:
                raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
            chunks.append(chunk)
        raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")

    def _request(
        self,
        path: str,
        query: Mapping[str, str] | None = None,
    ) -> urllib.request.Request:
        suffix = "" if query is None else f"?{urllib.parse.urlencode(query)}"
        return urllib.request.Request(
            f"{self._base_url}{path}{suffix}",
            method="GET",
            headers={
                **self._client_headers,
                "Accept": "application/json, application/octet-stream",
                "Authorization": f"Bearer {self._device_token}",
            },
        )

    @staticmethod
    def _items(payload: Mapping[str, object]) -> list[object]:
        items = payload.get("items")
        if not isinstance(items, list):
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        return cast(list[object], items)

    @staticmethod
    def _mapping(value: object) -> Mapping[str, object]:
        if not isinstance(value, Mapping):
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        return cast(Mapping[str, object], value)

    @classmethod
    def _message(cls, value: object) -> RemoteMessage:
        item = cls._mapping(value)
        record_key = cls._string(item, "record_key")
        conversation_id = cls._string(item, "conversation_id", allow_empty=True)
        created_at = cls._integer(item, "created_at")
        content_text = cls._string(item, "content_text", allow_empty=True)
        content_sha256 = cls._sha256(item, "content_sha256")
        server_sequence = cls._integer(item, "server_sequence", minimum=1)
        return RemoteMessage(
            record_key,
            conversation_id,
            created_at,
            content_text,
            content_sha256,
            server_sequence,
            # Absent and null both mean "the remote was never told". A v1
            # remote omits these keys entirely and has to keep working.
            sender_uid=cls._optional_string(item, "sender_uid"),
            sender_display_name=cls._optional_string(item, "sender_display_name"),
            is_self=cls._optional_bool(item, "is_self"),
            message_type=cls._optional_string(item, "message_type"),
            app_card_json=cls._optional_string(item, "app_card_json"),
            reply_to_key=cls._optional_string(item, "reply_to_key"),
            source_sequence=cls._optional_position(item, "source_sequence"),
            source_local_id=cls._optional_position(item, "source_local_id"),
            attachment_refs=cls._optional_refs(item, "attachment_refs"),
            source_account_id=cls._optional_string(item, "source_account_id"),
            sender_avatar_blob_id=cls._optional_string(item, "sender_avatar_blob_id"),
            transcript=cls._optional_transcript(item.get("transcript")),
            reply_preview=cls._optional_reply_preview(item.get("reply_preview")),
        )

    @classmethod
    def _optional_reply_preview(cls, value: object) -> ReplyPreviewRecord | None:
        if value is None:
            return None
        item = cls._mapping(value)
        return ReplyPreviewRecord(
            record_key=cls._string(item, "record_key"),
            sender_display_name=cls._optional_string(item, "sender_display_name"),
            content_text=cls._string(item, "content_text", allow_empty=True),
            message_type=cls._optional_string(item, "message_type"),
            attachment_refs=cls._optional_refs(item, "attachment_refs"),
        )

    @classmethod
    def _optional_transcript(cls, value: object) -> RemoteVoiceTranscript | None:
        """一条语音的机器转写，或者「这个远端没有这一版 / 这条没有转写」。

        键不在与 null 是同一句话（v12 及更早的远端根本不说这个），所以两者都
        走 None。形状读不通就当没有——**不抛**：一条脏转写不该让整页消息读不出来。
        """
        if not isinstance(value, Mapping):
            return None
        item = cast(Mapping[str, object], value)
        text = item.get("text")
        engine = item.get("engine")
        low_confidence = item.get("low_confidence")
        if not isinstance(text, str) or not text or not isinstance(engine, str):
            return None
        return RemoteVoiceTranscript(
            text=text,
            engine=engine,
            low_confidence=low_confidence is True,
        )

    @classmethod
    def _member(cls, value: object) -> RemoteConversationMember:
        item = cls._mapping(value)
        is_self = item.get("is_self")
        if not isinstance(is_self, bool):
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        return RemoteConversationMember(
            member_uid=cls._string(item, "member_uid"),
            display_name=cls._string(item, "display_name", allow_empty=True),
            group_alias=cls._optional_string(item, "group_alias"),
            is_self=is_self,
            avatar_blob_id=cls._optional_string(item, "avatar_blob_id"),
        )

    @staticmethod
    def _optional_bool(payload: Mapping[str, object], key: str) -> bool | None:
        value = payload.get(key)
        if value is None:
            return None
        if not isinstance(value, bool):
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        return value

    @staticmethod
    def _optional_position(payload: Mapping[str, object], key: str) -> int:
        value = payload.get(key)
        if value is None:
            return 0
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        return value

    @staticmethod
    def _optional_refs(payload: Mapping[str, object], key: str) -> tuple[str, ...]:
        value = payload.get(key)
        if value is None:
            return ()
        if not isinstance(value, list):
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        items = cast(list[object], value)
        if any(not isinstance(entry, str) for entry in items):
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        return tuple(cast(list[str], items))

    @classmethod
    def _record(cls, value: object) -> RemoteRecord:
        item = cls._mapping(value)
        message = cls._message(item)
        return RemoteRecord(
            record_key=message.record_key,
            conversation_id=message.conversation_id,
            created_at=message.created_at,
            content_text=message.content_text,
            content_sha256=message.content_sha256,
            source_instance_id=cls._string(item, "source_instance_id"),
            schema_version=cls._string(item, "schema_version"),
            server_sequence=message.server_sequence,
        )

    @classmethod
    def _conversation(cls, value: object) -> RemoteConversationSummary:
        item = cls._mapping(value)
        return RemoteConversationSummary(
            conversation_id=cls._string(item, "conversation_id", allow_empty=True),
            message_count=cls._integer(item, "message_count", minimum=0),
            last_created_at=cls._integer(item, "last_created_at"),
            last_server_sequence=cls._integer(item, "last_server_sequence", minimum=0),
            display_name=cls._optional_string(item, "display_name"),
            is_group=cls._optional_bool(item, "is_group"),
            source_account_id=cls._optional_string(item, "source_account_id"),
            last_message_type=cls._optional_string(item, "last_message_type"),
            last_message_preview=cls._optional_string(item, "last_message_preview"),
            avatar_blob_id=cls._optional_string(item, "avatar_blob_id"),
            announcement=cls._optional_string(item, "announcement"),
            my_group_alias=cls._optional_string(item, "my_group_alias"),
            # 缺席与 null 都收成 None，而 None 是「没有名单」而不是「名单是空的」。
            # 收成 0 就等于替远端声称这个群一个人都没有。
            member_count=cls._optional_count(item, "member_count"),
        )

    @staticmethod
    def _optional_count(payload: Mapping[str, object], key: str) -> int | None:
        value = payload.get(key)
        if value is None:
            return None
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        return value

    @classmethod
    def _page_tail(cls, payload: Mapping[str, object]) -> tuple[int, bool]:
        next_sequence = cls._integer(payload, "next_server_sequence", minimum=0)
        has_more = payload.get("has_more")
        if not isinstance(has_more, bool):
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        return next_sequence, has_more

    @staticmethod
    def _read_conversation_id(payload: Mapping[str, object]) -> str | None:
        """这一页/这一段是关于哪条会话的，按远端自己的说法（记录模型 v12，PSU-2.95）。

        缺席读成 `None`——「这个远端没说」，v11 及更早都是这样，读侧照旧拿
        调用方问的那个当主语。在场但不是字符串是**协议错**：那不是"没说"，
        是说了一句读不懂的话，两者混掉会把一个坏掉的远端当成一个老远端。
        空串留给上层拒（它认得那是"没有主语的一句话"），这里只管形状。
        """
        value = payload.get("read_conversation_id")
        if value is None:
            return None
        if not isinstance(value, str):
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        return value

    @staticmethod
    def _string(
        payload: Mapping[str, object],
        key: str,
        *,
        allow_empty: bool = False,
    ) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or (not allow_empty and not value):
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        return value

    @staticmethod
    def _integer(payload: Mapping[str, object], key: str, *, minimum: int | None = None) -> int:
        value = payload.get(key)
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or (minimum is not None and value < minimum)
        ):
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        return value

    @classmethod
    def _sha256(cls, payload: Mapping[str, object], key: str) -> str:
        value = cls._string(payload, key)
        if _SHA256_HEX.fullmatch(value) is None:
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        return value

    def _archive_path(self, archive_id: str, suffix: str) -> str:
        return f"/api/v1/archives/{self._quote_identity(archive_id)}/{suffix}"

    @staticmethod
    def _quote_identity(value: str) -> str:
        if not value:
            raise ValueError("remote identity must be non-empty")
        return urllib.parse.quote(value, safe="-_~")

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if isinstance(limit, bool) or limit <= 0 or limit > _MAX_PAGE_SIZE:
            raise ValueError("page limit must be between 1 and 500")

    @staticmethod
    def _validate_account(source_account_id: str) -> None:
        if not source_account_id:
            raise ValueError("account filter must be non-empty")

    @staticmethod
    def _validate_cursor(cursor: int) -> None:
        if isinstance(cursor, bool) or cursor < 0:
            raise ValueError("server sequence cursor must be non-negative")

    @staticmethod
    def _http_error(status: int) -> RemoteArchiveError:
        if status in {408, 425, 429} or status >= 500:
            return RemoteArchiveError("REMOTE_RETRYABLE")
        return RemoteArchiveError(f"REMOTE_HTTP_{status}")
