"""Request-scoped orchestration for the browser's remote archive view."""

from __future__ import annotations

from chat_archive.client_identity import collect_sync_client_info

import base64
import binascii
import hashlib
import hmac
import json
import threading
from collections.abc import Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Protocol, cast

from chat_archive.domain.errors import ArchiveError, ErrorCode, RecommendedAction
from chat_archive.domain.identity import thumbnail_blob_id
from chat_archive.domain.reply_preview import ReplyPreviewRecord
from chat_archive.ports.remote_archive import (
    RemoteShare,
    RemoteShareTransport,
    RemoteAccountsPage,
    RemoteSearchPosition,
    RemoteSearchResultPage,
    RemoteArchiveError,
    RemoteArchiveTransport,
    RemoteConversationMember,
    RemoteConversationMemberPage,
    RemoteConversationPage,
    RemoteConversationPosition,
    RemoteConversationSummary,
    RemoteHealth,
    RemoteMessage,
    RemoteMessagePage,
    RemoteMessagePosition,
    RemoteMessageWindow,
    RemoteBackupConfig,
    RemoteBackupDownload,
    RemoteBackupStatus,
    RemoteBackupVerification,
    RemoteObjectStorageConfig,
    RemoteObjectStorageVerification,
    RemotePresignedAttachment,
)
from chat_archive.ports.remote_credentials import (
    DeviceToken,
    RemoteCredentialStore,
    RemoteDeviceProfile,
)
from chat_archive.remote_archive import HttpRemoteArchiveClient
from chat_archive.share_draft import ShareDraftError, parse_share_draft

_CONVERSATION_CURSOR_KIND = "remote_conversations_v1"
_MESSAGE_CURSOR_KIND = "remote_messages_v1"
# A distinct kind, not a variant of the one above. The two carry different
# positions on different sort lines, and a cursor minted while the remote
# answered in upload order must not be replayable as a reading-order position
# after the remote is upgraded — it would silently land on a different row.
_MESSAGE_POSITION_CURSOR_KIND = "remote_messages_v2"
# 浏览顺序的会话游标，同样是**另一种**游标而不是上面那个的变体：它指的是
# (最近活跃时间, 会话标识) 这条排序线上的一个点，拿到按标识分页的列表里
# 重放会落在另一行上，而且不会报错。
_CONVERSATION_POSITION_CURSOR_KIND = "remote_conversations_v2"
# 从最新往回读的消息游标。与上面那个是**同一条排序线的两个方向**，正因如此
# 才必须分开：共用 kind 时，一个方向发出的游标拿到另一个方向下会验签通过、
# 解出一个合法位置，然后静默给出互补的那一半记录——不报错，看起来只是
# 「翻页翻丢了」。远端原地升级 v3→v4 之后，浏览器手里那个旧游标正是这样。
_MESSAGE_LATEST_CURSOR_KIND = "remote_messages_v3"
_CURSOR_DOMAIN = b"chat-archive-remote-cursor-v1\0"
_MAX_PAGE_SIZE = 500
# 读侧账号维度（记录模型 v3，PSU-2.35a）的最低版本。上传模型的对应常量在
# `ports/sync.py`（仍是 2——v3 不改上传字节），两者是两条线，别合并。
_ACCOUNT_DIMENSION_MIN_VERSION = 3
# 浏览语义（记录模型 v4，PSU-2.39）的最低版本：会话列表按最近活跃倒序、
# 转录从最新往回。低于这一版的远端答不出这两种顺序，读侧退回既有行为。
_BROWSE_ORDER_MIN_VERSION = 4
# 会话身份（记录模型 v6，PSU-2.44）的最低版本：一条会话的名字问得到、列表行带
# 最后一条消息的预览。低于这一版的远端答不出这个端点——**不回退成「自己去列表
# 里找」**，那条路正是这个单元要消灭的行为。
_CONVERSATION_DETAIL_MIN_VERSION = 6
# 索引化的搜索（记录模型 v7，PSU-2.45）的最低版本：游标、总数、每会话计数、
# 索引状态与六个过滤。低于这一版的远端**会忽略**认不得的过滤参数并把没筛过的
# 结果答回来，所以这一条必须按版本先挡住，不能打过去看结果。
_SEARCH_INDEX_MIN_VERSION = 7
# 通讯录（记录模型 v8，PSU-2.47）的最低版本：头像引用、群成员名单、群公告、
# 我的群昵称、成员数。低于这一版的远端答不出成员端点，而那几个字段在它的
# 回答里根本没有键——**缺席一律读成 None**，也就是「没被告知」，界面据此
# 退回首字母头像并说明原因，而不是画一个空名单。
_ROSTER_MIN_VERSION = 8
# 锚点定位（记录模型 v9，PSU-2.48）的最低版本：由一条记录的标识取到它前后的
# 一段。低于这一版的远端答不出——**不回退成「打开会话再自己往上翻」**，那正是
# 读者点「定位到上下文」时说自己不要的；界面按这个错误码退回并说明原因。
_ANCHOR_WINDOW_MIN_VERSION = 9
# 会话归并的阅读身份（记录模型 v12，PSU-2.95）的最低版本：远端在每一个带会话
# 身份的读答复里说出「我按哪条会话作答」。**这是一条放宽的闸门，方向与上面那些
# 相反**——上面几条是"到不了就别问"，这条是"到不了就别信"：低于这一版的远端说
# 的这句话一律当作协议错，因为会话归并本身是 v11 才有的事，更早的远端根本没有
# 正当理由换主语。
# 天花板升到 v13（转写发布流，PSU-2.163）时这里必须跟着到 13：这条闸门是
# 「到不了就别信」，低于天花板就等于替一个说不出那句话的服务端把话认下来。
_MERGED_CONVERSATION_MIN_VERSION = 13
# 搜索结果那条排序线上的游标。与其它几种一样是**另一个** kind：它指的是
# (时间, 记录标识) 这条线上的一个点，拿到别的分页里重放会解出一个合法但错的位置。
_SEARCH_POSITION_CURSOR_KIND = "remote_search_v1"
# 扇出那条路的游标：载荷是**每条流各自的位置**，与上面那个单一位置不是一回事。
# 另起一个 kind 而不是复用——形状不同的载荷共用一个 kind，改错了也验得过签。
_SEARCH_FANOUT_CURSOR_KIND = "remote_search_fanout_v1"
#: 远端一个维度最多给几个值（PSU-2.74）。比本机的 20 小，因为这一端的多选是
#: 按值扇出成 N 次**跨境**请求换来的；这是如实的能力差异，不是偷工。
#: 两处 HTTP 路由（本机代理与自托管 BFF）都从这里取，不各留一份——两份数会分家。
MAX_REMOTE_FILTER_VALUES = 5


def unique_filter_values(values: Sequence[str] | None) -> tuple[str, ...]:
    """多值过滤在进门时去重并保序。同一个值发两遍问的是同一件事，不该多扇出一条流。

    与 `MAX_REMOTE_FILTER_VALUES` 同住在这里，理由也是同一条：两处 HTTP 路由
    （本机代理与自托管 BFF）进门都要做同一次归一化，不各留一份——两份逻辑会分家。
    """
    return () if values is None else tuple(dict.fromkeys(values))
# 群成员名单那条排序线上的游标：它指的是一个成员标识，作用范围绑到
# (会话, 账号)，所以别的会话的名单游标拿过来解不开。
_MEMBER_CURSOR_KIND = "remote_members_v1"
# The remote already reduced every provider failure to one of these; passing
# them through keeps the local API's message as specific as the remote's
# without ever forwarding the provider's own error text.
# 「远端那里没有这个 blob」的两种说法。签名路由为「还没完成远程校验」抛
# `NOT_FOUND`，而 HTTP 那一层（以及刻意模仿它的进程内 transport）把它压成
# 404，于是到这里成了 `REMOTE_HTTP_404`——同一个状态码也承载着「这个归档根本
# 不存在」。
#
# 在**缩略图这条端点上**把两者当成一回事是有意的：它问的就是「有没有这份工件」，
# 而归档整个不见时，会话列表与消息页早在读到任何一个图片气泡之前就已经报出
# `REMOTE_ARCHIVE_NOT_FOUND` 了——这里不是诊断那件事的地方，把它伪装成一个
# 需要人去排查的错误只会让「这张图没有缩略图」这条常态看起来像故障。
_BLOB_ABSENT_CODES = frozenset({"NOT_FOUND", "REMOTE_HTTP_404"})

_OBJECT_STORAGE_ERROR_CODES: dict[str, ErrorCode] = {
    "OBJECT_STORAGE_NOT_CONFIGURED": ErrorCode.OBJECT_STORAGE_NOT_CONFIGURED,
    "OBJECT_STORAGE_CONFIGURATION_INVALID": ErrorCode.OBJECT_STORAGE_CONFIGURATION_INVALID,
    "OBJECT_STORAGE_VERIFICATION_FAILED": ErrorCode.OBJECT_STORAGE_VERIFICATION_FAILED,
    "OBJECT_STORAGE_GENERATION_MISMATCH": ErrorCode.OBJECT_STORAGE_GENERATION_MISMATCH,
    # 远端备份的拒绝（PSU-2.141 评审补）。少了这几行，每一种拒绝到桌面端都会变成
    # `REMOTE_PROTOCOL_ERROR`——「往返验证失败」和「远端坏了」在面板上看不出区别。
    "BACKUP_NOT_CONFIGURED": ErrorCode.BACKUP_NOT_CONFIGURED,
    "BACKUP_CONFIGURATION_INVALID": ErrorCode.BACKUP_CONFIGURATION_INVALID,
    "BACKUP_RETENTION_NEEDS_DELETE": ErrorCode.BACKUP_RETENTION_NEEDS_DELETE,
    "BACKUP_CREDENTIALS_UNREADABLE": ErrorCode.BACKUP_CREDENTIALS_UNREADABLE,
    "BACKUP_NOT_AVAILABLE": ErrorCode.BACKUP_NOT_AVAILABLE,
    "BACKUP_VERIFICATION_FAILED": ErrorCode.BACKUP_VERIFICATION_FAILED,
    "BACKUP_UNAVAILABLE": ErrorCode.BACKUP_UNAVAILABLE,
}


_SHARE_ERROR_CODES: dict[str, ErrorCode] = {
    code.value: code
    for code in (
        ErrorCode.SHARE_NOT_FOUND,
        ErrorCode.SHARE_LIMIT_EXCEEDED,
        ErrorCode.SHARE_INCOMPLETE,
        ErrorCode.SHARE_STATE_INVALID,
        ErrorCode.SHARE_UPLOAD_MISMATCH,
        ErrorCode.SHARE_STORAGE_FAILED,
        ErrorCode.SHARE_DELETE_FAILED,
    )
}


def _browses_after(
    earlier: tuple[int, str],
    later: tuple[int, str],
) -> bool:
    """Is ``later`` strictly further down the browse order than ``earlier``?

    The order is ``last_created_at`` **descending**, ``conversation_id``
    ascending -- two columns running opposite ways, so this cannot be a plain
    tuple comparison and every caller would otherwise hand-roll the same
    three-way condition. Same rule as the local list's
    ``ORDER BY s.last_message_at DESC, c.id ASC``.
    """
    if later[0] != earlier[0]:
        return later[0] < earlier[0]
    return later[1] > earlier[1]


class RemoteClientFactory(Protocol):
    def __call__(
        self,
        profile: RemoteDeviceProfile,
        token: DeviceToken,
        /,
    ) -> RemoteArchiveTransport: ...


def _default_client_factory(
    profile: RemoteDeviceProfile,
    token: DeviceToken,
    /,
) -> RemoteArchiveTransport:
    return HttpRemoteArchiveClient(profile.base_url, device_token=token.value, client_info=collect_sync_client_info())


