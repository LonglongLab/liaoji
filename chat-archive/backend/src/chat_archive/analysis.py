"""Local analysis over hourly materialized counts; no message content on read."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import Connection, text

from chat_archive.domain.errors import ArchiveError, ErrorCode

BEIJING = timezone(timedelta(hours=8))


def refresh_analysis(connection: Connection, account_id: str, days: set[str], now: int) -> None:
    """Rebuild only changed UTC days, in the writer's existing transaction."""
    for day in days:
        start = int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp())
        params = {"account": account_id, "start": start, "end": start + 86400, "now": now}
        connection.execute(
            text("""
            DELETE FROM analysis_hourly
            WHERE source_account_id = :account AND hour_start >= :start AND hour_start < :end
        """),
            params,
        )
        connection.execute(
            text("""
            INSERT INTO analysis_hourly
            SELECT source_account_id, conversation_id,
                   CAST(created_at / 3600 AS INTEGER) * 3600,
                   message_type, is_self, COUNT(*), :now
            FROM messages
            WHERE source_account_id = :account AND created_at >= :start
              AND created_at < :end AND message_type != 'system'
            GROUP BY conversation_id, CAST(created_at / 3600 AS INTEGER), message_type, is_self
        """),
            params,
        )


@dataclass(frozen=True)
class AnalysisSummary:
    message_count: int
    sent_count: int
    received_count: int
    active_days: int
    conversation_count: int
    private_count: int
    group_count: int
    unknown_direction_count: int = 0


@dataclass(frozen=True)
class AnalysisDay:
    day: str
    message_count: int


@dataclass(frozen=True)
class AnalysisHour:
    hour: int
    message_count: int


@dataclass(frozen=True)
class AnalysisType:
    message_type: str
    message_count: int


@dataclass(frozen=True)
class AnalysisConversation:
    conversation_id: str
    display_name: str
    is_group: bool | None
    message_count: int


@dataclass(frozen=True)
class AnalysisParticipant:
    sender_uid: str | None
    display_name: str
    is_self: bool
    message_count: int


@dataclass(frozen=True)
class AnalysisPage:
    date_from: str
    date_to: str
    timezone: str
    summary: AnalysisSummary
    daily: tuple[AnalysisDay, ...]
    hours: tuple[AnalysisHour, ...]
    types: tuple[AnalysisType, ...]
    conversations: tuple[AnalysisConversation, ...]
    ranking_truncated: bool
    updated_at: int | None
    participants: tuple[AnalysisParticipant, ...] = ()
    participant_count: int = 0
    participant_ranking_truncated: bool = False
    unknown_sender_count: int = 0
    selected_conversation_is_group: bool | None = None


def analysis_window(date_from: str, date_to: str) -> tuple[date, int, int, int]:
    """Validate the shared Beijing window before a storage adapter reads data."""
    try:
        first, last = date.fromisoformat(date_from), date.fromisoformat(date_to)
    except ValueError as exc:
        raise ArchiveError(ErrorCode.QUERY_INVALID) from exc
    length = (last - first).days + 1
    if not 1 <= length <= 366 or first.isoformat() != date_from or last.isoformat() != date_to:
        raise ArchiveError(ErrorCode.QUERY_INVALID)
    start = int(datetime.combine(first, datetime.min.time(), BEIJING).timestamp())
    return first, length, start, start + length * 86400


