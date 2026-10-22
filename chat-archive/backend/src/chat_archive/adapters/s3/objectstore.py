"""S3-compatible implementation of the ObjectStore contract (R2/S3/MinIO).

Parts travel over presigned single-method PUT URLs; the SDK client with the
service credential stays server-side. Completion verifies the object by
reading it back and hashing (explicit SHA-256 — ETag is never trusted as a
content hash), deleting the unverified object on mismatch so a caller can
never observe an unverified ``remote_ready`` candidate. Verified keys are
immutable. Provider capability gaps (versioning, conditional writes,
lifecycle rules, platform checksums) are reported by ``probe_capabilities``
instead of being silently emulated.

Errors carry only provider error codes — never URLs, keys+endpoint pairs,
or credentials (FR-BLOB-005, NFR-SEC-004).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, BinaryIO, cast

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from chat_archive.ports.objectstore import (
    CompletedPart,
    ObjectAttributes,
    ObjectHead,
    ObjectImmutabilityError,
    ObjectIntegrityError,
    ObjectNotFoundError,
    ObjectMultipartUploadMissingError,
    ObjectReconciliation,
    ObjectStoreCapabilityError,
    ObjectStoreError,
    ObjectStoreValidationError,
    PresignedRequest,
    StoreCapabilities,
    UploadPlan,
    new_object_key,
    reconcile_objects,
    require_archive_key,
    require_md5,
    require_sha256,
)

# Longest a write capability may live. Deliberately the same order as the read
# side: a presigned PUT is a bearer token for one key, and the only thing
# keeping a leaked one cheap is that it goes stale.
MAX_PRESIGNED_PUT_SECONDS = 3600

# How long a probe result may steer route selection. Short enough that a store
# which was briefly unreachable is asked again rather than written off; long
# enough that signing five hundred thumbnails does not re-probe five hundred
# times, which would mean five hundred probe objects written and deleted.
CAPABILITY_CACHE_SECONDS = 300.0

# 只有这几个码是「provider 答了：没有这条规则」。别的失败——尤其
# `AccessDenied`——是「问不到」，不是「没有」（PSU-2.57）。
_LIFECYCLE_ABSENT_CODES = frozenset(
    {
        "NoSuchLifecycleConfiguration",
        "NoSuchLifecycleConfigurationError",
        # 明说不实现这套 API 的 provider：那确实是能力缺席。
        "NotImplemented",
    }
)

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client
    from mypy_boto3_s3.type_defs import ObjectTypeDef, PartTypeDef


# 腾讯云 COS 的两个公网域名后缀（PSU-2.129）。
_TENCENT_COS_HOST_SUFFIXES = (".myqcloud.com", ".tencentcos.cn")


# PSU-2.144: how many HEADs `head_objects` keeps in flight. The server sits in
# the US and COS in Nanjing, so one HEAD is ~130 ms of round trip and almost no
# work; asked one after another, a batch-confirm of 100 was 13 seconds of the
# client's wall clock with every one of its upload threads idle.
HEAD_CONCURRENCY = 16
# botocore's default pool is 10; below the fan-out above it would queue the
# extra HEADs on a free connection and quietly cap the concurrency at 10.
_POOL_CONNECTIONS = HEAD_CONCURRENCY + 4


def client_config(endpoint_url: str) -> Config:
    """How to talk to this endpoint; only Tencent COS differs from the default.

    Path-style (``https://host/bucket/key``) is what the bundled MinIO wants,
    but Tencent refuses it on every bucket created after 2024-01-01 — each
    request is a 403, including the round trip the settings page runs before
    it saves. So a COS endpoint is addressed by bucket host
    (``https://bucket.cos.region.myqcloud.com/key``).

    COS also rejects the streaming CRC32 trailer boto3 has attached to every
    PutObject by default since 1.36; Tencent's own S3 SDK guide says to turn it
    off. "When required" keeps every checksum this adapter asks for explicitly.

    Recognised from the endpoint rather than configured: whoever fills in the
    settings page should not have to know what an addressing style is, and any
    endpoint that is not COS is left exactly as it was.
    """
    host = (urllib.parse.urlsplit(endpoint_url).hostname or "").lower()
    if host.endswith(_TENCENT_COS_HOST_SUFFIXES):
        return Config(
            signature_version="s3v4",
            s3={"addressing_style": "virtual"},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            max_pool_connections=_POOL_CONNECTIONS,
        )
    return Config(
        signature_version="s3v4",
        s3={"addressing_style": "path"},
        max_pool_connections=_POOL_CONNECTIONS,
    )


def _error_code(exc: ClientError) -> str:
    error = exc.response.get("Error", {})
    return str(error.get("Code", "Unknown"))


def _wrap(exc: ClientError, operation: str) -> ObjectStoreError:
    return ObjectStoreError(f"{operation} failed with provider code {_error_code(exc)}")


def _signed_headers(url: str, candidates: Mapping[str, str]) -> dict[str, str]:
    """Exactly the headers this signature binds, read off the signature.

    Not the headers we *meant* to bind. Which of them SigV4 actually covers is
    a property of the SDK version — flexible checksums added one, a future
    release may add another — and a URL handed out with a header the client is
    never told to send is a signature mismatch at the far end, minutes later,
    on someone else's machine. So the list comes from ``X-Amz-SignedHeaders``,
    and a name we have no value for is a build-time failure here rather than a
    runtime failure there.
    """
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    declared = query.get("X-Amz-SignedHeaders")
    if not declared:
        raise ObjectStoreError("presigned upload URL binds no headers")
    headers: dict[str, str] = {}
    for name in declared[0].split(";"):
        if not name or name == "host":
            # Set by whoever opens the connection, not by the caller.
            continue
        value = candidates.get(name)
        if value is None:
            raise ObjectStoreError(
                f"presigned upload URL binds an unsupported header: {name}"
            )
        headers[name] = value
    return headers


class S3CompatibleObjectStore:
    def __init__(
        self,
        *,
        endpoint_url: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        region: str = "us-east-1",
        allow_insecure_transport: bool = False,
        presign_expiry_seconds: int = 900,
    ) -> None:
        if not endpoint_url.startswith("https://") and not allow_insecure_transport:
            raise ObjectStoreValidationError(
                "endpoint must use TLS; insecure transport is an explicit opt-out"
            )
        if not bucket:
            raise ObjectStoreValidationError("bucket must be non-empty")
        self._bucket = bucket
        self._presign_expiry_seconds = presign_expiry_seconds
        # `probe_capabilities` writes and deletes real probe objects, so it is
        # far too expensive to ask once per signature. Signing a batch of five
        # hundred thumbnails asks the same question five hundred times, and the
        # answer cannot change without the store being reconfigured — which
        # builds a new instance anyway (`object_storage_config.replace`).
        self._capability_lock = threading.Lock()
        self._capabilities: StoreCapabilities | None = None
        self._capabilities_expire_at = 0.0
        self._client: S3Client = boto3.client(  # pyright: ignore[reportUnknownMemberType]
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=client_config(endpoint_url),
        )

    def _object_exists(self, object_key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=object_key)
        except ClientError as exc:
            if _error_code(exc) in ("404", "NoSuchKey", "NotFound"):
                return False
            raise _wrap(exc, "head_object") from None
        return True

    def _read_object(
        self,
        archive_id: str,
        object_key: str,
        sink: BinaryIO | None,
    ) -> ObjectAttributes:
        require_archive_key(archive_id, object_key)
        try:
            fetched = self._client.get_object(Bucket=self._bucket, Key=object_key)
        except ClientError as exc:
            if _error_code(exc) in ("404", "NoSuchKey", "NotFound"):
                raise ObjectNotFoundError("object not found") from None
            raise _wrap(exc, "get_object") from None
        digest = hashlib.sha256()
        size = 0
        body = fetched["Body"]
        try:
            for chunk in iter(lambda: body.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
                if sink is not None:
                    written = sink.write(chunk)
                    if written != len(chunk):
                        raise ObjectStoreError("download sink rejected object bytes")
        finally:
            body.close()
        sha256 = digest.hexdigest()
        declared = fetched.get("Metadata", {}).get("archive-sha256")
        declared_size = int(fetched["ContentLength"])
        if size != declared_size or (declared is not None and sha256 != declared):
            raise ObjectIntegrityError("stored object failed checksum verification")
        return ObjectAttributes(
            size=size,
            sha256=sha256,
            version_id=fetched.get("VersionId"),
        )

    def _existing_put_result(
        self,
        archive_id: str,
        object_key: str,
        *,
        size: int,
        sha256: str,
    ) -> ObjectAttributes:
        try:
            head = self._client.head_object(Bucket=self._bucket, Key=object_key)
        except ClientError as exc:
            raise _wrap(exc, "head_object") from None
        try:
            attributes = self._read_object(archive_id, object_key, None)
        except ObjectIntegrityError:
            raise ObjectImmutabilityError("object key already holds unverified content") from None
        declared = head.get("Metadata", {}).get("archive-sha256")
        if attributes.size == size and attributes.sha256 == sha256 and declared == sha256:
            return attributes
        raise ObjectImmutabilityError("object key already holds different content")

    def put_object(
        self,
        archive_id: str,
        object_key: str,
        data: bytes,
        *,
        sha256: str,
    ) -> ObjectAttributes:
        require_archive_key(archive_id, object_key)
        require_sha256(sha256)
        if hashlib.sha256(data).hexdigest() != sha256:
            raise ObjectIntegrityError("object does not match the declared checksum")
        if self._object_exists(object_key):
            return self._existing_put_result(
                archive_id,
                object_key,
                size=len(data),
                sha256=sha256,
            )
        for attempt in range(3):
            try:
                self._client.put_object(
                    Bucket=self._bucket,
                    Key=object_key,
                    Body=data,
                    Metadata={"archive-sha256": sha256},
                    IfNoneMatch="*",
                )
            except ClientError as exc:
                code = _error_code(exc)
                if code in ("PreconditionFailed", "412"):
                    return self._existing_put_result(
                        archive_id,
                        object_key,
                        size=len(data),
                        sha256=sha256,
                    )
                if code in ("ConditionalRequestConflict", "409") and attempt < 2:
                    continue
                raise _wrap(exc, "put_object") from None
            break
        try:
            attributes = self._read_object(archive_id, object_key, None)
        except ObjectIntegrityError:
            try:
                self._client.delete_object(Bucket=self._bucket, Key=object_key)
            except ClientError:
                pass
            raise
        if attributes.size != len(data) or attributes.sha256 != sha256:
            try:
                self._client.delete_object(Bucket=self._bucket, Key=object_key)
            except ClientError:
                pass
            raise ObjectIntegrityError("stored object does not match the declared checksum")
        return attributes

    def copy_object(
        self,
        source_scope: str,
        source_key: str,
        target_scope: str,
        target_key: str,
        *,
        sha256: str,
        size: int,
    ) -> ObjectAttributes:
        """Server-side ``CopyObject`` inside the one bucket (PSU-2.184).

        No read-back: re-hashing would pull every byte through this process,
        which is the transfer this method exists to avoid. What is checked is
        what the store can say from a HEAD — the length, and the
        ``archive-sha256`` metadata where the source carries it (the default
        ``MetadataDirective=COPY`` carries it over). The source itself was
        verified when its manifest went ``remote_ready``.
        """
        require_archive_key(source_scope, source_key)
        require_archive_key(target_scope, target_key)
        require_sha256(sha256)
        try:
            head = self._client.head_object(Bucket=self._bucket, Key=source_key)
        except ClientError as exc:
            if _error_code(exc) in ("404", "NoSuchKey", "NotFound"):
                raise ObjectNotFoundError("object not found") from None
            raise _wrap(exc, "head_object") from None
        declared = head.get("Metadata", {}).get("archive-sha256")
        if int(head["ContentLength"]) != size or (declared is not None and declared != sha256):
            raise ObjectIntegrityError("source object is not the declared content")
        if self._object_exists(target_key):
            raise ObjectImmutabilityError("object key already holds content")
        try:
            self._client.copy_object(
                Bucket=self._bucket,
                Key=target_key,
                CopySource={"Bucket": self._bucket, "Key": source_key},
            )
            copied = self._client.head_object(Bucket=self._bucket, Key=target_key)
        except ClientError as exc:
            raise _wrap(exc, "copy_object") from None
        if int(copied["ContentLength"]) != size:
            try:
                self._client.delete_object(Bucket=self._bucket, Key=target_key)
            except ClientError:
                pass
            raise ObjectIntegrityError("copied object does not match the declared length")
        return ObjectAttributes(size=size, sha256=sha256, version_id=copied.get("VersionId"))

    def plan_upload(
        self,
        archive_id: str,
        *,
        total_size: int,
        sha256: str,
        part_size: int,
        object_key: str | None = None,
    ) -> UploadPlan:
        if total_size <= 0 or part_size <= 0:
            raise ObjectStoreValidationError("total_size and part_size must be positive")
        require_sha256(sha256)
        if object_key is None:
            object_key = new_object_key(archive_id)
        require_archive_key(archive_id, object_key)
        if self._object_exists(object_key):
            raise ObjectImmutabilityError("object key already holds a verified object")
        try:
            created = self._client.create_multipart_upload(
                Bucket=self._bucket,
                Key=object_key,
                Metadata={"archive-sha256": sha256},
            )
        except ClientError as exc:
            raise _wrap(exc, "create_multipart_upload") from None
        return UploadPlan(
            archive_id=archive_id,
            object_key=object_key,
            upload_id=created["UploadId"],
            total_size=total_size,
            part_size=part_size,
            sha256=sha256,
        )

    def list_uploaded_parts(self, plan: UploadPlan) -> tuple[CompletedPart, ...]:
        completed: list[CompletedPart] = []
        marker: int | None = None
        while True:
            try:
                if marker is None:
                    listed = self._client.list_parts(
                        Bucket=self._bucket,
                        Key=plan.object_key,
                        UploadId=plan.upload_id,
                    )
                else:
                    listed = self._client.list_parts(
                        Bucket=self._bucket,
                        Key=plan.object_key,
                        UploadId=plan.upload_id,
                        PartNumberMarker=marker,
                    )
            except ClientError as exc:
                if _error_code(exc) == "NoSuchUpload":
                    raise ObjectMultipartUploadMissingError("multipart provider session is missing") from None
                raise _wrap(exc, "list_parts") from None
            listed_parts: list[PartTypeDef] = listed["Parts"] if "Parts" in listed else []
            for part in listed_parts:
                part_number = part.get("PartNumber")
                entity_tag = part.get("ETag")
                if part_number is None or entity_tag is None:
                    continue
                completed.append(
                    CompletedPart(
                        part_number=part_number,
                        entity_tag=entity_tag.strip('"'),
                        sha256=None,
                    )
                )
            if not listed.get("IsTruncated", False):
                break
            next_marker = listed.get("NextPartNumberMarker", 0)
            previous_marker = 0 if marker is None else marker
            if next_marker <= previous_marker:
                raise ObjectStoreError("list_parts returned invalid pagination")
            marker = next_marker
        return tuple(completed)

    def upload_part(self, plan: UploadPlan, part_number: int, data: bytes) -> CompletedPart:
        expected = {part.part_number: part.size for part in plan.parts()}
        if part_number not in expected:
            raise ObjectStoreValidationError("part number is outside the upload plan")
        if len(data) != expected[part_number]:
            raise ObjectStoreValidationError("part size does not match the upload plan")
        url = self._client.generate_presigned_url(
            "upload_part",
            Params={
                "Bucket": self._bucket,
                "Key": plan.object_key,
                "UploadId": plan.upload_id,
                "PartNumber": part_number,
                "ContentLength": len(data),
            },
            ExpiresIn=self._presign_expiry_seconds,
        )
        request = urllib.request.Request(
            url,
            data=data,
            method="PUT",
            headers={"Content-Type": "application/octet-stream"},
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                entity_tag = str(response.headers.get("ETag", "")).strip('"')
        except urllib.error.HTTPError as exc:
            raise ObjectStoreError(
                f"presigned part upload failed with HTTP {exc.code}"
            ) from None
        except urllib.error.URLError:
            raise ObjectStoreError("presigned part upload failed to connect") from None
        if not entity_tag:
            raise ObjectStoreError("presigned part upload returned no entity tag")
        return CompletedPart(
            part_number=part_number,
            entity_tag=entity_tag,
            sha256=hashlib.sha256(data).hexdigest(),
        )

    def complete_upload(
        self, plan: UploadPlan, parts: Sequence[CompletedPart]
    ) -> ObjectAttributes:
        planned_numbers = [part.part_number for part in plan.parts()]
        provided = {part.part_number: part for part in parts}
        if sorted(provided) != planned_numbers:
            raise ObjectStoreValidationError("completion must list every planned part exactly once")
        if self._object_exists(plan.object_key):
            raise ObjectImmutabilityError("object key already holds a verified object")
        try:
            self._client.complete_multipart_upload(
                Bucket=self._bucket,
                Key=plan.object_key,
                UploadId=plan.upload_id,
                MultipartUpload={
                    "Parts": [
                        {
                            "ETag": provided[number].entity_tag,
                            "PartNumber": number,
                        }
                        for number in planned_numbers
                    ]
                },
            )
        except ClientError as exc:
            raise _wrap(exc, "complete_multipart_upload") from None
        try:
            attributes = self._read_object(plan.archive_id, plan.object_key, None)
        except ObjectIntegrityError:
            try:
                self._client.delete_object(Bucket=self._bucket, Key=plan.object_key)
            except ClientError:
                pass
            raise
        if attributes.size != plan.total_size or attributes.sha256 != plan.sha256:
            # Fail closed: the unverified object must never become readable.
            try:
                self._client.delete_object(Bucket=self._bucket, Key=plan.object_key)
            except ClientError:
                pass
            raise ObjectIntegrityError("completed object does not match the declared checksum")
        return attributes

    def abort_upload(self, plan: UploadPlan) -> None:
        try:
            self._client.abort_multipart_upload(
                Bucket=self._bucket, Key=plan.object_key, UploadId=plan.upload_id
            )
        except ClientError:
            pass

    def download_object(
        self,
        archive_id: str,
        object_key: str,
        sink: BinaryIO,
    ) -> ObjectAttributes:
        return self._read_object(archive_id, object_key, sink)

    def get_object(self, archive_id: str, object_key: str) -> bytes:
        sink = io.BytesIO()
        self.download_object(archive_id, object_key, sink)
        return sink.getvalue()

    def presign_get(
        self, archive_id: str, object_key: str, *, expires_in_seconds: int
    ) -> PresignedRequest:
        require_archive_key(archive_id, object_key)
        if expires_in_seconds <= 0 or expires_in_seconds > 3600:
            raise ObjectStoreValidationError("presigned GET expiry must be short-lived")
        url = self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self._bucket, "Key": object_key},
            ExpiresIn=expires_in_seconds,
        )
        return PresignedRequest(
            url=url, method="GET", headers={}, expires_in_seconds=expires_in_seconds
        )

    def cached_capabilities(self) -> StoreCapabilities:
        """The probe result, taken at most once every few minutes.

        ``probe_capabilities`` stays uncached because its callers — the
        configuration test, the health *report* — are asking about right now
        and would be misled by an answer from an hour ago. Route selection is
        the opposite: it asks the same question once per object, and the probe
        writes and deletes real objects to answer.

        Cached with an expiry rather than for the process lifetime, because the
        probe reports gaps instead of raising: a store that was briefly
        unreachable answers "no capabilities at all", and keeping that would
        leave direct upload switched off until somebody restarted the service.
        """
        with self._capability_lock:
            now = time.monotonic()
            if self._capabilities is None or now >= self._capabilities_expire_at:
                self._capabilities = self.probe_capabilities()
                self._capabilities_expire_at = now + CAPABILITY_CACHE_SECONDS
            return self._capabilities

    def presign_put(
        self,
        archive_id: str,
        object_key: str,
        *,
        size: int,
        sha256: str,
        expires_in_seconds: int,
        md5: str | None = None,
    ) -> PresignedRequest:
        """Sign one write, with the declared bytes nailed into the signature."""
        require_archive_key(archive_id, object_key)
        require_sha256(sha256)
        if md5 is not None:
            require_md5(md5)
        if isinstance(size, bool) or size < 0:
            raise ObjectStoreValidationError("presigned PUT size must be non-negative")
        if expires_in_seconds <= 0 or expires_in_seconds > MAX_PRESIGNED_PUT_SECONDS:
            raise ObjectStoreValidationError("presigned PUT expiry must be short-lived")
        capabilities = self.cached_capabilities()
        if not capabilities.presigned_urls:
            raise ObjectStoreCapabilityError("this backend cannot sign upload URLs")
        params: dict[str, object] = {
            "Bucket": self._bucket,
            "Key": object_key,
            "ContentLength": size,
            "Metadata": {"archive-sha256": sha256},
        }
        # Every header the signature may bind, with the value the client must
        # send. Which ones actually end up bound is read back off the URL below
        # rather than assumed — see `_signed_headers`.
        candidates = {
            "content-length": str(size),
            "x-amz-meta-archive-sha256": sha256,
        }
        if capabilities.checksum_sha256:
            # The only layer with teeth against a client that uploads bytes
            # and a self-consistent lie about them: the store hashes what
            # arrives and refuses the write itself.
            encoded = base64.b64encode(bytes.fromhex(sha256)).decode("ascii")
            params["ChecksumSHA256"] = encoded
            candidates["x-amz-checksum-sha256"] = encoded
            candidates["x-amz-sdk-checksum-algorithm"] = "SHA256"
        if md5 is not None and capabilities.content_md5:
            # PSU-2.131: the layer with teeth on a store that ignores the
            # SHA-256 header. The store refuses bytes whose MD5 differs, and the
            # ETag it keeps is what `batch-confirm` checks instead of a read-back.
            # Only signed where the probe proved both halves of that are true.
            encoded_md5 = base64.b64encode(bytes.fromhex(md5)).decode("ascii")
            params["ContentMD5"] = encoded_md5
            candidates["content-md5"] = encoded_md5
        try:
            url = self._client.generate_presigned_url(
                "put_object",
                Params=params,
                ExpiresIn=expires_in_seconds,
            )
        except (ClientError, BotoCoreError):
            raise ObjectStoreError("presign_put failed to sign an upload URL") from None
        return PresignedRequest(
            url=url,
            method="PUT",
            headers=_signed_headers(url, candidates),
            expires_in_seconds=expires_in_seconds,
        )

    def head_objects(
        self, archive_id: str, object_keys: Sequence[str]
    ) -> Mapping[str, ObjectHead]:
        """Length and bare ETag per key, from a HEAD each; absent keys are skipped.

        What the ETag proves is the caller's call (PSU-2.131): only on a store
        with ``content_md5`` and only for a single-part object is it the MD5 of
        the bytes.
        """
        unique = tuple(dict.fromkeys(object_keys))
        # Every key is checked before the first request goes out, so a foreign
        # key refuses the whole batch instead of racing its neighbours.
        for object_key in unique:
            require_archive_key(archive_id, object_key)
        if len(unique) <= 1:
            answers = [self._head_one(object_key) for object_key in unique]
        else:
            # PSU-2.144: concurrently. boto3 clients are thread-safe; the pool
            # is sized for this in `client_config`. `map` keeps key order and
            # re-raises the first failure, as the old loop did.
            with ThreadPoolExecutor(
                max_workers=min(HEAD_CONCURRENCY, len(unique)),
                thread_name_prefix="s3-head",
            ) as pool:
                answers = list(pool.map(self._head_one, unique))
        return {
            object_key: head
            for object_key, head in zip(unique, answers, strict=True)
            if head is not None
        }

    def _head_one(self, object_key: str) -> ObjectHead | None:
        try:
            head = self._client.head_object(Bucket=self._bucket, Key=object_key)
        except ClientError as exc:
            if _error_code(exc) in ("404", "NoSuchKey", "NotFound"):
                return None
            raise _wrap(exc, "head_object") from None
        return ObjectHead(
            size=int(head["ContentLength"]),
            etag=str(head.get("ETag", "")).strip('"'),
            version_id=head.get("VersionId"),
        )

    def _platform_attributes(self, object_key: str) -> ObjectAttributes | None:
        """What the provider itself computed, or ``None`` if it will not say.

        Raises :class:`ObjectNotFoundError` for a key with no object, so the
        caller can tell "gone" from "here but unattested" without a second
        round trip.
        """
        try:
            head = self._client.head_object(
                Bucket=self._bucket, Key=object_key, ChecksumMode="ENABLED"
            )
        except ClientError as exc:
            if _error_code(exc) in ("404", "NoSuchKey", "NotFound"):
                raise ObjectNotFoundError("object not found") from None
            raise _wrap(exc, "head_object") from None
        encoded = head.get("ChecksumSHA256", "")
        if not encoded:
            return None
        try:
            digest = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            # "<base64>-<part count>" is S3's spelling of a *composite*
            # checksum: a hash of the part hashes, not of the object. It
            # answers a different question, so it does not answer this one.
            return None
        if len(digest) != 32:
            return None
        return ObjectAttributes(
            size=int(head["ContentLength"]),
            sha256=digest.hex(),
            version_id=head.get("VersionId"),
        )

    def describe_objects(
        self, archive_id: str, object_keys: Sequence[str]
    ) -> Mapping[str, ObjectAttributes]:
        for object_key in object_keys:
            require_archive_key(archive_id, object_key)
        platform_checksums = self.cached_capabilities().checksum_sha256
        described: dict[str, ObjectAttributes] = {}
        for object_key in dict.fromkeys(object_keys):
            try:
                attributes = (
                    self._platform_attributes(object_key) if platform_checksums else None
                )
                if attributes is None:
                    # Falls back to a whole-object GET that hashes as it reads.
                    # Costs the server-to-store leg, which is a LAN on the
                    # deployments this runs on — and that leg is exactly where
                    # this unit is trying to move work to.
                    attributes = self._read_object(archive_id, object_key, None)
            except ObjectNotFoundError:
                continue
            except ObjectIntegrityError:
                # Present but unvouchable. Omitted rather than raised: the
                # caller reports this one blob as unconfirmed and the other
                # 499 in the batch still land.
                continue
            described[object_key] = attributes
        return described

    def list_object_checksums(self, archive_id: str) -> Mapping[str, str]:
        require_archive_key(archive_id, f"a/{archive_id}/{'0' * 32}")
        checksums: dict[str, str] = {}
        paginator = self._client.get_paginator("list_objects_v2")
        try:
            for page in paginator.paginate(
                Bucket=self._bucket, Prefix=f"a/{archive_id}/"
            ):
                entries: list[ObjectTypeDef] = page["Contents"] if "Contents" in page else []
                for entry in entries:
                    object_key = entry.get("Key")
                    if object_key is None:
                        continue
                    head = self._client.head_object(Bucket=self._bucket, Key=object_key)
                    declared = head["Metadata"].get("archive-sha256")
                    if declared is not None:
                        checksums[object_key] = declared
        except ClientError as exc:
            raise _wrap(exc, "list_objects") from None
        return checksums

    def list_object_sizes(self, archive_id: str) -> Mapping[str, int]:
        """LIST only — deliberately no HEAD per object (PSU-2.58)."""
        require_archive_key(archive_id, f"a/{archive_id}/{'0' * 32}")
        sizes: dict[str, int] = {}
        paginator = self._client.get_paginator("list_objects_v2")
        try:
            for page in paginator.paginate(
                Bucket=self._bucket, Prefix=f"a/{archive_id}/"
            ):
                entries: list[ObjectTypeDef] = page["Contents"] if "Contents" in page else []
                for entry in entries:
                    object_key = entry.get("Key")
                    size = entry.get("Size")
                    if object_key is not None and size is not None:
                        sizes[object_key] = int(size)
        except ClientError as exc:
            raise _wrap(exc, "list_objects") from None
        return sizes

    def reconcile(
        self, archive_id: str, expected: Mapping[str, str]
    ) -> ObjectReconciliation:
        for object_key in expected:
            require_archive_key(archive_id, object_key)
        return reconcile_objects(
            expected=expected, stored=self.list_object_checksums(archive_id)
        )

    def delete_object(self, archive_id: str, object_key: str) -> None:
        require_archive_key(archive_id, object_key)
        try:
            self._client.delete_object(Bucket=self._bucket, Key=object_key)
        except ClientError as exc:
            raise _wrap(exc, "delete_object") from None

    def probe_capabilities(self) -> StoreCapabilities:
        """Probe each capability without raising; gaps are reported, not hidden."""
        endpoint = str(self._client.meta.endpoint_url)
        probe_key = f"capability-probe/{uuid.uuid4().hex}"
        return StoreCapabilities(
            tls=endpoint.startswith("https://"),
            multipart=self._probe_multipart(probe_key),
            checksum_sha256=self._probe_platform_checksum(probe_key),
            versioning=self._probe_versioning(),
            presigned_urls=self._probe_presigned(probe_key),
            conditional_writes=self._probe_conditional(probe_key),
            lifecycle_incomplete_multipart=self._probe_lifecycle(),
            content_md5=self._probe_content_md5(probe_key),
        )

    def _probe_multipart(self, probe_key: str) -> bool:
        try:
            created = self._client.create_multipart_upload(
                Bucket=self._bucket, Key=probe_key
            )
            self._client.abort_multipart_upload(
                Bucket=self._bucket, Key=probe_key, UploadId=created["UploadId"]
            )
        except (ClientError, BotoCoreError):
            return False
        return True

    def _probe_platform_checksum(self, probe_key: str) -> bool:
        """The store must accept the right SHA-256 **and refuse a wrong one**.

        Accepting the right one proves nothing on its own: a store that never
        reads the header accepts it too. Tencent COS is such a store, and
        trusting the first half alone signed a useless header into every
        upload and sent every confirmation through a HEAD that learns nothing
        (PSU-2.131).
        """
        payload = b"capability-probe"
        right = base64.b64encode(hashlib.sha256(payload).digest()).decode("ascii")
        wrong = base64.b64encode(hashlib.sha256(payload + b"!").digest()).decode("ascii")
        try:
            self._client.put_object(
                Bucket=self._bucket, Key=probe_key, Body=payload, ChecksumSHA256=right
            )
        except (ClientError, BotoCoreError):
            return False
        try:
            try:
                self._client.put_object(
                    Bucket=self._bucket, Key=probe_key, Body=payload, ChecksumSHA256=wrong
                )
            except ClientError:
                return True
            except BotoCoreError:
                return False
            return False
        finally:
            try:
                self._client.delete_object(Bucket=self._bucket, Key=probe_key)
            except (ClientError, BotoCoreError):
                pass

    def _probe_content_md5(self, probe_key: str) -> bool:
        """A wrong Content-MD5 is refused, and the ETag of a right one is the MD5.

        Both halves, because ``batch-confirm`` leans on both: the refusal is
        what stops bad bytes landing, and the ETag is what it checks instead of
        reading the object back (PSU-2.131). Server-side encryption, for one,
        keeps the refusal and breaks the ETag.
        """
        payload = b"capability-probe-md5"
        digest = hashlib.md5(payload)
        wrong = base64.b64encode(hashlib.md5(payload + b"!").digest()).decode("ascii")
        try:
            try:
                self._client.put_object(
                    Bucket=self._bucket, Key=probe_key, Body=payload, ContentMD5=wrong
                )
            except ClientError:
                pass
            else:
                return False
            self._client.put_object(
                Bucket=self._bucket,
                Key=probe_key,
                Body=payload,
                ContentMD5=base64.b64encode(digest.digest()).decode("ascii"),
            )
            head = self._client.head_object(Bucket=self._bucket, Key=probe_key)
        except (ClientError, BotoCoreError):
            return False
        finally:
            try:
                self._client.delete_object(Bucket=self._bucket, Key=probe_key)
            except (ClientError, BotoCoreError):
                pass
        return str(head.get("ETag", "")).strip('"') == digest.hexdigest()

    def _probe_versioning(self) -> bool:
        try:
            status = self._client.get_bucket_versioning(Bucket=self._bucket)
        except (ClientError, BotoCoreError):
            return False
        return status.get("Status") == "Enabled"

    def _probe_presigned(self, probe_key: str) -> bool:
        try:
            self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": probe_key},
                ExpiresIn=60,
            )
        except (ClientError, BotoCoreError):
            return False
        return True

    def _probe_conditional(self, probe_key: str) -> bool:
        try:
            self._client.put_object(
                Bucket=self._bucket, Key=probe_key, Body=b"probe", IfNoneMatch="*"
            )
            try:
                self._client.put_object(
                    Bucket=self._bucket, Key=probe_key, Body=b"probe", IfNoneMatch="*"
                )
            except ClientError as exc:
                return _error_code(exc) in ("PreconditionFailed", "412")
            return False
        except (ClientError, BotoCoreError):
            return False
        finally:
            try:
                self._client.delete_object(Bucket=self._bucket, Key=probe_key)
            except (ClientError, BotoCoreError):
                pass

    def _probe_lifecycle(self) -> bool | None:
        """Whether incomplete multipart uploads get cleaned up — or ``None``.

        Three outcomes, and the third is the point (PSU-2.57). The provider can
        answer "there is no such rule" (``NoSuchLifecycleConfiguration``, or a
        configuration with no abort rule in it), which is a real gap. It can
        also **refuse to answer**: a least-privilege key without
        ``s3:GetLifecycleConfiguration`` gets ``AccessDenied``, and that says
        nothing whatsoever about whether the rule exists. Reporting the second
        case as ``False`` reported a permissions fact as a provider gap, and
        the deployment sat on `OBJECT_STORAGE_UNHEALTHY` for it.
        """
        try:
            lifecycle = self._client.get_bucket_lifecycle_configuration(Bucket=self._bucket)
        except ClientError as error:
            code = str(error.response.get("Error", {}).get("Code", ""))
            return False if code in _LIFECYCLE_ABSENT_CODES else None
        except BotoCoreError:
            return None
        rules = cast(list[dict[str, object]], lifecycle.get("Rules", []))
        return any("AbortIncompleteMultipartUpload" in rule for rule in rules)