@dataclass(frozen=True)
class RemoteAppServices:
    """Dependencies for remote browsing; neither field may retain plaintext in repr."""

    credentials: RemoteCredentialStore = field(repr=False)
    client_factory: RemoteClientFactory = field(
        default=_default_client_factory,
        repr=False,
    )


@dataclass(frozen=True)
class RemoteStatusView:
    configured: bool
    archive_id: str | None


@dataclass(frozen=True)
class RemoteHealthView:
    archive_id: str
    backend_type: str
    device_id: str
    last_uploaded_local_sequence: int
    last_server_sequence: int
    remote_used_bytes: int
    remote_blob_count: int
    pending_attachment_count: int
    conflict_count: int
    last_success_at: int | None
    last_reconciled_at: int | None
    last_recovery_verified_at: int | None
    remote_service_status: str
    object_storage_status: str
    # 远端写锁与后台欠账（PSU-2.102）。`None` = 这个远端不报，不是零。
    account_merge_pending: int | None = None
    conversation_merge_pending: int | None = None
    conversation_merge_pending_rows: int | None = None
    search_rehome_pending_rows: int | None = None
    writer_lock_held_ms: int | None = None
    writer_lock_rejections_recent: int | None = None
    # 定时备份（PSU-2.141）。`None` = 这个远端答不出备份这件事，不是"没备过"。
    last_backup_at: int | None = None
    last_backup_bytes: int | None = None
    last_backup_status: str | None = None
    backup_retention_mode: str | None = None
    backup_overdue: bool | None = None
    backup_lifecycle_covered: bool | None = None


@dataclass(frozen=True)
class RemoteConversationView:
    conversation_id: str
    message_count: int
    last_created_at: int
    last_server_sequence: int
    # ``None`` = 会话流还没把名字送到这个远端。界面显示不透明标识并说明原因。
    display_name: str | None = None
    is_group: bool | None = None
    # ``None`` 兼指「老远端不说这个」与「v3 远端说了：合并行/未归属」。
    source_account_id: str | None = None
    # PSU-2.44：列表行上那一句「最后一条消息」。``None`` 兼指「远端到不了 v6」
    # 与「这条会话没有可预览的消息」——界面两种都退回条数。
    last_message_type: str | None = None
    last_message_preview: str | None = None
    # PSU-2.47：通讯录那一侧才知道的几样。``None`` 兼指「远端到不了 v8」与
    # 「这条会话没有这几样」——界面两种都退回首字母头像、不画抽屉。
    avatar_blob_id: str | None = None
    announcement: str | None = None
    my_group_alias: str | None = None
    # **没有名单时是 None，不是 0**：0 会声称「这个群是空的」，而没有群是空的。
    member_count: int | None = None


@dataclass(frozen=True)
class RemoteConversationMemberView:
    """一个群成员，如远端的通讯录所记（PSU-2.47）。"""

    member_uid: str
    display_name: str
    group_alias: str | None = None
    is_self: bool = False
    avatar_blob_id: str | None = None


@dataclass(frozen=True)
class RemoteConversationMembersPageView:
    """一页群成员。

    空的一页是一个**真实答案**——这条会话的名单还没发布过——与「这个远端根本
    没有名单这个概念」不是一回事。后者在 facade 里当场被拒（QUERY_INVALID），
    永远走不到这里，所以界面看到空名单时可以放心说「名单是空的」。
    """

    items: tuple[RemoteConversationMemberView, ...]
    next_cursor: str | None
    has_more: bool


@dataclass(frozen=True)
class RemoteAccountView:
    source_account_id: str
    record_count: int
    display_name: str | None = None
    # 头像的 blob 标识（记录模型 v10）。字节走的是与附件同一条预签名 URL，
    # 所以这一端「能不能取附件」就是「能不能取头像」——与会话行同一条规矩。
    avatar_blob_id: str | None = None
    # PSU-2.90：同一个微信的其它上传流 id。**两端最终序列化的都是这个 view**，
    # 所以少在这里加一列，前面几层加了也白加。
    merged_source_account_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class RemoteAccountsPageView:
    items: tuple[RemoteAccountView, ...]
    # 归属批次早于账号作用域化、流无法证明的行数。>0 时界面要说出来，
    # 而不是让这些行在所有账号的过滤视图里都消失得像不存在。
    unattributed_count: int


@dataclass(frozen=True)
class RemoteConversationPageView:
    items: tuple[RemoteConversationView, ...]
    next_cursor: str | None
    has_more: bool


@dataclass(frozen=True)
class RemoteTranscriptView:
    """机器转写在界面这一侧的形状。

    `status`/`failure_code` 由适配层补成 `ok`/null 而不是存在这里：远端只有
    转成功的那些，多存一个恒定值就是多一个可以和事实不一致的地方。
    """

    text: str = field(repr=False)
    engine: str
    low_confidence: bool


@dataclass(frozen=True)
class RemoteMessageView:
    record_key: str
    conversation_id: str
    created_at: int
    content_text: str = field(repr=False)
    server_sequence: int
    # PSU-2.30 的渲染模型。`None` 是「远端没被告知过」，不是「空」——
    # 界面据此如实呈现，而不是拿一个占位发送者顶上。
    sender_uid: str | None = None
    sender_display_name: str | None = None
    is_self: bool | None = None
    message_type: str | None = None
    app_card_json: str | None = field(default=None, repr=False)
    reply_to_key: str | None = None
    attachment_refs: tuple[str, ...] = ()
    source_account_id: str | None = None
    # PSU-2.47：读时接上的发送者头像，不是冻在这条记录里的副本——人会换头像。
    # **绝不**混进 `attachment_refs`：那是一列无类型的 blob 标识，塞进去会被
    # 当成这条消息的附件渲染出来。
    sender_avatar_blob_id: str | None = None
    # PSU-2.163：这条语音的机器转写。`None` 兼指「远端到不了 v13」「这条不是
    # 语音」与「本机还没发过来」——界面对三者都是同一个动作：不画那一行。
    transcript: RemoteTranscriptView | None = None
    reply_preview: ReplyPreviewRecord | None = None


@dataclass(frozen=True)
class RemoteMessagePageView:
    items: tuple[RemoteMessageView, ...] = field(repr=False)
    next_cursor: str | None
    has_more: bool


@dataclass(frozen=True)
class RemoteMessageWindowView:
    """锚点前后的一段，形状与本机 `MessageWindow` 逐字相同。

    没有游标：续读是拿窗口边上那一条当新锚点再问一次。这不是省事——窗口的两半
    本来就是两个方向各自的一页，给它第三种游标等于让同一条排序线上有三种走法。
    """

    items: tuple[RemoteMessageView, ...] = field(repr=False)
    anchor_message_id: str


@dataclass(frozen=True)
class RemoteSearchView:
    """一页搜索结果，连同这一次搜索的全貌（PSU-2.45 扩宽）。

    在 v7 之前这里只有 `items`：没有游标、没有总数、没有每会话计数，也没有
    任何过滤。那些不是装饰——没有总数的「100 条结果」读起来像「一共 100 条」，
    而它其实是「上限 100」。
    """

    items: tuple[RemoteMessageView, ...] = field(repr=False)
    # v7 之前的远端答不出下面这些。`next_cursor` 为 None 且 `has_more` 为 False
    # 是「就这些了」；`index_status` 的 `building` 是「还在建，现在这份可能不全」
    # ——两句话不能混，所以状态是一个字符串而不是一个布尔。
    next_cursor: str | None = None
    has_more: bool = False
    total_count: int = 0
    conversation_counts: dict[str, int] = field(default_factory=dict[str, int])
    index_status: str = "unavailable"


@dataclass(frozen=True)
class RemoteShareView:
    """「我的分享」一行，外加能直接复制的链接（PSU-2.181）。

    `url` 在本机上是远端对外地址 + `/s/<令牌>`；在远程网页上是 None，页面自己
    拿当前 origin 拼——那条路径上 facade 手里只有一个进程内的假地址。
    令牌本身就是钥匙，所以它和 `url` 一样不进 repr。
    """

    token: str = field(repr=False)
    url: str | None = field(repr=False)
    path: str = field(repr=False)
    title: str
    message_count: int
    attachment_count: int
    attachment_bytes: int
    created_at: int
    expires_at: int | None
    state: str
    views: int
    last_viewed_at: int | None
    purge_at: int | None
    # 见 `RemoteShare.awaiting_upload`：只有建草稿的应答里有。
    awaiting_upload: tuple[str, ...] | None = None


def share_view_payload(view: RemoteShareView) -> dict[str, object]:
    """`RemoteShareView` 的 JSON 形状：`awaiting_upload` 只在有的时候出现（契约里是可选键）。"""
    payload = asdict(view)
    awaiting = payload.pop("awaiting_upload")
    if awaiting is not None:
        payload["awaiting_upload"] = list(cast(tuple[str, ...], awaiting))
    return payload


@dataclass(frozen=True)
class RemoteAttachmentUrlView:
    """一个附件的直取 URL。两端答的是同一份形状（计划 §6 方案 3）。

    URL 本身不进 repr：它在有效期内**就是**那个附件，日志里出现一次就等于把
    附件泄出去一次。
    """

    url: str = field(repr=False)
    method: str
    expires_at: int
    storage_origin: str


@dataclass(frozen=True)
class RemoteThumbnailUrlView:
    """缩略图的直取 URL，外加原图的可得性（PSU-2.43）。

    与 `RemoteAttachmentUrlView` 并排而不是给它加一个可选字段：那一条今天有一份
    逐字段 `==` 的契约测试守着，而「多一个键」应当是**特意**加进来的，不是跟着
    某个被拓宽的 dataclass 混进去的。
    """

    url: str = field(repr=False)
    method: str
    expires_at: int
    storage_origin: str
    # True/False 是答案，None 是「这一问没问成」——三者在界面上各有各的渲染。
    original_available: bool | None


