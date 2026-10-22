"""Bounded, offline HTML pages and versioned ChatLab sidecars for local exports."""

from __future__ import annotations

import hashlib
import html
import json
import shutil
import sqlite3
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol, TextIO, cast
from urllib.parse import quote

from chat_archive.domain.errors import ArchiveError
from chat_archive.image_format import plain_image_format


class HtmlScope(Protocol):
    @property
    def title(self) -> str: ...

    @property
    def account_id(self) -> str: ...

    @property
    def attachment_kinds(self) -> tuple[str, ...]: ...


_STYLE = """
:root{color-scheme:light}*{box-sizing:border-box}body{margin:0;background:#fcf5eb;
color:#211d17;font:15px/1.6 'Microsoft YaHei',sans-serif}main{max-width:920px;margin:auto;
padding:24px}h1{font-size:25px}a{color:#137a3d}header{color:#5b5346;font-size:13px}
article{margin:16px 0;display:flex;flex-direction:column;align-items:flex-start}
article.self{align-items:flex-end}.bubble{padding:10px 14px;background:#fdfaf3;
border:1px solid #ddd3bf;border-radius:5px;max-width:90%;overflow-wrap:anywhere;
white-space:pre-wrap}.self .bubble{background:#dceecf}img{max-width:260px;max-height:300px}
audio,video{max-width:100%}.missing{color:#7a5a1e;background:#faf3e3;padding:8px}
input,button{font:inherit;padding:10px;border:1px solid #ddd3bf;background:#fbf7ef;
border-radius:4px}input{width:100%}.navigation{display:flex;gap:20px;margin:20px 0}
small{color:#5b5346}details{margin:8px 0}summary{cursor:pointer}ul{padding-left:22px}
.result{padding:12px;border-bottom:1px solid #ddd3bf}.result small{display:block}
[hidden]{display:none!important}@media(max-width:500px){main{padding:16px}}
"""
_SCRIPT = """
document.querySelectorAll('input[data-filter]').forEach(input=>input.addEventListener('input',()=>{
const q=input.value.toLocaleLowerCase();document.querySelectorAll('article').forEach(row=>{
row.hidden=!row.textContent.toLocaleLowerCase().includes(q)});}));
"""
_KINDS = {"image": "图片", "emoji": "表情", "audio": "语音", "video": "视频", "file": "文件"}
_TYPES = {"text": 0, "image": 1, "audio": 2, "video": 3, "file": 4, "emoji": 5,
          "link": 7, "quote": 25, "system": 80, "unknown": 99}
_CARD_TYPES = {"link": 7, "file": 4, "location": 8, "transfer": 21, "red_packet": 20,
               "merged_forward": 26, "record": 26, "mini_program": 24, "video_channel": 24, "quote": 25, "channel": 24, "redpacket": 20, "pat": 22, "voip": 23, "business_card": 27}
_EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
               "image/webp": ".webp", "audio/wav": ".wav", "audio/mpeg": ".mp3",
               "video/mp4": ".mp4", "video/quicktime": ".mov", "application/pdf": ".pdf",
               "text/plain": ".txt", "application/zip": ".zip",
               "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
               "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx"}


