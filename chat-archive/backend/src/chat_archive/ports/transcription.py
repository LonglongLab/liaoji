"""语音转写端口。

转写是**本机第一次主动把用户内容发出去**，所以这个端口刻意窄：进去是一段录音
字节加它的内容指纹，出来是三态之一，不暴露供应商、不暴露凭据、不暴露 HTTP。
换引擎只换适配器，上游一行不动。

三态（`OK` / `EMPTY` / `FAILED`）是「诚实展示」在这一摊的具体形态，理由与反例
见 `tests/unit/test_transcription_port.py` 的模块文档。这里只说结论：
**没有「静默跳过」这个返回值**——每一条送进来的录音都会带回一行可落库的结果。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable


#: 低于这个置信度就标「可能有误」。
#:
#: 放在端口而不是各自的实现里，是因为**两侧必须用同一个数**：读侧（气泡）
#: 和同步侧（发给远端）各写一份的话，本机标了远端不标，同一条转写在两个
#: 地方说法不一致。
#:
#: 取 0.8 不是拍脑袋：2026-09-21 那轮实测 30 条的置信度落在 0.72~0.99，
#: 明显转错的那几条（「有尾巴发」「伴长」）都在 0.75 以下，0.85 以上基本都对。
LOW_CONFIDENCE_BELOW = 0.8


@dataclass(frozen=True)
class AutomaticTranscriptionResult:
    """Separate unavailable audio from a failed transcription operation."""

    error_code: str | None = None
    skipped_audio_count: int = 0


class TranscriptionStatus(str, Enum):
    OK = "ok"
    #: 引擎跑完了，但没听出内容。实测存在：音量过低的录音三家引擎全崩。
    EMPTY = "empty"
    #: 没跑成。`failure_code` 说明是哪一种，决定要不要重试。
    FAILED = "failed"


@dataclass(frozen=True)
class TranscriptionRequest:
    """一次转写请求。

    `audio` 用 ``repr=False``：请求会进日志与异常字符串，录音字节不能跟着进去。
    `sha256` 留在 repr 里——它是安全的，而且是出问题时唯一能把一行日志对回
    某条语音的东西。
    """

    audio: bytes = field(repr=False)
    sha256: str
    duration_ms: int | None = None


@dataclass(frozen=True)
class TranscriptionResult:
    status: TranscriptionStatus
    text: str
    engine: str
    engine_version: str
    #: 引擎原样返回的内容。腾讯每段带 ``[0:0.000,0:1.700]``，是以后做「点一句字
    #: 跳到录音那一秒」的原料，在这一层丢掉就再也捡不回来了。
    raw_result: str | None = None
    #: 供界面标「可能有误」。引擎不给就留空，**不要自己编一个数**。
    confidence: float | None = None
    failure_code: str | None = None

    def __post_init__(self) -> None:
        if self.status is TranscriptionStatus.FAILED and not self.failure_code:
            raise ValueError("a failed transcription must carry a failure_code")
        if self.status is not TranscriptionStatus.OK and self.text:
            raise ValueError(f"{self.status.value} result must not carry text")


class Transcriber(Protocol):
    @property
    def engine(self) -> str:
        """落库用的引擎标识，例如 ``tencent:16k_zh_en_2.0``。"""
        ...

    @property
    def engine_version(self) -> str:
        """换了它就意味着旧结果可以被重转；不换就不该重转。"""
        ...

    def transcribe(self, request: TranscriptionRequest) -> TranscriptionResult: ...


@dataclass(frozen=True)
class StoredVoiceTranscript:
    """库里已有的一行转写。"""

    sha256: str
    text: str
    status: TranscriptionStatus
    engine: str
    engine_version: str
    raw_result: str | None = None
    confidence: float | None = None
    failure_code: str | None = None
    duration_ms: int | None = None


@dataclass(frozen=True)
class PendingVoice:
    """一条还没有可用转写的语音，够作业拿去取字节。

    `sha256` 可以是 `None`——语音附件的指纹实测 0/25,500 有值，要等转写那一步
    顺手补上。所以这里不能假定它存在。
    """

    attachment_id: str
    #: 检索索引按消息键，不按附件键——转写要接进检索就得带着它。
    message_id: str
    source_account_id: str
    source_reference: str | None
    sha256: str | None = None
    duration_ms: int | None = None


class TranscriptStore(Protocol):
    """转写作业对档案的全部需求，就这五件。

    窄到这个程度是故意的：作业不该拿到整个 `SQLiteArchiveRepository`，
    否则「只往空里填指纹」这类约束就没有边界可言了。
    """

    def untranscribed_voice_attachments(
        self,
        conversation_id: str | None = None,
        *,
        since: int | None = None,
        source_account_id: str | None = None,
    ) -> list[PendingVoice]: ...

    def set_attachment_sha256_if_absent(
        self, attachment_id: str, sha256: str
    ) -> bool: ...

    def voice_transcript(self, sha256: str) -> StoredVoiceTranscript | None: ...

    def republish_voice_transcript(self, sha256: str) -> None:
        """让这一行已有的转写重新进入「发往远端」的队列，文本一个字不动。

        一条消息借用了别处转好的结果（同一段录音），它自己从没被发出去过，
        而那一行的时间戳早在水位线后面了。
        """
        ...

    def store_voice_transcript(
        self,
        sha256: str,
        result: TranscriptionResult,
        *,
        duration_ms: int | None = None,
        replace: bool = False,
    ) -> None: ...


@dataclass(frozen=True)
class OutboxTranscript:
    """一条要发给远端的转写。

    键是 `record_key`——远端认消息就认它（`remote_record_key(provider,
    canonical_account_id, provider_message_key)`）。**不是 sha256**：那是本机
    「同一段音频只转一次」的键，远端没有附件指纹，拿它过去对不上任何东西。
    """

    record_key: str
    text: str
    #: 哪个引擎写的，如 `tencent:16k_zh_en_2.0`。**照实带过去**，不是装饰：
    #: 远端是两台机器的转写汇合的地方，而覆盖是按 `updated_at` 判的。哪一版
    #: 赢了，事后只能从这一列读出来——重算不出来。
    engine: str
    low_confidence: bool
    #: 本机这一行的 `created_at`，远端用它做「只向前覆盖」。
    source_updated_at: int


@dataclass(frozen=True)
class OutboxTranscriptSlice:
    """一批可发布的转写。

    切片的理由和通讯录一样：一个真实账号有两万多条语音，一次发完的发布
    等于永远发不完。
    """

    transcripts: tuple[OutboxTranscript, ...] = field(default=(), repr=False)
    #: 这一片里最大的 `source_updated_at`。远端收下之后，本机才把水位线推到这里。
    watermark: int = 0

    def is_empty(self) -> bool:
        return not self.transcripts


@runtime_checkable
class TranscriptSource(Protocol):
    """能把「还没发给这个远端的转写」切成片的档案。

    和 `RosterSource` 一样单独成一个协议，而不是塞进 `SyncOutbox`：
    用例里那些内存版 outbox 没有转写可交，让它们长一个出来，
    是让每条用例替一个只有真档案才有的能力买单。
    """

    def transcript_slices(
        self,
        source_account_id: str,
        *,
        archive_id: str,
        device_id: str,
        slice_size: int,
    ) -> tuple[OutboxTranscriptSlice, ...]: ...

    def note_published_transcripts(
        self,
        source_account_id: str,
        *,
        archive_id: str,
        device_id: str,
        watermark: int,
    ) -> None: ...


@runtime_checkable
class TranscriptPublisher(Protocol):
    """能把一片转写交给远端的传输。"""

    def publish_transcripts(
        self,
        archive_id: str,
        source_account_id: str,
        transcripts: OutboxTranscriptSlice,
    ) -> None: ...
