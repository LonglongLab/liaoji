"""在线分享的建与管（PSU-2.181）：设备令牌的路由与浏览器那一路调的是同一个它。

一条流程，两种调用方：本机（经设备令牌 HTTP）与远程网页（经 BFF、进程内）走到的
都是这里的六个方法，所以两端不会对同一件事给出两种答案。

附件字节由调用方交到这里、由这里写进对象存储。与同步附件那条「字节不经过本进程」
不一样，是有意的：分享是偶尔的一次、几十 MB 的量级，换来的是两种调用方只有一条
上传路径——远程网页从浏览器直传对象存储要改桶的 CORS，本机要多一轮预签名与核验。
上限在 `share_draft.py`：单个附件 200 MB、整份 1 GB。

**远端已有的字节不必再传（PSU-2.184）。** 草稿里每个附件可以带上「这份字节的 SHA-256」；
建草稿时在本归档里找当前代、同内容、已 `remote_ready` 的那一份，找得到就在对象存储里
复制到分享自己的键下、当场记为已传。按内容而不按附件 id 找，因为分享传的未必是归档存的
那份字节（wxgf 解码过、语音转过 mp3）——内容相同才复制，就不会拷错。复制仍是副本，
删分享只删它自己的对象。复制不成（没找到、对象不在、存储抖动）一律不报错，只是留在
`awaiting_upload` 里让调用方照旧上传。

所有失败都是 `RemoteServerError` 的稳定代号；provider 原话、对象键、路径一概不外露。
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

from chat_archive.ports.blob_state import BlobStateStore
from chat_archive.ports.objectstore import (
    ObjectStore,
    ObjectStoreError,
    new_object_key,
)
from chat_archive.server.http import RemoteServerError
from chat_archive.server.object_storage_config import ObjectStoreNotConfiguredError
from chat_archive.server.share_sweeper import remove_share
from chat_archive.server.shares import (
    EXPIRED_GRACE_SECONDS,
    SHARE_OBJECT_SCOPE,
    STALE_DRAFT_SECONDS,
    STATE_DRAFT,
    STATE_LIVE,
    Share,
    ShareNotFoundError,
    ShareObject,
    ShareObjectSpec,
    ShareStateError,
    SqliteShareStore,
    is_share_token,
)
from chat_archive.share_draft import (
    MAX_OBJECT_BYTES,
    ShareDraftError,
    parse_share_draft,
)

DAY_SECONDS = 24 * 3600
# 「延期」一次加多少（原型第 4 块）。已过期的「重新开」也是这么长。
EXTEND_SECONDS = 7 * DAY_SECONDS

SHARE_PATH_PREFIX = "/s/"
# 建草稿时同时发几个复制。每个都是对象存储的一次往返；500 条上限时最坏几百个。
COPY_CONCURRENCY = 4

_logger = logging.getLogger(__name__)


class ShareSourceCopier(Protocol):
    def __call__(self, archive_id: str, *, sha256: str, size: int, target_key: str) -> bool:
        """归档里有这份内容就复制到 ``target_key`` 并答 True；没有答 False。

        复制失败抛出来，由 `ShareService` 当作「没有」处理。
        """
        ...


class ArchiveContentCopier:
    """在本归档里按内容找 `remote_ready` 的一份，复制到分享的键下（PSU-2.184）。

    与附件上传的认领（`blob_routes.py`）用同一个查询：作用域是本归档、当前代。
    ``object_store_snapshot`` 给的是此刻的具体存储与它的代号，与签名地址那条路同源，
    所以「找的那一代」与「复制所在的桶」是同一个。
    """

    def __init__(
        self,
        blob_state_store: BlobStateStore,
        object_store_snapshot: Callable[[], tuple[ObjectStore, int]],
    ) -> None:
        self._blobs = blob_state_store
        self._snapshot = object_store_snapshot

    def __call__(self, archive_id: str, *, sha256: str, size: int, target_key: str) -> bool:
        concrete, generation = self._snapshot()
        manifest = self._blobs.find_ready_manifest_by_content(
            archive_id, sha256=sha256, size=size, storage_generation=generation
        )
        if manifest is None:
            return False
        concrete.copy_object(
            manifest.object_scope,
            manifest.object_key,
            SHARE_OBJECT_SCOPE,
            target_key,
            sha256=sha256,
            size=size,
        )
        return True


def share_summary(share: Share, moment: int) -> dict[str, object]:
    """「我的分享」一行要的东西。不含快照，也不含对象键。"""
    if share.state == STATE_DRAFT:
        state = "draft"
        purge_at: int | None = share.created_at + STALE_DRAFT_SECONDS
    elif share.expires_at is not None and share.expires_at <= moment:
        state = "expired"
        purge_at = share.expires_at + EXPIRED_GRACE_SECONDS
    else:
        state = "live"
        purge_at = None
    return {
        "token": share.token,
        "path": f"{SHARE_PATH_PREFIX}{share.token}",
        "title": share.title,
        "message_count": share.message_count,
        "attachment_count": len(share.objects),
        "attachment_bytes": sum(item.size for item in share.objects),
        "created_at": share.created_at,
        "expires_at": share.expires_at,
        "state": state,
        "views": share.views,
        "last_viewed_at": share.last_viewed_at,
        "purge_at": purge_at,
    }


class ShareService:
    def __init__(
        self,
        store: SqliteShareStore,
        objects: ObjectStore,
        *,
        copier: ShareSourceCopier | None = None,
    ) -> None:
        self._store = store
        self._objects = objects
        self._copier = copier

    # --- 建 -----------------------------------------------------------------

    def create(self, archive_id: str, payload: object) -> dict[str, object]:
        try:
            draft = parse_share_draft(payload)
        except ShareDraftError as error:
            raise RemoteServerError(error.code, "分享草稿未通过校验。") from None
        moment = self._store.now()
        expires_at = (
            None
            if draft.expires_in_days is None
            else moment + draft.expires_in_days * DAY_SECONDS
        )
        share = self._store.create_draft(
            archive_id=archive_id,
            title=draft.title,
            message_count=draft.message_count,
            snapshot_json=draft.snapshot_json,
            expires_at=expires_at,
            objects=[
                ShareObjectSpec(
                    attachment_id=item.attachment_id,
                    # 副本另起一个键：删分享只删它自己的对象（见 `shares.py`）。
                    object_key=new_object_key(SHARE_OBJECT_SCOPE),
                    size=item.size,
                    mime_type=item.mime_type,
                )
                for item in draft.attachments
            ],
        )
        digests = {item.attachment_id: item.sha256 for item in draft.attachments}
        copied = self._copy_known_content(archive_id, share, digests)
        for attachment_id in copied:
            self._store.mark_uploaded(share.token, attachment_id)
        return {
            **share_summary(share, moment),
            "awaiting_upload": [
                item.attachment_id for item in share.objects if item.attachment_id not in copied
            ],
        }

    def _copy_known_content(
        self, archive_id: str, share: Share, digests: Mapping[str, str | None]
    ) -> set[str]:
        """把归档里已有的那几份在对象存储里复制过去，答复制成了的附件 id。"""
        copier = self._copier
        wanted = [
            (item, digest)
            for item in share.objects
            if (digest := digests.get(item.attachment_id)) is not None
        ]
        if copier is None or not wanted:
            return set()

        def attempt(item: ShareObject, digest: str) -> str | None:
            try:
                found = copier(
                    archive_id, sha256=digest, size=item.size, target_key=item.object_key
                )
            except (ObjectStoreError, ObjectStoreNotConfiguredError):
                return None
            return item.attachment_id if found else None

        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=COPY_CONCURRENCY) as pool:
            results = list(pool.map(attempt, *zip(*wanted, strict=True)))
        copied = {attachment_id for attachment_id in results if attachment_id is not None}
        # 只记条数与耗时：对象键、摘要、附件名都不进日志。
        _logger.info(
            "share draft copied %d of %d declared attachments in %.2fs",
            len(copied),
            len(wanted),
            time.monotonic() - started,
        )
        return copied

    def upload(
        self, archive_id: str, token: str, attachment_id: str, content: bytes
    ) -> None:
        share = self._owned(archive_id, token)
        if share.state != STATE_DRAFT:
            raise RemoteServerError("SHARE_STATE_INVALID", "分享已生成，不能再补附件。")
        declared = next(
            (item for item in share.objects if item.attachment_id == attachment_id), None
        )
        if declared is None:
            raise RemoteServerError("SHARE_NOT_FOUND", "草稿里没有这个附件。")
        if len(content) != declared.size or len(content) > MAX_OBJECT_BYTES:
            raise RemoteServerError("SHARE_UPLOAD_MISMATCH", "附件大小与声明不符。")
        try:
            self._objects.put_object(
                SHARE_OBJECT_SCOPE,
                declared.object_key,
                content,
                sha256=hashlib.sha256(content).hexdigest(),
            )
        except ObjectStoreNotConfiguredError:
            raise RemoteServerError(
                "OBJECT_STORAGE_NOT_CONFIGURED", "对象存储尚未配置。"
            ) from None
        except ObjectStoreError:
            raise RemoteServerError(
                "SHARE_STORAGE_FAILED", "对象存储暂时不可用。", retryable=True
            ) from None
        try:
            self._store.mark_uploaded(token, attachment_id)
        except ShareStateError:
            raise RemoteServerError("SHARE_STATE_INVALID", "分享已生成。") from None

    def finalize(self, archive_id: str, token: str) -> dict[str, object]:
        share = self._owned(archive_id, token)
        if share.state != STATE_DRAFT:
            raise RemoteServerError("SHARE_STATE_INVALID", "分享已经生成过了。")
        if any(not item.uploaded for item in share.objects):
            raise RemoteServerError("SHARE_INCOMPLETE", "还有附件没传上。")
        try:
            published = self._store.publish(token)
        except ShareStateError:
            raise RemoteServerError("SHARE_STATE_INVALID", "分享已经生成过了。") from None
        return share_summary(published, self._store.now())

    # --- 管 -----------------------------------------------------------------

    def list(self, archive_id: str) -> list[dict[str, object]]:
        moment = self._store.now()
        return [
            share_summary(share, moment)
            for share in self._store.list_for_archive(archive_id)
            # 草稿是生成中的半成品：「我的分享」里只列生成好的。
            if share.state == STATE_LIVE
        ]

    def extend(self, archive_id: str, token: str) -> dict[str, object]:
        """再给 7 天：没过期的从原失效时刻往后加，已过期的从现在起重新开 7 天。

        永久的不动。只往后加、不往前挪：一个还剩 30 天的分享点「延期」不该变成 7 天。
        """
        share = self._owned(archive_id, token)
        if share.state != STATE_LIVE:
            raise RemoteServerError("SHARE_STATE_INVALID", "分享还没生成。")
        moment = self._store.now()
        if share.expires_at is None:
            return share_summary(share, moment)
        updated = self._store.set_expiry(
            token, max(share.expires_at, moment) + EXTEND_SECONDS
        )
        return share_summary(updated, moment)

    def delete(self, archive_id: str, token: str) -> None:
        """真删：先删对象、再删行。删完公开地址立刻就是 `SHARE_GONE`。"""
        share = self._owned(archive_id, token)
        if not remove_share(self._store, self._objects, share):
            raise RemoteServerError(
                "SHARE_DELETE_FAILED", "副本没删干净，稍后再删一次。", retryable=True
            )

    # --- 内部 ---------------------------------------------------------------

    def _owned(self, archive_id: str, token: str) -> Share:
        """只认本归档发出的分享；别的归档的令牌与不存在的令牌答同一句。"""
        share = self._store.get(token) if is_share_token(token) else None
        if share is None or share.archive_id != archive_id:
            raise RemoteServerError("SHARE_NOT_FOUND", "没有这个分享。")
        return share


def share_payload(value: object) -> Mapping[str, object]:
    """请求体里的 JSON 必须是一个对象。"""
    if not isinstance(value, Mapping):
        raise RemoteServerError("QUERY_INVALID", "请求体必须是 JSON 对象。")
    return value  # pyright: ignore[reportUnknownVariableType]


__all__ = [
    "EXTEND_SECONDS",
    "SHARE_PATH_PREFIX",
    "ShareNotFoundError",
    "ShareService",
    "share_payload",
    "share_summary",
]
