"""同源托管前端构建产物，并给这些响应一份**能跑得起来**的 CSP。

API 那份是 `default-src 'none'`——对一个只回 JSON 的端点，那是正确且最省的。
但同一份头下一个 SPA 一行脚本都加载不了，所以这里给静态响应换一份，
仍然按 `'self'` 收口。两份 CSP 各管各的路径，`server/http.py` 那个常量一个字
没动（它还被 Worker 侧的测试逐字对着）。

构建产物随镜像走、不落运行时卷（计划第 9 条）：容器根文件系统只读，
`dist` 目录是只读的，正合适。目录不存在时**什么都不注册**——开发机和后端
单测里没有 `dist`，那时服务器照常只提供 API，而不是每个请求 500。

PSU-2.67 把这个模块从 `server/` 提到这里，因为本地端打包之后也要同源托管同一份
产物。两端的错误信封不是同一份，所以「打错的 API 路径回什么」由调用方以
`not_found` 传进来——本地端不该为了复用一段静态托管去 import 远端模块。
"""

# pyright: reportUnusedFunction=false

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI, Response
from fastapi.responses import FileResponse

from chat_archive.domain.app_card import CHANNEL_IMAGE_HOSTS

# 一份保守 CSP，每一条放宽都有具体理由：
#   style-src 'unsafe-inline' —— Vue 的 `:style` 与虚拟滚动的定位都是内联 style
#     属性，CSP3 里那也归 style-src 管。样式里的内联比脚本里的内联风险小得多，
#     真正要挡的 script-src 一步没让。
#   img-src data: —— docx/md 消毒器只允许内嵌 `data:image`（外链图片会把「打开
#     预览」变成一次对外请求，早就被丢掉了）。
#   img-src 视频号图床 —— 视频号卡片的封面与作者头像是在线引用（不归档），
#     只放 CHANNEL_IMAGE_HOSTS 那几个、只放 https；解析时已把 http 改写成
#     https，名单与解析共用一份。代价是浏览这些卡片会向腾讯发一次图片请求
#     （不带 Referer）。消毒器仍然丢掉文档里的外链图片，这一条不影响它。
#   img-src/media-src blob: —— 附件字节是取回后 createObjectURL 播的。
#   connect-src blob: —— 多选导出要把屏上已经显示的图从 `blob:` 地址读回字节，
#     嵌进 HTML、画进长图。少了它 `fetch(blob:…)` 被拒：HTML 里的图全是空的、
#     长图直接失败（2026-09-27 安装版实测；开发服务器不发 CSP，测试一直是绿的）。
#     `blob:` 只指向本页自己造的对象，放行它不会让页面往任何地方发数据。
#   frame-ancestors 'none' + form-action 'none' —— 页面不该被嵌，也不提交表单。
#   script-src 'wasm-unsafe-eval' —— 浏览器没有内置 PDF 阅读器时（安卓上就是这样），
#     前端用 pdf.js 自己把每页画出来，它的 JBIG2/JPEG2000/ICC 解码器是 wasm。
#     没有这一条，`WebAssembly.instantiate` 会被挡下，pdf.js 退到纯 JS 解码器，
#     扫描件慢一个数量级。它**只放开 wasm 编译**：`'unsafe-eval'` 与内联脚本
#     一步没让，`new Function` 在这份 CSP 下仍然跑不起来。
_BASE_DIRECTIVES: tuple[tuple[str, str], ...] = (
    ("default-src", "'self'"),
    ("base-uri", "'none'"),
    ("object-src", "'none'"),
    ("frame-ancestors", "'none'"),
    ("form-action", "'none'"),
    ("script-src", "'self' 'wasm-unsafe-eval'"),
    ("style-src", "'self' 'unsafe-inline'"),
    ("img-src", " ".join(
        ("'self'", "data:", "blob:", *(f"https://{host}" for host in CHANNEL_IMAGE_HOSTS))
    )),
    ("media-src", "'self' blob:"),
    ("font-src", "'self'"),
    ("connect-src", "'self' blob:"),
    ("worker-src", "'self' blob:"),
)

# PSU-2.31：附件字节直接从对象存储取，不经过 API 进程。那是**另一个 origin**，
# 所以这三条必须放行它，否则预签名 URL 一上来就被 CSP 挡住，而浏览器控制台里
# 只会说 "Refused to load"——看起来像附件坏了，其实是这里没写。
#
# 只放行**当前配置的那一个** origin，而且是运行时读的：写死一个通配符等于把
# 「页面能往哪儿发数据」这件事整个放开。没配对象存储时一个都不加，CSP 与
# PSU-2.29 交付时逐字节相同。
_STORAGE_DIRECTIVES = frozenset({"img-src", "media-src", "connect-src"})

