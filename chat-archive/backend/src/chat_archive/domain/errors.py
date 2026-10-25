"""Stable public error codes and a privacy-preserving domain exception."""

from __future__ import annotations

import re
from enum import Enum
from typing import TypedDict


class ErrorCode(str, Enum):
    # 传输层的两个：说的不是任何一件领域里的事，而是「这条路由/这个方法压根
    # 不存在」。名字与远程端设备令牌那份契约（`server/http.py`）逐字相同——
    # 同一件事在两份错误契约里必须是同一个码，否则前端要为「404」记两个名字。
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    SNAPSHOT_CLIENT_MUST_CLOSE = "SNAPSHOT_CLIENT_MUST_CLOSE"
    KEY_INVALID = "KEY_INVALID"
    KEY_ACQUISITION_CANCELLED = "KEY_ACQUISITION_CANCELLED"
    KEY_METHOD_UNAVAILABLE = "KEY_METHOD_UNAVAILABLE"
    KEY_ACQUISITION_FAILED = "KEY_ACQUISITION_FAILED"
    KEY_PROTECTION_UNAVAILABLE = "KEY_PROTECTION_UNAVAILABLE"
    SOURCE_NOT_FOUND = "SOURCE_NOT_FOUND"
    SOURCE_MACHINE_CONFLICT = "SOURCE_MACHINE_CONFLICT"
    DISK_SPACE_INSUFFICIENT = "DISK_SPACE_INSUFFICIENT"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    SOURCE_LOCKED = "SOURCE_LOCKED"
    # 同一个账号已经有一个同种作业占着线程（PSU-2.106）。不复用
    # SOURCE_LOCKED：那句话是「数据源当前不可读取」，说的不是这件事，而
    # 错误消息是用户唯一看得到的东西。
    JOB_ALREADY_RUNNING = "JOB_ALREADY_RUNNING"
    MEDIA_FAILED = "MEDIA_FAILED"
    # 与 MEDIA_FAILED 分开：缩略图是可选的第二份工件，「没有」是常态之一
    # （源里就没有 `_t.dat`），前端拿这个码静默回退原图，而不是报错。
    THUMBNAIL_UNAVAILABLE = "THUMBNAIL_UNAVAILABLE"
    UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"
    # 与 UNSUPPORTED_VERSION（微信版本）无关：这一条说的是**归档文件**由更新
    # 版本的本程序写过。它承诺了「档案完好」，所以只能在确认 revision 不认识时
    # 使用——sqlite 损坏必须走各自原样的异常，误挂在坏档案上比裸堆栈更糟。
    ARCHIVE_FROM_NEWER_VERSION = "ARCHIVE_FROM_NEWER_VERSION"
    CURSOR_INVALID = "CURSOR_INVALID"
    QUERY_INVALID = "QUERY_INVALID"
    EXPORT_FAILED = "EXPORT_FAILED"
    EXPORT_DELETE_BUSY = "EXPORT_DELETE_BUSY"
    EXPORT_DELETE_UNSAFE = "EXPORT_DELETE_UNSAFE"
    EXPORT_DELETE_FAILED = "EXPORT_DELETE_FAILED"
    REMOTE_NOT_CONFIGURED = "REMOTE_NOT_CONFIGURED"
    OBJECT_STORAGE_NOT_CONFIGURED = "OBJECT_STORAGE_NOT_CONFIGURED"
    OBJECT_STORAGE_CONFIGURATION_INVALID = "OBJECT_STORAGE_CONFIGURATION_INVALID"
    OBJECT_STORAGE_VERIFICATION_FAILED = "OBJECT_STORAGE_VERIFICATION_FAILED"
    OBJECT_STORAGE_GENERATION_MISMATCH = "OBJECT_STORAGE_GENERATION_MISMATCH"
    # 转写凭据（PSU-2.173）。分两条不是一条：「这把密钥不对」要人改密钥，
    # 「还没填过密钥」要人先去填——同一句话会让后者去反复检查一把不存在的钥匙。
    TRANSCRIPTION_CREDENTIALS_REJECTED = "TRANSCRIPTION_CREDENTIALS_REJECTED"
    TRANSCRIPTION_NOT_CONFIGURED = "TRANSCRIPTION_NOT_CONFIGURED"
    # 远端备份（PSU-2.141）。桌面端只是转述远端的稳定代号：没有这几条，
    # 每一种拒绝在面板上都会变成「远程档案返回了不兼容的数据」。
    BACKUP_NOT_CONFIGURED = "BACKUP_NOT_CONFIGURED"
    BACKUP_CONFIGURATION_INVALID = "BACKUP_CONFIGURATION_INVALID"
    BACKUP_RETENTION_NEEDS_DELETE = "BACKUP_RETENTION_NEEDS_DELETE"
    BACKUP_CREDENTIALS_UNREADABLE = "BACKUP_CREDENTIALS_UNREADABLE"
    BACKUP_NOT_AVAILABLE = "BACKUP_NOT_AVAILABLE"
    BACKUP_VERIFICATION_FAILED = "BACKUP_VERIFICATION_FAILED"
    BACKUP_UNAVAILABLE = "BACKUP_UNAVAILABLE"
    REMOTE_AUTH_REQUIRED = "REMOTE_AUTH_REQUIRED"
    REMOTE_PERMISSION_DENIED = "REMOTE_PERMISSION_DENIED"
    REMOTE_ARCHIVE_NOT_FOUND = "REMOTE_ARCHIVE_NOT_FOUND"
    SHARE_GONE = "SHARE_GONE"
    SHARE_NOT_FOUND = "SHARE_NOT_FOUND"
    SHARE_LIMIT_EXCEEDED = "SHARE_LIMIT_EXCEEDED"
    SHARE_INCOMPLETE = "SHARE_INCOMPLETE"
    SHARE_STATE_INVALID = "SHARE_STATE_INVALID"
    SHARE_UPLOAD_MISMATCH = "SHARE_UPLOAD_MISMATCH"
    SHARE_STORAGE_FAILED = "SHARE_STORAGE_FAILED"
    SHARE_DELETE_FAILED = "SHARE_DELETE_FAILED"
    SHARE_UNSUPPORTED = "SHARE_UNSUPPORTED"
    REMOTE_UNAVAILABLE = "REMOTE_UNAVAILABLE"
    REMOTE_PROTOCOL_ERROR = "REMOTE_PROTOCOL_ERROR"
    REMOTE_CREDENTIALS_UNAVAILABLE = "REMOTE_CREDENTIALS_UNAVAILABLE"
    REMOTE_SIGN_IN_REQUIRED = "REMOTE_SIGN_IN_REQUIRED"
    REMOTE_SIGN_IN_REJECTED = "REMOTE_SIGN_IN_REJECTED"
    REMOTE_SIGN_IN_THROTTLED = "REMOTE_SIGN_IN_THROTTLED"
    REMOTE_PASSWORD_WEAK = "REMOTE_PASSWORD_WEAK"
    REMOTE_SETUP_CLOSED = "REMOTE_SETUP_CLOSED"
    REMOTE_ORIGIN_REJECTED = "REMOTE_ORIGIN_REJECTED"
    # 可选日常再认证（PSU-2.213）。REQUIRED 是 423 而不是 401：原登录仍然有效，
    # 前端不能把它当成「已退出」走登录页，也不能当成口令错误。
    REMOTE_REAUTH_REQUIRED = "REMOTE_REAUTH_REQUIRED"
    REMOTE_REAUTH_REJECTED = "REMOTE_REAUTH_REJECTED"
    REMOTE_REAUTH_FRESH_REQUIRED = "REMOTE_REAUTH_FRESH_REQUIRED"
    REMOTE_REAUTH_CONFLICT = "REMOTE_REAUTH_CONFLICT"
    REMOTE_REAUTH_UNAVAILABLE = "REMOTE_REAUTH_UNAVAILABLE"
    REMOTE_RECORD_MODEL_TOO_OLD = "REMOTE_RECORD_MODEL_TOO_OLD"
    REMOTE_PROTOCOL_INCOMPATIBLE = "REMOTE_PROTOCOL_INCOMPATIBLE"
    REMOTE_STORAGE_INSUFFICIENT = "REMOTE_STORAGE_INSUFFICIENT"
    # 远端记的「这台设备这个账号传到了哪一条」与本机接不上，批次被拒收（PSU-2.164）。
    # 重放同一批永远得到同一个答复，所以不可重试；此前它被当成「远程不可用」。
    REMOTE_SYNC_CONFLICT = "REMOTE_SYNC_CONFLICT"
    # 某一段说「还有更多」，却一轮一件都没动（PSU-2.116）。不是远端说的，
    # 是本机自己判出来的——与 `REMOTE_STORAGE_INSUFFICIENT` 同一类。
    SYNC_STAGE_STALLED = "SYNC_STAGE_STALLED"
    SYNC_ATTACHMENTS_PENDING = "SYNC_ATTACHMENTS_PENDING"
    SYNC_ATTACHMENT_SOURCE_CHANGED = "SYNC_ATTACHMENT_SOURCE_CHANGED"
    # Bounded verification used its work budget; progress is durable, not stalled.
    SYNC_VERIFICATION_LIMIT = "SYNC_VERIFICATION_LIMIT"
    # 某一段在本机抛了没料到的异常（PSU-2.127）。同样只写作业行、不作为请求错误
    # 返回。在这之前这种停法被兜底盖成 `SOURCE_LOCKED`，让人去关微信。
    SYNC_STAGE_CRASHED = "SYNC_STAGE_CRASHED"
    # 配对的两种失败分开记：一种是码本身不成立（抄漏了、粘进了别的东西），
    # 一种是远端认得这个码但不接受它（用过了、过期了）。前者重抄，后者重发。
    PAIRING_CODE_INVALID = "PAIRING_CODE_INVALID"
    PAIRING_CODE_REJECTED = "PAIRING_CODE_REJECTED"
    SETTINGS_UNAVAILABLE = "SETTINGS_UNAVAILABLE"
    STORAGE_UNLOAD_BLOCKED = "STORAGE_UNLOAD_BLOCKED"
    UPDATE_NOT_CONFIGURED = "UPDATE_NOT_CONFIGURED"
    UPDATE_UNAVAILABLE = "UPDATE_UNAVAILABLE"
    UPDATE_MANIFEST_INVALID = "UPDATE_MANIFEST_INVALID"
    UPDATE_SIGNATURE_INVALID = "UPDATE_SIGNATURE_INVALID"
    UPDATE_PACKAGE_INVALID = "UPDATE_PACKAGE_INVALID"
    UPDATE_INSTALL_LOCATION_REQUIRED = "UPDATE_INSTALL_LOCATION_REQUIRED"
    UPDATE_INSTALL_PERMISSION_REQUIRED = "UPDATE_INSTALL_PERMISSION_REQUIRED"
    UPDATE_VERSION_INVALID = "UPDATE_VERSION_INVALID"
    UPDATE_ROLLBACK_UNAVAILABLE = "UPDATE_ROLLBACK_UNAVAILABLE"
    UPDATE_STATE_INVALID = "UPDATE_STATE_INVALID"
    # 安装器跑完了但退出码不是 0（PSU-2.94）。与上面几条的分工：那些说的是
    # 「不该装」，这一条说的是「装了、没装成」——当前版本原样还在。
    UPDATE_INSTALL_FAILED = "UPDATE_INSTALL_FAILED"
    UPDATE_INSTALL_INCOMPLETE = "UPDATE_INSTALL_INCOMPLETE"
    # 「重启并安装」（PSU-2.173）的两种做不到：这个进程没有可关的窗口（CLI serve、
    # 退回浏览器），或者有作业在跑、用户在原生对话框里点了「不」。
    UPDATE_RESTART_UNAVAILABLE = "UPDATE_RESTART_UNAVAILABLE"
    UPDATE_RESTART_DECLINED = "UPDATE_RESTART_DECLINED"
    # 提交反馈那条出网代理的四个码。分开而不是复用 REMOTE_* ：那一族说的是
    # 「你自己的远程归档」，而反馈平台是与归档毫无关系的第三方服务，混在一个码里
    # 会让「反馈发不出去」看起来像「归档坏了」。
    #   REJECTED    —— 上游看懂了并且不收（隐私模式命中、项目标识不对）。改内容
    #                  才有用，重试没用。
    #   UNREACHABLE —— 网络不通、上游 5xx，或者把请求重定向到别处。重试可能有用。
    #   PROTOCOL    —— 上游答了 2xx，但回的东西读不出 id/owner_url。既不是拒收也
    #                  不是连不上，说成那两个里的任何一个都是撒谎。
    FEEDBACK_UPSTREAM_REJECTED = "FEEDBACK_UPSTREAM_REJECTED"
    FEEDBACK_UPSTREAM_UNREACHABLE = "FEEDBACK_UPSTREAM_UNREACHABLE"
    FEEDBACK_UPSTREAM_PROTOCOL_ERROR = "FEEDBACK_UPSTREAM_PROTOCOL_ERROR"
    # 附件或整体载荷超限。与 QUERY_INVALID 分开：字段本身合法，只是太大，而用户
    # 能做的事（删掉一张图）与「填错了」完全不同。
    FEEDBACK_PAYLOAD_TOO_LARGE = "FEEDBACK_PAYLOAD_TOO_LARGE"
    # 这台电脑上替用户操作微信界面做不到：不是 Windows，或桌面依赖（系统 OCR）没装（PSU-2.187）。
    WECHAT_UI_UNAVAILABLE = "WECHAT_UI_UNAVAILABLE"


