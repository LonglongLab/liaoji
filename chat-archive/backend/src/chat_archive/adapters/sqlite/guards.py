"""两道守卫，装在这套部署的每一条 SQLite 写入路径上（PSU-2.98）。

2026-09-07 23:15 起，远端对**所有设备**写瘫了一个小时：一次会话归并在
`publish_account_identity` 的请求线程里跑飞，握着那把全局写锁不放，于是六个写
入口一起排队；`threading.Lock.__enter__` 无限等，客户端 30 秒超时走人之后，被
弃的线程仍然永远排在队里（Starlette 取消不了 `run_in_threadpool` 的线程），实测
线程数 7 → 16。整整一个小时日志一个字都没有——uvicorn 只在响应完成时打访问
日志，一条跑一小时的请求在日志里根本不存在。

所以这里是两件事，不是一件：

* :class:`BoundedWriterLock` —— 拿不到就认输。等不到就快速 503，让服务端在客户端
  放弃之前先说话；排队的人满了就直接拒，被弃线程再也堆不起来。
* :func:`install_statement_guard` —— 说出**是哪条语句**跑飞了。容器里没有 py-spy、
  `cap_drop` 不给 ptrace、`/proc/<pid>/syscall` 读不到 EPERM，昨晚能用的手段只剩
  `/proc/<pid>/task/*/wchan`，而它说不出 SQL。`set_progress_handler` 能。

同一个裸锁模式原本在三个文件里各写了一份（`metadata.py` 6 处、`blob_state.py`
9 处、`browser_auth.py` 11 处），所以修的是这个模式而不是一个点。
"""

from __future__ import annotations

import inspect
import logging
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable

from chat_archive.ports.metadata import MetadataBusyError

_logger = logging.getLogger(__name__)

# 等一把写锁最多等多久。正常写（一批 500 条记录）服务端 duration 中位数 0.94 s，
# 事故当天观测到的最慢**正常**批次 5.4 s。5 秒覆盖「前面排着一笔正常写」，又远低
# 于客户端那 30 秒——要的就是让服务端在客户端放弃之前先说话。
DEFAULT_ACQUIRE_TIMEOUT_SECONDS = 5.0

# 排到第几个就不再进队。8 ≈ 正常并发写的两倍余量；第 9 个立刻被拒，于是被弃的
# 请求线程有了上限。**这个数不是设备数上限**，但扩到更多设备时要重定它。
DEFAULT_MAX_WAITERS = 8

# 等超过这个数就留一条 WARNING。低于它的排队是正常的背靠背写入，不值一条日志。
_SLOW_WAIT_WARN_SECONDS = 1.0

# 「最近」有多近，用于 `sync-health` 报出的拒绝次数。
DEFAULT_REJECTION_WINDOW_SECONDS = 60.0