SPA_CONTENT_SECURITY_POLICY = "; ".join(
    f"{name} {value}" for name, value in _BASE_DIRECTIVES
)


def content_security_policy(storage_origin: str | None) -> str:
    """The SPA CSP, with the object storage origin folded in when there is one."""
    if not storage_origin:
        return SPA_CONTENT_SECURITY_POLICY
    return "; ".join(
        f"{name} {value} {storage_origin}" if name in _STORAGE_DIRECTIVES
        else f"{name} {value}"
        for name, value in _BASE_DIRECTIVES
    )

# 文件名带内容哈希的资源可以放心长缓存；index.html 绝不能，否则改版之后
# 浏览器会拿着旧壳去要已经不存在的 chunk。
_IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
_DOCUMENT_CACHE_CONTROL = "no-store"


def _apply(response: Response, cache_control: str, policy: str) -> Response:
    response.headers["Content-Security-Policy"] = policy
    response.headers["Cache-Control"] = cache_control
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def register_spa_routes(
    app: FastAPI,
    dist_dir: Path | None,
    *,
    not_found: Callable[[str], Response],
    storage_origin: Callable[[], str | None] = lambda: None,
) -> bool:
    """注册静态托管；真的注册了返回 True。

    **必须最后注册**：这里有一条兜底路由，早注册会把后面所有 API 路由挡在外面。

    `not_found` 收到的是带前导斜杠的完整路径，回一份**该端自己的**错误信封。
    两端的信封不同（远端还要按路径在两份契约间挑），所以这件事不在这里判。
    """
    if dist_dir is None:
        return False
    root = dist_dir.resolve()
    index = root / "index.html"
    if not index.is_file():
        return False

    def _resolved(relative: str) -> Path | None:
        try:
            candidate = (root / relative).resolve()
            # 目录穿越：`is_relative_to` 比字符串前缀可靠，符号链接也已经被
            # resolve 展平过了。
            if candidate == root or not candidate.is_relative_to(root):
                return None
            return candidate if candidate.is_file() else None
        except (OSError, ValueError):
            # 路径里的 NUL 会让 `resolve()` 抛 ValueError，过长的路径抛 OSError。
            # 两者都只是「没有这个文件」，不该变成 500。
            return None

    @app.get("/{spa_path:path}", include_in_schema=False)
    def spa(spa_path: str) -> Response:
        # 每次请求都读一遍：对象存储是在设置页里配的，配完不该要求重启才生效。
        policy = content_security_policy(storage_origin())
        if spa_path == "api" or spa_path.startswith("api/"):
            # API 的 404 必须仍然是 JSON。回 index.html 会让前端把一个打错的
            # 接口当成一份合法响应去解析，然后报一个与真因无关的错。
            #
            # 走调用方给的构造器而不是自己拼一个：装了 `dist` 与没装 `dist` 的
            # 同一台服务器必须对同一个打错的路径给同一份回答，否则「有没有前端
            # 产物」会变成一个能改错误契约的开关。
            return not_found(f"/{spa_path}")
        if spa_path.startswith("s/") and (
            spa_path.endswith(".md") or "/media/" in spa_path
        ):
            # Supported share readers register before this fallback. A frozen profile
            # must not masquerade as a Markdown document or media response with HTML.
            return not_found(f"/{spa_path}")
        target = _resolved(spa_path) if spa_path else None
        if target is not None:
            hashed = "/assets/" in f"/{spa_path}"
            return _apply(
                FileResponse(target),
                _IMMUTABLE_CACHE_CONTROL if hashed else _DOCUMENT_CACHE_CONTROL,
                policy,
            )
        # history 模式的深链（/remote/conversations、/login……）都由同一份壳接住。
        shell = _apply(FileResponse(index), _DOCUMENT_CACHE_CONTROL, policy)
        if spa_path == "s" or spa_path.startswith("s/"):
            # 分享页（PSU-2.180）：链接被贴到公开网页上也不该进搜索结果。
            shell.headers["X-Robots-Tag"] = "noindex, nofollow"
        return shell

    return True


__all__ = [
    "SPA_CONTENT_SECURITY_POLICY",
    "content_security_policy",
    "register_spa_routes",
]
