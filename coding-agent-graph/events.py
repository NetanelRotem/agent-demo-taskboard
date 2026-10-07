"""Key run events in one place: SQLite (full timeline), the server log, and an
optional Telegram forum group with one topic per issue.

Emitting never blocks or raises: Telegram delivery runs in a background queue and
failures there are logged and dropped. The SQLite `events` table stays complete.

    python events.py                      # recent events
    python events.py --follow             # like tail -f
    python events.py --run-id 12ab34cd    # one run's timeline
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

logger = logging.getLogger("events")

SANDBOX_NAME = re.compile(r"^agent-issue-(\d+)-([0-9a-f]{8})$")
TELEGRAM_TEXT_LIMIT = 3500
QUEUE_SIZE = 500

ICONS = {
    "run_started": "🚀",
    "review_started": "📝",
    "sandbox_created": "🆕",
    "sandbox_started": "🟢",
    "sandbox_stopped": "⏸️",
    "sandbox_deleted": "🗑️",
    "sbx_retry": "🔁",
    "plan_ready": "📋",
    "waiting_for_human": "❓",
    "verification_passed": "✅",
    "verification_failed": "❌",
    "pr_opened": "🔀",
    "review_pushed": "⬆️",
    "run_finished": "🏁",
    "pr_closed": "📦",
    "work_item_failed": "🔥",
}


@dataclass
class Event:
    kind: str
    message: str
    run_id: str | None = None
    repo: str | None = None
    issue_number: int | None = None
    data: dict[str, Any] = field(default_factory=dict)


class NullEvents:
    async def emit(self, kind: str, message: str, **kwargs: Any) -> None:
        return None


class TelegramError(RuntimeError):
    pass


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, store: Any, client: httpx.AsyncClient | None = None):
        self.chat_id = chat_id
        self.store = store
        self.client = client or httpx.AsyncClient(
            base_url=f"https://api.telegram.org/bot{token}/", timeout=20
        )
        self.queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self.topics_disabled = False
        self.worker: asyncio.Task | None = None

    def start(self) -> None:
        self.worker = asyncio.create_task(self._work(), name="telegram-notifier")

    def submit(self, event: Event) -> None:
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("Telegram queue full; dropping %s event", event.kind)

    async def close(self, drain_seconds: float = 5) -> None:
        if self.worker:
            try:
                await asyncio.wait_for(self.queue.join(), timeout=drain_seconds)
            except asyncio.TimeoutError:
                logger.warning("Telegram queue not drained; %s event(s) dropped", self.queue.qsize())
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        await self.client.aclose()

    async def _work(self) -> None:
        while True:
            event = await self.queue.get()
            try:
                await self._deliver(event)
            except Exception as exc:
                logger.warning("Telegram delivery failed for %s: %s", event.kind, exc)
            finally:
                self.queue.task_done()

    async def _deliver(self, event: Event) -> None:
        payload: dict[str, Any] = {
            "chat_id": self.chat_id,
            "text": format_message(event),
            "disable_web_page_preview": True,
        }
        thread_id = await self._thread_for(event)
        if thread_id is not None:
            payload["message_thread_id"] = thread_id
        try:
            await self._call("sendMessage", payload)
        except TelegramError as exc:
            if thread_id is None or "thread not found" not in str(exc).lower():
                raise
            # The topic was deleted in Telegram: forget it and open a new one.
            await self.store.set_telegram_topic(event.repo, event.issue_number, None)
            payload["message_thread_id"] = await self._thread_for(event)
            await self._call("sendMessage", payload)

    async def _thread_for(self, event: Event) -> int | None:
        if self.topics_disabled or not event.repo or event.issue_number is None:
            return None
        existing = await self.store.get_telegram_topic(event.repo, event.issue_number)
        if existing is not None:
            return existing
        title = event.data.get("title") or event.repo
        try:
            result = await self._call(
                "createForumTopic",
                {"chat_id": self.chat_id, "name": f"#{event.issue_number} {title}"[:128]},
            )
        except TelegramError as exc:
            # Not a forum group, or the bot may not manage topics: use the main chat.
            self.topics_disabled = True
            logger.warning("Telegram topics unavailable, posting to the main chat: %s", exc)
            return None
        thread_id = int(result["message_thread_id"])
        await self.store.set_telegram_topic(event.repo, event.issue_number, thread_id)
        return thread_id

    async def _call(self, method: str, payload: dict[str, Any]) -> Any:
        for attempt in range(2):
            response = await self.client.post(method, json=payload)
            body = response.json()
            if body.get("ok"):
                return body.get("result")
            retry_after = (body.get("parameters") or {}).get("retry_after")
            if response.status_code == 429 and retry_after is not None and attempt == 0:
                await asyncio.sleep(min(float(retry_after), 30))
                continue
            raise TelegramError(f"{method}: {body.get('description', response.status_code)}")
        raise TelegramError(f"{method}: rate limited")


def format_message(event: Event) -> str:
    lines = [f"{ICONS.get(event.kind, '•')} {event.message}"]
    footer = []
    if event.run_id:
        footer.append(f"run {event.run_id[:8]}")
    if event.repo and event.issue_number is not None:
        footer.append(f"https://github.com/{event.repo}/issues/{event.issue_number}")
    if footer:
        lines.append(" · ".join(footer))
    text = "\n".join(lines)
    return text if len(text) <= TELEGRAM_TEXT_LIMIT else text[:TELEGRAM_TEXT_LIMIT] + "…"


class EventLog:
    def __init__(
        self,
        store: Any,
        telegram: TelegramNotifier | None = None,
        redact: Callable[[Any], Any] | None = None,
    ):
        self.store = store
        self.telegram = telegram
        self.redact = redact or (lambda value: value)

    async def emit(
        self,
        kind: str,
        message: str,
        *,
        run_id: str | None = None,
        repo: str | None = None,
        issue_number: int | None = None,
        sandbox: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        try:
            if sandbox and not run_id:
                run_id, repo, issue_number = await self._resolve_sandbox(sandbox, repo, issue_number)
            if run_id and (repo is None or issue_number is None):
                run = await self.store.find_run_by_prefix(run_id)
                if run:
                    repo = repo or run["repo"]
                    issue_number = issue_number if issue_number is not None else int(run["issue_number"])
            event = Event(
                kind=kind,
                message=self.redact(message),
                run_id=run_id,
                repo=repo,
                issue_number=issue_number,
                data=self.redact(data or {}),
            )
            await self.store.record_event(
                kind, event.message, run_id=run_id, repo=repo,
                issue_number=issue_number, data=event.data,
            )
            logger.info("[%s run=%s] %s", kind, (run_id or "-")[:8], event.message.splitlines()[0])
            if self.telegram:
                self.telegram.submit(event)
        except Exception:
            logger.warning("Could not record %s event", kind, exc_info=True)

    async def _resolve_sandbox(
        self, sandbox: str, repo: str | None, issue_number: int | None
    ) -> tuple[str | None, str | None, int | None]:
        match = SANDBOX_NAME.match(sandbox)
        if not match:
            return None, repo, issue_number
        run = await self.store.find_run_by_prefix(match.group(2))
        if not run:
            return match.group(2), repo, int(match.group(1))
        return run["run_id"], run["repo"], int(run["issue_number"])


def format_row(row: sqlite3.Row) -> str:
    run = (row["run_id"] or "-")[:8]
    issue = f"#{row['issue_number']}" if row["issue_number"] is not None else "-"
    return f"{row['created_at']} {ICONS.get(row['kind'], '•')} {row['kind']:<20} run={run} issue={issue} {row['message']}"


def main() -> None:
    from dotenv import load_dotenv

    from config import Settings

    load_dotenv()
    parser = argparse.ArgumentParser(description="Show the agent's key events")
    parser.add_argument("--run-id", help="Full run ID or its prefix")
    parser.add_argument("--follow", "-f", action="store_true", help="Wait for new events")
    parser.add_argument("--limit", type=int, default=100, help="Events to show initially")
    parser.add_argument("--db", type=Path, help="Override the SQLite database path")
    args = parser.parse_args()

    path = (args.db or Settings.from_env().database_path).resolve()
    if not path.exists():
        sys.exit(f"No database at {path}")
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    where, params = "", []
    if args.run_id:
        where, params = "WHERE run_id LIKE ?", [f"{args.run_id}%"]
    try:
        rows = conn.execute(
            f"SELECT * FROM (SELECT * FROM events {where} ORDER BY id DESC LIMIT ?) ORDER BY id",
            [*params, args.limit],
        ).fetchall()
    except sqlite3.OperationalError as exc:
        sys.exit(f"No events table yet ({exc}); restart the server once to create it.")
    last_id = 0
    for row in rows:
        print(format_row(row), flush=True)
        last_id = row["id"]
    while args.follow:
        time.sleep(1)
        clause = f"{where} AND id > ?" if where else "WHERE id > ?"
        for row in conn.execute(f"SELECT * FROM events {clause} ORDER BY id", [*params, last_id]):
            print(format_row(row), flush=True)
            last_id = row["id"]


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
