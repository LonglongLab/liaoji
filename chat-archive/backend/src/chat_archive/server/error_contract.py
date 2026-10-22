"""两份错误契约共处一个进程时，由路径决定说哪一份。

远程服务这个 app 上有两类调用方，它们读的是两份不同的错误契约：

- **设备令牌那一套**（`/api/v1/health`、`/api/v1/pair`、`/api/v1/archives/**`）：
  三字段 `RemoteError`，语义镜像 Worker 侧的 `http.ts`（`server/http.py`）。
- **浏览器那一套**（`/api/v1/auth/**`、`/api/v1/remote/**`、`/api/v1/capabilities`）：
  六字段 `Error`（`contracts/openapi.yaml`），与本机 app 是**同一份**契约，
  因为吃它们的是同一份前端代码。

路由函数自己抛的异常早就分得清楚（各自的 handler 各自翻译）。分不清楚的是
**还没进到路由就失败的那一层**：参数校验不过、方法不对、路径不存在。那些由
框架统一抛出，从前一律按设备令牌那份回，于是同一次「limit=0」在本机 app 上
是六字段、在远程 app 上是三字段——「同一份前端零改动」这句话在传输层不成立。

这里把「这条路径说哪份契约」抽成一个判定，让两个 app、SPA 兜底路由共用它。

默认是**六字段**，设备令牌那几个前缀是例外。方向是刻意的：一个叫不上名字的
`/api/...` 路径，几乎只可能是浏览器（打错、或拿着旧壳去要已经改名的接口）打来的；
设备客户端只会打它自己编译进去的那几条路径，而那几条都在例外表里。
"""

from __future__ import annotations

from fastapi.responses import JSONResponse

from chat_archive.domain.errors import HTTP_STATUS, ArchiveError, ErrorCode
from chat_archive.server.http import RemoteServerError, remote_error_response

# 设备令牌那一套的路径前缀。`/api/v1/archives/**` 底下**全部**属于它——
# 设备、统计、同步、附件、配对码、对象存储配置都在这条前缀下。
_DEVICE_TOKEN_PREFIXES: tuple[str, ...] = (
    "/api/v1/health",
    "/api/v1/pair",
    "/api/v1/archives",
)


def speaks_local_error_contract(path: str) -> bool:
    """这条路径回的是六字段 `Error` 还是三字段 `RemoteError`。"""
    for prefix in _DEVICE_TOKEN_PREFIXES:
        if path == prefix or path.startswith(f"{prefix}/"):
            return False
    return True


def archive_error_response(error: ArchiveError) -> JSONResponse:
    """本地那份六字段错误契约；本机 app、BFF 与登录端点共用。"""
    return JSONResponse(
        status_code=HTTP_STATUS.get(error.code, 500),
        content=error.to_contract(),
    )


# 六字段那份的码与三字段那份的码在这三件事上**同名**，所以按路径换的只是
# 信封，不是名字：客户端无论读哪一份，看到的 code 都是同一个字符串。
_TRANSPORT_FAILURES: dict[ErrorCode, str] = {
    ErrorCode.QUERY_INVALID: "请求参数无效。",
    ErrorCode.NOT_FOUND: "资源不存在。",
    ErrorCode.METHOD_NOT_ALLOWED: "该端点不支持此方法。",
}


def transport_error_response(path: str, code: ErrorCode) -> JSONResponse:
    """路由之前就失败了——按路径挑信封，回那一份契约。"""
    if speaks_local_error_contract(path):
        return archive_error_response(ArchiveError(code))
    return remote_error_response(RemoteServerError(code.value, _TRANSPORT_FAILURES[code]))


__all__ = [
    "archive_error_response",
    "speaks_local_error_contract",
    "transport_error_response",
]
