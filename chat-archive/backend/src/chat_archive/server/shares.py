"""在线分享的存储（PSU-2.180）：一份生成那一刻的副本，住在 `shares.sqlite`。

**只有自建 SQLite 这一支有它。** 分享页要挂在别人打得开的地方，本机只听回环；
postgres-s3 那一支暂不支持，Worker 那一支没有文件系统也没有长任务。不属于本端的
不注册路由（404，不是 403），见 `local-remote-boundary.md`。

存的东西刻意少：

- 快照是客户端组好的（2.181），隐藏名字在客户端就做掉了——服务器上从来没有真名；
- 访问者**什么都不记**：没有 IP、没有设备、没有 UA，只有打开次数与最近一次的时刻；
- 附件是**副本**，住在自己的前缀 `a/_shares/` 下（`SHARE_OBJECT_SCOPE`），与归档
  自己的附件不共享：删分享只删它自己的对象，归档里的原件一个字节不动。
  不放进归档那个前缀有两条理由——`reconcile` 按归档前缀列对象，副本会被报成
  `orphaned_in_store`；而预签名地址里带着对象键，放进去就等于把归档 id 递给了读者。

令牌是 `secrets.token_urlsafe(16)`：128 位随机，它就是这份分享唯一的钥匙
（用户点定「只要链接、不要提取码」）。
"""

from __future__ import annotations

import re
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

SHARES_DATABASE = "shares.sqlite"
# 分享副本的对象前缀。以下划线开头，而 `chat-archive-server provision` 拒收这个 id，
# 所以它永远不会和某个归档撞名。
SHARE_OBJECT_SCOPE = "_shares"

STATE_DRAFT = "draft"
STATE_LIVE = "live"

# 过期的在「我的分享」里再留 7 天，可以「重新开」；过了才真删（原型第 4 块）。
EXPIRED_GRACE_SECONDS = 7 * 24 * 3600
# 建了草稿却一小时都没定稿＝上传中断了。留着只会占对象存储。
STALE_DRAFT_SECONDS = 3600

# `token_urlsafe(16)` 恰好 22 个字符。不合这个形状的一律当作不存在——
# 连库都不必查，也就没有「形状对不对」这一条可以被试探出来的差别。
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{22}$")


def new_share_token() -> str:
    return secrets.token_urlsafe(16)


def is_share_token(value: str) -> bool:
    return _TOKEN.fullmatch(value) is not None


@dataclass(frozen=True)
class ShareObjectSpec:
    """建草稿时声明的一个附件副本。"""

    attachment_id: str
    object_key: str
    size: int
    mime_type: str | None


@dataclass(frozen=True)
class ShareObject:
    attachment_id: str
    object_key: str
    size: int
    mime_type: str | None
    uploaded: bool


@dataclass(frozen=True)
class Share:
    token: str
    archive_id: str
    title: str
    message_count: int
    snapshot_json: str
    state: str
    created_at: int
    expires_at: int | None
    views: int
    last_viewed_at: int | None
    objects: tuple[ShareObject, ...]

    def is_open_at(self, moment: int) -> bool:
        """公开地址此刻打不打得开：定稿了、而且没过期。"""
        if self.state != STATE_LIVE:
            return False
        return self.expires_at is None or moment < self.expires_at


class ShareNotFoundError(LookupError):
    """这个令牌下没有分享（或已经删了）。"""


class ShareStateError(ValueError):
    """在不对的状态上做了不对的事——比如给已定稿的分享补传附件。"""