# 错误码 → HTTP 状态。两端共用一张：本地 API 与远程端的浏览器 BFF 说的是同一份
# 错误契约（`contracts/openapi.yaml` 的 `Error`），状态码在两处各写一份迟早会分叉。
# 远程端**设备令牌那一套**用的是另一份更窄的契约（`server/http.py`），与本表无关。
HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.METHOD_NOT_ALLOWED: 405,
    ErrorCode.SNAPSHOT_CLIENT_MUST_CLOSE: 409,
    ErrorCode.KEY_INVALID: 401,
    ErrorCode.KEY_ACQUISITION_CANCELLED: 409,
    ErrorCode.KEY_ACQUISITION_FAILED: 503,
    ErrorCode.KEY_PROTECTION_UNAVAILABLE: 503,
    ErrorCode.SOURCE_NOT_FOUND: 404,
    ErrorCode.SOURCE_MACHINE_CONFLICT: 409,
    ErrorCode.DISK_SPACE_INSUFFICIENT: 503,
    ErrorCode.PERMISSION_DENIED: 403,
    ErrorCode.SOURCE_LOCKED: 503,
    # 409 而不是 503：这不是「暂时不行、待会自己会好」，是「你要的这件事
    # 已经在做了」。重试解决不了它，看进度才能。
    ErrorCode.JOB_ALREADY_RUNNING: 409,
    ErrorCode.MEDIA_FAILED: 502,
    ErrorCode.THUMBNAIL_UNAVAILABLE: 404,
    ErrorCode.UNSUPPORTED_VERSION: 501,
    ErrorCode.ARCHIVE_FROM_NEWER_VERSION: 409,
    ErrorCode.CURSOR_INVALID: 400,
    ErrorCode.QUERY_INVALID: 400,
    ErrorCode.EXPORT_FAILED: 500,
    ErrorCode.EXPORT_DELETE_BUSY: 409,
    ErrorCode.EXPORT_DELETE_UNSAFE: 409,
    ErrorCode.EXPORT_DELETE_FAILED: 503,
    ErrorCode.REMOTE_NOT_CONFIGURED: 409,
    ErrorCode.OBJECT_STORAGE_NOT_CONFIGURED: 409,
    ErrorCode.OBJECT_STORAGE_CONFIGURATION_INVALID: 400,
    # 400 而不是 401：被拒的是用户填进来的那把钥匙，不是这个应用的调用者身份。
    ErrorCode.TRANSCRIPTION_CREDENTIALS_REJECTED: 400,
    ErrorCode.TRANSCRIPTION_NOT_CONFIGURED: 409,
    ErrorCode.OBJECT_STORAGE_VERIFICATION_FAILED: 422,
    ErrorCode.OBJECT_STORAGE_GENERATION_MISMATCH: 409,
    ErrorCode.BACKUP_NOT_CONFIGURED: 409,
    ErrorCode.BACKUP_CONFIGURATION_INVALID: 400,
    ErrorCode.BACKUP_RETENTION_NEEDS_DELETE: 400,
    ErrorCode.BACKUP_CREDENTIALS_UNREADABLE: 409,
    ErrorCode.BACKUP_NOT_AVAILABLE: 409,
    ErrorCode.BACKUP_VERIFICATION_FAILED: 422,
    ErrorCode.BACKUP_UNAVAILABLE: 409,
    ErrorCode.REMOTE_AUTH_REQUIRED: 401,
    ErrorCode.PAIRING_CODE_INVALID: 400,
    ErrorCode.PAIRING_CODE_REJECTED: 409,
    ErrorCode.REMOTE_PERMISSION_DENIED: 403,
    ErrorCode.REMOTE_ARCHIVE_NOT_FOUND: 404,
    ErrorCode.SHARE_GONE: 404,
    ErrorCode.SHARE_NOT_FOUND: 404,
    ErrorCode.SHARE_LIMIT_EXCEEDED: 413,
    ErrorCode.SHARE_INCOMPLETE: 409,
    ErrorCode.SHARE_STATE_INVALID: 409,
    ErrorCode.SHARE_UPLOAD_MISMATCH: 400,
    ErrorCode.SHARE_STORAGE_FAILED: 503,
    ErrorCode.SHARE_DELETE_FAILED: 503,
    ErrorCode.SHARE_UNSUPPORTED: 409,
    ErrorCode.REMOTE_UNAVAILABLE: 503,
    ErrorCode.SYNC_ATTACHMENTS_PENDING: 409,
    ErrorCode.SYNC_ATTACHMENT_SOURCE_CHANGED: 409,
    ErrorCode.REMOTE_PROTOCOL_ERROR: 502,
    ErrorCode.REMOTE_CREDENTIALS_UNAVAILABLE: 503,
    ErrorCode.SETTINGS_UNAVAILABLE: 503,
    ErrorCode.STORAGE_UNLOAD_BLOCKED: 409,
    ErrorCode.UPDATE_NOT_CONFIGURED: 409,
    ErrorCode.UPDATE_UNAVAILABLE: 503,
    ErrorCode.UPDATE_MANIFEST_INVALID: 422,
    ErrorCode.UPDATE_SIGNATURE_INVALID: 422,
    ErrorCode.UPDATE_PACKAGE_INVALID: 422,
    ErrorCode.UPDATE_INSTALL_LOCATION_REQUIRED: 409,
    ErrorCode.UPDATE_INSTALL_PERMISSION_REQUIRED: 409,
    ErrorCode.UPDATE_VERSION_INVALID: 409,
    ErrorCode.UPDATE_ROLLBACK_UNAVAILABLE: 409,
    ErrorCode.UPDATE_STATE_INVALID: 409,
    ErrorCode.UPDATE_INSTALL_FAILED: 500,
    ErrorCode.UPDATE_INSTALL_INCOMPLETE: 500,
    ErrorCode.UPDATE_RESTART_UNAVAILABLE: 409,
    ErrorCode.UPDATE_RESTART_DECLINED: 409,
    ErrorCode.FEEDBACK_UPSTREAM_REJECTED: 400,
    ErrorCode.FEEDBACK_UPSTREAM_UNREACHABLE: 503,
    ErrorCode.FEEDBACK_UPSTREAM_PROTOCOL_ERROR: 502,
    ErrorCode.FEEDBACK_PAYLOAD_TOO_LARGE: 413,
    ErrorCode.WECHAT_UI_UNAVAILABLE: 409,
    ErrorCode.REMOTE_SIGN_IN_REQUIRED: 401,
    ErrorCode.REMOTE_SIGN_IN_REJECTED: 401,
    ErrorCode.REMOTE_SIGN_IN_THROTTLED: 429,
    ErrorCode.REMOTE_PASSWORD_WEAK: 400,
    ErrorCode.REMOTE_SETUP_CLOSED: 409,
    ErrorCode.REMOTE_ORIGIN_REJECTED: 403,
    ErrorCode.REMOTE_REAUTH_REQUIRED: 423,
    ErrorCode.REMOTE_REAUTH_REJECTED: 403,
    ErrorCode.REMOTE_REAUTH_FRESH_REQUIRED: 403,
    ErrorCode.REMOTE_REAUTH_CONFLICT: 409,
    ErrorCode.REMOTE_REAUTH_UNAVAILABLE: 503,
    ErrorCode.REMOTE_RECORD_MODEL_TOO_OLD: 409,
    ErrorCode.REMOTE_PROTOCOL_INCOMPATIBLE: 409,
    ErrorCode.REMOTE_STORAGE_INSUFFICIENT: 507,
    ErrorCode.REMOTE_SYNC_CONFLICT: 409,
}