def read_analysis(
    connection: sqlite3.Connection,
    account_id: str,
    *,
    date_from: str,
    date_to: str,
    conversation_kind: str = "all",
    conversation_id: str | None = None,
) -> AnalysisPage:
    first, length, start, stop = analysis_window(date_from, date_to)
    if conversation_kind not in {"all", "private", "group"}:
        raise ArchiveError(ErrorCode.QUERY_INVALID)
    clauses = ["s.source_account_id = ?", "s.hour_start >= ?", "s.hour_start < ?"]
    params: list[str | int] = [account_id, start, stop]
    if conversation_kind != "all":
        clauses.append("c.is_group = ?")
        params.append(int(conversation_kind == "group"))
    if conversation_id is not None:
        clauses.append("s.conversation_id = ?")
        params.append(conversation_id)
    source = f"""
        FROM analysis_hourly s JOIN conversations c
          ON c.id = s.conversation_id AND c.source_account_id = s.source_account_id
        WHERE {" AND ".join(clauses)}
    """
    # A single read transaction prevents a concurrent import from changing the
    # scope between dimensions. SQL returns bounded aggregates, not every bucket.
    if not connection.in_transaction:
        connection.execute("BEGIN")
    totals = connection.execute(
        f"""
        SELECT COALESCE(SUM(s.message_count), 0) AS total,
               COALESCE(SUM(CASE WHEN s.is_self THEN s.message_count ELSE 0 END), 0) AS sent,
               COALESCE(SUM(CASE WHEN s.is_self IS NULL THEN s.message_count ELSE 0 END), 0) AS unknown_direction,
               COUNT(DISTINCT s.conversation_id) AS conversations,
               COUNT(DISTINCT CASE WHEN c.is_group=1 THEN s.conversation_id END) AS groups,
               COUNT(DISTINCT CASE WHEN c.is_group=0 THEN s.conversation_id END) AS private,
               MAX(s.updated_at) AS updated_at
        {source}
    """,
        params,
    ).fetchone()
    day_rows = connection.execute(
        f"""
        SELECT date(s.hour_start + 28800, 'unixepoch') AS day, SUM(s.message_count) AS count
        {source} GROUP BY day ORDER BY day
    """,
        params,
    ).fetchall()
    hour_rows = connection.execute(
        f"""
        SELECT CAST(strftime('%H', s.hour_start + 28800, 'unixepoch') AS INTEGER) AS hour,
               SUM(s.message_count) AS count
        {source} GROUP BY hour
    """,
        params,
    ).fetchall()
    type_rows = connection.execute(
        f"""
        SELECT s.message_type, SUM(s.message_count) AS count
        {source} GROUP BY s.message_type ORDER BY count DESC, s.message_type
    """,
        params,
    ).fetchall()
    rank_rows = connection.execute(
        f"""
        SELECT s.conversation_id, c.display_name, c.is_group, SUM(s.message_count) AS count
        {source} GROUP BY s.conversation_id ORDER BY count DESC, s.conversation_id LIMIT 100
    """,
        params,
    ).fetchall()
    daily = {str(row["day"]): int(row["count"]) for row in day_rows}
    hours = {int(row["hour"]): int(row["count"]) for row in hour_rows}
    total, sent = int(totals["total"]), int(totals["sent"])
    conversations, groups = int(totals["conversations"]), int(totals["groups"])
    selected_group: bool | None = None
    participant_rows: list[sqlite3.Row] = []
    if conversation_id is not None:
        selected = connection.execute(
            "SELECT is_group FROM conversations WHERE source_account_id=? AND id=?",
            (account_id, conversation_id),
        ).fetchone()
        selected_group = bool(selected["is_group"]) if selected is not None else None
        if selected_group and conversation_kind != "private":
            # Read only sender metadata in this one group/window. Names never define identity.
            # Window totals remain complete even when the displayed ranking is capped.
            participant_rows = connection.execute(
                """
                WITH senders AS (
                    SELECT CASE WHEN is_self=1 THEN 'self:'
                                WHEN TRIM(sender_uid)='' THEN 'unknown:'
                                ELSE 'uid:' || sender_uid END AS identity,
                           CASE WHEN is_self=1 OR TRIM(sender_uid)='' THEN NULL
                                ELSE sender_uid END AS sender_uid,
                           CASE WHEN is_self=1 THEN 1 ELSE 0 END AS own,
                           MAX(NULLIF(TRIM(sender_display_name),'')) AS name,
                           COUNT(*) AS count
                    FROM messages
                    WHERE source_account_id=? AND conversation_id=?
                      AND created_at>=? AND created_at<? AND message_type!='system'
                    GROUP BY identity
                )
                SELECT s.*, COALESCE(NULLIF(c.remark,''),NULLIF(c.display_name,''),s.name,
                                     '未命名成员') AS display_name,
                       COUNT(*) OVER() AS bucket_count,
                       SUM(CASE WHEN identity='unknown:' THEN count ELSE 0 END) OVER() AS unknown_count,
                       SUM(CASE WHEN identity!='unknown:' THEN 1 ELSE 0 END) OVER() AS known_count
                FROM senders s LEFT JOIN contacts c
                  ON c.source_account_id=? AND c.contact_uid=s.sender_uid
                ORDER BY count DESC, identity LIMIT 100
                """,
                (account_id, conversation_id, start, stop, account_id),
            ).fetchall()
    member_totals = participant_rows[0] if participant_rows else None
    return AnalysisPage(
        date_from=date_from,
        date_to=date_to,
        timezone="Asia/Shanghai",
        participants=tuple(
            AnalysisParticipant(
                row["sender_uid"],
                "我" if row["own"] else "未识别发送者" if row["identity"] == "unknown:" else str(row["display_name"]),
                bool(row["own"]), int(row["count"]),
            ) for row in participant_rows
        ),
        participant_count=int(member_totals["known_count"]) if member_totals is not None else 0,
        participant_ranking_truncated=bool(member_totals is not None and member_totals["bucket_count"] > 100),
        unknown_sender_count=int(member_totals["unknown_count"]) if member_totals is not None else 0,
        selected_conversation_is_group=selected_group,
        summary=AnalysisSummary(
            total, sent, total - sent - int(totals["unknown_direction"]), len(daily),
            conversations, int(totals["private"]), groups, int(totals["unknown_direction"])
        ),
        daily=tuple(
            AnalysisDay(
                (first + timedelta(days=i)).isoformat(),
                daily.get((first + timedelta(days=i)).isoformat(), 0),
            )
            for i in range(length)
        ),
        hours=tuple(AnalysisHour(i, hours.get(i, 0)) for i in range(24)),
        types=tuple(AnalysisType(str(row["message_type"]), int(row["count"])) for row in type_rows),
        conversations=tuple(
            AnalysisConversation(
                str(row["conversation_id"]),
                str(row["display_name"]),
                None if row["is_group"] is None else bool(row["is_group"]),
                int(row["count"]),
            )
            for row in rank_rows
        ),
        ranking_truncated=conversations > 100,
        updated_at=int(totals["updated_at"]) if totals["updated_at"] is not None else None,
    )