class SqliteShareStore:
    """分享的行与它们的附件清单。"""

    def __init__(
        self,
        database_path: Path,
        *,
        now: Callable[[], int] | None = None,
        token_factory: Callable[[], str] = new_share_token,
    ) -> None:
        self._database_path = database_path
        self._now: Callable[[], int] = (
            (lambda: max(1, int(time.time()))) if now is None else now
        )
        self._token_factory = token_factory
        self._lock = threading.RLock()
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @property
    def database_path(self) -> Path:
        return self._database_path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._lock, closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS shares (
                    token TEXT PRIMARY KEY,
                    archive_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    message_count INTEGER NOT NULL CHECK (message_count >= 0),
                    snapshot_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ('draft', 'live')),
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER,
                    views INTEGER NOT NULL DEFAULT 0,
                    last_viewed_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS shares_by_archive
                    ON shares (archive_id, created_at);
                CREATE TABLE IF NOT EXISTS share_objects (
                    token TEXT NOT NULL REFERENCES shares (token) ON DELETE CASCADE,
                    attachment_id TEXT NOT NULL,
                    object_key TEXT NOT NULL,
                    size INTEGER NOT NULL CHECK (size >= 0),
                    mime_type TEXT,
                    uploaded INTEGER NOT NULL DEFAULT 0 CHECK (uploaded IN (0, 1)),
                    PRIMARY KEY (token, attachment_id)
                );
                """
            )
            connection.commit()

    # --- 建与改 -------------------------------------------------------------

    def create_draft(
        self,
        *,
        archive_id: str,
        title: str,
        message_count: int,
        snapshot_json: str,
        expires_at: int | None,
        objects: Sequence[ShareObjectSpec] = (),
    ) -> Share:
        token = self._token_factory()
        if not is_share_token(token):
            raise ValueError("share token factory produced a malformed token")
        created_at = self._now()
        with self._lock, closing(self._connect()) as connection:
            connection.execute(
                "INSERT INTO shares (token, archive_id, title, message_count, snapshot_json,"
                " state, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    token,
                    archive_id,
                    title,
                    message_count,
                    snapshot_json,
                    STATE_DRAFT,
                    created_at,
                    expires_at,
                ),
            )
            connection.executemany(
                "INSERT INTO share_objects (token, attachment_id, object_key, size, mime_type)"
                " VALUES (?, ?, ?, ?, ?)",
                [
                    (token, item.attachment_id, item.object_key, item.size, item.mime_type)
                    for item in objects
                ],
            )
            connection.commit()
        share = self.get(token)
        assert share is not None
        return share

    def mark_uploaded(self, token: str, attachment_id: str) -> None:
        with self._lock, closing(self._connect()) as connection:
            state = self._state_of(connection, token)
            if state != STATE_DRAFT:
                raise ShareStateError("objects can only be added to a draft")
            cursor = connection.execute(
                "UPDATE share_objects SET uploaded = 1 WHERE token = ? AND attachment_id = ?",
                (token, attachment_id),
            )
            if cursor.rowcount != 1:
                raise ShareNotFoundError(attachment_id)
            connection.commit()

    def publish(self, token: str) -> Share:
        """草稿 → 可公开读。缺对象的不许定稿（缺哪个由 2.181 的路由去说）。"""
        with self._lock, closing(self._connect()) as connection:
            state = self._state_of(connection, token)
            if state != STATE_DRAFT:
                raise ShareStateError("only a draft can be published")
            missing = connection.execute(
                "SELECT COUNT(*) FROM share_objects WHERE token = ? AND uploaded = 0",
                (token,),
            ).fetchone()[0]
            if missing:
                raise ShareStateError("some objects have not been uploaded yet")
            connection.execute(
                "UPDATE shares SET state = ? WHERE token = ?", (STATE_LIVE, token)
            )
            connection.commit()
        share = self.get(token)
        assert share is not None
        return share

    def set_expiry(self, token: str, expires_at: int | None) -> Share:
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute(
                "UPDATE shares SET expires_at = ? WHERE token = ?", (expires_at, token)
            )
            if cursor.rowcount != 1:
                raise ShareNotFoundError(token)
            connection.commit()
        share = self.get(token)
        assert share is not None
        return share

    def list_for_archive(self, archive_id: str) -> list[Share]:
        """这个归档发出去的全部分享，新的在前。"""
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT token FROM shares WHERE archive_id = ? ORDER BY created_at DESC, rowid DESC",
                (archive_id,),
            ).fetchall()
            shares = [self._load(connection, str(row["token"])) for row in rows]
        return [share for share in shares if share is not None]

    def now(self) -> int:
        return self._now()

    def delete(self, token: str) -> bool:
        """删行（连带附件清单）。**对象要调用方先删**——见 `ShareSweeper`。"""
        with self._lock, closing(self._connect()) as connection:
            cursor = connection.execute("DELETE FROM shares WHERE token = ?", (token,))
            connection.commit()
            return cursor.rowcount == 1

    # --- 读 -----------------------------------------------------------------

    def get(self, token: str) -> Share | None:
        if not is_share_token(token):
            return None
        with self._lock, closing(self._connect()) as connection:
            return self._load(connection, token)

    def open_public(self, token: str, *, count_view: bool = True) -> Share | None:
        """公开地址打开一次：打得开就计一次数并返回，打不开一律 `None`。

        过期、已删、不存在、还是草稿——调用方拿到的都是同一个 `None`，
        没有第二种返回值可以把它们区分开。
        """
        if not is_share_token(token):
            return None
        moment = self._now()
        with self._lock, closing(self._connect()) as connection:
            if not count_view:
                share = self._load(connection, token)
                return share if share is not None and share.is_open_at(moment) else None
            cursor = connection.execute(
                "UPDATE shares SET views = views + 1, last_viewed_at = ?"
                " WHERE token = ? AND state = ? AND (expires_at IS NULL OR expires_at > ?)",
                (moment, token, STATE_LIVE, moment),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return None
            connection.commit()
            return self._load(connection, token)

    def sweep_candidates(self) -> list[Share]:
        """该真删的：过期超过宽限期的，和陈旧的草稿。"""
        moment = self._now()
        with self._lock, closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT token FROM shares"
                " WHERE (state = ? AND expires_at IS NOT NULL AND expires_at <= ?)"
                "    OR (state = ? AND created_at <= ?)"
                " ORDER BY created_at",
                (
                    STATE_LIVE,
                    moment - EXPIRED_GRACE_SECONDS,
                    STATE_DRAFT,
                    moment - STALE_DRAFT_SECONDS,
                ),
            ).fetchall()
            shares = [self._load(connection, str(row["token"])) for row in rows]
        return [share for share in shares if share is not None]

    # --- 内部 ---------------------------------------------------------------

    @staticmethod
    def _state_of(connection: sqlite3.Connection, token: str) -> str:
        row = connection.execute(
            "SELECT state FROM shares WHERE token = ?", (token,)
        ).fetchone()
        if row is None:
            raise ShareNotFoundError(token)
        return str(row["state"])

    @staticmethod
    def _load(connection: sqlite3.Connection, token: str) -> Share | None:
        row = connection.execute("SELECT * FROM shares WHERE token = ?", (token,)).fetchone()
        if row is None:
            return None
        objects = tuple(
            ShareObject(
                attachment_id=str(item["attachment_id"]),
                object_key=str(item["object_key"]),
                size=int(item["size"]),
                mime_type=None if item["mime_type"] is None else str(item["mime_type"]),
                uploaded=bool(item["uploaded"]),
            )
            for item in connection.execute(
                "SELECT * FROM share_objects WHERE token = ? ORDER BY rowid", (token,)
            )
        )
        return Share(
            token=str(row["token"]),
            archive_id=str(row["archive_id"]),
            title=str(row["title"]),
            message_count=int(row["message_count"]),
            snapshot_json=str(row["snapshot_json"]),
            state=str(row["state"]),
            created_at=int(row["created_at"]),
            expires_at=None if row["expires_at"] is None else int(row["expires_at"]),
            views=int(row["views"]),
            last_viewed_at=(
                None if row["last_viewed_at"] is None else int(row["last_viewed_at"])
            ),
            objects=objects,
        )


__all__ = [
    "EXPIRED_GRACE_SECONDS",
    "SHARES_DATABASE",
    "SHARE_OBJECT_SCOPE",
    "STALE_DRAFT_SECONDS",
    "STATE_DRAFT",
    "STATE_LIVE",
    "Share",
    "ShareNotFoundError",
    "ShareObject",
    "ShareObjectSpec",
    "ShareStateError",
    "SqliteShareStore",
    "is_share_token",
    "new_share_token",
]