class RecommendedAction(str, Enum):
    CLOSE_WECHAT = "close_wechat"
    RESELECT_SOURCE = "reselect_source"
    RETRY = "retry"
    CHECK_DISK_SPACE = "check_disk_space"


_PUBLIC_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.NOT_FOUND: "请求的接口不存在",
    ErrorCode.METHOD_NOT_ALLOWED: "该接口不支持此请求方法",
    ErrorCode.SNAPSHOT_CLIENT_MUST_CLOSE: "请关闭微信客户端后重试",
    ErrorCode.KEY_INVALID: "密钥无效，请重新获取",
    ErrorCode.KEY_ACQUISITION_CANCELLED: "密钥获取已取消",
    ErrorCode.KEY_METHOD_UNAVAILABLE: "该获取方式当前不可用，请确认微信正在运行后重新检测",
    ErrorCode.KEY_ACQUISITION_FAILED: "取钥未完成，请确认微信已恢复运行并检查系统权限后重试",
    ErrorCode.KEY_PROTECTION_UNAVAILABLE: "系统凭据保护暂不可用，请先解锁或允许访问系统钥匙串后重试",
    ErrorCode.SOURCE_NOT_FOUND: "未找到微信数据源",
    ErrorCode.SOURCE_MACHINE_CONFLICT: (
        "这个微信号已由另一台机器导入过本归档，继续导入会产生重复数据；"
        "若确实要在本机采集，请走第二采集端接入流程（PSU-2.3b），不要共用同一个本地归档文件"
    ),
    ErrorCode.DISK_SPACE_INSUFFICIENT: "可用磁盘空间不足",
    ErrorCode.PERMISSION_DENIED: "当前操作没有足够权限",
    ErrorCode.SOURCE_LOCKED: "数据源当前不可读取",
    ErrorCode.JOB_ALREADY_RUNNING: "已经有一个同类任务在进行中，等它结束或先停掉它再来",
    ErrorCode.MEDIA_FAILED: "媒体文件暂时无法处理",
    ErrorCode.THUMBNAIL_UNAVAILABLE: "该附件没有缩略图",
    ErrorCode.UNSUPPORTED_VERSION: "当前微信版本暂不受支持",
    # 后半句必须有：这个场景里用户的第一反应是「档案坏了」，而档案没坏。
    ErrorCode.ARCHIVE_FROM_NEWER_VERSION: (
        "这份档案由更新版本的程序写入，请升级本程序后再打开。档案本身完好，未被修改。"
    ),
    ErrorCode.CURSOR_INVALID: "分页位置无效或已过期",
    ErrorCode.QUERY_INVALID: "查询参数无效",
    ErrorCode.EXPORT_FAILED: "导出暂时无法完成",
    ErrorCode.EXPORT_DELETE_BUSY: "导出任务尚未结束，请先取消并等待结束后再删除",
    ErrorCode.EXPORT_DELETE_UNSAFE: "无法确认文件属于这份导出，已停止删除以保护原始数据",
    ErrorCode.EXPORT_DELETE_FAILED: "导出清理尚未完成，请检查文件占用或存储权限后重试",
    ErrorCode.REMOTE_NOT_CONFIGURED: "尚未配置远程档案",
    ErrorCode.OBJECT_STORAGE_NOT_CONFIGURED: "对象存储尚未配置",
    ErrorCode.OBJECT_STORAGE_CONFIGURATION_INVALID: "对象存储配置无效",
    ErrorCode.OBJECT_STORAGE_VERIFICATION_FAILED: "对象存储往返验证失败",
    ErrorCode.OBJECT_STORAGE_GENERATION_MISMATCH: "附件属于旧对象存储代号",
    ErrorCode.TRANSCRIPTION_CREDENTIALS_REJECTED: "腾讯云拒绝了这把密钥",
    ErrorCode.TRANSCRIPTION_NOT_CONFIGURED: "还没有填腾讯云密钥，转写开不了",
    ErrorCode.BACKUP_NOT_CONFIGURED: "远程备份尚未配置",
    ErrorCode.BACKUP_NOT_AVAILABLE: "还没有一份可下载的完整备份；等第一轮跑完再试",
    ErrorCode.BACKUP_CONFIGURATION_INVALID: "备份配置无效：端点、桶名、前缀或保留策略不合规",
    ErrorCode.BACKUP_RETENTION_NEEDS_DELETE: (
        "这把密钥没有删除权限，无法用「应用自行清理」的保留方式；"
        "请改用生命周期规则，或换一把有删除权限的密钥"
    ),
    ErrorCode.BACKUP_CREDENTIALS_UNREADABLE: (
        "远端解不开存着的备份凭据（通常是恢复后主密钥不在了）；请重新填写一次"
    ),
    ErrorCode.BACKUP_VERIFICATION_FAILED: "备份目标往返验证失败：端点、桶名、前缀或权限不正确",
    ErrorCode.BACKUP_UNAVAILABLE: "这套远端部署没有运行备份线程",
    ErrorCode.REMOTE_AUTH_REQUIRED: "远程设备授权已失效，请重新配对",
    ErrorCode.PAIRING_CODE_INVALID: "配对码不完整或格式不对，请重新复制一遍",
    ErrorCode.PAIRING_CODE_REJECTED: (
        "配对码已失效——它只能用一次，且有效期约 10 分钟。请在已接入的那台机器上重新生成"
    ),
    ErrorCode.REMOTE_PERMISSION_DENIED: "当前设备无权访问该远程档案",
    ErrorCode.REMOTE_ARCHIVE_NOT_FOUND: "未找到可访问的远程档案",
    # 过期、已删、不存在、草稿四种说同一句（PSU-2.180）：不透露是哪一种。
    ErrorCode.SHARE_GONE: "这个分享已失效：链接已过期，或者分享的人已经把它删了",
    ErrorCode.SHARE_NOT_FOUND: "没有找到这个分享，可能已经删掉了",
    ErrorCode.SHARE_LIMIT_EXCEEDED: "分享的内容太多：最多 500 条消息、单个附件 200 MB、整份 1 GB",
    ErrorCode.SHARE_INCOMPLETE: "还有附件没传上，分享没法生成",
    ErrorCode.SHARE_STATE_INVALID: "这个分享当前的状态不允许这么做",
    ErrorCode.SHARE_UPLOAD_MISMATCH: "传上来的附件和声明的大小对不上",
    ErrorCode.SHARE_STORAGE_FAILED: "对象存储暂时不可用，请稍后重试",
    ErrorCode.SHARE_DELETE_FAILED: "服务器上的副本没删干净，请稍后再删一次",
    ErrorCode.SHARE_UNSUPPORTED: "这台远程归档不支持在线分享（只有自建 SQLite 部署支持）",
    ErrorCode.REMOTE_UNAVAILABLE: "远程档案暂时不可用",
    ErrorCode.SYNC_ATTACHMENTS_PENDING: "部分附件尚未传完，已完成部分会保留",
    ErrorCode.SYNC_ATTACHMENT_SOURCE_CHANGED: "附件当前字节与旧上传计划不同，需要找回匹配的原件",
    ErrorCode.REMOTE_PROTOCOL_ERROR: "远程档案返回了不兼容的数据",
    ErrorCode.REMOTE_CREDENTIALS_UNAVAILABLE: "远程档案凭据暂时无法读取",
    ErrorCode.REMOTE_SIGN_IN_REQUIRED: "请先登录远程归档",
    # 口令错、验证码错、引导令牌错共用一条：区分开就等于告诉猜的人猜对了哪一半。
    ErrorCode.REMOTE_SIGN_IN_REJECTED: "登录信息不正确",
    ErrorCode.REMOTE_SIGN_IN_THROTTLED: "登录尝试过于频繁，请稍后再试",
    ErrorCode.REMOTE_PASSWORD_WEAK: "登录口令不符合要求",
    ErrorCode.REMOTE_SETUP_CLOSED: "本部署已完成初始化，首次向导已关闭",
    ErrorCode.REMOTE_ORIGIN_REJECTED: "请求来源不被信任",
    ErrorCode.REMOTE_REAUTH_REQUIRED: "已锁定，请先完成系统验证解锁",
    ErrorCode.REMOTE_REAUTH_REJECTED: "系统验证未通过或已过期，请重新验证",
    ErrorCode.REMOTE_REAUTH_FRESH_REQUIRED: "修改安全设置前需要重新完成一次系统验证",
    ErrorCode.REMOTE_REAUTH_CONFLICT: "再认证设置已变化或前提不满足，请刷新后重试",
    ErrorCode.REMOTE_REAUTH_UNAVAILABLE: "系统验证暂时不可用，私人内容保持锁定",
    ErrorCode.REMOTE_RECORD_MODEL_TOO_OLD: "远程档案的记录模型版本过旧，无法接收本机已构建的批次",
    ErrorCode.REMOTE_PROTOCOL_INCOMPATIBLE: "本机与远程档案的协议不兼容，请更新到兼容版本",
    ErrorCode.REMOTE_STORAGE_INSUFFICIENT: "远程磁盘空间不足以接收本次附件上传",
    ErrorCode.REMOTE_SYNC_CONFLICT: "远程记录的上传位置与本机对不上，这一批被拒收；重试不会好转",
    ErrorCode.SETTINGS_UNAVAILABLE: "本地设置暂时无法读取或保存",
    ErrorCode.STORAGE_UNLOAD_BLOCKED: "远程提交、校验、对账或恢复验证尚未完成，不能卸载本地媒体缓存",
    ErrorCode.UPDATE_NOT_CONFIGURED: "当前安装未配置可信更新源",
    ErrorCode.UPDATE_UNAVAILABLE: "暂时无法连接可信更新源",
    ErrorCode.UPDATE_MANIFEST_INVALID: "更新清单格式或来源无效",
    ErrorCode.UPDATE_SIGNATURE_INVALID: "更新签名验证失败，已阻止安装",
    ErrorCode.UPDATE_PACKAGE_INVALID: "更新包完整性或结构验证失败，已阻止安装",
    ErrorCode.UPDATE_INSTALL_LOCATION_REQUIRED: "请先将聊记拖到应用程序，从那里重新打开后再下载更新",
    ErrorCode.UPDATE_INSTALL_PERMISSION_REQUIRED: "当前安装位置不可写，请使用可写的应用程序目录安装聊记后再更新",
    ErrorCode.UPDATE_VERSION_INVALID: "所选更新版本不可用或已过期",
    ErrorCode.UPDATE_ROLLBACK_UNAVAILABLE: "没有可用的上一版本可回滚",
    ErrorCode.UPDATE_STATE_INVALID: "本地更新状态异常，已停止更新",
    ErrorCode.UPDATE_INSTALL_FAILED: "上次更新安装失败，当前版本未改变",
    ErrorCode.UPDATE_INSTALL_INCOMPLETE: "上次更新没有装完整，有文件没有替换上",
    ErrorCode.UPDATE_RESTART_UNAVAILABLE: "当前运行方式无法自动重启，请手动退出聊记，新版本会在退出时安装",
    ErrorCode.UPDATE_RESTART_DECLINED: "已取消重启：有导入或同步正在进行",
    # 拒收那条通常会被更具体的一句盖掉（命中了哪几个隐私模式），这是兜底。
    ErrorCode.FEEDBACK_UPSTREAM_REJECTED: "反馈平台没有接收这条反馈，请修改后重试",
    ErrorCode.FEEDBACK_UPSTREAM_UNREACHABLE: "暂时无法连接反馈平台，请稍后重试",
    ErrorCode.FEEDBACK_UPSTREAM_PROTOCOL_ERROR: "反馈平台返回了无法识别的响应",
    ErrorCode.FEEDBACK_PAYLOAD_TOO_LARGE: "反馈内容或附件过大，请删掉一些附件后重试",
    ErrorCode.WECHAT_UI_UNAVAILABLE: "这台电脑上没法替你操作微信，请在微信里手动打开清理页",
}
_JOB_REF = re.compile(r"^job_[A-Za-z0-9_-]{8,64}$")
_DETAILS_REF = re.compile(r"^diag_[A-Za-z0-9_-]{8,64}$")


