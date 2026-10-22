"""The structured card an app message carries, independent of any parser."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from typing import Literal, cast
from urllib.parse import urlsplit

from chat_archive.domain.forward_media import RemoteMediaRef, media_ref

MAX_NEST_DEPTH = 2
MAX_CARD_BYTES = 256 * 1024

# The hosts a video-channel cover or author avatar may be loaded from, over
# https only. The parser keeps an image only when it is on one of these and
# rewrites it to https; the SPA's img-src admits exactly these. One list, so
# the two cannot drift: a stored image the policy refuses is a card that loses
# its picture without a word. The real archive writes 1,030 of 1,051 covers'
# host as http://wxapp.tc.qq.com, which serves the same bytes over https.
CHANNEL_IMAGE_HOSTS: tuple[str, ...] = (
    "wxapp.tc.qq.com",
    "wx.qlogo.cn",
    "finder.video.qq.com",
    "findernd.video.qq.com",
)

CARD_LABELS: dict[str, str] = {
    "merged_forward": "聊天记录",
    "link": "链接",
    # An app message can carry media as its own subtype. Upstream's text path
    # already names these ([图片: 标题]), so a card that dropped them to the
    # generic label would delete the word from the transcript and from search.
    "image": "图片",
    "voice": "语音",
    "video": "视频",
    "miniprogram": "小程序",
    "channel": "视频号",
    "location": "位置",
    "business_card": "名片",
    "voip": "通话",
    "file": "文件",
    "quote": "引用",
    "transfer": "转账",
    "redpacket": "红包",
    # appmsg type 2001 is not only 红包: the same type carries a group bill,
    # which the archive used to file as a red packet. WeChat draws them as two
    # different things, and one of them is a request for money.
    "group_payment": "群收款",
    "pat": "拍一拍",
    "card": "卡片",
}

# `kind` is the machine identifier that reaches the contract and the frontend;
# this is what a person reads. Keeping them separate is why an item can stay
# "unknown" in the data while the rendered line says nothing it cannot back up.
ITEM_LABELS: dict[str, str] = {
    "text": "文本",
    "image": "图片",
    "video": "视频",
    "voice": "语音",
    "link": "链接",
    "file": "文件",
    "record": "聊天记录",
    "sticker": "动画表情",
    "unknown": "消息",
}

_UNNAMED_SENDER = "未知成员"


def safe_forward_avatar(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 for c in value):
        return None
    try:
        parts = urlsplit(value)
        if (
            parts.scheme not in ("http", "https") or parts.hostname not in CHANNEL_IMAGE_HOSTS
            or parts.port is not None or parts.username is not None or parts.password is not None
            or parts.fragment
        ):
            return None
        return parts._replace(scheme="https").geturl()
    except ValueError:
        return None


@dataclass(frozen=True)
class AppCardItem:
    sender: str | None
    created_at: int | None
    kind: str
    text: str | None
    nested: AppCard | None
    # What the forwarded item's own payload is. A file item carries all three;
    # an image or a video carries the size and often the format but no name.
    # Without them a forwarded file can only be rendered as its filename in
    # prose, which is why WeChat's file card had nothing to draw.
    file_name: str | None = None
    file_size: int | None = None
    file_format: str | None = None
    # WeChat renders this straight after the sender: 联系人C@中国交通建设集团有限公司.
    source_desc: str | None = None
    # The forwarded media item's own content hashes, exactly as WeChat's
    # recordinfo writes them (lowercase hex): fullmd5 for the full-size
    # artifact, thumbfullmd5 for the thumbnail. Kept so the media backfill can
    # locate this item's bytes in WeChat's local Rec cache by content, and so
    # attachment_id can be derived from them. None for items carrying no media.
    full_md5: str | None = None
    thumb_md5: str | None = None
    # The archive attachment this forwarded media item resolves to, stamped by
    # stamp_attachment_ids once the owning message's id is known. It is a
    # deterministic function of (message_id, kind, full_md5|thumb_md5), so
    # import, replay and the backfill all produce the same value and a replay
    # never clears an id the backfill wrote. None until stamped, and for items
    # carrying no media. A dangling id (no such attachment row) is expected on
    # any end that has the record but not the bytes -- readers fall to a
    # placeholder rather than treating it as an error.
    attachment_id: str | None = None
    remote_media: RemoteMediaRef | None = None
    avatar_url: str | None = None
    avatar_ref: str | None = None
    avatar_blob_id: str | None = None
    reply: AppCard | None = None


@dataclass(frozen=True)
class AppCard:
    kind: str
    title: str
    subtitle: str | None = None
    source_name: str | None = None
    url: str | None = None
    items: tuple[AppCardItem, ...] = ()
    item_total: int = 0
    truncated: bool = False
    # For a quote whose target was itself a card: the target, parsed. subtitle
    # keeps the flat one-line summary, so anything reading text is unaffected;
    # this is what lets a quoted chat record be opened instead of shown as its
    # title alone. None whenever the quoted message was ordinary prose or media
    # — its summary is already the whole of what the source carries.
    quoted: AppCard | None = None
    # Transfer fields are source strings rather than numbers: feedesc includes
    # the source currency symbol and formatting, and the archive must not
    # silently reinterpret it.
    transfer_amount: str | None = None
    transfer_memo: str | None = None
    # paysubtype 1/3/4 are the started/received/refunded messages of one
    # transfer, paired by transcationid; see the adapter for how a started
    # transfer inherits the state its settling message recorded.
    transfer_status: Literal["received", "pending", "refunded", "unknown"] | None = None
    # Epoch seconds. transfer_time is the source begintransfertime;
    # transfer_settled_at is the settling message's own timestamp, because the
    # source records no received or refunded time of its own.
    transfer_time: int | None = None
    transfer_settled_at: int | None = None
    # 源里指名的两端，wcpayinfo/payer_username 与 receiver_username 原样搬过来
    # （归一见 appmsg._appmsg_card）。这里存标识而不是名字，有两条硬理由:
    #
    #   * replay 的约束是「没有源可读」(replay.py 开篇)。名字要查花名册，
    #     回填出来就全是 None——等于给存量档案做了个空功能。
    #   * 名字会变。冻在 2019 年那条转账上的那一份会永远是旧的。
    #
    # null 不含糊，因为 transfer_status 已经说了消息发送者是哪一边:
    # pending 那半条的付款方就是发送者，received/refunded 那半条的收款方就是
    # 发送者。剩下那一边单聊是会话对面，群聊里源没写就是没写。
    transfer_payer_uid: str | None = None
    transfer_payee_uid: str | None = None
    # 这条消息自己的发送者站在转账的哪一边，读的是 paysubtype：
    # 1（发起）那半条是付款方发的，3/4（已收款/已退还）那半条是收款方发的。
    #
    # **不能拿 transfer_status 反推。** 一笔转账结清之后 `settle_transfers` 会把
    # 发起那半条也提升成 received，于是两半条的状态一模一样；按状态推会把发起
    # 那半条读成「发送者是收款方」，正好反过来。真机上确认过这一点：一条
    # paysubtype=1、对方发给我的转账，卡片上存的就是 received。
    #
    # 单聊只有发起那半条写了对方标识（见 _transfer_parties），所以那 960 条要
    # 靠这一格才定得了向；源里两端全空的 5 条发起消息更是只剩这一格可读。
    transfer_sender_role: Literal["payer", "payee"] | None = None
    # The wcpayinfo block of an appmsg type 2001, which covers both 红包 and
    # 群收款. Measured on this machine's own archive: 368 such messages, 363 of
    # them red packets and 5 group bills.
    #
    # There is deliberately no "opened" field and no amount field, and neither
    # is a gap in the parsing — both were looked for and are not on this machine:
    #
    #   * The message has no amount. A transfer carries feedesc; a red packet
    #     carries nothing of the sort, because its amount is meant to stay
    #     hidden until it is opened and each share of a group packet differs.
    #   * Nothing pairs a red packet with its outcome the way a transfer's
    #     settling message does: every paymsgid occurs exactly once (348 of 348)
    #     and the archive holds no 领取 notice of any kind.
    #   * WeChat's own general.db has a redEnvelopeTable, and its nine columns
    #     do include hb_status and receive_status — but no amount column at all,
    #     and it held a single row for the account that sent 368 red packets.
    #     It is a short-lived UI cache, not a ledger. transferTable is the same
    #     shape: payer, receiver, times, no amount.
    #
    # Both numbers live on WeChat's servers, which its client re-fetches every
    # time someone opens the detail page. Anything here would be a guess.
    #
    # greeting is receivertitle, which equalled sendertitle in all 368 messages;
    # it is the line WeChat prints on the packet itself, and it was the whole of
    # what this archive used to throw away — 154 distinct blessings that no
    # search could reach.
    packet_greeting: str | None = None
    # scenetext: 微信红包 / 活动账单 / 群收款. WeChat's own footer line.
    packet_scene: str | None = None
    # corpname: whose custom cover the packet wears (42 messages, 22 brands).
    packet_cover: str | None = None
    # senderdes / receiverdes: 查看红包 vs 领取红包 — the source keeps one line
    # for each side, and which one applies depends on who is reading. A group
    # bill writes the same line into both, and that line is the only place its
    # amount appears at all (每人需支付94.67元).
    packet_sender_action: str | None = None
    packet_receiver_action: str | None = None
    # exclusive_recv_username: a 专属红包, addressed to one member of a group.
    packet_exclusive: bool = False
    # invalidtime, epoch seconds. When it stops being openable — not evidence
    # about whether anyone opened it.
    packet_expires_at: int | None = None
    # Optional source metadata for video-channel shares, not archived media.
    cover_url: str | None = None
    avatar_url: str | None = None
    duration_seconds: int | None = None
    media_width: int | None = None
    media_height: int | None = None


# The forwarded item kinds whose bytes an attachment can hold. A record is a
# nested forward, not a file; text/link/voice/sticker/unknown carry no locatable
# media in this archive's Rec-cache path.
_MEDIA_ITEM_KINDS = frozenset({"image", "video", "file", "sticker"})


def _attachment_id_for(message_id: str, kind: str, discriminator: str) -> str:
    """The deterministic attachment id for one forwarded media item.

    Mirrors the shape of the parser's own `_opaque_id("att", …)` (att_ + the
    first 24 hex of a NUL-joined sha256) so a forwarded item's attachment reads
    like any other attachment id -- but folds the item's own content hash in as
    a third part. Without it every media item of one kind on the same forward
    would collapse to a single id (the parser keys a normal attachment on
    message id + kind alone), and N forwarded images would share one row.
    """
    logical = "\x1f".join((message_id, kind, discriminator))
    digest = hashlib.sha256(logical.encode("utf-8")).hexdigest()[:24]
    return f"att_{digest}"


def stamp_attachment_ids(card: AppCard, message_id: str) -> AppCard:
    """Stamp every forwarded media item with its attachment id, nested included.

    Pure and deterministic in (card, message_id): the import path, a replay and
    the backfill command all call it and get byte-for-byte the same ids, so a
    replay that re-derives the card from raw bytes can never clear an id the
    backfill wrote. Idempotent -- stamping an already-stamped card is a no-op.

    Nested records used to keep None, on the reasoning that the Rec-cache path
    covered the outermost datalist only. Measured on this machine's own archive,
    that reasoning cost real pictures: 280 media items sit inside nested records
    and **87 of them have their bytes in the Rec cache right now** -- they drew
    an "未保存媒体引用" placeholder purely because nothing had given them an id
    to be found by. The id is a function of (message_id, kind, content md5), so
    recursing cannot change an id already stamped at the top level, and two
    copies of one picture at different depths collapse to one attachment, which
    is what they are.
    """
    if card.kind != "merged_forward" or not card.items:
        return card
    changed = False
    stamped: list[AppCardItem] = []
    for item in card.items:
        updated = item
        discriminator = item.full_md5 or item.thumb_md5
        if item.kind in _MEDIA_ITEM_KINDS and discriminator is not None:
            new_id = _attachment_id_for(message_id, item.kind, discriminator)
            if new_id != item.attachment_id:
                updated = replace(updated, attachment_id=new_id)
        if item.nested is not None:
            # The parser bounds nesting at MAX_NEST_DEPTH, so this terminates on
            # anything it produced; `is not` is the change signal because the
            # recursive call returns the same object when nothing moved.
            nested = stamp_attachment_ids(item.nested, message_id)
            if nested is not item.nested:
                updated = replace(updated, nested=nested)
        if updated is not item:
            changed = True
        stamped.append(updated)
    return replace(card, items=tuple(stamped)) if changed else card


def _item_label(kind: str) -> str:
    return ITEM_LABELS.get(kind, ITEM_LABELS["unknown"])


def _epoch_or_none(value: object) -> int | None:
    """Accept a stored epoch second, refusing anything that is not one.

    `type(...) is int` rather than isinstance because bool subclasses int: a
    decoded `true` must not become a timestamp. Negative values are refused
    too — the source writes zero for "no time", and 1970 on a card is noise.
    """
    return value if type(value) is int and 0 < value < 2**63 else None


def _one_line(value: str) -> str:
    """Flatten a value onto one line.

    Every consumer of this text reads it a line at a time, so this has to be
    applied to *every* field that reaches it. A multi-line WeChat message is
    ordinary, and a newline inside an item's text otherwise makes the body
    claim speakers the record does not contain — with a leading two-space
    indent even reading as a nested forward.
    """
    return " ".join(value.split())


def _render_items(items: tuple[AppCardItem, ...], indent: int) -> list[str]:
    prefix = "  " * indent
    lines: list[str] = []
    for item in items:
        body = item.text if item.text is not None else f"[{_item_label(item.kind)}]"
        sender = _one_line(item.sender) if item.sender else _UNNAMED_SENDER
        lines.append(f"{prefix}{sender}：{_one_line(body)}")
        if item.reply is not None and item.reply.subtitle:
            lines.append(f"{prefix}  引用：{_one_line(item.reply.subtitle)}")
        if item.nested is not None:
            lines.extend(_render_items(item.nested.items, indent + 1))
    return lines


def _packet_lines(card: AppCard, head: str, title: str) -> list[str]:
    """The searchable text of a 红包 or a 群收款.

    The blessing is already in `head` — the parser puts it in `title`, because
    it is what the message is about and what someone would search for.

    The action line is included only when both sides carry the same one. That
    is the difference between a button and a fact: a red packet writes
    查看红包 for the sender and 领取红包 for the receiver, so neither belongs in
    a transcript; a group bill writes 每人需支付94.67元 into both, and that is
    the only place the amount is recorded at all.
    """
    lines = [head]
    scene = _one_line(card.packet_scene) if card.packet_scene else None
    if scene and scene != title:
        lines.append(scene)
    sender, receiver = card.packet_sender_action, card.packet_receiver_action
    if sender and sender == receiver:
        lines.append(_one_line(sender))
    if card.packet_exclusive:
        lines.append("专属红包")
    if card.packet_cover:
        lines.append(f"封面：{_one_line(card.packet_cover)}")
    return lines


def render_card_text(card: AppCard) -> str:
    """Build the archive's own content_text for a card.

    Deliberately free of upstream's `sha:` / `md5:` / `↳svrid:` anchors: those
    exist for LLM recall, and this string is what a person reads and what
    search indexes.

    Must be called on the *untruncated* card. Rendering a size-limited or
    decoded card would print `· N条` above fewer than N lines — a header
    stating something the lines below it do not support.
    """
    label = CARD_LABELS.get(card.kind, CARD_LABELS["card"])
    title = _one_line(card.title)
    # The parser falls the title back to the label when the XML carries none,
    # so repeating it here would render "[文件] 文件" into the search index.
    head = f"[{label}]" if title in ("", label) else f"[{label}] {title}"
    if card.kind == "pat":
        return title or CARD_LABELS["pat"]
    if card.kind == "merged_forward":
        return "\n".join([f"{head} · {card.item_total}条", *_render_items(card.items, 0)])
    if card.kind == "transfer":
        amount = _one_line(card.transfer_amount) if card.transfer_amount else None
        lines = [f"[转账] {amount}" if amount else head]
        if card.transfer_memo:
            lines.append(_one_line(card.transfer_memo))
        status = {
            "received": "已收款",
            "pending": "待收款",
            "refunded": "已退还",
            "unknown": "状态未知",
        }.get(card.transfer_status or "")
        if status is not None:
            lines.append(status)
        return "\n".join(lines)
    if card.kind in ("redpacket", "group_payment"):
        return "\n".join(_packet_lines(card, head, title))
    lines = [head]
    # Truthiness, not `is not None`: a decoded card can carry "", and "来源："
    # with nothing after it asserts a source the card does not have.
    if card.subtitle:
        lines.append(_one_line(card.subtitle))
    if card.source_name:
        lines.append(f"来源：{_one_line(card.source_name)}")
    if card.url and card.kind != "channel":
        lines.append(_one_line(card.url))
    return "\n".join(lines)


def card_to_json(card: AppCard) -> str:
    payload = asdict(card)

    def strip_empty(value: object) -> None:
        if isinstance(value, dict):
            value = cast(dict[str, object], value)
            if value.get("remote_media") is None:
                value.pop("remote_media", None)
            for key in ("avatar_ref", "avatar_blob_id"):
                if value.get(key) is None:
                    value.pop(key, None)
            for child in value.values():
                strip_empty(child)
        elif isinstance(value, (list, tuple)):
            for child in cast("list[object] | tuple[object, ...]", value):
                strip_empty(child)

    strip_empty(payload)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _json_size(card: AppCard) -> int:
    return len(card_to_json(card).encode("utf-8"))


def _clip(value: str, budget: int) -> str:
    """Cut a string to a byte budget without splitting a character."""
    encoded = value.encode("utf-8")[:budget]
    return encoded.decode("utf-8", errors="ignore")


def _fit_items(card: AppCard) -> AppCard | None:
    """Keep as many leading items as actually fit, or None if none do.

    Binary search rather than a chunked scan: the scan overshot by ~5%, and the
    cap is a bound, not a licence to drop content that had room. It also probes
    ~8 times instead of ~66, and each probe re-serialises the whole card.
    At least one item is always dropped, so `truncated` is never a lie.
    """
    low, high = 0, len(card.items) - 1
    while low < high:
        middle = (low + high + 1) // 2
        if _json_size(replace(card, items=card.items[:middle], truncated=True)) <= MAX_CARD_BYTES:
            low = middle
        else:
            high = middle - 1
    if low <= 0:
        return None
    return replace(card, items=card.items[:low], truncated=True)


def _clip_title_to_fit(card: AppCard) -> AppCard:
    """Clip `title` until the encoded card fits, by binary search.

    The budget cannot be computed from the source string: the cap is measured
    on the JSON, and escaping turns one `"` into two bytes and one NUL into
    six, so a title of quotes is twice the size its characters suggest.
    Searching on the encoded size measures the thing being bounded instead of
    predicting it.
    """
    high = len(card.title.encode("utf-8"))
    low = 0
    while low < high:
        middle = (low + high + 1) // 2
        if _json_size(replace(card, title=_clip(card.title, middle))) <= MAX_CARD_BYTES:
            low = middle
        else:
            high = middle - 1
    return replace(card, title=_clip(card.title, low))


def enforce_size_limit(card: AppCard) -> AppCard:
    """Bound the stored card to MAX_CARD_BYTES, cheapest loss first.

    item_total keeps the real count, so the UI can say how many are missing
    rather than quietly presenting a short list as the whole record.

    Assumes `kind` is one of the parser's fixed identifiers. If `kind` alone
    exceeded the cap the result would still be over it, but clipping an
    identifier would mislabel the card rather than shorten it.
    """
    if _json_size(card) <= MAX_CARD_BYTES:
        return card
    fitted = _fit_items(card)
    if fitted is not None:
        return fitted
    # Every item is gone and it still does not fit. Title and subtitle come
    # from the same uncontrolled XML and are not length-bounded either, so give
    # them up one at a time — a 300 KB subtitle is what blew the budget, and
    # discarding a 25-byte url to make room that was never contended would
    # leave the card poorer than the text it replaces.
    bare = replace(card, items=(), truncated=True)
    if _json_size(bare) <= MAX_CARD_BYTES:
        return bare
    # A quoted card is a whole card of someone else's making, so it is the next
    # thing large enough to be worth giving up — and subtitle still says in one
    # line what it was.
    without_quote = replace(bare, quoted=None)
    if _json_size(without_quote) <= MAX_CARD_BYTES:
        return without_quote
    without_subtitle = replace(without_quote, subtitle=None)
    if _json_size(without_subtitle) <= MAX_CARD_BYTES:
        return without_subtitle
    # The packet strings come from the same uncontrolled XML and are no more
    # length-bounded than subtitle was. title still carries the blessing, so
    # giving them up costs the detail panel, not the transcript line.
    stripped = replace(
        without_subtitle,
        source_name=None,
        url=None,
        cover_url=None,
        avatar_url=None,
        packet_greeting=None,
        packet_scene=None,
        packet_cover=None,
        packet_sender_action=None,
        packet_receiver_action=None,
    )
    if _json_size(stripped) <= MAX_CARD_BYTES:
        return stripped
    clipped = _clip_title_to_fit(stripped)
    if _json_size(clipped) <= MAX_CARD_BYTES:
        return clipped
    # Only reachable through card_from_json, which can hand back a `kind` no
    # parser would emit. Clipping an identifier mislabels the card, so keep
    # just enough of it to stay recognisable and give up everything else.
    return AppCard(kind=_clip(card.kind, 64), title="", truncated=True)


def _as_mapping(payload: object) -> dict[str, object] | None:
    if not isinstance(payload, dict):
        return None
    return cast("dict[str, object]", payload)


def _item_from_mapping(entry: object, depth: int) -> AppCardItem | None:
    payload = _as_mapping(entry)
    if payload is None:
        return None
    kind = payload.get("kind")
    if not isinstance(kind, str):
        return None
    sender = payload.get("sender")
    created_at = payload.get("created_at")
    text = payload.get("text")
    nested = payload.get("nested")
    file_name = payload.get("file_name")
    file_size = payload.get("file_size")
    file_format = payload.get("file_format")
    source_desc = payload.get("source_desc")
    full_md5 = payload.get("full_md5")
    thumb_md5 = payload.get("thumb_md5")
    attachment_id = payload.get("attachment_id")
    avatar_url = payload.get("avatar_url")
    return AppCardItem(
        sender=sender if isinstance(sender, str) else None,
        # `type(...) is int` rather than isinstance: bool subclasses int, so an
        # isinstance check would let `true` through into a field the contract
        # declares as an integer.
        created_at=created_at if type(created_at) is int else None,
        kind=kind,
        text=text if isinstance(text, str) else None,
        nested=(
            _card_from_mapping(nested, depth + 1)
            if nested is not None and depth < MAX_NEST_DEPTH
            else None
        ),
        file_name=file_name if isinstance(file_name, str) else None,
        file_size=file_size if type(file_size) is int and file_size >= 0 else None,
        file_format=file_format if isinstance(file_format, str) else None,
        source_desc=source_desc if isinstance(source_desc, str) else None,
        full_md5=full_md5 if isinstance(full_md5, str) else None,
        thumb_md5=thumb_md5 if isinstance(thumb_md5, str) else None,
        attachment_id=attachment_id if isinstance(attachment_id, str) else None,
        remote_media=media_ref(payload.get("remote_media")),
        avatar_url=safe_forward_avatar(avatar_url),
        avatar_ref=(value if isinstance(value := payload.get("avatar_ref"), str) else None),
        avatar_blob_id=(value if isinstance(value := payload.get("avatar_blob_id"), str) else None),
        reply=(
            _card_from_mapping(payload.get("reply"), depth + 1)
            if payload.get("reply") is not None and depth <= MAX_NEST_DEPTH else None
        ),
    )


def _card_from_mapping(entry: object, depth: int) -> AppCard | None:
    payload = _as_mapping(entry)
    if payload is None:
        return None
    kind = payload.get("kind")
    title = payload.get("title")
    if not isinstance(kind, str) or not isinstance(title, str):
        return None
    raw_items = payload.get("items")
    items: list[AppCardItem] = []
    if isinstance(raw_items, list):
        for element in cast("list[object]", raw_items):
            item = _item_from_mapping(element, depth)
            if item is not None:
                items.append(item)
    subtitle = payload.get("subtitle")
    source_name = payload.get("source_name")
    url = payload.get("url")
    item_total = payload.get("item_total")
    quoted = payload.get("quoted")
    transfer_amount = payload.get("transfer_amount")
    transfer_memo = payload.get("transfer_memo")
    transfer_status = payload.get("transfer_status")
    safe_transfer_status = cast(
        'Literal["received", "pending", "refunded", "unknown"] | None',
        transfer_status
        if transfer_status in {"received", "pending", "refunded", "unknown"}
        else None,
    )
    transfer_time = payload.get("transfer_time")
    transfer_settled_at = payload.get("transfer_settled_at")
    transfer_payer_uid = payload.get("transfer_payer_uid")
    transfer_payee_uid = payload.get("transfer_payee_uid")
    transfer_sender_role = payload.get("transfer_sender_role")
    safe_sender_role = cast(
        'Literal["payer", "payee"] | None',
        transfer_sender_role if transfer_sender_role in {"payer", "payee"} else None,
    )
    packet_greeting = payload.get("packet_greeting")
    packet_scene = payload.get("packet_scene")
    packet_cover = payload.get("packet_cover")
    packet_sender_action = payload.get("packet_sender_action")
    packet_receiver_action = payload.get("packet_receiver_action")
    packet_expires_at = payload.get("packet_expires_at")
    cover_url = payload.get("cover_url")
    avatar_url = payload.get("avatar_url")
    return AppCard(
        kind=kind,
        title=title,
        subtitle=subtitle if isinstance(subtitle, str) else None,
        source_name=source_name if isinstance(source_name, str) else None,
        url=url if isinstance(url, str) else None,
        items=tuple(items),
        # Bounded by the same depth budget as `nested`, and for the same
        # reason: this module's mutual recursion runs out of stack long before
        # json.loads gives up on a deeply nested payload.
        quoted=(
            _card_from_mapping(quoted, depth + 1)
            if quoted is not None and depth < MAX_NEST_DEPTH
            else None
        ),
        # What the writer recorded, not what survived decoding: telling the UI
        # the surviving items are the whole record would be a quiet lie.
        # Bounded so a corrupted count cannot make card_to_json raise on the
        # way back out: int -> str refuses past 4300 digits.
        item_total=item_total if type(item_total) is int and 0 <= item_total < 2**63 else len(items),
        truncated=payload.get("truncated") is True,
        transfer_amount=transfer_amount if isinstance(transfer_amount, str) else None,
        transfer_memo=transfer_memo if isinstance(transfer_memo, str) else None,
        transfer_status=safe_transfer_status,
        transfer_time=_epoch_or_none(transfer_time),
        transfer_settled_at=_epoch_or_none(transfer_settled_at),
        transfer_payer_uid=transfer_payer_uid if isinstance(transfer_payer_uid, str) else None,
        transfer_payee_uid=transfer_payee_uid if isinstance(transfer_payee_uid, str) else None,
        transfer_sender_role=safe_sender_role,
        packet_greeting=packet_greeting if isinstance(packet_greeting, str) else None,
        packet_scene=packet_scene if isinstance(packet_scene, str) else None,
        packet_cover=packet_cover if isinstance(packet_cover, str) else None,
        packet_sender_action=(
            packet_sender_action if isinstance(packet_sender_action, str) else None
        ),
        packet_receiver_action=(
            packet_receiver_action if isinstance(packet_receiver_action, str) else None
        ),
        packet_exclusive=payload.get("packet_exclusive") is True,
        packet_expires_at=_epoch_or_none(packet_expires_at),
        cover_url=cover_url if isinstance(cover_url, str) else None,
        avatar_url=avatar_url if isinstance(avatar_url, str) else None,
        duration_seconds=_epoch_or_none(payload.get("duration_seconds")),
        media_width=_epoch_or_none(payload.get("media_width")),
        media_height=_epoch_or_none(payload.get("media_height")),
    )


def card_from_json(payload: str) -> AppCard | None:
    try:
        decoded = json.loads(payload)
    except (ValueError, RecursionError):
        # Deeply nested junk exhausts the decoder's stack rather than failing
        # its grammar; both are "this is not a card".
        return None
    # Depth 1 because the payload itself is the outermost card. Nothing here
    # ever writes deeper than MAX_NEST_DEPTH, so anything deeper is not a card
    # this archive produced — and decoding it would run this module's own
    # mutual recursion out of stack well before json.loads gives up.
    return _card_from_mapping(decoded, 1)
