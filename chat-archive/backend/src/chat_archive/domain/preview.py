"""会话列表那一行显示的「最后一条消息」，一份口径。

住在 `domain/` 而不是 `query.py` 里，是因为**两端都要说这一句**：本机档案的
`ArchiveQueryService` 从 `messages` 表算它，远程归档的 SQLite 元数据适配器从
`archive_conversation_state` 物化的两列算它（PSU-2.44）。两边各写一份，第一次
改文案就会分叉——而这一行恰恰是用户在会话列表上唯一读得到的正文。

`query.py` 是本机档案的查询服务，import 它意味着让远程端的适配器依赖一条与自己
无关的演进线；`domain/` 两端本来就都 import（`app_card` / `errors` / `models`）。
"""

from __future__ import annotations

# 预览最长这么长；超出就截断加省略号。远端把正文截断存进状态表时，存的长度
# 必须**不短于**这个数，否则截断会改变这一句本该说出的内容。
PREVIEW_MAX_LENGTH = 160

_LABELS: dict[str, str] = {
    "text": "文本",
    "quote": "引用",
    "emoji": "表情",
    "image": "图片",
    "video": "视频",
    "audio": "语音",
    "file": "文件",
    "link": "链接",
    "app": "应用消息",
    "unknown": "未知消息",
}


def conversation_preview(
    message_type: str | None,
    content_text: str | None,
    attachment_file_name: str | None,
) -> str | None:
    """把最后一条消息说成列表上的一行。

    `message_type is None` = 这条会话没有可预览的消息，答 `None`——界面据此退回
    条数，而不是印一句空文本冒充预览。
    """
    if message_type is None:
        return None
    content = " ".join((content_text or "").split())
    file_name = " ".join((attachment_file_name or "").split())
    if message_type in {"text", "quote", "emoji"}:
        preview = content or f"[{_LABELS[message_type]}]"
    elif message_type == "file":
        preview = f"[文件] {file_name}" if file_name else "[文件]"
    elif message_type in {"link", "app"}:
        label = _LABELS[message_type]
        preview = f"[{label}] {content}" if content else f"[{label}]"
    else:
        label = _LABELS.get(message_type, message_type)
        preview = f"[{label}]"
    if len(preview) <= PREVIEW_MAX_LENGTH:
        return preview
    return f"{preview[: PREVIEW_MAX_LENGTH - 1]}…"


__all__ = ["PREVIEW_MAX_LENGTH", "conversation_preview"]
