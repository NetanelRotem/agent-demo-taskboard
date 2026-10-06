"""Inspect persisted Pi JSON events from the local SQLite database."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from config import Settings


def compact(value: Any, limit: int = 1600) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "..."


def result_text(result: dict) -> str:
    return " ".join(
        item.get("text", "")
        for item in result.get("content", [])
        if item.get("type") == "text"
    )


def message_text(message: dict) -> str:
    content = message.get("content") or []
    if isinstance(content, str):
        return content
    return "".join(
        item.get("text", "") for item in content if item.get("type") == "text"
    )


def format_event(row: sqlite3.Row) -> str:
    event_type = row["event_type"]
    prefix = f"{row['created_at']} run={row['run_id'][:8]} phase={row['phase']}"
    raw_payload = row["payload_json"]
    try:
        event = json.loads(raw_payload)
    except (TypeError, json.JSONDecodeError):
        preview = str(raw_payload).replace("\r", " ").replace("\n", " ")[:2000]
        return f"{prefix} {event_type}: [malformed stored payload] {preview}"
    if event.get("_truncated"):
        preview = str(event.get("preview", "")).replace("\r", " ").replace("\n", " ")
        return f"{prefix} {event_type}: [truncated] {preview[:2000]}"
    if event_type == "tool_execution_start":
        detail = f"{event.get('toolName')} {compact(event.get('args', {}))}"
    elif event_type == "tool_execution_update":
        detail = f"{event.get('toolName')} {result_text(event.get('partialResult') or {})}"
    elif event_type == "tool_execution_end":
        status = "ERROR" if event.get("isError") else "OK"
        detail = f"{event.get('toolName')} {status} {result_text(event.get('result') or {})}"
    elif event_type == "message_end":
        message = event.get("message") or {}
        detail = message_text(message) if message.get("role") == "assistant" else message.get("role", "")
    else:
        detail = compact(event)
    return f"{prefix} {event_type}: {detail[:2000]}"


def read_rows(
    connection: sqlite3.Connection,
    after_id: int,
    run_id: str | None,
    limit: int,
) -> list[sqlite3.Row]:
    if after_id == 0:
        if run_id:
            rows = connection.execute(
                """SELECT * FROM pi_events
                   WHERE run_id LIKE ? ORDER BY id DESC LIMIT ?""",
                (f"{run_id}%", limit),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM pi_events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return list(reversed(rows))
    if run_id:
        return connection.execute(
            """SELECT * FROM pi_events
               WHERE id > ? AND run_id LIKE ? ORDER BY id ASC LIMIT ?""",
            (after_id, f"{run_id}%", limit),
        ).fetchall()
    return connection.execute(
        "SELECT * FROM pi_events WHERE id > ? ORDER BY id ASC LIMIT ?",
        (after_id, limit),
    ).fetchall()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    load_dotenv()
    parser = argparse.ArgumentParser(description="Show persisted Pi activity")
    parser.add_argument("--run-id", help="Full run ID or its prefix")
    parser.add_argument("--follow", "-f", action="store_true", help="Wait for new events")
    parser.add_argument("--limit", type=int, default=200, help="Maximum events per read")
    parser.add_argument("--db", type=Path, help="Override the SQLite database path")
    args = parser.parse_args()

    path = (args.db or Settings.from_env().database_path).resolve()
    if not path.exists():
        raise SystemExit(f"Pi event database does not exist yet: {path}")

    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    last_id = 0
    try:
        while True:
            rows = read_rows(connection, last_id, args.run_id, args.limit)
            for row in rows:
                print(format_event(row), flush=True)
                last_id = row["id"]
            if not args.follow:
                break
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        connection.close()


if __name__ == "__main__":
    main()
