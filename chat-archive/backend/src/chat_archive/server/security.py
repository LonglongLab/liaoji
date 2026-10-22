"""服务端安全原语；语义镜像 Worker `security.ts`。"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid

from chat_archive.ports.device_auth import BearerToken


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def generate_secret_hex() -> str:
    return secrets.token_bytes(32).hex()


def generate_opaque_id() -> str:
    return uuid.uuid4().hex


def parse_bearer_token(header: str | None) -> BearerToken | None:
    if header is None or not header.startswith("Bearer "):
        return None
    token = header[len("Bearer ") :]
    separator = token.find(".")
    if separator <= 0 or separator == len(token) - 1:
        return None
    return BearerToken(device_id=token[:separator], secret=token[separator + 1 :])