class ErrorBody(TypedDict):
    code: str
    message: str
    retryable: bool
    recommended_action: str | None
    job_id: str | None
    details_ref: str | None


class ErrorContract(TypedDict):
    error: ErrorBody


class ArchiveError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        *,
        retryable: bool = False,
        recommended_action: RecommendedAction | None = None,
        job_id: str | None = None,
        details_ref: str | None = None,
        message: str | None = None,
    ) -> None:
        if job_id is not None and _JOB_REF.fullmatch(job_id) is None:
            raise ValueError("job_id 必须是不含路径或 URL 的不透明 ID")
        if details_ref is not None and _DETAILS_REF.fullmatch(details_ref) is None:
            raise ValueError("details_ref 必须是不含路径或 URL 的不透明 ID")
        super().__init__(code.value)
        self.code = code
        self.retryable = retryable
        self.recommended_action = recommended_action
        self.job_id = job_id
        self.details_ref = details_ref
        # 默认永远是码对应的固定文案。覆盖只给一种情况：用户**刚刚自己输入**的
        # 东西不合规（新口令太短之类），那时说清楚哪里不合规不构成信息泄漏。
        # 任何与「猜对了没有」相关的路径都不得使用它。
        self.message = _PUBLIC_MESSAGES[code] if message is None else message

    def to_contract(self) -> ErrorContract:
        """Map the exception to a public response without internal details."""
        return {
            "error": {
                "code": self.code.value,
                "message": self.message,
                "retryable": self.retryable,
                "recommended_action": (
                    self.recommended_action.value if self.recommended_action else None
                ),
                "job_id": self.job_id,
                "details_ref": self.details_ref,
            }
        }
