from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AgentStore:
    def __init__(self, path: Path):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS deliveries (
                delivery_id TEXT PRIMARY KEY,
                received_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS work_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                delivery_id TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                thread_id TEXT NOT NULL UNIQUE,
                repo TEXT NOT NULL,
                issue_number INTEGER NOT NULL,
                status TEXT NOT NULL,
                branch TEXT,
                worktree TEXT,
                requirements_version TEXT,
                plan_version INTEGER NOT NULL DEFAULT 0,
                verified_revision TEXT,
                pr_url TEXT,
                pr_number INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS runs_issue_status
                ON runs(repo, issue_number, status);
            CREATE TABLE IF NOT EXISTS requests (
                request_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                plan_version INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                github_comment_id INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',
                response_json TEXT,
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                UNIQUE(run_id, kind, plan_version, content_hash)
            );
            CREATE TABLE IF NOT EXISTS pi_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                phase TEXT NOT NULL,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                run_id TEXT,
                repo TEXT,
                issue_number INTEGER,
                kind TEXT NOT NULL,
                message TEXT NOT NULL,
                data_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_run ON events(run_id, id);
            CREATE TABLE IF NOT EXISTS telegram_topics (
                repo TEXT NOT NULL,
                issue_number INTEGER NOT NULL,
                thread_id INTEGER NOT NULL,
                PRIMARY KEY (repo, issue_number)
            );
            """
        )
        await self._ensure_column("runs", "pr_number", "INTEGER")
        await self.conn.commit()

    async def _ensure_column(self, table: str, column: str, sql_type: str) -> None:
        cursor = await self._db().execute(f"PRAGMA table_info({table})")
        columns = {row[1] for row in await cursor.fetchall()}
        if column not in columns:
            await self._db().execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"
            )

    async def close(self) -> None:
        if self.conn is not None:
            await self.conn.close()
            self.conn = None

    def _db(self) -> aiosqlite.Connection:
        if self.conn is None:
            raise RuntimeError("AgentStore is not open")
        return self.conn

    async def claim_delivery(self, delivery_id: str) -> bool:
        cursor = await self._db().execute(
            "INSERT OR IGNORE INTO deliveries(delivery_id, received_at) VALUES (?, ?)",
            (delivery_id, _now()),
        )
        await self._db().commit()
        return cursor.rowcount == 1

    async def release_delivery(self, delivery_id: str) -> None:
        await self._db().execute(
            "DELETE FROM deliveries WHERE delivery_id = ?", (delivery_id,)
        )
        await self._db().commit()

    async def persist_work_item(
        self, delivery_id: str, payload: dict[str, Any]
    ) -> int | None:
        db = self._db()
        now = _now()
        cursor = await db.execute(
            """INSERT OR IGNORE INTO work_items(
                delivery_id, payload_json, status, created_at, updated_at
            ) VALUES (?, ?, 'queued', ?, ?)""",
            (delivery_id, json.dumps(payload, ensure_ascii=False), now, now),
        )
        if cursor.rowcount != 1:
            await db.commit()
            return None
        await db.execute(
            "INSERT OR IGNORE INTO deliveries(delivery_id, received_at) VALUES (?, ?)",
            (delivery_id, now),
        )
        await db.commit()
        return int(cursor.lastrowid)

    async def recover_work_items(self, limit: int) -> list[dict[str, Any]]:
        db = self._db()
        now = _now()
        await db.execute(
            """UPDATE work_items SET status = 'pending', updated_at = ?
               WHERE status IN ('queued', 'processing')""",
            (now,),
        )
        cursor = await db.execute(
            """SELECT id, payload_json FROM work_items
               WHERE status = 'pending' ORDER BY id LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
        ids = [int(row["id"]) for row in rows]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            await db.execute(
                f"UPDATE work_items SET status = 'queued', updated_at = ? "
                f"WHERE id IN ({placeholders})",
                (now, *ids),
            )
        await db.commit()
        return [
            {"id": int(row["id"]), "payload": json.loads(row["payload_json"])}
            for row in rows
        ]

    async def claim_pending_work_items(self, limit: int) -> list[dict[str, Any]]:
        db = self._db()
        cursor = await db.execute(
            """SELECT id, payload_json FROM work_items
               WHERE status = 'pending' ORDER BY id LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
        ids = [int(row["id"]) for row in rows]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            await db.execute(
                f"UPDATE work_items SET status = 'queued', updated_at = ? "
                f"WHERE id IN ({placeholders})",
                (_now(), *ids),
            )
            await db.commit()
        return [
            {"id": int(row["id"]), "payload": json.loads(row["payload_json"])}
            for row in rows
        ]

    async def mark_work_processing(self, item_id: int) -> int:
        await self._db().execute(
            """UPDATE work_items
               SET status = 'processing', attempts = attempts + 1, updated_at = ?
               WHERE id = ?""",
            (_now(), item_id),
        )
        await self._db().commit()
        cursor = await self._db().execute(
            "SELECT attempts FROM work_items WHERE id = ?", (item_id,)
        )
        row = await cursor.fetchone()
        return int(row["attempts"])

    async def mark_work_done(self, item_id: int) -> None:
        await self._db().execute(
            "UPDATE work_items SET status = 'done', updated_at = ? WHERE id = ?",
            (_now(), item_id),
        )
        await self._db().commit()

    async def mark_work_pending(self, item_id: int, error: str = "") -> None:
        await self._db().execute(
            """UPDATE work_items
               SET status = 'pending', last_error = ?, updated_at = ? WHERE id = ?""",
            (error[:2000], _now(), item_id),
        )
        await self._db().commit()

    async def mark_work_failed(self, item_id: int, error: str) -> None:
        await self._db().execute(
            """UPDATE work_items
               SET status = 'failed', last_error = ?, updated_at = ? WHERE id = ?""",
            (error[:2000], _now(), item_id),
        )
        await self._db().commit()

    async def create_run(self, run: dict[str, Any]) -> None:
        now = _now()
        await self._db().execute(
            """INSERT INTO runs(
                run_id, thread_id, repo, issue_number, status, branch, worktree,
                requirements_version, plan_version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run["run_id"], run["thread_id"], run["repo"], run["issue_number"],
                run.get("status", "starting"), run.get("branch"), run.get("worktree"),
                run.get("requirements_version"), run.get("plan_version", 0), now, now,
            ),
        )
        await self._db().commit()

    async def update_run(self, run_id: str, **values: Any) -> None:
        allowed = {
            "thread_id", "status", "branch", "worktree", "requirements_version", "plan_version",
            "verified_revision", "pr_url", "pr_number",
        }
        selected = {key: value for key, value in values.items() if key in allowed}
        if not selected:
            return
        selected["updated_at"] = _now()
        assignments = ", ".join(f"{key} = ?" for key in selected)
        await self._db().execute(
            f"UPDATE runs SET {assignments} WHERE run_id = ?",
            (*selected.values(), run_id),
        )
        await self._db().commit()

    async def get_run(self, run_id: str) -> dict[str, Any] | None:
        cursor = await self._db().execute("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_run_by_pr(self, repo: str, pr_number: int) -> dict[str, Any] | None:
        cursor = await self._db().execute(
            """SELECT * FROM runs
               WHERE repo = ? AND pr_number = ?
               ORDER BY created_at DESC LIMIT 1""",
            (repo, pr_number),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_active_run(self, repo: str, issue_number: int) -> dict[str, Any] | None:
        terminal = ("failed", "stopped", "published", "merged", "closed", "superseded")
        placeholders = ",".join("?" for _ in terminal)
        cursor = await self._db().execute(
            f"""SELECT * FROM runs
                WHERE repo = ? AND issue_number = ? AND status NOT IN ({placeholders})
                ORDER BY created_at DESC LIMIT 1""",
            (repo, issue_number, *terminal),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def ensure_request(
        self,
        request_id: str,
        run_id: str,
        kind: str,
        plan_version: int,
        content_hash: str,
    ) -> dict[str, Any]:
        await self._db().execute(
            """INSERT OR IGNORE INTO requests(
                request_id, run_id, kind, plan_version, content_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            (request_id, run_id, kind, plan_version, content_hash, _now()),
        )
        await self._db().commit()
        request = await self.get_request(request_id)
        if request is None:
            raise RuntimeError("Failed to persist request")
        return request

    async def set_request_comment(self, request_id: str, comment_id: int) -> None:
        await self._db().execute(
            "UPDATE requests SET github_comment_id = ? WHERE request_id = ?",
            (comment_id, request_id),
        )
        await self._db().commit()

    async def get_request(self, request_id: str) -> dict[str, Any] | None:
        cursor = await self._db().execute(
            "SELECT * FROM requests WHERE request_id = ?", (request_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def resolve_request(self, request_id: str, response: dict[str, Any]) -> bool:
        cursor = await self._db().execute(
            """UPDATE requests SET status = 'resolved', response_json = ?, resolved_at = ?
               WHERE request_id = ? AND status = 'pending'""",
            (json.dumps(response, ensure_ascii=False), _now(), request_id),
        )
        await self._db().commit()
        return cursor.rowcount == 1

    async def record_event(
        self,
        kind: str,
        message: str,
        *,
        run_id: str | None = None,
        repo: str | None = None,
        issue_number: int | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        await self._db().execute(
            """INSERT INTO events (created_at, run_id, repo, issue_number, kind, message, data_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                _now(), run_id, repo, issue_number, kind, message,
                json.dumps(data or {}, ensure_ascii=False, default=str),
            ),
        )
        await self._db().commit()

    async def find_run_by_prefix(self, prefix: str) -> dict[str, Any] | None:
        cursor = await self._db().execute(
            "SELECT * FROM runs WHERE run_id LIKE ? ORDER BY created_at DESC LIMIT 1",
            (f"{prefix}%",),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_telegram_topic(self, repo: str, issue_number: int) -> int | None:
        cursor = await self._db().execute(
            "SELECT thread_id FROM telegram_topics WHERE repo = ? AND issue_number = ?",
            (repo, issue_number),
        )
        row = await cursor.fetchone()
        return int(row["thread_id"]) if row else None

    async def set_telegram_topic(self, repo: str, issue_number: int, thread_id: int | None) -> None:
        if thread_id is None:
            await self._db().execute(
                "DELETE FROM telegram_topics WHERE repo = ? AND issue_number = ?",
                (repo, issue_number),
            )
        else:
            await self._db().execute(
                """INSERT INTO telegram_topics (repo, issue_number, thread_id) VALUES (?, ?, ?)
                   ON CONFLICT(repo, issue_number) DO UPDATE SET thread_id = excluded.thread_id""",
                (repo, issue_number, thread_id),
            )
        await self._db().commit()

    async def record_pi_event(
        self, run_id: str, phase: str, event_type: str, payload: dict[str, Any]
    ) -> None:
        compact = json.dumps(payload, ensure_ascii=False, default=str)
        if len(compact) > 20_000:
            compact = json.dumps(
                {
                    "type": payload.get("type", event_type),
                    "toolName": payload.get("toolName"),
                    "isError": payload.get("isError"),
                    "_truncated": True,
                    "preview": compact[:19_000],
                },
                ensure_ascii=False,
                default=str,
            )
        await self._db().execute(
            """INSERT INTO pi_events(run_id, phase, event_type, payload_json, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (run_id, phase, event_type, compact, _now()),
        )
        await self._db().commit()
