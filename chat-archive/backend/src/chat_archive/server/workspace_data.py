"""Read-only remote projections for the shared analysis and export engines.

Only parsed domain records are read. No local archive or acquisition service is
constructed, and no remote record is modified.
"""

from __future__ import annotations

import sqlite3
import hashlib
import time
from contextlib import closing
from collections.abc import Callable, Mapping
from pathlib import Path
from functools import lru_cache

from chat_archive.analysis import AnalysisPage, analysis_window, read_analysis
from chat_archive.export import ExportScope, scope_conditions
from chat_archive.domain.errors import ArchiveError, ErrorCode
from chat_archive.domain.forward_media import card_blob_metadata


class RemoteWorkspaceData:
    def __init__(
        self, path: Path, blob_state: Callable[[str, str], str | None] | None = None,
        *, blob_states: Callable[[str, tuple[str, ...]], Mapping[str, str | None]] | None = None,
    ) -> None:
        self.path = path
        self.blob_state = blob_state
        self.blob_states = blob_states

    def connection(self, archive_id: str, *, scope: ExportScope | None = None) -> sqlite3.Connection:
        connection = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.create_function("workspace_archive", 0, lambda: archive_id)
        read_at = int(time.time())
        connection.create_function("workspace_read_time", 0, lambda: read_at)
        states: dict[str, str] = {}
        prepared = False

        def cache(values: Mapping[str, str | None]) -> None:
            for key, value in values.items():
                states[key] = "ready" if value == "remote_ready" else "discovered" if value is not None else "source_missing"

        def state(blob_id: str) -> str:
            nonlocal prepared
            if not prepared and self.blob_states is not None:
                prepared = True
                kinds = scope.attachment_kinds if scope is not None else ()
                where = " WHERE workspace_blob_kind(m.app_card_json,j.value,m.message_type) IN (" + ",".join("?" for _ in kinds) + ")" if kinds else ""
                cursor = connection.execute("SELECT DISTINCT j.value FROM messages m,json_each(COALESCE(m.attachment_refs,'[]')) j" + where, kinds)
                while rows := cursor.fetchmany(500):
                    cache(self.blob_states(archive_id, tuple(str(row[0]) for row in rows)))
            if blob_id not in states:
                value = (
                    self.blob_state(archive_id, blob_id) if self.blob_state is not None else None
                )
                states[blob_id] = (
                    "ready"
                    if value == "remote_ready"
                    else "discovered"
                    if value is not None
                    else "source_missing"
                )
            return states[blob_id]

        connection.create_function("workspace_blob_state", 1, state)

        @lru_cache(maxsize=8)
        def media_metadata(raw: str | None) -> Mapping[str, tuple[str, str | None]]:
            return card_blob_metadata(raw)

        def media_kind(raw: str | None, blob_id: str, message_type: str) -> str:
            meta = media_metadata(raw).get(blob_id)
            return (
                meta[0]
                if meta is not None
                else message_type
                if message_type in {"image", "emoji", "audio", "video", "file"}
                else "file"
            )

        def media_name(raw: str | None, blob_id: str) -> str | None:
            meta = media_metadata(raw).get(blob_id)
            return meta[1] if meta is not None else None

        connection.create_function("workspace_blob_kind", 3, media_kind)
        connection.create_function("workspace_blob_name", 2, media_name)

        def thumbnail_path(blob_id: str) -> str:
            return hashlib.sha256(blob_id.encode()).hexdigest() + ".thumbnail.bin"

        connection.create_function("workspace_thumbnail_path", 1, thumbnail_path)

        try:
            connection.executescript("""
            CREATE TEMP VIEW workspace_records AS
              SELECT r.record_key,r.created_at,r.content_text,r.sender_uid,r.sender_display_name,r.is_self,
                     r.message_type,r.reply_to_key,r.app_card_json,r.source_sequence,r.source_local_id,
                     r.attachment_refs,r.server_sequence,
                     COALESCE(a.primary_account_id,r.source_account_id) AS workspace_account_id,
                     COALESCE(c.primary_conversation_id,r.conversation_id) AS workspace_conversation_id
              FROM archive_records r
              LEFT JOIN archive_account_links a ON a.archive_id=r.archive_id
                AND a.source_account_id=r.source_account_id
              LEFT JOIN archive_conversation_links c ON c.archive_id=r.archive_id
                AND c.conversation_id=r.conversation_id
              WHERE r.archive_id=workspace_archive();
            CREATE TEMP VIEW source_accounts AS
              SELECT source_account_id AS id,display_name FROM archive_accounts
              WHERE archive_id=workspace_archive() AND NOT EXISTS(
                SELECT 1 FROM archive_account_links l WHERE l.archive_id=workspace_archive()
                  AND l.source_account_id=archive_accounts.source_account_id
                  AND l.primary_account_id!=l.source_account_id);
            CREATE TEMP VIEW conversations AS
              SELECT r.conversation_id AS id,r.source_account_id,
                     COALESCE(c.display_name,r.conversation_id) AS display_name,c.is_group,
                     r.conversation_id AS conversation_uid
              FROM (SELECT DISTINCT source_account_id,conversation_id FROM messages) r
              LEFT JOIN archive_conversations c ON c.archive_id=workspace_archive()
                AND c.source_account_id=r.source_account_id AND c.conversation_id=r.conversation_id;
            CREATE TEMP VIEW contacts AS
              SELECT source_account_id,uid AS contact_uid,display_name,remark,0 AS is_unknown
              FROM archive_identities WHERE archive_id=workspace_archive();
            CREATE TEMP VIEW workspace_all_messages AS
              SELECT record_key AS id,workspace_account_id AS source_account_id,
                     workspace_conversation_id AS conversation_id,
                     COALESCE(sender_uid,'') AS sender_uid,sender_display_name,
                     created_at,COALESCE(message_type,'unknown') AS message_type,
                     content_text,is_self,reply_to_key AS reply_to_id,'parsed' AS parse_status,
                     record_key AS source_message_id,app_card_json,
                     source_sequence,source_local_id,attachment_refs,server_sequence
              FROM workspace_records;
            CREATE TEMP VIEW messages AS SELECT * FROM workspace_all_messages;
            CREATE TEMP VIEW attachments AS
              SELECT j.value AS id,m.id AS message_id,
                     workspace_blob_kind(m.app_card_json,j.value,m.message_type) AS kind,
                     NULL AS cache_path,workspace_thumbnail_path(j.value) AS thumb_cache_path,workspace_blob_state(j.value) AS cache_status,
                     NULL AS mime_type,
                     COALESCE(workspace_blob_name(m.app_card_json,j.value),CASE WHEN json_valid(m.app_card_json) THEN json_extract(m.app_card_json,'$.file_name') END) AS file_name
              FROM messages m,json_each(COALESCE(m.attachment_refs,'[]')) j
              UNION ALL
              SELECT m.id||':missing',m.id,m.message_type,NULL,NULL,'source_missing',NULL,NULL
              FROM messages m WHERE m.message_type IN ('image','emoji','audio','video','file')
                AND json_array_length(COALESCE(m.attachment_refs,'[]'))=0;
            """)
            if scope is not None:
                clauses, parameters = scope_conditions(scope)
                for i, value in enumerate(parameters):
                    def parameter(value: str | int = value) -> str | int:
                        return value
                    connection.create_function(f"workspace_scope_{i}", 0, parameter, deterministic=True)
                where = " AND ".join(clauses)
                for i in range(len(parameters)):
                    where = where.replace("?", f"workspace_scope_{i}()", 1)
                connection.execute("DROP VIEW messages")
                connection.execute(f"CREATE TEMP VIEW messages AS SELECT * FROM workspace_all_messages m WHERE {where}")
        except BaseException:
            connection.close()
            raise
        return connection

    @staticmethod
    def require_account(connection: sqlite3.Connection, account_id: str) -> None:
        if (
            connection.execute("SELECT 1 FROM source_accounts WHERE id=?", (account_id,)).fetchone()
            is None
        ):
            raise ArchiveError(ErrorCode.SOURCE_NOT_FOUND)

    def analysis(
        self,
        archive_id: str,
        account_id: str,
        *,
        date_from: str,
        date_to: str,
        conversation_kind: str = "all",
        conversation_id: str | None = None,
    ) -> AnalysisPage:
        _, _, start, stop = analysis_window(date_from, date_to)
        if conversation_kind not in {"all", "private", "group"}:
            raise ArchiveError(ErrorCode.QUERY_INVALID)
        scope = ExportScope(account_id, conversation_ids=(conversation_id,) if conversation_id is not None else (), date_from=start, date_to=stop-1)
        with closing(self.connection(archive_id, scope=scope)) as connection:
            connection.execute("BEGIN")
            self.require_account(connection, account_id)
            connection.execute("""
            CREATE TEMP TABLE analysis_hourly AS
              SELECT source_account_id,conversation_id,
                     CAST(created_at/3600 AS INTEGER)*3600 AS hour_start,
                     message_type,is_self,COUNT(*) AS message_count,
                     workspace_read_time() AS updated_at
              FROM messages WHERE message_type!='system'
              GROUP BY source_account_id,conversation_id,hour_start,message_type,is_self;
            """)
            connection.execute("CREATE INDEX analysis_hourly_scope ON analysis_hourly(source_account_id,conversation_id,hour_start)")
            connection.execute("CREATE TEMP TABLE analysis_conversations AS SELECT * FROM conversations")
            connection.execute("DROP VIEW conversations")
            connection.execute("CREATE TEMP VIEW conversations AS SELECT * FROM analysis_conversations")
            connection.execute("CREATE INDEX analysis_conversations_id ON analysis_conversations(source_account_id,id)")
            return read_analysis(
                connection,
                account_id,
                date_from=date_from,
                date_to=date_to,
                conversation_kind=conversation_kind,
                conversation_id=conversation_id,
            )