class RemoteArchiveFacade:
    """Resolve protected credentials anew for every operation and proxy only thin reads."""

    def __init__(self, services: RemoteAppServices) -> None:
        self._services = services
        # 记录模型协商的结果，按**对端身份**存，不按客户端实例存。
        #
        # `HttpRemoteArchiveClient` 自己也缓存这个值，但那份缓存在本机代理这条
        # 路径上永远是冷的：`_client()` 每个操作都新建一个客户端。于是桌面端
        # 每翻一页消息都是 `GET /health` + `GET …/messages` 两跳。缓存挪到这里
        # 才落到一个真的活得够久的对象上——本机 app 的 facade 是建 app 时建的。
        #
        # 键是 (base_url, archive_id, device_id)：换了对端就重新协商，令牌轮换
        # 不算换对端。代价是对端**原地升级** v1→v2 时本机要到重启才跟上，那反而
        # 比从前稳：从前它可能在两页之间翻脸，而两种排序的游标互不认，
        # 下一页会直接 `CURSOR_INVALID`。
        self._record_model_versions: dict[tuple[str, str, str], int] = {}
        self._health_lock = threading.Lock()
        self._health_flights: dict[tuple[str, str, str, bytes], Future[RemoteHealthView]] = {}
        self._health_closed = False
        self._health_epoch = 0


    def status(self) -> RemoteStatusView:
        loaded = self._load_credentials(required=False)
        if loaded is None:
            return RemoteStatusView(configured=False, archive_id=None)
        profile, _ = loaded
        return RemoteStatusView(configured=True, archive_id=profile.archive_id)

    def object_storage_config(self) -> RemoteObjectStorageConfig:
        profile, _token, client = self._client()
        try:
            return client.get_object_storage_config(profile.archive_id)
        except RemoteArchiveError as error:
            raise self._public_remote_error(error) from None

    def test_object_storage_config(
        self,
        payload: Mapping[str, str],
    ) -> RemoteObjectStorageVerification:
        profile, _token, client = self._client()
        try:
            return client.test_object_storage_config(profile.archive_id, payload)
        except RemoteArchiveError as error:
            raise self._public_remote_error(error) from None

    def save_object_storage_config(
        self,
        payload: Mapping[str, str],
    ) -> RemoteObjectStorageConfig:
        profile, _token, client = self._client()
        try:
            return client.put_object_storage_config(profile.archive_id, payload)
        except RemoteArchiveError as error:
            raise self._public_remote_error(error) from None

    # ------------------------------------------------------------------
    # 定时备份（PSU-2.141）。本机端是**配对设备**，所以配置、触发和取下载链接
    # 都从这里走；浏览器那一端只有一条只读的近况。

    # ------------------------------------------------------------------
    # 在线分享（PSU-2.181）。本机与远程网页走同一组方法：本机经设备令牌 HTTP，
    # 远程网页经进程内传输，两者背后都是远端的同一个 `ShareService`。

    def create_share(self, payload: Mapping[str, object]) -> RemoteShareView:
        # 先在本端判一次：超限当场说，不必把 2 MB 的快照送过去再被拒回来。
        try:
            draft = parse_share_draft(payload)
        except ShareDraftError as error:
            raise ArchiveError(ErrorCode(error.code)) from None
        profile, client = self._share_client()
        try:
            share = client.create_share(profile.archive_id, draft.to_payload())
        except RemoteArchiveError as error:
            raise self._share_error(error) from None
        return self._share_view(profile, share)

    def upload_share_object(self, token: str, attachment_id: str, content: bytes) -> None:
        profile, client = self._share_client()
        try:
            client.upload_share_object(profile.archive_id, token, attachment_id, content)
        except RemoteArchiveError as error:
            raise self._share_error(error) from None

    def finalize_share(self, token: str) -> RemoteShareView:
        profile, client = self._share_client()
        try:
            share = client.finalize_share(profile.archive_id, token)
        except RemoteArchiveError as error:
            raise self._share_error(error) from None
        return self._share_view(profile, share)

    def list_shares(self) -> list[RemoteShareView]:
        profile, client = self._share_client()
        try:
            shares = client.list_shares(profile.archive_id)
        except RemoteArchiveError as error:
            raise self._share_error(error) from None
        return [self._share_view(profile, share) for share in shares]

    def extend_share(self, token: str) -> RemoteShareView:
        profile, client = self._share_client()
        try:
            share = client.extend_share(profile.archive_id, token)
        except RemoteArchiveError as error:
            raise self._share_error(error) from None
        return self._share_view(profile, share)

    def delete_share(self, token: str) -> None:
        profile, client = self._share_client()
        try:
            client.delete_share(profile.archive_id, token)
        except RemoteArchiveError as error:
            raise self._share_error(error) from None

    def _share_client(self) -> tuple[RemoteDeviceProfile, RemoteShareTransport]:
        profile, _token, client = self._client()
        if not isinstance(client, RemoteShareTransport):
            raise ArchiveError(ErrorCode.SHARE_UNSUPPORTED)
        return profile, client

    @staticmethod
    def _share_view(profile: RemoteDeviceProfile, share: RemoteShare) -> RemoteShareView:
        base = profile.base_url.rstrip("/")
        url = f"{base}{share.path}" if base.startswith(("https://", "http://")) else None
        return RemoteShareView(
            token=share.token,
            url=url,
            path=share.path,
            title=share.title,
            message_count=share.message_count,
            attachment_count=share.attachment_count,
            attachment_bytes=share.attachment_bytes,
            created_at=share.created_at,
            expires_at=share.expires_at,
            state=share.state,
            views=share.views,
            last_viewed_at=share.last_viewed_at,
            purge_at=share.purge_at,
            awaiting_upload=share.awaiting_upload,
        )

    @classmethod
    def _share_error(cls, error: RemoteArchiveError) -> ArchiveError:
        if error.code in _SHARE_ERROR_CODES:
            retryable = error.code in {"SHARE_STORAGE_FAILED", "SHARE_DELETE_FAILED"}
            return ArchiveError(
                _SHARE_ERROR_CODES[error.code],
                retryable=retryable,
                recommended_action=RecommendedAction.RETRY if retryable else None,
            )
        if error.code in {"REMOTE_HTTP_404", "REMOTE_HTTP_405"}:
            # 这组路由只有自托管 SQLite 那一支注册；别的远端答 404。
            return ArchiveError(ErrorCode.SHARE_UNSUPPORTED)
        return cls._public_remote_error(error)

    def backup_config(self) -> RemoteBackupConfig:
        profile, _token, client = self._client()
        try:
            return client.get_backup_config(profile.archive_id)
        except RemoteArchiveError as error:
            raise self._public_remote_error(error) from None

    def test_backup_config(self, payload: Mapping[str, object]) -> RemoteBackupVerification:
        profile, _token, client = self._client()
        try:
            return client.test_backup_config(profile.archive_id, payload)
        except RemoteArchiveError as error:
            raise self._public_remote_error(error) from None

    def save_backup_config(self, payload: Mapping[str, object]) -> RemoteBackupConfig:
        profile, _token, client = self._client()
        try:
            return client.put_backup_config(profile.archive_id, payload)
        except RemoteArchiveError as error:
            raise self._public_remote_error(error) from None

    def backup_runs(self) -> RemoteBackupStatus:
        profile, _token, client = self._client()
        try:
            return client.list_backup_runs(profile.archive_id)
        except RemoteArchiveError as error:
            raise self._public_remote_error(error) from None

    def request_backup_run(self) -> RemoteBackupStatus:
        profile, _token, client = self._client()
        try:
            return client.request_backup_run(profile.archive_id)
        except RemoteArchiveError as error:
            raise self._public_remote_error(error) from None

    def latest_backup_download(self) -> RemoteBackupDownload:
        profile, _token, client = self._client()
        try:
            return client.latest_backup_download(profile.archive_id)
        except RemoteArchiveError as error:
            raise self._public_remote_error(error) from None

    def restore_object_storage_generation(
        self,
        storage_generation: int,
    ) -> RemoteObjectStorageConfig:
        profile, _token, client = self._client()
        try:
            return client.restore_object_storage_generation(
                profile.archive_id,
                storage_generation,
            )
        except RemoteArchiveError as error:
            raise self._public_remote_error(error) from None

    def health(self) -> RemoteHealthView:
        profile, _token, client = self._client()
        return self._read_health(profile, client)

    def shared_health(self) -> RemoteHealthView:
        """Coalesce concurrent status reads only; never cache success for destructive actions."""
        profile, token, client = self._client()
        key = (profile.base_url, profile.archive_id, profile.device_id, hashlib.sha256(token.value.encode()).digest())
        with self._health_lock:
            if self._health_closed:
                raise ArchiveError(ErrorCode.REMOTE_UNAVAILABLE)
            epoch = self._health_epoch
            future: Future[RemoteHealthView] | None = self._health_flights.get(key)
            owner = future is None
            if future is None:
                future = Future()
                self._health_flights[key] = future
        if owner:
            try:
                value = self._read_health(profile, client)
                loaded = self._load_credentials(required=False)
                current_key = None if loaded is None else (loaded[0].base_url, loaded[0].archive_id,
                    loaded[0].device_id, hashlib.sha256(loaded[1].value.encode()).digest())
                with self._health_lock:
                    if self._health_closed or epoch != self._health_epoch or key != current_key:
                        raise ArchiveError(ErrorCode.REMOTE_UNAVAILABLE)
                    future.set_result(value)
            except BaseException as error:
                future.set_exception(error)
            finally:
                with self._health_lock:
                    if self._health_flights.get(key) is future:
                        del self._health_flights[key]
        return future.result()

    def open_health(self) -> None:
        with self._health_lock:
            self._health_closed = False
            self._health_epoch += 1
            self._health_flights.clear()

    def close_health(self) -> None:
        with self._health_lock:
            self._health_closed = True
            self._health_epoch += 1
            self._health_flights.clear()

    def _read_health(self, profile: RemoteDeviceProfile, client: RemoteArchiveTransport) -> RemoteHealthView:
        try:
            health = client.sync_health(profile.archive_id)
        except RemoteArchiveError as error:
            raise self._public_remote_error(error) from None
        self._validate_health(health, profile=profile)
        return RemoteHealthView(
            archive_id=health.archive_id,
            backend_type=health.backend_type,
            device_id=health.device_id,
            last_uploaded_local_sequence=health.last_uploaded_local_sequence,
            last_server_sequence=health.last_server_sequence,
            remote_used_bytes=health.remote_used_bytes,
            remote_blob_count=health.remote_blob_count,
            pending_attachment_count=health.pending_attachment_count,
            conflict_count=health.conflict_count,
            last_success_at=health.last_success_at,
            last_reconciled_at=health.last_reconciled_at,
            last_recovery_verified_at=health.last_recovery_verified_at,
            remote_service_status=health.remote_service_status,
            object_storage_status=health.object_storage_status,
            account_merge_pending=health.account_merge_pending,
            conversation_merge_pending=health.conversation_merge_pending,
            conversation_merge_pending_rows=health.conversation_merge_pending_rows,
            search_rehome_pending_rows=health.search_rehome_pending_rows,
            writer_lock_held_ms=health.writer_lock_held_ms,
            writer_lock_rejections_recent=health.writer_lock_rejections_recent,
            last_backup_at=health.last_backup_at,
            last_backup_bytes=health.last_backup_bytes,
            last_backup_status=health.last_backup_status,
            backup_retention_mode=health.backup_retention_mode,
            backup_overdue=health.backup_overdue,
            backup_lifecycle_covered=health.backup_lifecycle_covered,
        )

    def list_conversations(
        self,
        *,
        cursor: str | None = None,
        limit: int = 100,
        source_account_id: str | None = None,
        query: str | None = None,
    ) -> RemoteConversationPageView:
        self._validate_limit(limit)
        profile, token, client = self._client()
        self._require_account_dimension(client, profile, source_account_id)
        recent = self._record_model_version(client, profile) >= _BROWSE_ORDER_MIN_VERSION
        if not recent:
            if query:
                # 过滤只在浏览顺序上定义。老远端上悄悄不过滤，会让人以为
                # 「搜不到」而不是「这一端搜不了」。
                raise ArchiveError(ErrorCode.QUERY_INVALID)
            return self._list_conversations_by_identity(
                client,
                cursor=cursor,
                limit=limit,
                profile=profile,
                token=token,
                source_account_id=source_account_id,
            )
        # 游标绑定它的过滤范围：A 账号翻到一半的游标拿到 B 账号（或不过滤的
        # 列表）下重放，页与页会重叠或跳空而不报错——scope 不同直接判
        # CURSOR_INVALID。关键词也进 scope，理由相同：换了词就是另一个结果集。
        scope = self._conversation_scope(source_account_id, query)
        after = self._decode_conversation_position_cursor(
            cursor,
            profile=profile,
            token=token,
            scope=scope,
        )
        try:
            page = client.list_conversations(
                profile.archive_id,
                recent=True,
                after_position=after,
                query=query,
                limit=limit,
                source_account_id=source_account_id,
            )
        except RemoteArchiveError as error:
            raise self._public_remote_error(error) from None
        self._validate_recent_conversation_page(page, after=after)
        items = tuple(self._conversation_view(item) for item in page.conversations)
        next_cursor = None
        if page.has_more:
            spot = page.next_position
            assert spot is not None
            next_cursor = self._encode_cursor(
                _CONVERSATION_POSITION_CURSOR_KIND,
                [spot.last_created_at, spot.conversation_id],
                profile=profile,
                token=token,
                scope=scope,
            )
        return RemoteConversationPageView(items, next_cursor, page.has_more)

    def _list_conversations_by_identity(
        self,
        client: RemoteArchiveTransport,
        *,
        cursor: str | None,
        limit: int,
        profile: RemoteDeviceProfile,
        token: DeviceToken,
        source_account_id: str | None,
    ) -> RemoteConversationPageView:
        """The pre-v4 walk, kept whole rather than degraded.

        A remote that cannot order by activity keeps no conversation state to
        order by; walking its opaque ids is the honest answer, and it is what
        every archive answered with until this unit.
        """
        after = self._decode_conversation_cursor(
            cursor,
            profile=profile,
            token=token,
            scope=source_account_id,
        )
        try:
            page = client.list_conversations(
                profile.archive_id,
                after_conversation_id=after,
                limit=limit,
                source_account_id=source_account_id,
            )
        except RemoteArchiveError as error:
            raise self._public_remote_error(error) from None
        self._validate_conversation_page(page, after=after)
        items = tuple(self._conversation_view(item) for item in page.conversations)
        next_cursor = None
        if page.has_more:
            assert page.next_conversation_id is not None
            next_cursor = self._encode_cursor(
                _CONVERSATION_CURSOR_KIND,
                page.next_conversation_id,
                profile=profile,
                token=token,
                scope=source_account_id,
            )
        return RemoteConversationPageView(items, next_cursor, page.has_more)

    @staticmethod
    def _conversation_view(item: RemoteConversationSummary) -> RemoteConversationView:
        return RemoteConversationView(
            conversation_id=item.conversation_id,
            message_count=item.message_count,
            last_created_at=item.last_created_at,
            last_server_sequence=item.last_server_sequence,
            display_name=item.display_name,
            is_group=item.is_group,
            source_account_id=item.source_account_id,
            last_message_type=item.last_message_type,
            last_message_preview=item.last_message_preview,
            avatar_blob_id=item.avatar_blob_id,
            announcement=item.announcement,
            my_group_alias=item.my_group_alias,
            member_count=item.member_count,
        )

    @staticmethod
    def _conversation_scope(source_account_id: str | None, query: str | None) -> str:
        """游标的作用范围串：账号 + 关键词。

        分隔符与 `_message_scope` 同一个 NUL，理由也同一条：它到不了任何一段
        （账号标识经线上校验，关键词是单行输入框的值），所以拼接可逆，两个
        不同的 (账号, 关键词) 拼不出同一个 scope。
        """
        return f"{source_account_id or str()}\x00{query or str()}"

    def get_conversation(
        self,
        conversation_id: str,
        *,
        source_account_id: str | None = None,
    ) -> RemoteConversationView:
        """一条会话的身份，不必先把它翻出来（记录模型 v6，PSU-2.44）。

        远端到不了 v6 就当场拒绝，**不悄悄回退到「在已加载的页里找」**——
        那条路会让标题取决于读者恰好加载过哪几页，正是这个单元存在的原因。
        远端说没有这条会话时抛 NOT_FOUND：那是一个确定性的答案，不是故障。
        """
        profile, _token, client = self._client()
        self._require_account_dimension(client, profile, source_account_id)
        version = self._record_model_version(client, profile)
        if version < _CONVERSATION_DETAIL_MIN_VERSION:
            raise ArchiveError(ErrorCode.QUERY_INVALID)
        try:
            item = client.get_conversation(
                profile.archive_id,
                conversation_id,
                source_account_id=source_account_id,
            )
        except RemoteArchiveError as error:
            raise self._public_remote_error(error) from None
        if item is None:
            raise ArchiveError(ErrorCode.NOT_FOUND)
        # 答非所问就是协议错。悄悄接受会让界面把另一条会话的名字挂在这条会话
        # 的标题上——比没有名字更糟。**只有一个例外**：v12 起远端把被并掉的 id
        # 读成 primary 再作答，这个端点问的正是「这条会话是谁」，所以它答回来
        # 的编号就是那句声明本身（PSU-2.95）。
        self._answered_conversation(
            item.conversation_id, conversation_id=conversation_id, version=version
        )
        return self._conversation_view(item)

    def list_conversation_members(
        self,
        conversation_id: str,
        *,
        cursor: str | None = None,
        limit: int = 200,
        source_account_id: str | None = None,
    ) -> RemoteConversationMembersPageView:
        """一页群成员（记录模型 v8，PSU-2.47）。

        远端到不了 v8 就当场拒绝，**不回退成一个空名单**：空名单是一句断言
        （「这个群没有别人」），而这里的实情是「这一端还没有名单这回事」。
        两句话混掉就再也分不开——所以低版本在这里出不去，界面看到空名单时
        可以放心照它说的画。
        """
        if not conversation_id:
            raise ArchiveError(ErrorCode.QUERY_INVALID)
        self._validate_limit(limit)
        profile, token, client = self._client()
        self._require_account_dimension(client, profile, source_account_id)
        if self._record_model_version(client, profile) < _ROSTER_MIN_VERSION:
            raise ArchiveError(ErrorCode.QUERY_INVALID)
        scope = self._message_scope(conversation_id, source_account_id)
        after = self._decode_member_cursor(
            cursor, profile=profile, token=token, scope=scope
        )
        try:
            page = client.list_conversation_members(
                profile.archive_id,
                conversation_id,
                source_account_id=source_account_id,
                after_member_uid=after,
                limit=limit,
            )
        except RemoteArchiveError as error:
            raise self._public_remote_error(error) from None
        self._validate_member_page(page)
        next_cursor: str | None = None
        if page.has_more and page.next_member_uid is not None:
            next_cursor = self._encode_cursor(
                _MEMBER_CURSOR_KIND,
                page.next_member_uid,
                profile=profile,
                token=token,
                scope=scope,
            )
        return RemoteConversationMembersPageView(
            items=tuple(self._member_view(item) for item in page.members),
            next_cursor=next_cursor,
            has_more=page.has_more,
        )

    @staticmethod
    def _member_view(item: RemoteConversationMember) -> RemoteConversationMemberView:
        return RemoteConversationMemberView(
            member_uid=item.member_uid,
            display_name=item.display_name,
            group_alias=item.group_alias,
            is_self=item.is_self,
            avatar_blob_id=item.avatar_blob_id,
        )

    @staticmethod
    def _validate_member_page(page: RemoteConversationMemberPage) -> None:
        if page.has_more and not page.next_member_uid:
            raise ArchiveError(ErrorCode.REMOTE_PROTOCOL_ERROR)
        seen: set[str] = set()
        for item in page.members:
            if not item.member_uid or item.member_uid in seen:
                # 一页里同一个人出现两次，翻页就会把他数两遍；界面上那个
                # 「N 人」于是比群里的人多。
                raise ArchiveError(ErrorCode.REMOTE_PROTOCOL_ERROR)
            seen.add(item.member_uid)

    def _decode_member_cursor(
        self,
        cursor: str | None,
        *,
        profile: RemoteDeviceProfile,
        token: DeviceToken,
        scope: str,
    ) -> str | None:
        if cursor is None:
            return None
        position = self._decode_cursor(
            cursor,
            kind=_MEMBER_CURSOR_KIND,
            profile=profile,
            token=token,
            scope=scope,
        )
        if not isinstance(position, str) or not position:
            raise ArchiveError(ErrorCode.CURSOR_INVALID)
        return position

    def list_conversation_messages(
        self,
        conversation_id: str,
        *,
        cursor: str | None = None,
        limit: int = 100,
        source_account_id: str | None = None,
    ) -> RemoteMessagePageView:
        if not conversation_id:
            raise ArchiveError(ErrorCode.QUERY_INVALID)
        self._validate_limit(limit)
        profile, token, client = self._client()
        self._require_account_dimension(client, profile, source_account_id)
        version = self._record_model_version(client, profile)
        if version < 2:
            return self._list_messages_by_upload_order(
                client,
                conversation_id,
                cursor=cursor,
                limit=limit,
                profile=profile,
                token=token,
                version=version,
            )
        # v4 起从**最新**那一页开始，与本机转录同一个起点；更早的远端只会从
        # 会话开头正序读，那是它能做到的全部。两种走法用同一条排序线，但各用
        # 各的游标 kind——方向不同的两个游标互相不认。
        latest = version >= _BROWSE_ORDER_MIN_VERSION
        kind = _MESSAGE_LATEST_CURSOR_KIND if latest else _MESSAGE_POSITION_CURSOR_KIND
        # 过滤进游标的 scope：换了账号（或摘掉过滤）的旧游标必须作废，
        # 理由同会话列表。
        scope = self._message_scope(conversation_id, source_account_id)
        after = self._decode_position_cursor(
            cursor,
            kind=kind,
            profile=profile,
            token=token,
            scope=scope,
        )
        try:
            page = client.list_conversation_messages(
                profile.archive_id,
                conversation_id,
                chronological=not latest,
                latest=latest,
                after=after,
                limit=limit,
                source_account_id=source_account_id,
            )
        except RemoteArchiveError as error:
            raise self._public_remote_error(error) from None
        answered = self._answered_conversation(
            page.read_conversation_id, conversation_id=conversation_id, version=version
        )
        if latest:
            self._validate_latest_page(page, conversation_id=answered, before=after)
        else:
            self._validate_time_ordered_page(page, conversation_id=answered, after=after)
        next_cursor = None
        if page.has_more:
            position = page.next_position
            assert position is not None
            next_cursor = self._encode_cursor(
                kind,
                [
                    position.created_at,
                    position.source_sequence,
                    position.source_local_id,
                    position.record_key,
                ],
                profile=profile,
                token=token,
                scope=scope,
            )
        return RemoteMessagePageView(
            tuple(self._message_view(item) for item in page.records),
            next_cursor,
            page.has_more,
        )

    def message_window(
        self,
        conversation_id: str,
        *,
        anchor_message_id: str,
        before: int = 20,
        after: int = 20,
        source_account_id: str | None = None,
    ) -> RemoteMessageWindowView:
        """这条消息前后的一段，与本机 `/messages/window` 同名同形。

        v9 起才有。更早的远端**当场拒绝**而不是给一页最新的：读者点的是
        「定位到上下文」，把别处的一屏交给他，他会以为那就是命中所在。
        """
        if not conversation_id or not anchor_message_id:
            raise ArchiveError(ErrorCode.QUERY_INVALID)
        if before < 0 or after < 0:
            raise ArchiveError(ErrorCode.QUERY_INVALID)
        self._validate_limit(before + after + 1)
        profile, token, client = self._client()
        del token
        self._require_account_dimension(client, profile, source_account_id)
        version = self._record_model_version(client, profile)
        if version < _ANCHOR_WINDOW_MIN_VERSION:
            raise ArchiveError(ErrorCode.REMOTE_RECORD_MODEL_TOO_OLD)
        try:
            window = client.message_window(
                profile.archive_id,
                conversation_id,
                anchor_record_key=anchor_message_id,
                before=before,
                after=after,
                source_account_id=source_account_id,
            )
        except RemoteArchiveError as error:
            raise self._public_remote_error(error) from None
        self._validate_window(
            window,
            conversation_id=self._answered_conversation(
                window.read_conversation_id,
                conversation_id=conversation_id,
                version=version,
            ),
        )
        return RemoteMessageWindowView(
            tuple(self._message_view(item) for item in window.records),
            window.anchor_record_key,
        )

    @staticmethod
    def _validate_window(
        window: RemoteMessageWindow,
        *,
        conversation_id: str,
    ) -> None:
        """窗口必须是这条会话的、含锚点的、按阅读顺序升序的一段。

        与两个分页共用同一条规矩（`_validate_latest_page`）：一段读不出顺序的
        记录在界面上不会报错，只会静静地把对话讲错。
        """
        keys: set[str] = set()
        previous: tuple[int, int, int, str] | None = None
        for record in window.records:
            if record.conversation_id != conversation_id:
                raise ArchiveError(ErrorCode.REMOTE_PROTOCOL_ERROR)
            if record.record_key in keys:
                raise ArchiveError(ErrorCode.REMOTE_PROTOCOL_ERROR)
            keys.add(record.record_key)
            position = (
                record.created_at,
                record.source_sequence,
                record.source_local_id,
                record.record_key,
            )
            if previous is not None and position <= previous:
                raise ArchiveError(ErrorCode.REMOTE_PROTOCOL_ERROR)
            previous = position
        if window.anchor_record_key not in keys:
            raise ArchiveError(ErrorCode.REMOTE_PROTOCOL_ERROR)

    def _list_messages_by_upload_order(
        self,
        client: RemoteArchiveTransport,
        conversation_id: str,
        *,
        cursor: str | None,
        limit: int,
        profile: RemoteDeviceProfile,
        token: DeviceToken,
        version: int,
    ) -> RemoteMessagePageView:
        """The v1 path, kept whole rather than degraded.

        A remote that has not got the render model has not got the source's
        own positions either, so reading order is not merely unsupported there
        — it is unknowable. Paging by upload order is the honest answer, and
        the page says as much through the null render fields.
        """
        after = self._decode_message_cursor(
            cursor,
            profile=profile,
            token=token,
            conversation_id=conversation_id,
        )
        try:
            page = client.list_conversation_messages(
                profile.archive_id,
                conversation_id,
                after_server_sequence=after,
                limit=limit,
            )
        except RemoteArchiveError as error:
            raise self._public_remote_error(error) from None
        # 这条路上的 `version` 永远 < 2，所以那句声明在这里只有一种读法：
        # 拒绝。会话归并是 v11 才有的事，这么老的远端没有换主语的正当理由
        # （PSU-2.95）。规矩与另外四处共用一个助手，不在这里再写一遍。
        self._validate_message_page(
            page,
            conversation_id=self._answered_conversation(
                page.read_conversation_id,
                conversation_id=conversation_id,
                version=version,
            ),
            after=after,
        )
        next_cursor = None
        if page.has_more:
            next_cursor = self._encode_cursor(
                _MESSAGE_CURSOR_KIND,
                page.next_server_sequence,
                profile=profile,
                token=token,
                scope=conversation_id,
            )
        return RemoteMessagePageView(
            tuple(self._message_view(item) for item in page.records),
            next_cursor,
            page.has_more,
        )

    def attachment_url(self, blob_id: str) -> RemoteAttachmentUrlView:
        """签一个附件直取 URL，字节不经过任何 API 进程。

        本机代理与浏览器 BFF 走的是这同一段：一边的 transport 是 HTTP 客户端、
        另一边是进程内实现，但映射、错误翻译与返回形状只有这一份——
        「同一份前端零改动可用」靠的就是这个，不靠人工对齐两处。
        """
        if not blob_id:
            raise ArchiveError(ErrorCode.QUERY_INVALID)
        profile, _token, client = self._client()
        try:
            presigned = client.presign_attachment(profile.archive_id, blob_id)
        except RemoteArchiveError as error:
            raise self._public_remote_error(error) from None
        self._validate_presigned(presigned)
        return RemoteAttachmentUrlView(
            url=presigned.url,
            method=presigned.method,
            expires_at=presigned.expires_at,
            storage_origin=presigned.storage_origin,
        )

    def thumbnail_url(self, blob_id: str) -> RemoteThumbnailUrlView:
        """签这条附件的**缩略图**，顺便回答原图在不在。

        入参是主工件的标识——前端手上只有 `attachment_refs` 里那一个——缩略图
        的标识在这里派生。派生留在服务端而不是浏览器：`crypto.subtle` 要安全
        上下文，而且同一条规则写成两种语言迟早会各自演化，两端谁也不会报错。

        两个问题一次答完，是因为页面为了画气泡本来就要发这一条。代价说清楚：
        本机代理那一端要向源站问两次（一次签缩略图、一次问原图），远端自己那
        一端两次都在进程内。多的那一次只花在**已经**要发的这条请求里，前端的
        往返数没有变。
        """
        if not blob_id:
            raise ArchiveError(ErrorCode.QUERY_INVALID)
        profile, _token, client = self._client()
        try:
            presigned = client.presign_attachment(
                profile.archive_id, thumbnail_blob_id(blob_id)
            )
        except RemoteArchiveError as error:
            if error.code in _BLOB_ABSENT_CODES:
                # 「这条附件没有缩略图」——与本机 `/attachments/{id}/thumbnail`
                # **同一个错误码**，于是前端回退到原图的那段代码只有一份。
                # 不可重试：这个 blob 没有第二份工件是永久事实。
                raise ArchiveError(
                    ErrorCode.THUMBNAIL_UNAVAILABLE, retryable=False
                ) from None
            raise self._public_remote_error(error) from None
        self._validate_presigned(presigned)
        return RemoteThumbnailUrlView(
            url=presigned.url,
            method=presigned.method,
            expires_at=presigned.expires_at,
            storage_origin=presigned.storage_origin,
            original_available=self._original_available(client, profile, blob_id),
        )

    def _original_available(
        self,
        client: RemoteArchiveTransport,
        profile: RemoteDeviceProfile,
        blob_id: str,
    ) -> bool | None:
        """原图取不取得到；**问不出来时答 `None`，不答 `False`**。

        只有「这个 blob 不在」才是 `False`。别的失败（网络抖动、远端 5xx）说明的
        是这一问没问成，把它折成 `False` 就会因为一次瞬时故障宣布一张图永久丢失，
        而界面据此写下的「仅缩略图」正是本单元最不该制造的那种假象。
        """
        try:
            client.presign_attachment(profile.archive_id, blob_id)
        except RemoteArchiveError as error:
            if error.code in _BLOB_ABSENT_CODES:
                return False
            return None
        return True

    @staticmethod
    def _validate_presigned(presigned: RemotePresignedAttachment) -> None:
        if (
            not presigned.url
            or presigned.method != "GET"
            or presigned.expires_at <= 0
            or not presigned.storage_origin
            or not presigned.url.startswith(f"{presigned.storage_origin}/")
        ):
            raise ArchiveError(ErrorCode.REMOTE_PROTOCOL_ERROR)

    def search(
        self,
        query: str | None,
        *,
        limit: int = 100,
        source_account_id: str | None = None,
        conversation_ids: Sequence[str] = (),
        sender_uids: Sequence[str] = (),
        message_types: tuple[str, ...] | None = None,
        has_attachment: bool | None = None,
        date_from: int | None = None,
        date_to: int | None = None,
        cursor: str | None = None,
    ) -> RemoteSearchView:
        """一页搜索结果。

        v7 及以上走索引那一条（游标、总数、每会话计数、六个过滤）；更早的远端
        走它一直以来的那一条，并且**当场拒绝**任何它答不出的东西——把过滤发给
        一个会忽略它的远端，等于告诉用户「筛过了」而其实没筛。

        一个维度给多个值时（PSU-2.74）**远端并不认得多值**：这里按值扇出成几次
        远程请求再归并。这样换来的是不必升远端记录模型、不必重新部署两套远端
        实现；代价是每多一个值就多一次跨境往返，所以上限比本机小（5 对 20）。
        """
        self._validate_limit(limit)
        if (
            len(conversation_ids) > MAX_REMOTE_FILTER_VALUES
            or len(sender_uids) > MAX_REMOTE_FILTER_VALUES
        ):
            raise ArchiveError(ErrorCode.QUERY_INVALID)
        if len(conversation_ids) > 1 and len(sender_uids) > 1:
            # 扇出一次只挑得动一个维度，而没被扇出的那个维度远端只认**一个**值。
            # 两边都多值时没有一条不撒谎的路：悄悄只发第一个发送人，界面上就是
            # 「筛过了」而其实没筛。所以这个组合必须拒绝，而不是悄悄截断。
            raise ArchiveError(ErrorCode.QUERY_INVALID)
        profile, token, client = self._client()
        self._require_account_dimension(client, profile, source_account_id)
        indexed = self._record_model_version(client, profile) >= _SEARCH_INDEX_MIN_VERSION
        wants_more = any(
            (
                conversation_ids,
                sender_uids,
                message_types,
                has_attachment is not None,
                date_from is not None,
                date_to is not None,
                cursor is not None,
            )
        )
        if not indexed:
            if wants_more:
                raise ArchiveError(ErrorCode.QUERY_INVALID)
            if not query:
                raise ArchiveError(ErrorCode.QUERY_INVALID)
            try:
                records = client.search(
                    profile.archive_id,
                    query,
                    limit=limit,
                    source_account_id=source_account_id,
                )
            except RemoteArchiveError as error:
                raise self._public_remote_error(error) from None
            return RemoteSearchView(tuple(self._message_view(item) for item in records))
        # 游标绑定它的整个条件集：换了词、换了账号、换了任何一个过滤，就是另一个
        # 结果集，旧游标在上面重放会重叠或跳空而不报错。
        scope = self._search_scope(
            query,
            source_account_id,
            conversation_ids,
            sender_uids,
            message_types,
            has_attachment,
            date_from,
            date_to,
        )
        # 扇出只挑一个维度，会话优先于发送人——与本机同一条选择度顺序。没被扇出
        # 的那个维度经上面的双维拒绝**至多剩一个值**（远端不认得多值，而本机在
        # 那里是靠行内 `IN` 过滤实现的，这一端没有那条路），原样带在每次扇出
        # 请求上。
        fans_conversations = len(conversation_ids) > 1
        fanned = tuple(conversation_ids) if fans_conversations else ()
        if not fanned and len(sender_uids) > 1:
            fanned = tuple(sender_uids)
        if fanned:
            return self._fanned_out_search(
                client,
                query,
                profile=profile,
                token=token,
                scope=scope,
                cursor=cursor,
                limit=limit,
                source_account_id=source_account_id,
                fanned_values=fanned,
                fans_conversations=fans_conversations,
                single_conversation_id=(
                    conversation_ids[0]
                    if conversation_ids and not fans_conversations
                    else None
                ),
                single_sender_uid=(
                    sender_uids[0] if sender_uids and fans_conversations else None
                ),
                message_types=message_types,
                has_attachment=has_attachment,
                date_from=date_from,
                date_to=date_to,
            )
        after = self._decode_search_cursor(cursor, profile=profile, token=token, scope=scope)
        try:
            page = client.search_page(
                profile.archive_id,
                query,
                source_account_id=source_account_id,
                conversation_id=conversation_ids[0] if conversation_ids else None,
                sender_uid=sender_uids[0] if sender_uids else None,
                message_types=message_types,
                has_attachment=has_attachment,
                date_from=date_from,
                date_to=date_to,
                after=after,
                limit=limit,
            )
        except RemoteArchiveError as error:
            raise self._public_remote_error(error) from None
        self._validate_search_page(page)
        next_cursor = None
        if page.has_more:
            spot = page.next_position
            assert spot is not None
            next_cursor = self._encode_cursor(
                _SEARCH_POSITION_CURSOR_KIND,
                [spot.created_at, spot.record_key],
                profile=profile,
                token=token,
                scope=scope,
            )
        return RemoteSearchView(
            items=tuple(self._message_view(item) for item in page.records),
            next_cursor=next_cursor,
            has_more=page.has_more,
            total_count=page.total_count,
            conversation_counts=dict(page.conversation_counts),
            index_status=page.index_status,
        )

    def _fanned_out_search(
        self,
        client: RemoteArchiveTransport,
        query: str | None,
        *,
        profile: RemoteDeviceProfile,
        token: DeviceToken,
        scope: str,
        cursor: str | None,
        limit: int,
        source_account_id: str | None,
        fanned_values: tuple[str, ...],
        fans_conversations: bool,
        single_conversation_id: str | None,
        single_sender_uid: str | None,
        message_types: tuple[str, ...] | None,
        has_attachment: bool | None,
        date_from: int | None,
        date_to: int | None,
    ) -> RemoteSearchView:
        """一个维度多个值：按值各问一次远端，归并成一页。

        **每条流各带自己的位置**，而不是共用一个全局游标。远端在同一秒内的次序
        由它自己的 `docid` 决定（`created_at << 20 | 哈希`），那个哈希这一端算不出
        来——拿 (时间, 记录标识) 当比较器去推进一个全局位置，同一秒里就可能把某条
        流还没给过的行judge成「已经翻过去了」。各记各的位置就不必知道它的次序：
        每条流只从**它自己**上次停的地方往后要。

        但「各记各的位置」还差一半才成立：位置推进到「本流最后被取走的那条」，
        远端从它**严格往后**续读，只有当被取走的是该流页内次序的一个**前缀**时
        才不跳不重。所以归并只从各流头部按远端给的次序一条条取（见下），不做
        全局排序后截断。

        显示顺序按 (时间, 记录标识) 在各流头部之间挑——同一秒内谁先谁后是观感
        问题，而前缀那条是正确性问题，观感给正确性让路。

        请求默认仍是**串行**发的；传输自己声明扛得住并发时才按流并发（HTTP 那条
        已核：`search_page` 每次调用各开各的请求，不碰共享可变状态），错误语义
        与串行一致。进程内传输背后是 sqlite 的 store，线程安全没被核实过，
        仍走串行。
        """
        positions = self._decode_fanned_search_cursor(
            cursor, profile=profile, token=token, scope=scope, streams=len(fanned_values)
        )

        def fetch(index: int, value: str) -> RemoteSearchResultPage:
            return client.search_page(
                profile.archive_id,
                query,
                source_account_id=source_account_id,
                conversation_id=value if fans_conversations else single_conversation_id,
                sender_uid=single_sender_uid if fans_conversations else value,
                message_types=message_types,
                has_attachment=has_attachment,
                date_from=date_from,
                date_to=date_to,
                after=positions[index],
                limit=limit,
            )

        pages: list[RemoteSearchResultPage] = []
        # 并发只在传输自己声明扛得住时开；没声明就当不行——沉默的默认必须落在
        # 串行那条安全路上。结果按流的序号收，所以两条路里都是序号最小的那条
        # 失败流决定报出去的错，页的排列也与串行一字不差。
        if getattr(client, "supports_concurrent_requests", False) and len(fanned_values) > 1:
            with ThreadPoolExecutor(max_workers=len(fanned_values)) as pool:
                futures = [
                    pool.submit(fetch, index, value)
                    for index, value in enumerate(fanned_values)
                ]
                for future in futures:
                    try:
                        page = future.result()
                    except RemoteArchiveError as error:
                        raise self._public_remote_error(error) from None
                    self._validate_search_page(page)
                    pages.append(page)
        else:
            for index, value in enumerate(fanned_values):
                try:
                    page = fetch(index, value)
                except RemoteArchiveError as error:
                    raise self._public_remote_error(error) from None
                self._validate_search_page(page)
                pages.append(page)

        # 归并**只从各流的头部取**，一次一条：在所有流的当前头里，挑显示序
        # （时间倒序、记录标识破并列）最靠前的那条放进这一页。这样每条流被取走
        # 的必然是它自己那一页的一个**前缀**——上一段注释立的那条「位置不能越过
        # 没被取走的记录」的不变式，靠的正是这个：流的页内次序就是远端的续读
        # 次序（带关键词时是 docid，浏览时是 (时间, 记录标识)），位置推进到
        # 本流最后被**取走**的那条，远端严格往后走才既不跳也不重。全局排序后
        # 截断没有这个保证：docid 同一秒内按哈希排，与记录标识的字典序对约一半
        # 的键对是反的，截断线落在中间时，被留下的那条就永远轮不到了。浏览那条
        # 路上流内次序与显示序本来就是同一条线，头部归并给出的页与全局排序逐字
        # 相同，所以不必分支。
        heads = [0] * len(pages)
        kept: list[tuple[RemoteMessage, int]] = []
        next_positions: list[RemoteSearchPosition | None] = list(positions)
        while len(kept) < limit:
            best = -1
            for index, page in enumerate(pages):
                if heads[index] >= len(page.records):
                    continue
                record = page.records[heads[index]]
                if best < 0 or (-record.created_at, record.record_key) < (
                    -pages[best].records[heads[best]].created_at,
                    pages[best].records[heads[best]].record_key,
                ):
                    best = index
            if best < 0:
                break
            record = pages[best].records[heads[best]]
            heads[best] += 1
            kept.append((record, best))
            # 一条都没被取走的流不进这里，位置留在原地——下一页从同一处再要。
            next_positions[best] = RemoteSearchPosition(
                created_at=record.created_at, record_key=record.record_key
            )
        # 一条流「还有」有两种情形：它这一页有没被取走的记录（下一页会从同一个
        # 位置再要一次，所以它的位置**不能**前进），或者远端说它自己还有下一页。
        has_more = any(
            heads[index] < len(page.records) or page.has_more
            for index, page in enumerate(pages)
        )
        next_cursor = (
            self._encode_cursor(
                _SEARCH_FANOUT_CURSOR_KIND,
                [
                    None if spot is None else [spot.created_at, spot.record_key]
                    for spot in next_positions
                ],
                profile=profile,
                token=token,
                scope=scope,
            )
            if has_more
            else None
        )
        counts: dict[str, int] = {}
        for page in pages:
            for conversation, count in page.conversation_counts.items():
                counts[conversation] = counts.get(conversation, 0) + count
        # 计数逐流相加：按会话或按发送人扇出的切片互不相交，所以是精确值。
        # 索引状态取最悲观的一条，悲观序是 unavailable > building > ready：
        # 「查不了」比「还在建」更糟——任一条流答 unavailable，这一页就不只是
        # 可能不全，而是整片缺席，把它折成 building 会让界面许一个「建完就有」
        # 的空愿。
        if any(page.index_status == "unavailable" for page in pages):
            index_status = "unavailable"
        elif any(page.index_status != "ready" for page in pages):
            index_status = "building"
        else:
            index_status = "ready"
        return RemoteSearchView(
            items=tuple(self._message_view(record) for record, _ in kept),
            next_cursor=next_cursor,
            has_more=has_more,
            total_count=sum(page.total_count for page in pages),
            conversation_counts=counts,
            index_status=index_status,
        )

    def _decode_fanned_search_cursor(
        self,
        cursor: str | None,
        *,
        profile: RemoteDeviceProfile,
        token: DeviceToken,
        scope: str,
        streams: int,
    ) -> list[RemoteSearchPosition | None]:
        """扇出那条路的游标：**每条流一个位置**，所以它是另一种 kind。

        条数对不上就是无效游标：那说明它是在另一组值上发出的，而 scope 已经把
        值本身折进去了——两道都过不去的，只可能是被人改过。
        """
        if cursor is None:
            return [None] * streams
        payload = self._decode_cursor(
            cursor,
            kind=_SEARCH_FANOUT_CURSOR_KIND,
            profile=profile,
            token=token,
            scope=scope,
        )
        if not isinstance(payload, list) or len(payload) != streams:
            raise ArchiveError(ErrorCode.CURSOR_INVALID)
        positions: list[RemoteSearchPosition | None] = []
        for entry in payload:
            if entry is None:
                positions.append(None)
                continue
            if not isinstance(entry, list):
                raise ArchiveError(ErrorCode.CURSOR_INVALID)
            spot = cast(list[object], entry)
            if len(spot) != 2:
                raise ArchiveError(ErrorCode.CURSOR_INVALID)
            created_at, record_key = spot
            if (
                not isinstance(created_at, int)
                or isinstance(created_at, bool)
                or created_at < 0
                or not isinstance(record_key, str)
                or not record_key
            ):
                raise ArchiveError(ErrorCode.CURSOR_INVALID)
            positions.append(
                RemoteSearchPosition(created_at=created_at, record_key=record_key)
            )
        return positions

    @staticmethod
    def _search_scope(
        query: str | None,
        source_account_id: str | None,
        conversation_ids: Sequence[str],
        sender_uids: Sequence[str],
        message_types: tuple[str, ...] | None,
        has_attachment: bool | None,
        date_from: int | None,
        date_to: int | None,
    ) -> str:
        """游标的作用范围串：这一次搜索的**每一个**条件。

        分隔符与另外两个 scope 同一个 NUL，理由也同一条：它到不了任何一段
        （JSON 把控制字符一律转义，其余是输入框的值或数字），所以拼接可逆。
        少折一个条件进来，那个条件一改，旧游标就会在一个不同的结果集上重放。

        多值字段各自 JSON 编码，不拿逗号拼：逗号拼接下 `("a,b","c")` 与
        `("a","b,c")` 是同一个串、同一个 HMAC——在一组过滤下签出的扇出游标换一组
        过滤照样解得开，而它携带的每流位置会应用到**另一组**流上：静默错页，
        不是 CURSOR_INVALID。改这条编码会作废在途的扇出游标，可以接受：多值
        （PSU-2.74）昨天才提交、线上远端还在 2.74 之前，旧游标过不了 HMAC 得到的
        正是干净的 CURSOR_INVALID——这套 scope 机制该给的安全失败。
        """
        return "\x00".join(
            (
                query or "",
                source_account_id or "",
                json.dumps(list(conversation_ids), ensure_ascii=False, separators=(",", ":")),
                json.dumps(list(sender_uids), ensure_ascii=False, separators=(",", ":")),
                json.dumps(list(message_types or ()), ensure_ascii=False, separators=(",", ":")),
                "" if has_attachment is None else str(has_attachment),
                "" if date_from is None else str(date_from),
                "" if date_to is None else str(date_to),
            )
        )

    @staticmethod
    def _validate_search_page(page: RemoteSearchResultPage) -> None:
        if page.has_more != (page.next_position is not None):
            # 说还有下一页却不给位置（或反过来）——两种都会让翻页静默停在这里。
            raise ArchiveError(ErrorCode.REMOTE_PROTOCOL_ERROR)
        if page.total_count < 0 or any(
            count < 0 for count in page.conversation_counts.values()
        ):
            raise ArchiveError(ErrorCode.REMOTE_PROTOCOL_ERROR)
        if page.index_status not in {"ready", "building", "unavailable"}:
            raise ArchiveError(ErrorCode.REMOTE_PROTOCOL_ERROR)

    def _decode_search_cursor(
        self,
        cursor: str | None,
        *,
        profile: RemoteDeviceProfile,
        token: DeviceToken,
        scope: str,
    ) -> RemoteSearchPosition | None:
        if cursor is None:
            return None
        position = self._decode_cursor(
            cursor,
            kind=_SEARCH_POSITION_CURSOR_KIND,
            profile=profile,
            token=token,
            scope=scope,
        )
        if not isinstance(position, list) or len(position) != 2:
            raise ArchiveError(ErrorCode.CURSOR_INVALID)
        created_at, record_key = position
        if (
            not isinstance(created_at, int)
            or isinstance(created_at, bool)
            or created_at < 0
            or not isinstance(record_key, str)
            or not record_key
        ):
            raise ArchiveError(ErrorCode.CURSOR_INVALID)
        return RemoteSearchPosition(created_at=created_at, record_key=record_key)

    def list_accounts(self) -> RemoteAccountsPageView:
        """归档里有哪些账号、各占多少行、多少行谁的都不是。

        v3 才有这个端点。对更老的远端**先按版本拒绝**而不是打过去吃 404：
        404 会被映射成「归档不存在」，而归档明明在，只是旧。
        """
        profile, _token, client = self._client()
        if self._record_model_version(client, profile) < _ACCOUNT_DIMENSION_MIN_VERSION:
            raise ArchiveError(ErrorCode.QUERY_INVALID)
        try:
            page = client.list_accounts(profile.archive_id)
        except RemoteArchiveError as error:
            raise self._public_remote_error(error) from None
        self._validate_accounts_page(page)
        return RemoteAccountsPageView(
            items=tuple(
                RemoteAccountView(
                    source_account_id=item.source_account_id,
                    record_count=item.record_count,
                    display_name=item.display_name,
                    avatar_blob_id=item.avatar_blob_id,
                    merged_source_account_ids=item.merged_source_account_ids,
                )
                for item in page.items
            ),
            unattributed_count=page.unattributed_count,
        )

    def _require_account_dimension(
        self,
        client: RemoteArchiveTransport,
        profile: RemoteDeviceProfile,
        source_account_id: str | None,
    ) -> None:
        """带账号过滤的读要求对端已到 v3。

        **不能把过滤发给不认它的远端**：老服务端会忽略认不得的查询参数并把
        全部账号的行都答回来——过滤悄悄失效，正是这个参数要防的事故。
        """
        if source_account_id is None:
            return
        if not source_account_id:
            raise ArchiveError(ErrorCode.QUERY_INVALID)
        if self._record_model_version(client, profile) < _ACCOUNT_DIMENSION_MIN_VERSION:
            raise ArchiveError(ErrorCode.QUERY_INVALID)

    @staticmethod
    def _message_scope(conversation_id: str, source_account_id: str | None) -> str:
        """一个消息游标的作用范围：会话，及（有过滤时）账号。

        账号 id 经线上校验非空，NUL 不会出现在两种标识里，所以拼接可逆、
        不会与任何未过滤的纯会话 scope 撞车。
        """
        if source_account_id is None:
            return conversation_id
        return f"{source_account_id}\x00{conversation_id}"

    @staticmethod
    def _validate_accounts_page(page: RemoteAccountsPage) -> None:
        identities = tuple(item.source_account_id for item in page.items)
        if any(not item for item in identities):
            raise ArchiveError(ErrorCode.REMOTE_PROTOCOL_ERROR)
        if any(current <= previous for previous, current in zip(identities, identities[1:])):
            raise ArchiveError(ErrorCode.REMOTE_PROTOCOL_ERROR)
        counts = (*(item.record_count for item in page.items), page.unattributed_count)
        if any(isinstance(value, bool) or value < 0 for value in counts):
            raise ArchiveError(ErrorCode.REMOTE_PROTOCOL_ERROR)

    def _record_model_version(
        self,
        client: RemoteArchiveTransport,
        profile: RemoteDeviceProfile,
    ) -> int:
        """Ask the peer which record model it keeps, once per peer.

        A failed negotiation is not memoized and not answered with 1: an
        unreachable remote is not an old remote, and guessing would silently
        page the archive in upload order for the rest of the session.
        """
        key = (profile.base_url, profile.archive_id, profile.device_id)
        cached = self._record_model_versions.get(key)
        if cached is not None:
            return cached
        try:
            version = client.record_model_version()
        except RemoteArchiveError as error:
            raise self._public_remote_error(error) from None
        # 两个线程同时协商最多是多问一次；写进去的值一样。
        self._record_model_versions[key] = version
        return version

    def supports_forward_media(self) -> bool:
        from chat_archive.ports.sync import FORWARD_MEDIA_MIN_VERSION

        try:
            _profile, _token, client = self._client()
            return self._record_model_version(client, _profile) >= FORWARD_MEDIA_MIN_VERSION
        except (ArchiveError, RemoteArchiveError):
            return False

    def _client(
        self,
    ) -> tuple[RemoteDeviceProfile, DeviceToken, RemoteArchiveTransport]:
        loaded = self._load_credentials(required=True)
        assert loaded is not None
        profile, token = loaded
        try:
            client = self._services.client_factory(profile, token)
        except ValueError:
            raise ArchiveError(ErrorCode.REMOTE_CREDENTIALS_UNAVAILABLE) from None
        return profile, token, client

    def _load_credentials(
        self,
        *,
        required: bool,
    ) -> tuple[RemoteDeviceProfile, DeviceToken] | None:
        try:
            loaded = self._services.credentials.get()
        except (OSError, UnicodeError):
            raise ArchiveError(ErrorCode.REMOTE_CREDENTIALS_UNAVAILABLE) from None
        if loaded is None:
            if required:
                raise ArchiveError(ErrorCode.REMOTE_NOT_CONFIGURED)
            return None
        profile, token = loaded
        if not all(
            (
                profile.base_url,
                profile.archive_id,
                profile.device_id,
                token.value,
            )
        ):
            raise ArchiveError(ErrorCode.REMOTE_CREDENTIALS_UNAVAILABLE)
        return profile, token

    @staticmethod
    def _message_view(message: RemoteMessage) -> RemoteMessageView:
        return RemoteMessageView(
            record_key=message.record_key,
            conversation_id=message.conversation_id,
            created_at=message.created_at,
            content_text=message.content_text,
            server_sequence=message.server_sequence,
            sender_uid=message.sender_uid,
            sender_display_name=message.sender_display_name,
            is_self=message.is_self,
            message_type=message.message_type,
            app_card_json=message.app_card_json,
            reply_to_key=message.reply_to_key,
            attachment_refs=message.attachment_refs,
            source_account_id=message.source_account_id,
            sender_avatar_blob_id=message.sender_avatar_blob_id,
            reply_preview=message.reply_preview,
            transcript=(
                None
                if message.transcript is None
                else RemoteTranscriptView(
                    text=message.transcript.text,
                    engine=message.transcript.engine,
                    low_confidence=message.transcript.low_confidence,
                )
            ),
        )

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if isinstance(limit, bool) or limit <= 0 or limit > _MAX_PAGE_SIZE:
            raise ArchiveError(ErrorCode.QUERY_INVALID)

    @staticmethod
    def _validate_health(
        health: RemoteHealth,
        *,
        profile: RemoteDeviceProfile,
    ) -> None:
        if (
            health.archive_id != profile.archive_id
            or health.device_id != profile.device_id
            or not health.backend_type
            or health.remote_service_status not in {"healthy", "degraded"}
            or health.object_storage_status not in {"healthy", "degraded", "unknown", "not_configured"}
        ):
            raise ArchiveError(ErrorCode.REMOTE_PROTOCOL_ERROR)
        numbers = (
            health.last_uploaded_local_sequence,
            health.last_server_sequence,
            health.remote_used_bytes,
            health.remote_blob_count,
            health.pending_attachment_count,
            health.conflict_count,
        )
        if any(isinstance(value, bool) or value < 0 for value in numbers):
            raise ArchiveError(ErrorCode.REMOTE_PROTOCOL_ERROR)

    @staticmethod
    def _validate_conversation_page(
        page: RemoteConversationPage,
        *,
        after: str | None,
    ) -> None:
        identities = tuple(item.conversation_id for item in page.conversations)
        if any(not item for item in identities):
            raise ArchiveError(ErrorCode.REMOTE_PROTOCOL_ERROR)
        if any(current <= previous for previous, current in zip(identities, identities[1:])):
            raise ArchiveError(ErrorCode.REMOTE_PROTOCOL_ERROR)
        if after is not None and identities and identities[0] <= after:
            raise ArchiveError(ErrorCode.REMOTE_PROTOCOL_ERROR)
        expected_next = identities[-1] if identities else None
        if page.next_conversation_id != expected_next:
            raise ArchiveError(ErrorCode.REMOTE_PROTOCOL_ERROR)
        if page.has_more and expected_next is None:
            raise ArchiveError(ErrorCode.REMOTE_PROTOCOL_ERROR)

    @staticmethod
    def _answered_conversation(
        declared: str | None,
        *,
        conversation_id: str,
        version: int,
    ) -> str:
        """这一段答复是**关于哪条会话**的——按对端自己的说法（PSU-2.95）。

        五处自检从前拿「我问的那个 id」当主语。归并之后远端会把被并掉的 id
        读成 primary 再作答，于是「答对了」正好长得像「答错了」，桌面 App 的
        转录变成一行红字（PSU-2.90 真机）。这个助手把主语换成对端声明的那个，
        **只在它真的声明过时**。

        `None` 与「说的就是问的那个」都是没有翻译发生，主语退回调用方问的那个
        ——老远端的行为一字未变，而它们本来也没有换主语的理由：会话归并是 v11
        才有的事。会话详情那一路没有单独的声明键，它整条答复就是那句声明，所以
        那里把**答回来的编号**当 `declared` 传进来，同一条规矩照用。

        **换来的与丢掉的**（别让人以为这道自检更强）：

        - 仍然证明：这一页里的每一行都属于这条答复自称的那条会话。远端顺手
          答错一条会话——从前那道自检真正挡住的那类错——不会顺带把声明也改对，
          所以照旧当场拒绝。
        - 不再证明：自称的那条**就是**问的那条。一个同时改了口径与内容的远端
          骗得过它。要堵这个口子，读侧得能独立复核归并链，而那份链条只有远端
          有——真要不信它，该换的是"信不信这个远端"，不是这里多一道校验。
        """
        if declared is None or declared == conversation_id:
            return conversation_id
        if not declared or version < _MERGED_CONVERSATION_MIN_VERSION:
            # 键在、版本不到（或主语是空串）：对端的两句话对不上。信版本那一
            # 句——能力是它自己报的，而读侧的每一条降级都建在那个数上。
            raise ArchiveError(ErrorCode.REMOTE_PROTOCOL_ERROR)
        return declared

    @staticmethod
    def _validate_message_page(
        page: RemoteMessagePage,
        *,
        conversation_id: str,
        after: int,
    ) -> None:
        sequences = tuple(item.server_sequence for item in page.records)
        if any(item.conversation_id != conversation_id for item in page.records):
            raise ArchiveError(ErrorCode.REMOTE_PROTOCOL_ERROR)
        if any(isinstance(item, bool) for item in sequences):
            raise ArchiveError(ErrorCode.REMOTE_PROTOCOL_ERROR)
        if sequences and (
            sequences[0] <= after
            or any(current <= previous for previous, current in zip(sequences, sequences[1:]))
        ):
            raise ArchiveError(ErrorCode.REMOTE_PROTOCOL_ERROR)
        expected_next = sequences[-1] if sequences else after
        if page.next_server_sequence != expected_next:
            raise ArchiveError(ErrorCode.REMOTE_PROTOCOL_ERROR)
        if page.has_more and not sequences:
            raise ArchiveError(ErrorCode.REMOTE_PROTOCOL_ERROR)

    @staticmethod
    def _validate_time_ordered_page(
        page: RemoteMessagePage,
        *,
        conversation_id: str,
        after: RemoteMessagePosition | None,
    ) -> None:
        """A reading-order page has to be strictly ascending on the whole key.

        Checked on all four columns, not on ``created_at`` alone: the archive
        is full of seconds holding several messages, and a page that repeated
        or reversed inside one of them would page forever without ever
        tripping a coarser check.
        """
        keys = tuple(
            (item.created_at, item.source_sequence, item.source_local_id, item.record_key)
            for item in page.records
        )
        if any(item.conversation_id != conversation_id for item in page.records):
            raise ArchiveError(ErrorCode.REMOTE_PROTOCOL_ERROR)
        if any(current <= previous for previous, current in zip(keys, keys[1:])):
            raise ArchiveError(ErrorCode.REMOTE_PROTOCOL_ERROR)
        if after is not None and keys:
            boundary = (
                after.created_at,
                after.source_sequence,
                after.source_local_id,
                after.record_key,
            )
            if keys[0] <= boundary:
                raise ArchiveError(ErrorCode.REMOTE_PROTOCOL_ERROR)
        position = page.next_position
        expected = keys[-1] if keys else None
        actual = (
            None
            if position is None
            else (
                position.created_at,
                position.source_sequence,
                position.source_local_id,
                position.record_key,
            )
        )
        if actual != expected:
            raise ArchiveError(ErrorCode.REMOTE_PROTOCOL_ERROR)
        if page.has_more and expected is None:
            raise ArchiveError(ErrorCode.REMOTE_PROTOCOL_ERROR)

    @staticmethod
    def _validate_latest_page(
        page: RemoteMessagePage,
        *,
        conversation_id: str,
        before: RemoteMessagePosition | None,
    ) -> None:
        """A newest-first page: ascending inside, strictly older than the cursor.

        The same four-column key as the forward walk, checked the same way and
        for the same reason -- a page that repeated or reversed inside a shared
        second would page forever without tripping a coarser check. What
        differs is which end the cursor names: here it is the *oldest* record
        already held, so every record on the page sorts below it, and
        ``next_position`` must be this page's own oldest.
        """
        keys = tuple(
            (item.created_at, item.source_sequence, item.source_local_id, item.record_key)
            for item in page.records
        )
        if any(item.conversation_id != conversation_id for item in page.records):
            raise ArchiveError(ErrorCode.REMOTE_PROTOCOL_ERROR)
        if any(current <= previous for previous, current in zip(keys, keys[1:])):
            raise ArchiveError(ErrorCode.REMOTE_PROTOCOL_ERROR)
        if before is not None and keys:
            boundary = (
                before.created_at,
                before.source_sequence,
                before.source_local_id,
                before.record_key,
            )
            if keys[-1] >= boundary:
                raise ArchiveError(ErrorCode.REMOTE_PROTOCOL_ERROR)
        position = page.next_position
        actual = (
            None
            if position is None
            else (
                position.created_at,
                position.source_sequence,
                position.source_local_id,
                position.record_key,
            )
        )
        # 与正序页不同：这里的 next_position 是本页**最老**的那条，而且只在
        # 还有更早记录时才该出现。没有更多却给了一个位置，说明对端在按另一
        # 套规则回答。
        if page.has_more:
            if not keys or actual != keys[0]:
                raise ArchiveError(ErrorCode.REMOTE_PROTOCOL_ERROR)
        elif actual is not None:
            raise ArchiveError(ErrorCode.REMOTE_PROTOCOL_ERROR)

    @staticmethod
    def _validate_recent_conversation_page(
        page: RemoteConversationPage,
        *,
        after: RemoteConversationPosition | None,
    ) -> None:
        """A browse-order page: activity descending, id ascending on a tie.

        Both columns, because the archive is full of conversations whose last
        message landed in the same second, and a page that repeated inside one
        of those would page forever.
        """
        keys: list[tuple[int, str]] = []
        for item in page.conversations:
            if not item.conversation_id:
                raise ArchiveError(ErrorCode.REMOTE_PROTOCOL_ERROR)
            keys.append((item.last_created_at, item.conversation_id))
        for previous, current in zip(keys, keys[1:]):
            if not _browses_after(previous, current):
                raise ArchiveError(ErrorCode.REMOTE_PROTOCOL_ERROR)
        if after is not None and keys:
            boundary = (after.last_created_at, after.conversation_id)
            if not _browses_after(boundary, keys[0]):
                raise ArchiveError(ErrorCode.REMOTE_PROTOCOL_ERROR)
        spot = page.next_position
        actual = None if spot is None else (spot.last_created_at, spot.conversation_id)
        expected = keys[-1] if keys else None
        if actual != expected:
            raise ArchiveError(ErrorCode.REMOTE_PROTOCOL_ERROR)
        if page.has_more and expected is None:
            raise ArchiveError(ErrorCode.REMOTE_PROTOCOL_ERROR)

    def _decode_conversation_position_cursor(
        self,
        cursor: str | None,
        *,
        profile: RemoteDeviceProfile,
        token: DeviceToken,
        scope: str,
    ) -> RemoteConversationPosition | None:
        if cursor is None:
            return None
        position = self._decode_cursor(
            cursor,
            kind=_CONVERSATION_POSITION_CURSOR_KIND,
            profile=profile,
            token=token,
            scope=scope,
        )
        if not isinstance(position, list) or len(position) != 2:
            raise ArchiveError(ErrorCode.CURSOR_INVALID)
        last_created_at, conversation_id = position
        if (
            not isinstance(last_created_at, int)
            or isinstance(last_created_at, bool)
            or last_created_at < 0
            or not isinstance(conversation_id, str)
            or not conversation_id
        ):
            raise ArchiveError(ErrorCode.CURSOR_INVALID)
        return RemoteConversationPosition(
            last_created_at=last_created_at,
            conversation_id=conversation_id,
        )

    def _decode_position_cursor(
        self,
        cursor: str | None,
        *,
        kind: str,
        profile: RemoteDeviceProfile,
        token: DeviceToken,
        scope: str,
    ) -> RemoteMessagePosition | None:
        """A four-column reading-order position, in whichever direction asked.

        ``kind`` is a parameter rather than a constant precisely because the
        two directions must not accept each other's cursors: the shape decodes
        either way, so the *kind* is the only thing standing between a stale
        forward cursor and a silently complementary page.
        """
        if cursor is None:
            return None
        position = self._decode_cursor(
            cursor,
            kind=kind,
            profile=profile,
            token=token,
            scope=scope,
        )
        if not isinstance(position, list) or len(position) != 4:
            raise ArchiveError(ErrorCode.CURSOR_INVALID)
        created_at, source_sequence, source_local_id, record_key = position
        if (
            not isinstance(created_at, int)
            or isinstance(created_at, bool)
            or created_at < 0
            or not isinstance(source_sequence, int)
            or isinstance(source_sequence, bool)
            or source_sequence < 0
            or not isinstance(source_local_id, int)
            or isinstance(source_local_id, bool)
            or source_local_id < 0
            or not isinstance(record_key, str)
            or not record_key
        ):
            raise ArchiveError(ErrorCode.CURSOR_INVALID)
        return RemoteMessagePosition(
            created_at=created_at,
            source_sequence=source_sequence,
            source_local_id=source_local_id,
            record_key=record_key,
        )

    def _decode_conversation_cursor(
        self,
        cursor: str | None,
        *,
        profile: RemoteDeviceProfile,
        token: DeviceToken,
        scope: str | None,
    ) -> str | None:
        if cursor is None:
            return None
        position = self._decode_cursor(
            cursor,
            kind=_CONVERSATION_CURSOR_KIND,
            profile=profile,
            token=token,
            scope=scope,
        )
        if not isinstance(position, str) or not position:
            raise ArchiveError(ErrorCode.CURSOR_INVALID)
        return position

    def _decode_message_cursor(
        self,
        cursor: str | None,
        *,
        profile: RemoteDeviceProfile,
        token: DeviceToken,
        conversation_id: str,
    ) -> int:
        if cursor is None:
            return 0
        position = self._decode_cursor(
            cursor,
            kind=_MESSAGE_CURSOR_KIND,
            profile=profile,
            token=token,
            scope=conversation_id,
        )
        if not isinstance(position, int) or isinstance(position, bool) or position < 0:
            raise ArchiveError(ErrorCode.CURSOR_INVALID)
        return position

    def _encode_cursor(
        self,
        kind: str,
        position: str | int | list[object],
        *,
        profile: RemoteDeviceProfile,
        token: DeviceToken,
        scope: str | None,
    ) -> str:
        document = json.dumps(
            {
                "v": 1,
                "k": kind,
                "p": self._profile_fingerprint(profile),
                "s": scope,
                "c": position,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        signature = hmac.digest(self._cursor_key(token), document, "sha256")
        return f"{self._encode_base64(document)}.{self._encode_base64(signature)}"

    def _decode_cursor(
        self,
        cursor: str,
        *,
        kind: str,
        profile: RemoteDeviceProfile,
        token: DeviceToken,
        scope: str | None,
    ) -> str | int | list[object]:
        try:
            document_part, signature_part = cursor.split(".")
            document = self._decode_base64(document_part)
            signature = self._decode_base64(signature_part)
            expected = hmac.digest(self._cursor_key(token), document, "sha256")
            if not hmac.compare_digest(signature, expected):
                raise ValueError("cursor signature mismatch")
            parsed = json.loads(document.decode("ascii"))
        except (ValueError, binascii.Error, UnicodeError, json.JSONDecodeError):
            raise ArchiveError(ErrorCode.CURSOR_INVALID) from None
        if not isinstance(parsed, dict):
            raise ArchiveError(ErrorCode.CURSOR_INVALID)
        payload = cast(dict[object, object], parsed)
        if set(payload) != {"v", "k", "p", "s", "c"}:
            raise ArchiveError(ErrorCode.CURSOR_INVALID)
        version = payload.get("v")
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version != 1
            or payload.get("k") != kind
            or payload.get("p") != self._profile_fingerprint(profile)
            or payload.get("s") != scope
        ):
            raise ArchiveError(ErrorCode.CURSOR_INVALID)
        position = payload.get("c")
        if not isinstance(position, (str, int, list)) or isinstance(position, bool):
            raise ArchiveError(ErrorCode.CURSOR_INVALID)
        return cast(str | int | list[object], position)

    @staticmethod
    def _profile_fingerprint(profile: RemoteDeviceProfile) -> str:
        return hashlib.sha256(
            "\0".join(
                (profile.base_url, profile.archive_id, profile.device_id)
            ).encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _cursor_key(token: DeviceToken) -> bytes:
        return hashlib.sha256(_CURSOR_DOMAIN + token.value.encode("utf-8")).digest()

    @staticmethod
    def _encode_base64(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

    @classmethod
    def _decode_base64(cls, value: str) -> bytes:
        decoded = base64.b64decode(
            f"{value}{'=' * (-len(value) % 4)}".encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
        if cls._encode_base64(decoded) != value:
            raise ValueError("cursor base64 is not canonical")
        return decoded

    @staticmethod
    def _public_remote_error(error: RemoteArchiveError) -> ArchiveError:
        if error.code == "REMOTE_PROTOCOL_INCOMPATIBLE":
            return ArchiveError(ErrorCode.REMOTE_PROTOCOL_INCOMPATIBLE)
        object_storage = _OBJECT_STORAGE_ERROR_CODES.get(error.code)
        if object_storage is not None:
            return ArchiveError(object_storage)
        if error.code == "NOT_FOUND":
            # 远端答得出这条读，只是它要的那一条不在那里（v9 起：锚点不在这条
            # 会话里）。这与「归档不存在」「协议对不上」都不是一回事，界面据此
            # 回到「这条记录定位不到」而不是「远端坏了」。
            return ArchiveError(ErrorCode.NOT_FOUND)
        if error.code == "REMOTE_HTTP_401":
            return ArchiveError(ErrorCode.REMOTE_AUTH_REQUIRED)
        if error.code == "REMOTE_HTTP_403":
            return ArchiveError(ErrorCode.REMOTE_PERMISSION_DENIED)
        if error.code == "REMOTE_HTTP_404":
            return ArchiveError(ErrorCode.REMOTE_ARCHIVE_NOT_FOUND)
        if error.code in {"NETWORK_UNAVAILABLE", "REMOTE_RETRYABLE"}:
            return ArchiveError(
                ErrorCode.REMOTE_UNAVAILABLE,
                retryable=True,
                recommended_action=RecommendedAction.RETRY,
            )
        return ArchiveError(ErrorCode.REMOTE_PROTOCOL_ERROR)


__all__ = [
    "RemoteAppServices",
    "RemoteArchiveFacade",
    "RemoteClientFactory",
    "RemoteHealthView",
]
