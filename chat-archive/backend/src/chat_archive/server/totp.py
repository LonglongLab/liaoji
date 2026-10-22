"""RFC 6238 TOTP 与恢复码，只用标准库 hmac/hashlib/base64/secrets。

不引入 `pyotp`：算法本身十几行，而多一个依赖就多一份要跟进的供应链。
参数固定 SHA-1 / 6 位 / 30 秒——不是因为 SHA-1 好，是因为 Google Authenticator
一类的验证器只认这一组；写 SHA-256 会得到一个扫得进去、永远对不上的二维码。

**验证窗口 ±1 步**（计划：±30 秒一个窗）。窗口本身要记账：同一个验证码在
30 秒内可以被重放第二次，除非把用过的那一步记下来。调用方负责持久化
`consumed step`，本模块只回答「这个码对应哪一步」。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from urllib.parse import quote

DIGITS = 6
PERIOD_SECONDS = 30
# ±1 步。0 会让时钟差一秒就登不上，±2 把窗口拉到 2.5 分钟，得不偿失。
WINDOW_STEPS = 1
_SECRET_BYTES = 20  # RFC 4226 建议的 160 位
RECOVERY_CODE_COUNT = 10
_RECOVERY_GROUPS = 3
_RECOVERY_GROUP_LENGTH = 4
# 去掉 0/O/1/I/L：恢复码是抄在纸上的，形近字符是唯一真实的失败模式。
_RECOVERY_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def generate_secret() -> str:
    """Base32（无填充）密钥，验证器直接吃这个字符串。"""
    return base64.b32encode(secrets.token_bytes(_SECRET_BYTES)).decode("ascii").rstrip("=")


def _decode_secret(secret: str) -> bytes:
    padded = secret + "=" * (-len(secret) % 8)
    return base64.b32decode(padded.encode("ascii"), casefold=True)


def otpauth_uri(secret: str, *, account: str, issuer: str) -> str:
    """`otpauth://` URI；二维码由前端画，服务端只给这一行文本。"""
    label = quote(f"{issuer}:{account}", safe="")
    query = "&".join(
        (
            f"secret={quote(secret, safe='')}",
            f"issuer={quote(issuer, safe='')}",
            "algorithm=SHA1",
            f"digits={DIGITS}",
            f"period={PERIOD_SECONDS}",
        )
    )
    return f"otpauth://totp/{label}?{query}"


def code_at(secret: str, step: int) -> str:
    """某一步的验证码。测试与验证共用同一条实现，不另写一份。"""
    counter = step.to_bytes(8, "big")
    digest = hmac.new(_decode_secret(secret), counter, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    truncated = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFF_FFFF
    return f"{truncated % (10**DIGITS):0{DIGITS}d}"


def current_step(now: int) -> int:
    return now // PERIOD_SECONDS


def matching_step(secret: str, code: str, now: int) -> int | None:
    """验证码落在哪一步；不在窗口内返回 None。

    逐步比较都走 `compare_digest`，避免按位提前返回把「差在第几位」漏出去。
    """
    candidate = code.strip()
    # `str.isdigit()` 对全角与阿拉伯-印度数字也返回 True，而 `compare_digest`
    # 见到非 ASCII 字符直接抛 TypeError——那会把一次手滑的输入变成 500。
    # 这里只认 ASCII 0-9。
    if len(candidate) != DIGITS or not all("0" <= ch <= "9" for ch in candidate):
        return None
    center = current_step(now)
    matched: int | None = None
    for step in range(center - WINDOW_STEPS, center + WINDOW_STEPS + 1):
        # 不 break：命中后继续走完剩下的步，比较次数与命中位置无关。
        if hmac.compare_digest(code_at(secret, step), candidate):
            matched = step
    return matched


def generate_recovery_codes() -> tuple[str, ...]:
    """一次性恢复码；只显示这一次，服务端只存慢哈希。"""
    return tuple(
        "-".join(
            "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(_RECOVERY_GROUP_LENGTH))
            for _ in range(_RECOVERY_GROUPS)
        )
        for _ in range(RECOVERY_CODE_COUNT)
    )


def normalize_recovery_code(code: str) -> str:
    """抄纸上的东西要宽容：大小写、空格、分隔符都不计较。"""
    return "".join(character for character in code.upper() if character.isalnum())


def looks_like_recovery_code(code: str) -> bool:
    return len(normalize_recovery_code(code)) == _RECOVERY_GROUPS * _RECOVERY_GROUP_LENGTH


__all__ = [
    "DIGITS",
    "PERIOD_SECONDS",
    "RECOVERY_CODE_COUNT",
    "WINDOW_STEPS",
    "code_at",
    "current_step",
    "generate_recovery_codes",
    "generate_secret",
    "looks_like_recovery_code",
    "matching_step",
    "normalize_recovery_code",
    "otpauth_uri",
]
