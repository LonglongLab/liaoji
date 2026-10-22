"""备份对象的读写口（PSU-2.140）。

**为什么不复用 `S3CompatibleObjectStore`。** 那个适配器实现的是附件契约：键必须是
`a/<archive_id>/<32 hex>` 的不透明形状（`require_archive_key` 逐次强制）、写进去的
对象不可覆盖、完成时要按 sha256 读回核验。备份要的是相反的一组性质——可排序的键、
按前缀列举、按保留策略过期。硬掰那个口只会把两件事都弄坏，所以这里是一个小而独立的
客户端，只与它共用 `client_config`（COS 的 virtual-hosted 寻址与 checksum 开关）
和错误码不外泄的约定。

**Content-MD5 就是完整性证明。** PSU-2.131 已经在真桶上确认：COS 会拒收签名
`Content-MD5` 与实际字节不符的上传（`BadDigest`），单件对象的 ETag 就是它的 MD5。
所以"传上去的确实是我算过的那些字节"这件事，一次带 MD5 的 PUT 加一次 HEAD 就证完了，
不需要把 400 MB 跨太平洋拉回来重算。

错误只带 provider 错误码，不带 URL、键、端点或凭据（`FR-BLOB-005`、`NFR-SEC-004`）。
"""

from __future__ import annotations

import base64
import hashlib
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from chat_archive.adapters.s3.objectstore import client_config

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client
    from mypy_boto3_s3.type_defs import ObjectTypeDef

_CHUNK = 1024 * 1024

# 只有这几个码是「provider 答了：没有这条规则」。别的失败——尤其 `AccessDenied`
# ——是「问不到」，不是「没有」。这条教训是 PSU-2.57 用一次假的
# `OBJECT_STORAGE_UNHEALTHY` 换来的，别在这里重犯。
_LIFECYCLE_ABSENT_CODES = frozenset(
    {"NoSuchLifecycleConfiguration", "NoSuchLifecycleConfigurationError", "NotImplemented"}
)

_NOT_FOUND_CODES = frozenset({"404", "NoSuchKey", "NotFound"})

# 签进 Content-Disposition 的文件名只允许这几种字符：它会原样进 HTTP 头，
# 引号、分号、路径分隔符都不许出现。备份段名本来就是 `<备份号>-<库名>.gz`。
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class BackupStoreError(RuntimeError):
    """备份对象存储操作失败。"""


class BackupObjectNotFoundError(BackupStoreError):
    """这个键上没有对象。"""


@dataclass(frozen=True)
class BackupObject:
    key: str
    size: int
    etag: str


def md5_base64(payload: bytes) -> str:
    return base64.b64encode(hashlib.md5(payload).digest()).decode("ascii")