def _text(value: object) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _json(value: object) -> str:
    # Also safe when embedded in classic script tags; never interpolate raw content.
    return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def _stem(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def _url(value: str) -> str:
    return "/".join(quote(part, safe="") for part in value.split("/"))


def _begin(stream: TextIO, title: str, *, search: bool = False) -> None:
    stream.write('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
                 '<meta name="viewport" content="width=device-width,initial-scale=1">'
                 '<meta name="referrer" content="no-referrer">'
                 f'<title>{_text(title)}</title><style>{_STYLE}</style></head><body><main>'
                 f'<h1>{_text(title)}</h1>')
    if search:
        stream.write('<input data-filter type="search" aria-label="搜索当前页" placeholder="搜索当前页">')


def _finish(stream: TextIO) -> None:
    stream.write(f'</main><script>{_SCRIPT}</script></body></html>')


def _card(row: sqlite3.Row) -> dict[str, object]:
    raw = row["app_card_json"]
    if not raw:
        return {}
    try:
        value: object = json.loads(str(raw))
        return cast(dict[str, object], value) if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def _card_html(card: dict[str, object], depth: int = 0) -> str:
    if not card or depth > 3:
        return ""
    label = card.get("title") or card.get("description") or card.get("kind") or "消息卡片"
    result = f'<div>{_text(label)}</div>'
    items = card.get("items")
    if isinstance(items, list):
        items = cast(list[object], items)
        result += '<details><summary>展开聊天记录</summary>'
        for item in items:
            if not isinstance(item, dict):
                continue
            entry = cast(dict[str, object], item)
            result += f'<p>{_text(entry.get("sender") or entry.get("sender_display_name") or entry.get("sender_name"))} '
            result += _text(entry.get("text") or entry.get("content") or entry.get("content_text") or entry.get("file_name") or entry.get("title") or entry.get("kind")) + '</p>'
            nested = entry.get("nested")
            if isinstance(nested, dict):
                result += _card_html(cast(dict[str, object], nested), depth + 1)
        total = card.get("item_total")
        if isinstance(total, int) and total > len(items):
            result += '<small>归档未保留全部转发条目。</small>'
        result += '</details>'
    return result


class HtmlBundleWriter:
    """One message/page at a time; membership and search are spooled in SQLite."""

    def __init__(self, connection: sqlite3.Connection, root: Path, scope: HtmlScope,
                 *, media_root: Path | None, resolve_media: Callable[[str, str], str | None] | None,
                 allow_source_media_decode: bool = True,
                 decode_source_image: Callable[[Path], bytes | None] | None = None) -> None:
        self.connection = connection
        self.root = root
        self.scope = scope
        self.media_root = media_root.resolve() if media_root is not None else None
        self.resolve_media = resolve_media
        self.allow_source_media_decode = allow_source_media_decode
        self.decode_source_image = decode_source_image
        for folder in ("会话", "附件", "ChatLab", "搜索"):
            (root / folder).mkdir()
        self.spool = sqlite3.connect(root / ".index.sqlite")
        self.spool.execute("CREATE TABLE search (page TEXT, sender TEXT, text TEXT, timestamp INTEGER)")
        self.spool.execute("CREATE TABLE assets (source TEXT PRIMARY KEY, path TEXT, mime TEXT, representation TEXT)")
        self.pages: list[tuple[str, str]] = []
        self.conversation: str | None = None
        self.page: TextIO | None = None
        self.data: TextIO | None = None
        self.page_number = 0
        self.page_count = 0
        self.message_count = 0
        self.conversation_name = ""
        self.page_path = ""
        self.report = {"included": 0, "missing": 0, "excluded": 0}
        self.report_stream = (root / "attachment-report.jsonl").open("w", encoding="utf-8")

    def __enter__(self) -> HtmlBundleWriter:
        return self

    def __exit__(self, *_args: object) -> None:
        if self.page is not None:
            self.page.close()
        if self.data is not None:
            self.data.close()
        self.report_stream.close()
        self.spool.close()
        (self.root / ".index.sqlite").unlink(missing_ok=True)

    def _close_conversation(self) -> None:
        if self.page is not None:
            _finish(self.page)
            self.page.close()
            self.page = None
        if self.data is not None:
            self.data.close()
            self.data = None

    def _open_page(self) -> None:
        self.page_number += 1
        self.page_count = 0
        self.page_path = f"会话/{_stem(str(self.conversation))}-{self.page_number}.html"
        self.page = (self.root / self.page_path).open("w", encoding="utf-8")
        self.pages.append((self.page_path, f"{self.conversation_name} · 第 {self.page_number} 页"))
        _begin(self.page, self.conversation_name, search=True)
        previous = (f'<a href="{_stem(str(self.conversation))}-{self.page_number - 1}.html">上一页</a>'
                    if self.page_number > 1 else "")
        self.page.write(f'<nav class="navigation"><a href="../打开聊天记录.html">全部会话</a>'
                        f'<a href="../搜索聊天记录.html">搜索全部记录</a>{previous}</nav>')

    def add(self, row: sqlite3.Row) -> None:
        conversation = str(row["conversation_id"])
        if conversation != self.conversation:
            self._close_conversation()
            self.conversation = conversation
            meta = self.connection.execute("SELECT display_name, is_group, conversation_uid FROM conversations WHERE id=? AND source_account_id=?",
                                           (conversation, self.scope.account_id)).fetchone()
            self.conversation_name = str(meta["display_name"]) if meta else "聊天记录"
            self.page_number = 0
            self.data = (self.root / "ChatLab" / f"{_stem(conversation)}.jsonl").open("w", encoding="utf-8")
            metadata: dict[str, object] = {"name": self.conversation_name, "platform": "wechat", "type": "group" if meta and meta["is_group"] else "private"}
            if meta and meta["is_group"] and meta["conversation_uid"]:
                metadata["groupId"] = str(meta["conversation_uid"])
            owners = self.connection.execute("SELECT DISTINCT sender_uid FROM messages WHERE conversation_id=? AND is_self=1 AND sender_uid<>'' LIMIT 2", (conversation,)).fetchall()
            if len(owners) == 1:
                metadata["ownerId"] = str(owners[0][0])
            self.data.write(_json({"_type": "header", "chatlab": {"version": "0.0.2", "exportedAt": int(datetime.now(UTC).timestamp()), "generator": "chat-archive"}, "meta": metadata}) + "\n")
            self._open_page()
        elif self.page_count == 500:
            assert self.page is not None
            self.page.write(f'<a href="{_stem(conversation)}-{self.page_number + 1}.html">下一页</a>')
            _finish(self.page)
            self.page.close()
            self._open_page()
        assert self.page is not None and self.data is not None
        sender = str(row["sender_uid"] or "unknown")
        name = str(row["sender_display_name"] or ("我" if row["is_self"] else "未知发送人"))
        # ChatLab JSONL can collect members from message rows. Include accountName
        # on every row instead of interleaving member rows after messages.
        card = _card(row)
        message: dict[str, object] = {"_type": "message", "sender": sender, "accountName": name,
                                      "timestamp": int(row["created_at"]), "type": _CARD_TYPES.get(str(card.get("kind")), _TYPES.get(str(row["message_type"]), 99)),
                                      "content": str(row["content_text"] or "")}
        if row["source_message_id"]:
            message["platformMessageId"] = str(row["source_message_id"])
        if row["reply_to_id"]:
            reply = self.connection.execute("SELECT source_message_id FROM messages WHERE id=? AND source_account_id=? AND conversation_id=?",
                                            (row["reply_to_id"], self.scope.account_id, conversation)).fetchone()
            if reply and reply[0]:
                # Only associate an exported reply when its target is in this export.
                message["replyToMessageId"] = str(reply[0])
        self.data.write(_json(message) + "\n")
        stamp = datetime.fromtimestamp(int(row["created_at"]), timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
        klass = "self" if row["is_self"] else ""
        self.page.write(f'<article class="{klass}" id="m-{_stem(str(row["id"]))}"><header>{_text(name)} · {stamp} UTC+8</header><div class="bubble">{_text(row["content_text"])}')
        if row["reply_to_id"]:
            self.page.write('<small>引用回复：原记录的引用关系已保留；范围外正文不补入。</small>')
        self.page.write(_card_html(card))
        attachments = self.connection.execute("SELECT id,kind,cache_path,thumb_cache_path,cache_status,mime_type,file_name FROM attachments WHERE message_id=? ORDER BY id", (row["id"],))
        for attachment in attachments:
            self.page.write(self._attachment(attachment))
        self.page.write('</div></article>')
        self.spool.execute("INSERT INTO search VALUES(?,?,?,?)", (self.page_path + "#m-" + _stem(str(row["id"])), name, str(row["content_text"] or ""), int(row["created_at"])))
        self.page_count += 1
        self.message_count += 1

    def _target(self, cache_path: str | None) -> Path | None:
        if self.media_root is None or not cache_path:
            return None
        relative = Path(cache_path)
        if relative.is_absolute() or ".." in relative.parts:
            return None
        path = (self.media_root / relative).resolve()
        return path if path.is_relative_to(self.media_root) and path.is_file() else None

    def _attachment(self, row: sqlite3.Row) -> str:
        kind = str(row["kind"])
        identifier = str(row["id"])
        selected = kind in self.scope.attachment_kinds
        if not selected:
            self.report["excluded"] += 1
            self.report_stream.write(_json({"id": identifier, "kind": kind, "status": "excluded"}) + "\n")
            # Video without the original can still use its cached cover.
            cover = self._target(row["thumb_cache_path"]) if kind == "video" else None
            if cover is not None:
                target = self.root / "附件" / f"{_stem(identifier)}-cover{cover.suffix if cover.suffix in {'.jpg', '.png', '.webp'} else '.bin'}"
                shutil.copyfile(cover, target)
                return f'<img alt="视频封面" src="../{_url(target.relative_to(self.root).as_posix())}"><small>视频文件未选择</small>'
            return f'<small>{_KINDS.get(kind, "附件")}未选择携带</small>'
        family = "image" if kind in {"image", "emoji"} else kind
        key = family + ":" + str(row["cache_path"] or row["thumb_cache_path"] or identifier)
        cached = self.spool.execute("SELECT path,mime,representation FROM assets WHERE source=?", (key,)).fetchone()
        if cached is not None and row["cache_status"] in {"ready", "evicted", "discovered"}:
            self.report["included"] += 1
            self.report_stream.write(_json({"id": identifier, "kind": kind, "status": "included", "path": cached[0]}) + "\n")
            return self._attachment_link(kind, str(cached[0]), str(cached[1]), str(row["file_name"] or "打开附件"), str(cached[2]))
        target = self._target(row["cache_path"]) if row["cache_status"] == "ready" else None
        failure = "source_missing"
        if target is None and self.resolve_media is not None and row["cache_status"] in {"ready", "evicted", "discovered"}:
            try:
                target = self._target(self.resolve_media(self.scope.account_id, identifier))
            except (ArchiveError, OSError):
                failure = "unavailable"
        if target is None and kind in {"image", "emoji"}:
            target = self._target(row["thumb_cache_path"])
        if target is None:
            self.report["missing"] += 1
            self.report_stream.write(_json({"id": identifier, "kind": kind, "status": "missing", "reason": failure}) + "\n")
            return f'<div class="missing">{_KINDS.get(kind, "附件")}没能带上，消息记录已保留。</div>'
        mime = str(row["mime_type"] or "").split(";")[0].lower()
        converted: bytes | None = None
        used_thumbnail = target == self._target(row["thumb_cache_path"])
        with target.open("rb") as probe:
            signature = probe.read(16)
        if kind in {"image", "emoji"}:
            # The scope snapshot predates on-demand decoding (also after resume).
            # Reuse the plaintext format detector on a bounded header, never the
            # user-supplied filename or stale MIME of the source container.
            detected = plain_image_format(signature)
            if detected is not None:
                mime = detected[1]
        if signature.startswith(b"RIFF") and signature[8:12] == b"WAVE":
            mime = "audio/wav"
        elif signature.startswith(b"OggS"):
            mime = "audio/ogg"
        elif signature.startswith(b"ID3") or (len(signature) > 1 and signature[0] == 255 and signature[1] & 224 == 224):
            mime = "audio/mpeg" if kind == "audio" else mime
        elif signature[4:8] == b"ftyp":
            mime = "video/mp4" if kind == "video" else mime
        if signature.startswith(b"wxgf"):
            converted = (
                self.decode_source_image(target)
                if self.allow_source_media_decode and self.decode_source_image is not None
                else None
            )
            if converted is None:
                thumbnail = self._target(row["thumb_cache_path"])
                thumbnail_mime: str | None = None
                if thumbnail is not None:
                    with thumbnail.open("rb") as probe:
                        detected = plain_image_format(probe.read(16))
                        if detected is not None:
                            thumbnail_mime = detected[1]
                if thumbnail is not None and thumbnail_mime is not None:
                    target = thumbnail
                    mime = thumbnail_mime
                    used_thumbnail = True
                elif not self.allow_source_media_decode:
                    # Preserve uploaded original bytes without decoding on the
                    # server. The offline reader explains this format honestly.
                    mime = "image/x-wxgf"
                else:
                    self.report["missing"] += 1
                    self.report_stream.write(_json({"id": identifier, "kind": kind, "status": "missing", "reason": "image_decode_unavailable"}) + "\n")
                    return '<div class="missing">微信专用图片暂时无法转换，没能带上。</div>'
            else:
                mime = "image/png"

        suffix = ".wxgf" if mime == "image/x-wxgf" else _EXTENSIONS.get(mime, ".bin")
        # User-supplied names are labels, never paths; no active extension guessing.
        destination = self.root / "附件" / f"{_stem(identifier)}{suffix}"
        try:
            if converted is None:
                shutil.copyfile(target, destination)
            else:
                destination.write_bytes(converted)
        except OSError:
            self.report["missing"] += 1
            self.report_stream.write(_json({"id": identifier, "kind": kind, "status": "missing", "reason": "copy_failed"}) + "\n")
            return '<div class="missing">附件读取失败，没能带上。</div>'
        self.report["included"] += 1
        relative_path = destination.relative_to(self.root).as_posix()
        representation = "thumbnail" if used_thumbnail else "png_static" if converted is not None else "original"
        self.spool.execute("INSERT OR REPLACE INTO assets VALUES(?,?,?,?)", (key, relative_path, mime, representation))
        self.report_stream.write(_json({"id": identifier, "kind": kind, "status": "included", "path": destination.relative_to(self.root).as_posix()}) + "\n")
        return self._attachment_link(kind, relative_path, mime, str(row["file_name"] or "打开附件"), representation)

    @staticmethod
    def _attachment_link(kind: str, relative_path: str, mime: str, file_name: str, representation: str) -> str:
        path = "../" + _url(relative_path)
        if kind in {"image", "emoji"} and mime in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
            note = '<small>微信专用图片已转为 PNG；动画仅保留静态画面。</small>' if representation == "png_static" else '<small>仅携带缩略图。</small>' if representation == "thumbnail" else ''
            return f'<a href="{path}" target="_blank" rel="noreferrer"><img src="{path}" alt="图片，点击放大"></a>{note}'
        if kind == "audio":
            return f'<audio controls preload="none" src="{path}"></audio>'
        if kind == "video":
            return f'<video controls preload="none" src="{path}"></video>'
        return f'<a href="{path}" download="{_text(file_name)}">{_text(file_name)}</a>'

    def finish(self) -> None:
        self._close_conversation()
        self.spool.commit()
        (self.root / "report.json").write_text(_json({**self.report, "message_count": self.message_count}), encoding="utf-8")
        with (self.root / "打开聊天记录.html").open("w", encoding="utf-8") as stream:
            _begin(stream, self.scope.title)
            stream.write('<p>网页和附件需一起解压。双击会话即可阅读；整个文件夹一起移动。</p><nav class="navigation"><a href="搜索聊天记录.html">搜索全部记录</a><a href="导出报告.html">导出报告</a></nav><ul>')
            for path, name in self.pages:
                stream.write(f'<li><a href="{_url(path)}">{_text(name)}</a></li>')
            stream.write('</ul><small>ChatLab 目录中的 JSONL 可供支持该标准的工具导入。媒体播放兼容需单独核对。</small>')
            _finish(stream)
        with (self.root / "导出报告.html").open("w", encoding="utf-8") as stream:
            _begin(stream, "导出报告")
            stream.write(f'<p>消息 {self.message_count} 条；已携带附件 {self.report["included"]} 个；缺失 {self.report["missing"]} 个；未选择 {self.report["excluded"]} 个。</p><p>详情见 attachment-report.jsonl。导出不等于微信备份，不能恢复到微信。</p><a href="打开聊天记录.html">返回聊天记录</a>')
            _finish(stream)
        self._search_files()

    def _search_files(self) -> None:
        cursor = self.spool.execute("SELECT page,sender,text,timestamp FROM search")
        part = 0
        while rows := cursor.fetchmany(500):
            with (self.root / "搜索" / f"{part}.js").open("w", encoding="utf-8") as stream:
                stream.write("window.chatExportChunk=" + _json([list(row) for row in rows]) + ";")
            part += 1
        script = """
const field=document.getElementById('search'), results=document.getElementById('results');
let version=0;field.addEventListener('input',()=>{const run=++version,q=field.value.trim().toLowerCase();
results.replaceChildren();if(!q)return;let i=0,count=0;function next(){if(run!==version||i>=PARTS)return;
const s=document.createElement('script');s.src='搜索/'+(i++)+'.js';s.onload=()=>{const rows=window.chatExportChunk||[];
delete window.chatExportChunk;s.remove();if(run!==version)return;
for(const r of rows){if(!(r[1]+' '+r[2]).toLowerCase().includes(q))continue;
const div=document.createElement('div');div.className='result';const a=document.createElement('a');
a.href=r[0];a.textContent=r[2].slice(0,200)||'非文本消息';div.append(a);const meta=document.createElement('small');
meta.textContent=r[1];div.append(meta);results.append(div);if(++count>=200)break;}
if(count<200)next();else document.getElementById('hint').textContent='先显示 200 条，请输入更精确的关键词';};
s.onerror=()=>{s.remove();document.getElementById('hint').textContent='搜索文件不完整，请重新解压整个包';};document.head.append(s);}next();});
""".replace("PARTS", str(part))
        with (self.root / "搜索聊天记录.html").open("w", encoding="utf-8") as stream:
            _begin(stream, "搜索全部记录")
            stream.write('<a href="打开聊天记录.html">全部会话</a><p>输入关键词，离线搜索本次导出的所有记录。</p><input id="search" type="search" aria-label="搜索全部记录"><p id="hint"></p><div id="results"></div>')
            stream.write(f'<script>{script}</script>')
            _finish(stream)


def write_html_bundle(connection: sqlite3.Connection, root: Path, scope: HtmlScope,
                      chunks: Iterable[list[sqlite3.Row]], *, media_root: Path | None,
                      resolve_media: Callable[[str, str], str | None] | None,
                      on_chunk: Callable[[int], None] | None,
                      on_stage: Callable[[str, int], None] | None = None,
                      allow_source_media_decode: bool = True,
                      decode_source_image: Callable[[Path], bytes | None] | None = None) -> int:
    with HtmlBundleWriter(connection, root, scope, media_root=media_root, resolve_media=resolve_media,
                          allow_source_media_decode=allow_source_media_decode,
                          decode_source_image=decode_source_image) as writer:
        last_report = time.monotonic()
        for chunk in chunks:
            for row in chunk:
                writer.add(row)
                if on_chunk is not None and writer.message_count % 25 == 0 and time.monotonic() - last_report >= 0.5:
                    on_chunk(writer.message_count)
                    last_report = time.monotonic()
            writer.spool.commit()
            if on_chunk is not None:
                on_chunk(writer.message_count)
                last_report = time.monotonic()
        if on_stage is not None:
            on_stage("search", writer.message_count)
        writer.finish()
        return writer.message_count
