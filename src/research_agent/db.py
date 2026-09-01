"""Small SQLite repository for durable local threads, runs, events, and sources."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from research_agent.models import ResearchAnswer, SourceRecord, utc_now


TERMINAL_STATUSES = {"completed", "failed", "cancelled", "interrupted"}


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS threads (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
                    run_id TEXT,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
                    question TEXT NOT NULL,
                    document_ids_json TEXT NOT NULL,
                    use_web INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    answer_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS runs_thread_status_idx ON runs(thread_id, status);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    type TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    message TEXT NOT NULL,
                    data_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_run_id_idx ON events(run_id, id);
                CREATE TABLE IF NOT EXISTS sources (
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    source_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT,
                    document_id TEXT,
                    block_id TEXT,
                    page INTEGER,
                    page_end INTEGER,
                    section TEXT,
                    excerpt TEXT NOT NULL,
                    retrieved_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY (run_id, source_id),
                    UNIQUE (run_id, fingerprint)
                );
                CREATE TABLE IF NOT EXISTS run_counters (
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    value INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (run_id, name)
                );
                """
            )

    def mark_incomplete_interrupted(self) -> int:
        now = utc_now()
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT id FROM runs WHERE status IN ('queued', 'running')"
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE runs SET status='interrupted', updated_at=? WHERE id=?",
                    (now, row["id"]),
                )
                connection.execute(
                    "INSERT INTO events(run_id,type,stage,message,data_json,created_at) VALUES(?,?,?,?,?,?)",
                    (row["id"], "interrupted", "system", "服务重启，任务已中断", "{}", now),
                )
            return len(rows)

    def create_thread(self, title: str) -> dict[str, Any]:
        thread_id = f"thread_{uuid.uuid4().hex}"
        now = utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO threads(id,title,created_at,updated_at) VALUES(?,?,?,?)",
                (thread_id, title.strip(), now, now),
            )
        return self.get_thread(thread_id, include_detail=False)

    def list_threads(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """SELECT t.*, (SELECT COUNT(*) FROM messages m WHERE m.thread_id=t.id) AS message_count
                   FROM threads t ORDER BY updated_at DESC"""
            ).fetchall()
        return [dict(row) for row in rows]

    def get_thread(self, thread_id: str, *, include_detail: bool = True) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
            if row is None:
                raise KeyError(thread_id)
            output = dict(row)
            if include_detail:
                messages = connection.execute(
                    "SELECT * FROM messages WHERE thread_id=? ORDER BY created_at, rowid", (thread_id,)
                ).fetchall()
                runs = connection.execute(
                    "SELECT * FROM runs WHERE thread_id=? ORDER BY created_at", (thread_id,)
                ).fetchall()
                output["messages"] = [self._message_payload(item) for item in messages]
                output["runs"] = [self._run_payload(item) for item in runs]
            return output

    def add_message(
        self,
        thread_id: str,
        role: str,
        content: str,
        *,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        message_id = f"msg_{uuid.uuid4().hex}"
        now = utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO messages(id,thread_id,run_id,role,content,metadata_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (message_id, thread_id, run_id, role, content, json.dumps(metadata or {}, ensure_ascii=False), now),
            )
            connection.execute("UPDATE threads SET updated_at=? WHERE id=?", (now, thread_id))
        return {"id": message_id, "thread_id": thread_id, "run_id": run_id, "role": role, "content": content,
                "metadata": metadata or {}, "created_at": now}

    def create_run(self, thread_id: str, question: str, document_ids: list[str], use_web: bool) -> dict[str, Any]:
        run_id = f"run_{uuid.uuid4().hex}"
        now = utc_now()
        with self._lock, self._connect() as connection:
            active = connection.execute(
                "SELECT id FROM runs WHERE thread_id=? AND status IN ('queued','running')", (thread_id,)
            ).fetchone()
            if active:
                raise RuntimeError("该研究会话已有正在执行的任务")
            connection.execute(
                """INSERT INTO runs(id,thread_id,question,document_ids_json,use_web,status,created_at,updated_at)
                   VALUES(?,?,?,?,?,'queued',?,?)""",
                (run_id, thread_id, question, json.dumps(document_ids), int(use_web), now, now),
            )
        self.add_message(thread_id, "user", question, run_id=run_id,
                         metadata={"document_ids": document_ids, "use_web": use_web})
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return self._run_payload(row)

    def set_run_status(
        self,
        run_id: str,
        status: str,
        *,
        answer: ResearchAnswer | None = None,
        error: str | None = None,
    ) -> None:
        now = utc_now()
        answer_json = answer.model_dump_json() if answer else None
        with self._lock, self._connect() as connection:
            connection.execute(
                """UPDATE runs SET status=?, answer_json=COALESCE(?,answer_json),
                   error=COALESCE(?,error), updated_at=? WHERE id=?""",
                (status, answer_json, error, now, run_id),
            )

    def add_event(
        self,
        run_id: str,
        event_type: str,
        stage: str,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO events(run_id,type,stage,message,data_json,created_at) VALUES(?,?,?,?,?,?)",
                (run_id, event_type, stage, message, json.dumps(data or {}, ensure_ascii=False), now),
            )
            event_id = int(cursor.lastrowid)
        return {"id": event_id, "run_id": run_id, "type": event_type, "stage": stage,
                "message": message, "data": data or {}, "created_at": now}

    def list_events(self, run_id: str, after_id: int = 0) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id", (run_id, after_id)
            ).fetchall()
        return [self._event_payload(row) for row in rows]

    def add_source(self, run_id: str, payload: dict[str, Any]) -> tuple[SourceRecord, bool]:
        fingerprint_value = "|".join(
            str(payload.get(key) or "") for key in ("kind", "url", "document_id", "block_id", "page", "excerpt")
        )
        fingerprint = hashlib.sha256(fingerprint_value.encode("utf-8")).hexdigest()
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM sources WHERE run_id=? AND fingerprint=?", (run_id, fingerprint)
            ).fetchone()
            if existing:
                return self._source_payload(existing), False
            count = connection.execute("SELECT COUNT(*) FROM sources WHERE run_id=?", (run_id,)).fetchone()[0]
            source_id = f"S{count + 1}"
            retrieved_at = utc_now()
            record = SourceRecord(
                source_id=source_id,
                run_id=run_id,
                kind=payload["kind"],
                title=(payload.get("title") or "未命名来源")[:500],
                url=payload.get("url"),
                document_id=payload.get("document_id"),
                block_id=payload.get("block_id"),
                page=payload.get("page"),
                page_end=payload.get("page_end"),
                section=payload.get("section"),
                excerpt=(payload.get("excerpt") or "")[:6000],
                retrieved_at=retrieved_at,
                status=payload.get("status", "available"),
                metadata=payload.get("metadata") or {},
            )
            connection.execute(
                """INSERT INTO sources(run_id,source_id,fingerprint,kind,title,url,document_id,block_id,page,page_end,
                   section,excerpt,retrieved_at,status,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, source_id, fingerprint, record.kind, record.title, record.url, record.document_id,
                 record.block_id, record.page, record.page_end, record.section, record.excerpt,
                 record.retrieved_at, record.status, json.dumps(record.metadata, ensure_ascii=False)),
            )
        return record, True

    def claim_budget(self, run_id: str, name: str, limit: int) -> tuple[bool, int]:
        """Atomically reserve one tool call across all DeepAgents child tasks."""
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM run_counters WHERE run_id=? AND name=?", (run_id, name)
            ).fetchone()
            current = int(row["value"]) if row else 0
            if current >= limit:
                return False, current
            next_value = current + 1
            connection.execute(
                """INSERT INTO run_counters(run_id,name,value) VALUES(?,?,?)
                   ON CONFLICT(run_id,name) DO UPDATE SET value=excluded.value""",
                (run_id, name, next_value),
            )
            return True, next_value

    def increment_counter(self, run_id: str, name: str) -> int:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM run_counters WHERE run_id=? AND name=?", (run_id, name)
            ).fetchone()
            next_value = (int(row["value"]) if row else 0) + 1
            connection.execute(
                """INSERT INTO run_counters(run_id,name,value) VALUES(?,?,?)
                   ON CONFLICT(run_id,name) DO UPDATE SET value=excluded.value""",
                (run_id, name, next_value),
            )
            return next_value

    def get_counter(self, run_id: str, name: str) -> int:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM run_counters WHERE run_id=? AND name=?", (run_id, name)
            ).fetchone()
        return int(row["value"]) if row else 0

    def reset_counter(self, run_id: str, name: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM run_counters WHERE run_id=? AND name=?", (run_id, name)
            )

    def list_sources(self, run_id: str) -> list[SourceRecord]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM sources WHERE run_id=? ORDER BY CAST(SUBSTR(source_id,2) AS INTEGER)", (run_id,)
            ).fetchall()
        return [self._source_payload(row) for row in rows]

    def count_sources(self, run_id: str, kind: str | None = None) -> int:
        with self._lock, self._connect() as connection:
            if kind is None:
                row = connection.execute(
                    "SELECT COUNT(*) AS count FROM sources WHERE run_id=?", (run_id,)
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT COUNT(*) AS count FROM sources WHERE run_id=? AND kind=?", (run_id, kind)
                ).fetchone()
        return int(row["count"])

    @staticmethod
    def _message_payload(row: sqlite3.Row) -> dict[str, Any]:
        output = dict(row)
        output["metadata"] = json.loads(output.pop("metadata_json"))
        return output

    @staticmethod
    def _run_payload(row: sqlite3.Row) -> dict[str, Any]:
        output = dict(row)
        output["document_ids"] = json.loads(output.pop("document_ids_json"))
        output["use_web"] = bool(output["use_web"])
        raw_answer = output.pop("answer_json")
        output["answer"] = json.loads(raw_answer) if raw_answer else None
        return output

    @staticmethod
    def _event_payload(row: sqlite3.Row) -> dict[str, Any]:
        output = dict(row)
        output["data"] = json.loads(output.pop("data_json"))
        return output

    @staticmethod
    def _source_payload(row: sqlite3.Row) -> SourceRecord:
        output = dict(row)
        output.pop("fingerprint", None)
        output["metadata"] = json.loads(output.pop("metadata_json"))
        return SourceRecord.model_validate(output)