def file_md5_base64(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as stream:
        while chunk := stream.read(_CHUNK):
            digest.update(chunk)
    return base64.b64encode(digest.digest()).decode("ascii")


class BackupStore(Protocol):
    """备份路径用到的全部对象操作。测试用假实现顶上这个口。"""

    def put_file(self, key: str, path: Path, *, content_md5: str) -> str: ...

    def put_bytes(self, key: str, payload: bytes, *, content_md5: str) -> str: ...

    def head(self, key: str) -> BackupObject: ...

    def get_bytes(self, key: str) -> bytes: ...

    def download(self, key: str, destination: Path) -> None: ...

    def list_keys(self, prefix: str) -> tuple[str, ...]: ...

    def copy(self, source_key: str, destination_key: str) -> None: ...

    def delete(self, key: str) -> None: ...

    def lifecycle_expiration_prefixes(self) -> tuple[str, ...] | None: ...

    def presign_get(
        self, key: str, *, expires_in: int = 900, filename: str | None = None
    ) -> str: ...


def _error_code(exc: ClientError) -> str:
    error = cast(dict[str, object], exc.response.get("Error", {}))
    return str(error.get("Code", "Unknown"))


def _wrap(exc: ClientError, operation: str) -> BackupStoreError:
    return BackupStoreError(f"{operation} failed with provider code {_error_code(exc)}")


class S3BackupStore:
    """S3 兼容的备份目标（腾讯云 COS / MinIO / S3）。"""

    def __init__(
        self,
        *,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        region: str = "us-east-1",
        allow_insecure_transport: bool = False,
    ) -> None:
        if not endpoint_url.startswith("https://") and not allow_insecure_transport:
            raise BackupStoreError(
                "backup endpoint must use TLS; insecure transport is an explicit opt-out"
            )
        if not bucket:
            raise BackupStoreError("backup bucket must be non-empty")
        self._bucket = bucket
        self._client: S3Client = boto3.client(  # pyright: ignore[reportUnknownMemberType]
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=client_config(endpoint_url),
        )

    def put_file(self, key: str, path: Path, *, content_md5: str) -> str:
        try:
            with path.open("rb") as stream:
                response = self._client.put_object(
                    Bucket=self._bucket, Key=key, Body=stream, ContentMD5=content_md5
                )
        except ClientError as exc:
            raise _wrap(exc, "put_object") from None
        except BotoCoreError:
            raise BackupStoreError("put_object failed in transport") from None
        return str(response.get("ETag", ""))

    def put_bytes(self, key: str, payload: bytes, *, content_md5: str) -> str:
        try:
            response = self._client.put_object(
                Bucket=self._bucket, Key=key, Body=payload, ContentMD5=content_md5
            )
        except ClientError as exc:
            raise _wrap(exc, "put_object") from None
        except BotoCoreError:
            raise BackupStoreError("put_object failed in transport") from None
        return str(response.get("ETag", ""))

    def head(self, key: str) -> BackupObject:
        try:
            response = self._client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if _error_code(exc) in _NOT_FOUND_CODES:
                raise BackupObjectNotFoundError("backup object is missing") from None
            raise _wrap(exc, "head_object") from None
        except BotoCoreError:
            raise BackupStoreError("head_object failed in transport") from None
        return BackupObject(
            key=key,
            size=int(response.get("ContentLength", 0)),
            etag=str(response.get("ETag", "")),
        )

    def get_bytes(self, key: str) -> bytes:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            return response["Body"].read()
        except ClientError as exc:
            if _error_code(exc) in _NOT_FOUND_CODES:
                raise BackupObjectNotFoundError("backup object is missing") from None
            raise _wrap(exc, "get_object") from None
        except BotoCoreError:
            raise BackupStoreError("get_object failed in transport") from None

    def download(self, key: str, destination: Path) -> None:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            body = response["Body"]
            with destination.open("wb") as stream:
                while chunk := body.read(_CHUNK):
                    stream.write(chunk)
        except ClientError as exc:
            if _error_code(exc) in _NOT_FOUND_CODES:
                raise BackupObjectNotFoundError("backup object is missing") from None
            raise _wrap(exc, "get_object") from None
        except BotoCoreError:
            raise BackupStoreError("get_object failed in transport") from None

    def _pages(self, prefix: str) -> Iterator[list[ObjectTypeDef]]:
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            yield page["Contents"] if "Contents" in page else []

    def list_keys(self, prefix: str) -> tuple[str, ...]:
        keys: list[str] = []
        try:
            for entries in self._pages(prefix):
                for entry in entries:
                    key = entry.get("Key")
                    if key is not None:
                        keys.append(str(key))
        except ClientError as exc:
            raise _wrap(exc, "list_objects") from None
        except BotoCoreError:
            raise BackupStoreError("list_objects failed in transport") from None
        return tuple(sorted(keys))

    def copy(self, source_key: str, destination_key: str) -> None:
        try:
            self._client.copy_object(
                Bucket=self._bucket,
                Key=destination_key,
                CopySource={"Bucket": self._bucket, "Key": source_key},
            )
        except ClientError as exc:
            raise _wrap(exc, "copy_object") from None
        except BotoCoreError:
            raise BackupStoreError("copy_object failed in transport") from None

    def delete(self, key: str) -> None:
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            raise _wrap(exc, "delete_object") from None
        except BotoCoreError:
            raise BackupStoreError("delete_object failed in transport") from None

    def presign_get(
        self, key: str, *, expires_in: int = 900, filename: str | None = None
    ) -> str:
        """一条短命的只读链接，给"把最新一份下载回本机"那一步用。

        不走 API 转发：那会让 400 MB 跨太平洋走两趟，还要一台 2 核机器当中继。
        签出来的是 GET，签名里绑死这一个键，过期就作废。

        `filename` 签进 `response-content-disposition`：下载是浏览器/WebView2
        自己发起的跨域 GET，`<a download>` 的名字在跨域上不算数，文件叫什么
        只有服务端的 Content-Disposition 说了算。不签它，落地的就是对象键的
        最后一段 `metadata.sqlite.gz`，五份备份下来分不清哪份是哪份。
        """
        if not 60 <= expires_in <= 3600:
            raise BackupStoreError("presign expiry is out of range")
        params: dict[str, str] = {"Bucket": self._bucket, "Key": key}
        if filename is not None:
            if not _SAFE_FILENAME.fullmatch(filename):
                raise BackupStoreError("presign filename is invalid")
            params["ResponseContentDisposition"] = f'attachment; filename="{filename}"'
        try:
            return str(
                self._client.generate_presigned_url(
                    "get_object",
                    Params=params,
                    ExpiresIn=expires_in,
                )
            )
        except ClientError as exc:
            raise _wrap(exc, "presign_get") from None
        except BotoCoreError:
            raise BackupStoreError("presign_get failed in transport") from None

    def lifecycle_expiration_prefixes(self) -> tuple[str, ...] | None:
        """哪些前缀配了过期规则——或者 `None` = 问不到。

        三种结果，第三种才是重点：`AccessDenied` 说明不了任何关于规则是否存在的事，
        把它报成"没有规则"就是把权限事实当成了配置缺口。
        """
        try:
            lifecycle = self._client.get_bucket_lifecycle_configuration(Bucket=self._bucket)
        except ClientError as exc:
            return () if _error_code(exc) in _LIFECYCLE_ABSENT_CODES else None
        except BotoCoreError:
            return None
        prefixes: list[str] = []
        rules = cast(list[dict[str, object]], lifecycle.get("Rules", []))
        for rule in rules:
            if str(rule.get("Status", "")) != "Enabled":
                continue
            if "Expiration" not in rule and "NoncurrentVersionExpiration" not in rule:
                continue
            candidate = rule.get("Prefix")
            if isinstance(candidate, str):
                prefixes.append(candidate)
                continue
            rule_filter = rule.get("Filter")
            if isinstance(rule_filter, dict):
                nested = cast(dict[str, object], rule_filter).get("Prefix")
                prefixes.append(nested if isinstance(nested, str) else "")
        return tuple(prefixes)


@dataclass(frozen=True)
class BackupVerification:
    """一次真往返的结论。没有键、没有 URL、没有 provider 原话。

    `can_delete` 不是"能力缺陷"而是**一项配置事实**：推荐的子账号密钥**故意**
    不带 `DeleteObject`，于是删除权只在桶的生命周期规则手里，服务器被打穿也毁不掉
    自己的备份。所以这里如实报出来，由调用方决定它与 `retention_mode` 配不配套——
    选了 `application` 却删不动，是会静默堆积到把桶撑满的那一类错误配置。
    """

    verified: bool
    can_delete: bool
    lifecycle_prefixes: tuple[str, ...] | None


PROBE_PREFIX = ".probe"


def verify_backup_store(store: BackupStore, prefix: str) -> BackupVerification:
    """写一个探针对象、读回来核字节、再尽量删掉。

    删不掉不算失败——那正是最小权限密钥该有的样子。留下的探针对象由生命周期
    规则过期；它只有几十字节，且落在备份自己的前缀下，不进任何清单对账。
    """
    payload = f"chat-archive backup probe {uuid.uuid4().hex}".encode("ascii")
    key = f"{prefix}/{PROBE_PREFIX}/{uuid.uuid4().hex}"
    store.put_bytes(key, payload, content_md5=md5_base64(payload))
    landed = store.head(key)
    if landed.size != len(payload):
        raise BackupStoreError("probe object did not land with the size it was sent with")
    if store.get_bytes(key) != payload:
        raise BackupStoreError("probe object did not read back as it was written")
    can_delete = True
    try:
        store.delete(key)
    except BackupStoreError:
        can_delete = False
    return BackupVerification(
        verified=True,
        can_delete=can_delete,
        lifecycle_prefixes=store.lifecycle_expiration_prefixes(),
    )



__all__ = [
    "PROBE_PREFIX",
    "BackupObject",
    "BackupObjectNotFoundError",
    "BackupStore",
    "BackupStoreError",
    "BackupVerification",
    "S3BackupStore",
    "file_md5_base64",
    "md5_base64",
    "verify_backup_store",
]