class BoundedWriterLock:
    """一把拿不到就认输的写锁。

    形状照 `cache_maintenance.MaintenanceCoordinator.exclusive(timeout=)` ——
    那里已经有过同一种病的先例（存储回收握着锁跑分钟级 VACUUM，创建导入的 HTTP
    线程被钉住到浏览器超时），拿不到就抛一个 `retryable=True` 的错。这里不另起
    炉灶，只是把它做成一个能直接替掉 `threading.Lock` 的对象：26 个
    ``with self._writer_lock`` 一个字都不用改。

    不做的两件事：**不加排队公平性**（FIFO、优先级）——真问题是持锁时间无界，
    加公平性只会让 12 条线程更有秩序地一起超时；**不可重入**——原来那把
    `threading.Lock` 也不可重入，所以今天没有任何一条路径是递归取锁的，
    换成可重入等于悄悄允许一类新写法。
    """

    def __init__(
        self,
        *,
        name: str,
        acquire_timeout: float = DEFAULT_ACQUIRE_TIMEOUT_SECONDS,
        max_waiters: int = DEFAULT_MAX_WAITERS,
        rejection_window: float = DEFAULT_REJECTION_WINDOW_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if acquire_timeout <= 0 or max_waiters <= 0 or rejection_window <= 0:
            raise ValueError("writer lock bounds must be positive")
        self._name = name
        self._acquire_timeout = acquire_timeout
        self._max_waiters = max_waiters
        self._rejection_window = rejection_window
        self._monotonic = monotonic
        self._lock = threading.Lock()
        # 只护 `_waiters` 与 `_rejections` 这两个计数，永远只握着跑几条语句，
        # 所以它自己不需要有界——把「有界」再套一层才是真的说不清。
        self._bookkeeping = threading.Lock()
        self._waiters = 0
        self._held_since: float | None = None
        self._rejections: deque[float] = deque()

    @property
    def held_ms(self) -> int:
        """此刻已经被握了多久（毫秒）；没人握着是 0。

        `sync-health` 报的就是它。读路径不过写锁（健康探针用裸连接），所以在
        写瘫的一小时里探针一直答 200——**没有这个数，「远程忙」在界面上就是一句
        空话**。
        """
        since = self._held_since
        if since is None:
            return 0
        return max(0, int((self._monotonic() - since) * 1000))

    @property
    def waiter_count(self) -> int:
        with self._bookkeeping:
            return self._waiters

    def rejections_recent(self, window: float | None = None) -> int:
        """最近这段时间里被拒了几次。"""
        horizon = self._monotonic() - (self._rejection_window if window is None else window)
        with self._bookkeeping:
            while self._rejections and self._rejections[0] < horizon:
                self._rejections.popleft()
            return len(self._rejections)

    def __enter__(self) -> None:
        # 快路径：没人握着就直接进，不付簿记的钱，也不去问调用方是谁。
        if self._lock.acquire(blocking=False):
            self._held_since = self._monotonic()
            return
        with self._bookkeeping:
            full = self._waiters >= self._max_waiters
            if full:
                self._reject()
            else:
                self._waiters += 1
        if full:
            _logger.warning(
                "%s: %s refused, %d writers are already waiting (REMOTE_BUSY)",
                self._name,
                _calling_entry(),
                self._max_waiters,
            )
            raise MetadataBusyError(
                f"{self._name}: {self._max_waiters} writers are already waiting"
            )
        started = self._monotonic()
        try:
            acquired = self._lock.acquire(timeout=self._acquire_timeout)
        finally:
            with self._bookkeeping:
                self._waiters -= 1
        waited = self._monotonic() - started
        if not acquired:
            with self._bookkeeping:
                self._reject()
            _logger.warning(
                "%s: %s gave up waiting for the writer lock after %.1fs (REMOTE_BUSY)",
                self._name,
                _calling_entry(),
                waited,
            )
            raise MetadataBusyError(
                f"{self._name}: the writer lock was held for longer than "
                f"{self._acquire_timeout:.1f}s"
            )
        if waited >= _SLOW_WAIT_WARN_SECONDS:
            # 入口名是在**这里**才去取的：翻栈不算贵，但一条正常的写每秒会走
            # 好几次，而这条路已经等了一秒以上，多付几微秒无所谓。
            _logger.warning(
                "%s: %s waited %.1fs for the writer lock",
                self._name,
                _calling_entry(),
                waited,
            )
        self._held_since = self._monotonic()

    def __exit__(self, *_exception: object) -> None:
        self._held_since = None
        self._lock.release()

    def _reject(self) -> None:
        """记一次拒绝。调用方必须已经握着 `_bookkeeping`；日志留给它去打，
        免得一个慢的 logging handler 反过来把簿记锁也变成一段无界的等待。"""
        now = self._monotonic()
        horizon = now - self._rejection_window
        while self._rejections and self._rejections[0] < horizon:
            self._rejections.popleft()
        self._rejections.append(now)


def _calling_entry() -> str:
    """哪个写入口在等。只在已经决定要打日志之后才调。

    往上翻两层：`__enter__` 的调用者就是 `with self._writer_lock` 那一行所在的
    方法（`commit_batch` / `publish_account_identity` / …），而那正是运维要看的
    「是哪条路被堵住了」。
    """
    frame = inspect.currentframe()
    for _hop in range(2):
        if frame is None:
            return "<unknown>"
        frame = frame.f_back
    return "<unknown>" if frame is None else frame.f_code.co_qualname


# 单条语句跑多久算慢。超过就留一条 WARNING（带最后一条 SQL 的前缀），
# 语句照跑不误——先看见，再决定要不要拦。
DEFAULT_STATEMENT_WARN_SECONDS = 5.0

# 超过这个就直接中止那条语句（`sqlite3.OperationalError: interrupted`）。
# 只抓「跑飞」级别：现有的 `except Exception: rollback(); raise` 接得住它，
# 而一次被中止的搬家下一趟会重来——比握着全局写锁跑一个小时好。
DEFAULT_STATEMENT_ABORT_SECONDS = 120.0

# 每多少条 VDBE 指令回调一次。回调本身是一次 Python 调用，定得太小会拖慢所有
# 语句；这里只用来抓秒级的东西，所以宁可粗一点。
_PROGRESS_INSTRUCTIONS = 200_000

# 日志里带多长的 SQL。够认出是哪条语句，不至于把整条 IN (…2000 个 ?) 印出来。
_SQL_PREFIX_CHARS = 200

class _StatementClock:
    """一条连接上「此刻在跑哪条语句、在它里面花了多久」。

    `progress_handler` 的回调**拿不到 SQL 文本**，所以 SQL 由
    :class:`StatementGuardConnection` 在每次 execute 时记下来。

    量的是 `busy`——**在 SQLite 里面**的累计时间，不是墙钟。两者的差别不是
    精度问题：懒迭代一个大结果集、每行再去写另一个库，墙钟能到几分钟而
    SQLite 只跑了几秒；按墙钟判，那条语句会被硬上限中止掉。

    「在里面」是**正向标记**出来的，不是靠两次回调的间隔猜的：游标每次从
    `execute` / `fetch*` / `__next__` 返回，就把 `last` 清零，于是下一次回调
    不会把中间那段 Python 时间算进来。曾经用过「间隔小于 0.5 秒才计」的启发式，
    那在它要抓的那种事故里正好失效——2026-09-07 那条 GROUP BY 是磁盘排序溢出、
    加权等待 1 s/s，两次回调隔上几秒是常态，全都会被当成「在别处」丢掉，
    硬上限于是永远到不了。

    这条连接不跨线程共享（每次 `_connect()` 新建），所以不需要锁。
    """

    __slots__ = ("sql", "busy", "last", "running", "warned")

    def __init__(self) -> None:
        self.sql = ""
        self.busy = 0.0
        self.last = 0.0
        self.running = False
        self.warned = False


class StatementGuardConnection(sqlite3.Connection):
    """记住这条连接最后执行的一条 SQL（**不展开绑定参数**）。

    起初这件事是 `set_trace_callback` 做的，那是错的：CPython 交给 trace 回调的
    是 `sqlite3_expanded_sql`，它把每一个绑定参数都展开成字面量——一个 200 KB 的
    blob 会被整个转成十六进制字符串再丢掉。本机实测 `executemany` 500 行 × 200 KB
    是 0.33 s → 2.13 s（6.5 倍），5000 行 × 2.3 KB 是 0.11 s → 0.41 s（3.8 倍），
    而这条守卫装在**每一条**连接上，包括 `commit_batch` 那条热路径。

    子类只做一次切片。参数不参与，所以日志里也不会漏出正文——那是顺带的好处，
    不是主要理由。

    `Connection.execute` 不走被覆盖的 `cursor()`（CPython 实测如此），所以两边
    都要覆盖。
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.statement_clock = _StatementClock()

    # 下面三个都绕道 `self.cursor()`：`Connection.execute()` 自己建的游标是**裸**
    # `sqlite3.Cursor`（本机实测），拿不到工厂，于是 fetch 那一侧就没人标记
    # 「离开了 SQLite」。
    def execute(self, sql: str, parameters: object = (), /) -> sqlite3.Cursor:  # type: ignore[override]
        return self.cursor().execute(sql, parameters)  # type: ignore[arg-type]

    def executemany(self, sql: str, parameters: object, /) -> sqlite3.Cursor:  # type: ignore[override]
        return self.cursor().executemany(sql, parameters)  # type: ignore[arg-type]

    def executescript(self, sql_script: str, /) -> sqlite3.Cursor:
        return self.cursor().executescript(sql_script)

    def cursor(self, factory: object = None) -> sqlite3.Cursor:  # type: ignore[override]
        return super().cursor(_GuardedCursor if factory is None else factory)  # type: ignore[arg-type]


class _GuardedCursor(sqlite3.Cursor):
    """记住 SQL，并在每次从 SQLite 返回时标一下「出来了」。

    `__next__` 也覆盖，因为 `for row in connection.execute(...)` 走的正是它——
    而那恰恰是「循环体很慢」最常见的写法。代价实测：纯迭代 400k 行 0.127 s →
    0.161 s（+27%，约 90 ns/行）。真实循环体里这点比例会塌掉，而换来的是一条
    不会误杀正常语句、也不会在真事故里失灵的硬上限。
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        super().__init__(connection)
        # 取一次就存住。这几个方法里有 `__next__`——每一行都要走一遍，一次
        # `isinstance` 加两级属性查找在 40 万行上是看得见的钱。
        self._clock: _StatementClock | None = getattr(connection, "statement_clock", None)

    def _remember(self, sql: str) -> None:
        clock = self._clock
        if clock is not None:
            clock.sql = sql[:_SQL_PREFIX_CHARS]
            clock.busy = 0.0
            clock.last = 0.0
            clock.running = True
            clock.warned = False

    def _left(self) -> None:
        clock = self._clock
        if clock is not None:
            clock.last = 0.0

    def execute(self, sql: str, parameters: object = (), /) -> sqlite3.Cursor:  # type: ignore[override]
        self._remember(sql)
        try:
            return super().execute(sql, parameters)  # type: ignore[arg-type]
        finally:
            self._left()

    def executemany(self, sql: str, parameters: object, /) -> sqlite3.Cursor:  # type: ignore[override]
        self._remember(sql)
        try:
            return super().executemany(sql, parameters)  # type: ignore[arg-type]
        finally:
            self._left()

    def executescript(self, sql_script: str, /) -> sqlite3.Cursor:
        self._remember(sql_script)
        try:
            return super().executescript(sql_script)
        finally:
            self._left()

    def __next__(self) -> object:
        try:
            return super().__next__()
        finally:
            self._left()

    def fetchone(self) -> object:
        try:
            return super().fetchone()
        finally:
            self._left()

    def fetchmany(self, size: int | None = None) -> list[object]:  # type: ignore[override]
        try:
            return super().fetchmany(self.arraysize if size is None else size)
        finally:
            self._left()

    def fetchall(self) -> list[object]:  # type: ignore[override]
        try:
            return super().fetchall()
        finally:
            self._left()


def install_statement_guard(
    connection: sqlite3.Connection,
    *,
    name: str,
    warn_after: float = DEFAULT_STATEMENT_WARN_SECONDS,
    abort_after: float | None = DEFAULT_STATEMENT_ABORT_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """让这条连接说得出「哪条语句跑飞了」，并给它一个硬上限。

    连接**必须**用 :class:`StatementGuardConnection` 建
    （`sqlite3.connect(factory=)`）：计时靠游标在进出 SQLite 时打的标记，
    没有它守卫连「哪条语句正在跑」都不知道，装了也不会响。不是的话就不装，
    并留一条 WARNING——一个装了却永远不响的守卫比没有守卫更坏。

    ``abort_after=None`` 是给运维命令用的：`settle-merges` 自己**就是**那条长语句，
    守卫防的是请求路径上的跑飞，不是防运维在停机窗口里干活。
    """
    if warn_after <= 0 or (abort_after is not None and abort_after <= warn_after):
        raise ValueError("statement guard thresholds are not ordered")
    if not isinstance(connection, StatementGuardConnection):
        _logger.warning(
            "%s: no statement guard on a connection built without the factory", name
        )
        return
    clock = connection.statement_clock

    def _progress() -> int:
        if not clock.running:
            return 0
        now = monotonic()
        # `last == 0` = 上一次是从 SQLite 里返回、中间那段是调用方的时间，不计。
        # 有值就一定是「上次回调之后一直在 SQLite 里」，全部计入。
        if clock.last:
            clock.busy += now - clock.last
        clock.last = now
        if abort_after is not None and clock.busy >= abort_after:
            _logger.error(
                "%s: aborting a statement after %.0fs inside sqlite: %s",
                name,
                clock.busy,
                clock.sql,
            )
            # 非零 = 中止当前语句。这是这个容器里唯一能拿到「是哪条语句」的手段。
            clock.running = False
            return 1
        if clock.busy >= warn_after and not clock.warned:
            clock.warned = True
            _logger.warning(
                "%s: a statement has spent %.1fs inside sqlite: %s",
                name,
                clock.busy,
                clock.sql,
            )
        return 0

    connection.set_progress_handler(_progress, _PROGRESS_INSTRUCTIONS)


__all__ = [
    "BoundedWriterLock",
    "StatementGuardConnection",
    "DEFAULT_ACQUIRE_TIMEOUT_SECONDS",
    "DEFAULT_MAX_WAITERS",
    "DEFAULT_REJECTION_WINDOW_SECONDS",
    "DEFAULT_STATEMENT_ABORT_SECONDS",
    "DEFAULT_STATEMENT_WARN_SECONDS",
    "install_statement_guard",
]
