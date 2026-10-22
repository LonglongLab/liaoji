"""浏览器登录口令的强度规则与慢哈希。

参数照抄 `portable_bundle.py` 已在用的那一组（`hashlib.scrypt`，N=2^15/r=8/p=1），
不另立一套：同一个进程里两套 KDF 参数迟早会有一套忘了跟进。

**验证耗时与候选口令内容无关**：scrypt 的代价只由 N/r/p 与 dklen 决定，与输入
字节无关，摘要比较走 `compare_digest`。这不等于「正确与错误的总时延不可区分」
——退避本身就是刻意的可区分时延，那条更强的性质写不出真测试，也不该假装有。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import unicodedata

# 与 server/portable_bundle.py 的 SCRYPT_N/R/P 同值同来源。
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_MAXMEM = 64 * 1024 * 1024
_SALT_BYTES = 32
_DIGEST_BYTES = 32

MINIMUM_LENGTH = 12
MAXIMUM_LENGTH = 1024

# 常见弱口令。这份表刻意短：它挡的是「用户随手写了个人人都会先试的串」，
# 不是一份真正的字典——把 rockyou 塞进仓库既进不了包，也解决不了这个问题。
# 归一化后按小写比对，所以表里只写小写。
_COMMON_PASSWORDS = frozenset(
    {
        "password",
        "passw0rd",
        "password1",
        "password123",
        "passwordpassword",
        "123456789012",
        "1234567890123",
        "12345678901234",
        "111111111111",
        "000000000000",
        "qwertyuiop12",
        "qwertyuiopas",
        "1qaz2wsx3edc",
        "adminadmin12",
        "administrator",
        "letmein12345",
        "iloveyou1234",
        "welcome12345",
        "chatarchive1",
        "chat-archive",
        "woaini1314520",
        "wangyiyi1234",
        "zhangsan1234",
        "aaaaaaaaaaaa",
        "abcdefghijkl",
        "abcd1234abcd",
    }
)


def normalize(password: str) -> str:
    """NFKC 归一，让「同一个口令换个输入法打出来」仍然是同一个口令。

    不做 strip：首尾空格是用户口令的一部分，替他删掉会让他下次登不上。
    """
    return unicodedata.normalize("NFKC", password)


def rejection_reason(password: str) -> str | None:
    """新口令不合规的原因；合规返回 None。

    只在用户**自己选口令**时调用，所以这里给具体理由不构成信息泄漏；
    登录失败永远只有一个稳定拒绝。
    """
    normalized = normalize(password)
    if len(normalized) < MINIMUM_LENGTH:
        return f"登录口令至少 {MINIMUM_LENGTH} 位。"
    if len(normalized) > MAXIMUM_LENGTH:
        return f"登录口令最长 {MAXIMUM_LENGTH} 位。"
    if any(character in normalized for character in ("\n", "\r", "\x00")):
        return "登录口令不能包含换行或空字符。"
    if normalized.lower() in _COMMON_PASSWORDS:
        return "这个口令太常见，换一个。"
    if len(set(normalized)) < 4:
        return "登录口令重复字符太多，换一个。"
    return None


def new_salt() -> bytes:
    return secrets.token_bytes(_SALT_BYTES)


def derive(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        normalize(password).encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        maxmem=_SCRYPT_MAXMEM,
        dklen=_DIGEST_BYTES,
    )


def verify(password: str, salt: bytes, digest: bytes) -> bool:
    return hmac.compare_digest(derive(password, salt), digest)


def scrypt_parameters() -> dict[str, int]:
    """落库的参数快照，好让将来提高代价时认得出旧行。"""
    return {"n": _SCRYPT_N, "r": _SCRYPT_R, "p": _SCRYPT_P, "dklen": _DIGEST_BYTES}


__all__ = [
    "MAXIMUM_LENGTH",
    "MINIMUM_LENGTH",
    "derive",
    "new_salt",
    "normalize",
    "rejection_reason",
    "scrypt_parameters",
    "verify",
]
