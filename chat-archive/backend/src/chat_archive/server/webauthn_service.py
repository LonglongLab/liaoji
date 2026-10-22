"""WebAuthn 依赖方（RP）配置与 py_webauthn 薄封装（仅自托管远程端，PSU-2.213）。

不自己实现 COSE / 签名校验，全部交给 Duo Labs `webauthn`（可选依赖
`chat-archive[webauthn]`）。这里只做三件事：

1. 固定 RP：RP ID 与 HTTPS Origin 允许清单来自显式配置，**绝不**从请求的 Host 或
   Forwarded 头推导。生产只接受 https；`localhost` 仅在测试 profile 显式放开。
2. 每一次生成选项与验签都显式要求用户验证（`userVerification: required` 与
   `require_user_verification=True`），不依赖库默认值；attestation 一律 `none`。
3. 任何解析或验签失败都折叠成同一个 `BrowserReauthRejected`，不把原始 assertion
   或库的异常文本带出去。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib.util import find_spec
from typing import Any, cast
from urllib.parse import urlsplit

from chat_archive.ports.browser_reauth import BrowserReauthRejected, StoredPasskey

_RP_NAME = "聊记"
_USER_NAME = "chat-archive"
_USER_DISPLAY_NAME = "聊记账户"
_MAX_CREDENTIAL_ID_BYTES = 1024
_KNOWN_TRANSPORTS = frozenset({"ble", "cable", "hybrid", "internal", "nfc", "smart-card", "usb"})


def _valid_rp_id(rp_id: str) -> bool:
    if not rp_id or len(rp_id) > 253 or rp_id != rp_id.strip().lower():
        return False
    if any(char in rp_id for char in "/:@?#[] "):
        return False
    return all(part and len(part) <= 63 for part in rp_id.split("."))


def _origin_matches_rp(origin: str, rp_id: str, *, allow_localhost: bool) -> bool:
    parsed = urlsplit(origin)
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username:
        return False
    host = (parsed.hostname or "").lower()
    if not host or not (host == rp_id or host.endswith(f".{rp_id}")):
        return False
    if parsed.scheme == "https":
        return True
    return allow_localhost and parsed.scheme == "http" and host == "localhost"


@dataclass(frozen=True)
class WebAuthnConfig:
    """一个稳定的 RP。`origins` 是完整 Origin（scheme://host[:port]），不带路径。"""

    rp_id: str
    origins: tuple[str, ...]
    rp_name: str = _RP_NAME

    @classmethod
    def parse(
        cls,
        rp_id: str | None,
        origins: str | None,
        *,
        allow_localhost: bool = False,
    ) -> WebAuthnConfig | None:
        """配置缺失或不合法一律返回 None（fail closed：再认证不可用）。"""
        if not rp_id or not origins:
            return None
        clean_rp = rp_id.strip().lower()
        if not _valid_rp_id(clean_rp):
            return None
        if clean_rp == "localhost" and not allow_localhost:
            return None
        items = tuple(
            item.strip().rstrip("/") for item in origins.split(",") if item.strip()
        )
        if not items:
            return None
        for item in items:
            if not _origin_matches_rp(item, clean_rp, allow_localhost=allow_localhost):
                return None
        return cls(rp_id=clean_rp, origins=items)


@dataclass(frozen=True)
class VerifiedNewPasskey:
    credential_id: bytes
    public_key: bytes
    sign_count: int
    backed_up: bool
    transports: tuple[str, ...]


@dataclass(frozen=True)
class VerifiedAssertion:
    passkey: StoredPasskey
    new_sign_count: int
    backed_up: bool


def webauthn_installed() -> bool:
    return find_spec("webauthn") is not None


def _b64url_decode(value: object) -> bytes:
    from webauthn.helpers import base64url_to_bytes

    if not isinstance(value, str) or not value or len(value) > 4096:
        raise BrowserReauthRejected()
    try:
        return base64url_to_bytes(value)
    except Exception:  # noqa: BLE001 - 任何解码失败都是同一种拒绝
        raise BrowserReauthRejected() from None


def _response_of(credential: dict[str, Any]) -> dict[str, object]:
    response: object = credential.get("response")
    return cast(dict[str, object], response) if isinstance(response, dict) else {}


def _transports_of(credential: dict[str, Any]) -> tuple[str, ...]:
    raw = _response_of(credential).get("transports")
    if not isinstance(raw, list):
        return ()
    items = cast(list[object], raw)[:8]
    return tuple(
        item for item in items if isinstance(item, str) and item in _KNOWN_TRANSPORTS
    )


