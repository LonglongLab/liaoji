"""自建远程服务的契约/schema 版本常量。"""

from __future__ import annotations

from chat_archive.ports.metadata import (
    AccountIdentityMetadataStore,
    AccountScopedMetadataStore,
    AnchoredMetadataStore,
    BrowsableMetadataStore,
    ConversationDetailMetadataStore,
    RichMetadataStore,
    RosterMetadataStore,
    TranscriptMetadataStore,
    SearchableMetadataStore,
)

CONTRACT_VERSION = "0.0.1"
SCHEMA_VERSION = "0008"

# 这一版代码**能够**提供的记录模型版本。真正下发的那个数还要过一道
# `record_model_version_of` 的运行时判定，因为最终要服务这条请求的是那个
# store，而不是这个常量。
#
# `schema_version` 不背这份职责：它是 D1 迁移编号，两端各自维护，当前迁移标识为
# "0008"；把「支不支持富字段」压进那个编号，等于让两条互不相干的演进线共用
# 一个数字，第一次分叉就说不清了。缺席即 1——PSU-2.30 之前的远端全都不报这个
# 键，那正是「落后到 v1」的机器可读声明（计划 §7 方案 7 / 决定 D）。
# v3（PSU-2.35a）= 读侧有账号维度：记录/会话带 `source_account_id`，
# 三个查询端点认账号过滤，另有 `query/accounts`。
# v4（PSU-2.39）= 读侧按**本机的浏览语义**答：会话列表最近活跃倒序并接会话名
# 过滤，转录从最新往回分页。同样只是加法——两个查询端点的缺省行为一字未动，
# 上传字节与 v2 相同。
# v5（PSU-2.40）= 账号身份：批次多一段自描述的账号名，`query/accounts` 带
# `display_name`。**这一版改上传字节**——所以客户端必须协商到 5 才附这一段，
# 附给更早的远端会被整批拒绝（对方 `additionalProperties: false`）。
# v6（PSU-2.44）= 读侧的会话身份：一条会话的名字问得到，不必先在列表页里找到它；
# 列表行同时带上最后一条消息的预览。仍然只是加法——上传字节与 v2 相同，既有端点
# 的缺省行为一字未动。
# v7（PSU-2.45）= 读侧的搜索与本机同能力：本机那套索引原样跑在远端上，搜索带
# 游标、总数、每会话计数、索引状态与六个过滤。同样是加法——`indexed` 缺省 false
# 时走的仍是 v1 起就有的那一支，上传字节与 v2 相同。
# v8（PSU-2.47）= 通讯录：这个账号认识谁、谁在哪个群里、他们的头像在哪。
# **这一版改上传字节**（多一条 roster 发布流），所以客户端必须协商到 8 才发
# 那一份；发给更早的远端会被整体拒绝。读侧随之多出会话行与记录行上的头像
# 引用、群成员名单端点，以及扩到 7 列的会话名过滤。
# v9（PSU-2.48）= 读侧的锚点定位：由一条记录的 key 问得到它在阅读顺序上的位置，
# 于是「这条消息的上下文」在远端也取得到（本机自 `/messages/window` 起就有）。
# 又一版纯读侧的加法——上传字节与 v2 相同，既有端点的缺省行为一字未动。
# v10（PSU-2.55）= 账号自己的头像：账号身份那条发布流多一个 `avatar_blob_id`。
# **这一版又改上传字节**，所以客户端必须协商到 10 才带那个键；更早的远端收到的
# 请求体与 v5 起的一字不差。读侧随之在 `query/accounts` 上多出同名的一列。
# v11（PSU-2.86）= 账号自己的微信身份：账号身份那条发布流再多一个 `account_key`
# （裸 wxid），会话那条多一个 `conversation_key`。**又改上传字节**。
# 远端据此把同一个微信在两台机器上的两条上传流认成一个账号——本机的 `acc_…`、
# 微信数据目录名与 `source_instance_id` 全都绑着"这台机器"，两台机器算不出同一个值。
# 读侧一个字段都没加：归并是把 `source_account_id` 这一列的值换掉，不是换口径。
RECORD_MODEL_VERSION = 16


def record_model_version_of(metadata_store: object) -> int:
    """这个 store 真正答得出哪一版记录模型。

    health、进程内 BFF transport 与查询路由的能力守卫都问这同一个判定——
    同一个二进制跑在冻结后端上时必须报旧版，否则客户端会来要一个它答不出
    的键集或过滤。探测按能力协议逐级降：v10/v9 还要能定位锚点，v8 还要能存
    通讯录，v7 还要能带索引地搜，
    v6 还要能单独答一条会话的身份，v5 要能记住账号叫什么，v4 要能按浏览顺序读，
    v3 要能按账号读，
    v2 要能按阅读顺序读，其余都是 1。

    v12/v11/v10/v9 共用最上面那一档，**没有**各自的探测协议：它们加的是账号身份表
    上的一列、两张链接表、以及读答复里的一个键，而协议只看得见方法，看不见表、
    列和键。这一档今天只有
    一个实现（SQLite），建表与这次改动同一个提交，所以「答得出 v9」与「存得下
    头像」「并得了账号」在事实上是同一件事。第二个实现出现时，这里要先长出自己
    的协议再说。
    """
    # v13 有自己的协议，和 v9~v12 不同：那几版加的是表、列与读答复里的键，
    # 协议只看得见方法，探不出来也只是少一个键。v13 加的是一条**发布流**——
    # 答不出它的远端会把整片转写连同 404 退回，而发送方会把那当成网络故障
    # 反复重试。所以这一档必须真的问一句「你收得下转写吗」。
    if (
        isinstance(metadata_store, TranscriptMetadataStore)
        and isinstance(metadata_store, AnchoredMetadataStore)
        and isinstance(metadata_store, RosterMetadataStore)
        and isinstance(metadata_store, SearchableMetadataStore)
    ):
        if getattr(metadata_store, "supports_forward_media_publications", False):
            return RECORD_MODEL_VERSION
        if getattr(metadata_store, "supports_forward_media_enrichment", False):
            return 15
        return 14 if getattr(metadata_store, "supports_forward_media", False) else 13
    if (
        isinstance(metadata_store, AnchoredMetadataStore)
        and isinstance(metadata_store, RosterMetadataStore)
        and isinstance(metadata_store, SearchableMetadataStore)
    ):
        return 12
    if isinstance(metadata_store, RosterMetadataStore) and isinstance(
        metadata_store, SearchableMetadataStore
    ):
        return 8
    if isinstance(metadata_store, SearchableMetadataStore) and isinstance(
        metadata_store, ConversationDetailMetadataStore
    ):
        return 7
    if isinstance(metadata_store, ConversationDetailMetadataStore) and isinstance(
        metadata_store, AccountIdentityMetadataStore
    ):
        return 6
    if isinstance(metadata_store, AccountIdentityMetadataStore):
        return 5
    if isinstance(metadata_store, BrowsableMetadataStore):
        return 4
    if isinstance(metadata_store, AccountScopedMetadataStore):
        return 3
    if isinstance(metadata_store, RichMetadataStore):
        return 2
    return 1
