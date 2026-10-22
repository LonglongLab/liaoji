"""把会话归并搬出请求路径，交给一条会让路的后台线程（PSU-2.98）。

2026-09-07 23:15，远端对**所有设备**写瘫了一小时。病灶是
`SQLiteMetadataStore._settle_conversation_merges`：它挂在
`publish_account_identity` 上，而那是每一轮同步的第一个写请求；那天它要搬
216,282 行、约 108 批，全程握着那把全局写锁不放。六个写入口于是一起排队，
客户端 30 秒超时走人之后，被弃的线程还永远排在队里。

搬这件事本身没有错——它是「两台机器把同一条会话记成两条」的唯一自愈路径，
理由写在 `settle_conversation_merge_step` 的 docstring 里。换掉的是**执行位置**
（请求内 → 这里）与**边界**（无界 → 一批一放锁）。

**为什么要让路。** `SearchBackfillWorker` 是「追平为止再睡 60 秒」，那对它成立：
它只写 `search.sqlite`，谁也不挡。这条不一样——它握的是同一把写锁、写的是同一个
归档库，不让路就等于把一小时的阻塞摊成一小时的持续争抢。所以搬多久就让多久
（`duty_cycle=0.5`），同步的写请求总有窗口挤进来。搬得慢，好过搬的时候谁都写不进去。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence

from chat_archive.ports.metadata import ConversationMergeStep, MetadataBusyError

_logger = logging.getLogger(__name__)

# 一趟最多推多少批。到了就歇一口气再来，免得一个说谎的判据把线程变成死循环
# ——`RemoteSyncWorker._MAX_ROUNDS_PER_STAGE` 立的是同一条规矩。
_MAX_STEPS_PER_ROUND = 2_000


class ConversationMergeWorker:
    """一条只做有界小段、并且主动让路的后台搬运线程。

    起停的壳照抄 `SearchBackfillWorker`（`start` / `stop` / `run_once`）——
    那一套在 lifespan 里已经跑了几个版本，没有理由换一个形状。让路逻辑是新写的。
    """

    def __init__(
        self,
        *,
        archives: Callable[[], Sequence[str]],
        step: Callable[..., ConversationMergeStep],
        refresh_pending_rows: Callable[[str], int] | None = None,
        duty_cycle: float = 0.5,
        idle_seconds: float = 60.0,
        max_steps_per_round: int = _MAX_STEPS_PER_ROUND,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not 0.0 < duty_cycle <= 1.0:
            raise ValueError("duty cycle must be in (0, 1]")
        if idle_seconds <= 0 or max_steps_per_round <= 0:
            raise ValueError("merge pacing must be positive")
        self._archives = archives
        self._step = step
        self._refresh_pending_rows = refresh_pending_rows
        self._duty_cycle = duty_cycle
        self._idle_seconds = idle_seconds
        self._max_steps = max_steps_per_round
        self._monotonic = monotonic
        self._stop = threading.Event()
        # 有人给账本记了账（账号归并）就叫醒：空闲等待等的是它，不是 sleep。
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def poke(self) -> None:
        """有索引欠着了，下一轮别等空闲周期。线程安全，可从请求路径调。"""
        self._wake.set()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="chat-archive-conversation-merge", daemon=True
        )
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=timeout)

    def run_once(self) -> int:
        """把每个归档推到欠账清零（或推到上限），返回这一趟搬了多少行。

        测试驱动的是这个方法，与 `SearchBackfillWorker` 同一条接缝。
        """
        moved = 0
        for archive_id in self._archives():
            self._refresh(archive_id)
            # 本趟已经放弃的那几条败者会话。判据按行判是自愈的前提，但一对搬完
            # 仍然被判成欠搬的链接（四张表任意一张有残留就会这样）会让这里一直
            # 取到同一对——**不跳过它，同一归档里其余所有对都会被饿死**，
            # 而它们本来是能清完的。
            skip: set[str] = set()
            for _step in range(self._max_steps):
                if self._stop.is_set():
                    return moved
                started = self._monotonic()
                try:
                    result = self._step(archive_id, skip=skip)
                except MetadataBusyError:
                    # 请求路径正忙。它优先——这条线程存在的全部意义就是别挡着它。
                    self._yield(self._monotonic() - started)
                    continue
                moved += result.moved_rows
                idle = (
                    result.conversation_id is None
                    and result.replayed_rows == 0
                    and not result.contended
                )
                if result.done or idle:
                    # `conversation_id is None` 且没有在还账本 = 除去跳过的之后
                    # 没有活干了。还账本的那一趟（PSU-2.101）也没有 conversation_id，
                    # 但它不是没活干。
                    self._refresh(archive_id)
                    break
                if result.contended:
                    self._yield(self._monotonic() - started)
                    continue
                if result.stuck and result.conversation_id is not None:
                    # 搬完、合完，判据仍然说它欠搬。放它一边去做别的——不放的话
                    # 下一步又取到它，同一归档里其余所有对就永远轮不到。
                    skip.add(result.conversation_id)
                    _logger.warning(
                        "%s: conversation %s still owes a move after settling; "
                        "skipping it for this round",
                        archive_id,
                        result.conversation_id,
                    )
                self._yield(self._monotonic() - started)
        return moved

    def _yield(self, worked: float) -> None:
        """搬多久就让多久（`duty_cycle=0.5` 时）。

        `Event.wait` 而不是 `sleep`：停的时候要能立刻停，否则每次优雅关机都要
        白等一个让路窗口。
        """
        if self._duty_cycle >= 1.0:
            return
        self._stop.wait(max(0.0, worked) * (1.0 - self._duty_cycle) / self._duty_cycle)

    def _refresh(self, archive_id: str) -> None:
        if self._refresh_pending_rows is None:
            return
        try:
            self._refresh_pending_rows(archive_id)
        except Exception:
            # 这只是给健康探针看的一个数，数不出来不该把搬运带走。
            _logger.exception("could not count the conversation merge backlog")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                # 搬家塌了不该把服务带走——下一轮再来（欠不欠搬按行判，所以
                # 「下一轮再来」本来就是这条路径的常态）。但必须留下痕迹：一个
                # 安静地永远搬不完的归并，界面会一直显示两个联系人A而没人知道为什么。
                _logger.exception("conversation merge round failed")
            self._wake.wait(self._idle_seconds)
            self._wake.clear()


__all__ = ["ConversationMergeWorker"]