def _descriptors(items: list[tuple[bytes, tuple[str, ...]]]) -> list[Any]:
    from webauthn.helpers.structs import AuthenticatorTransport, PublicKeyCredentialDescriptor

    known = {member.value: member for member in AuthenticatorTransport}
    return [
        PublicKeyCredentialDescriptor(
            id=credential_id,
            transports=[known[name] for name in transports if name in known] or None,
        )
        for credential_id, transports in items
    ]


class WebAuthnService:
    """绑定到一个 RP 的选项生成与验签。只有 `available()` 为真时才能调用其余方法。"""

    def __init__(self, config: WebAuthnConfig | None) -> None:
        self._config = config
        self._installed = webauthn_installed()

    @property
    def config(self) -> WebAuthnConfig | None:
        return self._config

    def available(self) -> bool:
        return self._config is not None and self._installed

    def _require(self) -> WebAuthnConfig:
        if self._config is None or not self._installed:
            raise RuntimeError("WebAuthn is not configured")
        return self._config

    def registration_options(
        self,
        *,
        user_handle: bytes,
        challenge: bytes,
        timeout_seconds: int,
        exclude: list[tuple[bytes, tuple[str, ...]]],
    ) -> dict[str, Any]:
        config = self._require()
        from webauthn import generate_registration_options, options_to_json
        from webauthn.helpers.structs import (
            AttestationConveyancePreference,
            AuthenticatorSelectionCriteria,
            ResidentKeyRequirement,
            UserVerificationRequirement,
        )

        options = generate_registration_options(
            rp_id=config.rp_id,
            rp_name=config.rp_name,
            user_id=user_handle,
            user_name=_USER_NAME,
            user_display_name=_USER_DISPLAY_NAME,
            challenge=challenge,
            timeout=timeout_seconds * 1000,
            attestation=AttestationConveyancePreference.NONE,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.PREFERRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=_descriptors(exclude),
        )
        return json.loads(options_to_json(options))

    def verify_registration(
        self, *, credential: dict[str, Any], challenge: bytes
    ) -> VerifiedNewPasskey:
        config = self._require()
        from webauthn import verify_registration_response

        try:
            verified = verify_registration_response(
                credential=credential,
                expected_challenge=challenge,
                expected_rp_id=config.rp_id,
                expected_origin=list(config.origins),
                require_user_verification=True,
            )
        except Exception:  # noqa: BLE001 - 库的异常文本可能含响应片段，不外泄
            raise BrowserReauthRejected() from None
        if not verified.user_verified or len(verified.credential_id) > _MAX_CREDENTIAL_ID_BYTES:
            raise BrowserReauthRejected()
        return VerifiedNewPasskey(
            credential_id=bytes(verified.credential_id),
            public_key=bytes(verified.credential_public_key),
            sign_count=int(verified.sign_count),
            backed_up=bool(verified.credential_backed_up),
            transports=_transports_of(credential),
        )

    def authentication_options(
        self,
        *,
        challenge: bytes,
        timeout_seconds: int,
        allow: list[tuple[bytes, tuple[str, ...]]],
    ) -> dict[str, Any]:
        config = self._require()
        from webauthn import generate_authentication_options, options_to_json
        from webauthn.helpers.structs import UserVerificationRequirement

        options = generate_authentication_options(
            rp_id=config.rp_id,
            challenge=challenge,
            timeout=timeout_seconds * 1000,
            allow_credentials=_descriptors(allow),
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        return json.loads(options_to_json(options))

    def verify_authentication(
        self,
        *,
        credential: dict[str, Any],
        challenge: bytes,
        passkeys: list[StoredPasskey],
        user_handle: bytes,
    ) -> VerifiedAssertion:
        config = self._require()
        from webauthn import verify_authentication_response

        raw_id = _b64url_decode(credential.get("rawId", credential.get("id")))
        passkey = next((item for item in passkeys if item.credential_id == raw_id), None)
        if passkey is None:
            raise BrowserReauthRejected()
        returned_handle = _response_of(credential).get("userHandle")
        if returned_handle not in (None, "") and _b64url_decode(returned_handle) != user_handle:
            raise BrowserReauthRejected()
        try:
            verified = verify_authentication_response(
                credential=credential,
                expected_challenge=challenge,
                expected_rp_id=config.rp_id,
                expected_origin=list(config.origins),
                credential_public_key=passkey.public_key,
                credential_current_sign_count=passkey.sign_count,
                require_user_verification=True,
            )
        except Exception:  # noqa: BLE001 - 同上
            raise BrowserReauthRejected() from None
        if not verified.user_verified:
            raise BrowserReauthRejected()
        return VerifiedAssertion(
            passkey=passkey,
            new_sign_count=int(verified.new_sign_count),
            backed_up=bool(verified.credential_backed_up),
        )


__all__ = [
    "VerifiedAssertion",
    "VerifiedNewPasskey",
    "WebAuthnConfig",
    "WebAuthnService",
    "webauthn_installed",
]
