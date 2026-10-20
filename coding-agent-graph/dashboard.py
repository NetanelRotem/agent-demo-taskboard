"""Live dashboard: every run/PR, its flow, costs, and comments, read straight from SQLite.

The server mounts it at GET /dashboard. To browse a database without running the agent:

    python dashboard.py                       # http://127.0.0.1:8090/dashboard
    python dashboard.py --backfill            # first pull comments/titles from GitHub
    python dashboard.py --db path/to/agent.sqlite3 --port 8090
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse

PAGE = Path(__file__).with_name("dashboard.html")
TERMINAL = {"failed", "stopped", "published", "merged", "closed", "superseded", "no_changes"}

# The flow every run moves through, in order; each run status maps to the stage it is in.
STAGES = [
    ("trigger", "Triggered"),
    ("plan", "Plan"),
    ("approval", "Approval"),
    ("implement", "Implement"),
    ("verify", "Verify"),
    ("pr", "PR opened"),
    ("review", "Review"),
    ("done", "Done"),
]
STATUS_STAGE = {
    "starting": "trigger",
    "reading_issue": "trigger",
    "planning": "plan",
    "waiting_for_human": "approval",
    "implementing": "implement",
    "verifying": "verify",
    "verified": "pr",
    "reviewing": "review",
    "reading_review": "review",
    "implementing_review": "review",
    "verifying_review": "review",
}
PHASE_STAGE = {"plan": "plan", "implement": "implement", "implement_review": "review"}


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).isoformat() if value else None


def _loads(text: str | None) -> Any:
    try:
        return json.loads(text) if text else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(
        part.get("text", "") for part in content or [] if isinstance(part, dict) and part.get("type") == "text"
    )


def _short(value: Any, limit: int = 240) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


class DashboardDB:
    """Read-only queries. A fresh connection per call keeps it safe next to the writer."""

    def __init__(self, path: Path):
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        if not self.path.exists():
            raise FileNotFoundError(self.path)
        conn = sqlite3.connect(f"file:{self.path.resolve().as_posix()}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def _tables(self, conn: sqlite3.Connection) -> set[str]:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    # -- change detection for the live stream -------------------------------------------

    def version(self) -> str:
        try:
            conn = self._connect()
        except FileNotFoundError:
            return "none"
        try:
            tables = self._tables(conn)
            parts = [
                conn.execute("SELECT COALESCE(MAX(updated_at), '') FROM runs").fetchone()[0],
                conn.execute("SELECT COALESCE(MAX(id), 0) FROM pi_events").fetchone()[0],
                conn.execute("SELECT COALESCE(MAX(updated_at), '') FROM work_items").fetchone()[0],
                conn.execute(
                    "SELECT COUNT(*) || COALESCE(MAX(resolved_at), '') FROM requests"
                ).fetchone()[0],
            ]
            for table in ("events", "comments"):
                if table in tables:
                    parts.append(conn.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()[0])
            if "comments" in tables:
                parts.append(conn.execute("SELECT COALESCE(SUM(LENGTH(body)), 0) FROM comments").fetchone()[0])
            return "|".join(str(part) for part in parts)
        finally:
            conn.close()

    # -- run list ------------------------------------------------------------------------

    def runs(self, limit: int = 100) -> list[dict[str, Any]]:
        try:
            conn = self._connect()
        except FileNotFoundError:
            return []
        try:
            rows = [dict(row) for row in conn.execute(
                "SELECT * FROM runs ORDER BY updated_at DESC LIMIT ?", (limit,)
            )]
            costs = {
                row["run_id"]: dict(row)
                for row in conn.execute(
                    """SELECT run_id,
                         SUM(COALESCE(json_extract(payload_json, '$.message.usage.cost.total'), 0)) AS cost,
                         SUM(COALESCE(json_extract(payload_json, '$.message.usage.input'), 0)
                           + COALESCE(json_extract(payload_json, '$.message.usage.output'), 0)
                           + COALESCE(json_extract(payload_json, '$.message.usage.cacheRead'), 0)) AS tokens,
                         MAX(created_at) AS last_pi
                       FROM pi_events WHERE event_type = 'message_end' GROUP BY run_id"""
                )
            }
            tables = self._tables(conn)
            comment_counts: dict[tuple[str, int], int] = {}
            if "comments" in tables:
                for row in conn.execute("SELECT repo, number, COUNT(*) AS n FROM comments GROUP BY 1, 2"):
                    comment_counts[(row["repo"], row["number"])] = row["n"]
            pending = {
                row["run_id"]
                for row in conn.execute(
                    "SELECT DISTINCT run_id FROM requests WHERE status = 'pending' "
                    "AND kind IN ('clarification', 'approval')"
                )
            }
            out = []
            for run in rows:
                usage = costs.get(run["run_id"], {})
                comments = comment_counts.get((run["repo"], run["issue_number"]), 0)
                if run.get("pr_number"):
                    comments += comment_counts.get((run["repo"], run["pr_number"]), 0)
                out.append({
                    "run_id": run["run_id"],
                    "short_id": run["run_id"][:8],
                    "repo": run["repo"],
                    "issue_number": run["issue_number"],
                    "title": run.get("title") or "",
                    "status": run["status"],
                    "stage": stage_of(run["status"]),
                    "terminal": run["status"] in TERMINAL,
                    "needs_human": run["run_id"] in pending,
                    "pr_number": run.get("pr_number"),
                    "pr_url": run.get("pr_url"),
                    "branch": run.get("branch"),
                    "created_at": run["created_at"],
                    "updated_at": max(run["updated_at"], usage.get("last_pi") or ""),
                    "cost": round(usage.get("cost") or 0, 6),
                    "tokens": int(usage.get("tokens") or 0),
                    "comments": comments,
                })
            return out
        finally:
            conn.close()

    # -- one run's timeline --------------------------------------------------------------

    def timeline(self, run_prefix: str) -> dict[str, Any] | None:
        try:
            conn = self._connect()
        except FileNotFoundError:
            return None
        try:
            row = conn.execute(
                "SELECT * FROM runs WHERE run_id LIKE ? ORDER BY created_at DESC LIMIT 1",
                (run_prefix.replace("%", "").replace("_", "") + "%",),
            ).fetchone()
            if row is None:
                return None
            run = dict(row)
            return build_timeline(conn, run, self._tables(conn))
        finally:
            conn.close()

    def thread_id(self, run_id: str) -> str:
        conn = self._connect()
        try:
            return conn.execute("SELECT thread_id FROM runs WHERE run_id = ?", (run_id,)).fetchone()[0]
        finally:
            conn.close()

    def pi_steps(self, run_prefix: str, first_id: int, last_id: int) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT * FROM pi_events WHERE run_id LIKE ? AND id BETWEEN ? AND ?
                   AND event_type IN ('tool_execution_start', 'tool_execution_end', 'message_end')
                   ORDER BY id LIMIT 2000""",
                (run_prefix.replace("%", "") + "%", first_id, last_id),
            ).fetchall()
        finally:
            conn.close()
        steps: list[dict[str, Any]] = []
        open_tools: dict[str, dict[str, Any]] = {}
        for row in rows:
            event = _loads(row["payload_json"])
            kind = row["event_type"]
            if event.get("_truncated"):
                steps.append({"type": "note", "at": row["created_at"], "text": _short(event.get("preview"), 400)})
                continue
            if kind == "tool_execution_start":
                step = {
                    "type": "tool",
                    "at": row["created_at"],
                    "tool": event.get("toolName"),
                    "args": _short(event.get("args") or {}, 400),
                    "ok": None,
                    "result": "",
                }
                steps.append(step)
                open_tools[str(event.get("toolCallId") or len(steps))] = step
            elif kind == "tool_execution_end":
                step = open_tools.pop(str(event.get("toolCallId")), None)
                result = _short(_text_of((event.get("result") or {}).get("content")), 600)
                if step is None:
                    steps.append({"type": "tool", "at": row["created_at"], "tool": event.get("toolName"),
                                  "args": "", "ok": not event.get("isError"), "result": result})
                else:
                    step.update(ok=not event.get("isError"), result=result, ended_at=row["created_at"])
            else:
                message = event.get("message") or {}
                if message.get("role") != "assistant":
                    continue
                text = _text_of(message.get("content")).strip()
                usage = message.get("usage") or {}
                cost = ((usage.get("cost") or {}).get("total")) or 0
                if text or cost:
                    steps.append({"type": "message", "at": row["created_at"], "text": _short(text, 1200),
                                  "cost": cost, "model": message.get("model")})
        return steps


def stage_of(status: str) -> str:
    if status in {"merged", "closed", "published"}:
        return "done"
    return STATUS_STAGE.get(status, "trigger")


def _pi_segments(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for row in conn.execute(
        "SELECT id, phase, event_type, payload_json, created_at FROM pi_events WHERE run_id = ? ORDER BY id",
        (run_id,),
    ):
        kind = row["event_type"]
        if current is None or kind == "agent_start" or row["phase"] != current["phase"]:
            current = {
                "type": "pi",
                "phase": row["phase"],
                "stage": PHASE_STAGE.get(row["phase"], "implement"),
                "at": row["created_at"],
                "end": row["created_at"],
                "first_id": row["id"],
                "last_id": row["id"],
                "cost": 0.0, "input": 0, "output": 0, "cache_read": 0,
                "messages": 0, "tools": 0, "tool_errors": 0,
                "model": None, "summary": "", "running": True,
            }
            segments.append(current)
        current["end"] = row["created_at"]
        current["last_id"] = row["id"]
        if kind == "agent_end" or kind == "agent_settled":
            current["running"] = False
        elif kind == "tool_execution_start":
            current["tools"] += 1
        elif kind == "tool_execution_end" or kind == "message_end":
            event = _loads(row["payload_json"])
            if kind == "tool_execution_end":
                current["tool_errors"] += 1 if event.get("isError") else 0
                continue
            message = event.get("message") or {}
            if message.get("role") != "assistant":
                continue
            current["messages"] += 1
            usage = message.get("usage") or {}
            current["cost"] += float((usage.get("cost") or {}).get("total") or 0)
            current["input"] += int(usage.get("input") or 0)
            current["output"] += int(usage.get("output") or 0)
            current["cache_read"] += int(usage.get("cacheRead") or 0)
            current["model"] = message.get("model") or current["model"]
            text = _text_of(message.get("content")).strip()
            if text.startswith("{"):
                # Structured results (submit_result) carry a human summary field.
                text = str(_loads(text).get("summary") or text) if isinstance(_loads(text), dict) else text
            if text:
                current["summary"] = _short(text, 500)
    return segments


PI_STATUSES = {"planning", "implementing", "implementing_review", "reading_review"}


def _live_segments(segments: list[dict[str, Any]], status: str) -> list[dict[str, Any]]:
    """Only the newest session of a run that is still working can be running; drop
    sessions that were started and abandoned without doing anything."""
    for index, segment in enumerate(segments):
        if segment["running"] and (index < len(segments) - 1 or status not in PI_STATUSES):
            segment["running"] = False
    return [s for s in segments if s["running"] or s["messages"] or s["tools"]]


def build_timeline(conn: sqlite3.Connection, run: dict[str, Any], tables: set[str]) -> dict[str, Any]:
    repo, issue, pr = run["repo"], int(run["issue_number"]), run.get("pr_number")
    numbers = {issue} | ({int(pr)} if pr else set())
    start = parse_time(run["created_at"]) - timedelta(minutes=2)
    # Comments and commands on the same issue belong to this run until the next run starts.
    nxt = conn.execute(
        "SELECT MIN(created_at) FROM runs WHERE repo = ? AND issue_number = ? AND created_at > ?",
        (repo, issue, run["created_at"]),
    ).fetchone()[0]
    end = parse_time(nxt) - timedelta(minutes=2) if nxt else None

    def in_window(value: str | None) -> bool:
        moment = parse_time(value)
        return bool(moment) and moment >= start and (end is None or moment < end)

    items: list[dict[str, Any]] = []
    comments: dict[tuple[str, int], dict[str, Any]] = {}
    if "comments" in tables:
        placeholders = ",".join("?" for _ in numbers)
        for row in conn.execute(
            f"SELECT * FROM comments WHERE repo = ? AND number IN ({placeholders})", (repo, *numbers)
        ):
            if in_window(row["created_at"]):
                comments[(row["source"], row["comment_id"])] = dict(row)

    # Human commands (start / approve / review / merge) from the work queue.
    for row in conn.execute("SELECT * FROM work_items ORDER BY id"):
        payload = _loads(row["payload_json"])
        if payload.get("repo") != repo or not in_window(row["created_at"]):
            continue
        if int(payload.get("issue_number") or 0) not in numbers and payload.get("pull_number") not in numbers:
            continue
        command = payload.get("command") or {}
        name = command.get("name")
        source = "review" if name == "review" else "issue"
        comment = comments.pop((source, int(payload.get("comment_id") or 0)), None)
        title = {
            "review": "Review submitted",
            "cleanup": f"PR {command.get('text') or 'closed'}",
        }.get(name, f"/agent {name}")
        items.append({
            "type": "command",
            "stage": {"start": "trigger", "approve": "approval", "answer": "approval",
                      "review": "review", "cleanup": "done"}.get(name, "trigger"),
            "at": (comment or {}).get("created_at") or row["created_at"],
            "title": title,
            "actor": payload.get("user"),
            "status": row["status"],
            "attempts": row["attempts"],
            "error": row["last_error"],
            "body": (comment or {}).get("body") or command.get("text") or "",
            "url": (comment or {}).get("url"),
        })

    # What the agent asked for (plan approval, clarification) and the human's answer.
    for row in conn.execute("SELECT * FROM requests WHERE run_id = ? ORDER BY created_at", (run["run_id"],)):
        comment = comments.pop(("issue", int(row["github_comment_id"] or 0)), None) if row["github_comment_id"] else None
        response = _loads(row["response_json"])
        items.append({
            "type": "request",
            "stage": {"approval": "approval", "clarification": "approval"}.get(row["kind"]),
            "kind": row["kind"],
            "at": row["created_at"],
            "resolved_at": row["resolved_at"],
            "status": row["status"],
            "plan_version": row["plan_version"],
            "body": (comment or {}).get("body") or "",
            "url": (comment or {}).get("url"),
            "response": response,
        })

    if "events" in tables:
        for row in conn.execute("SELECT * FROM events WHERE run_id = ? ORDER BY id", (run["run_id"],)):
            items.append({
                "type": "event",
                "stage": _event_stage(row["kind"]),
                "kind": row["kind"],
                "at": row["created_at"],
                "title": row["message"].splitlines()[0][:200],
                "body": row["message"],
            })

    items.extend(_live_segments(_pi_segments(conn, run["run_id"]), run["status"]))

    for comment in comments.values():
        items.append({
            "type": "comment",
            "stage": None,
            "at": comment["created_at"],
            "source": comment["source"],
            "actor": comment["author"],
            "from_agent": bool(comment["from_agent"]),
            "body": comment["body"],
            "url": comment["url"],
            "path": comment["path"],
            "state": comment["state"],
            "on_pr": bool(pr and comment["number"] == pr),
        })

    for item in items:
        item["_t"] = parse_time(item["at"]) or start
    items.sort(key=lambda item: item["_t"])
    # Comments, sandbox events and notices belong to whichever stage was underway.
    stage = "trigger"
    for item in items:
        del item["_t"]
        if item.get("stage") is None:
            item["stage"] = "review" if item["type"] == "comment" and item.get("source") != "issue" else stage
        elif item["stage"] in {"verify", "pr"} and stage == "review":
            item["stage"] = "review"  # checks run again inside each review round
        stage = item["stage"]
    _number_review_rounds(items)

    return {
        "run": {**run, "short_id": run["run_id"][:8], "stage": stage_of(run["status"]),
                "terminal": run["status"] in TERMINAL},
        "stages": build_stages(run, items),
        "items": items,
        "costs": _cost_breakdown(items),
    }


def _starts_review(item: dict[str, Any]) -> bool:
    return (
        (item["type"] == "command" and item.get("title") == "Review submitted")
        or (item["type"] == "event" and item.get("kind") == "review_started")
        or (item["type"] == "comment" and item.get("source") in {"review", "review_comment"})
    )


def _number_review_rounds(items: list[dict[str, Any]]) -> None:
    """Each PR review starts a round: feedback, Pi fixes, verify, push. The review, its
    inline comments and the review_started event that follows all open the same round;
    a new one begins only once the previous round has done some work."""
    rounds, worked = 0, False
    for item in items:
        if item["stage"] != "review":
            continue
        if _starts_review(item) and (rounds == 0 or worked):
            rounds, worked = rounds + 1, False
        if item["type"] == "pi" or item.get("kind") == "review_pushed":
            worked = True
        item["round"] = max(rounds, 1)


def _event_stage(kind: str) -> str | None:
    if kind.startswith("verification"):
        return "verify"
    if kind in {"pr_opened"}:
        return "pr"
    if kind in {"review_started", "review_pushed"}:
        return "review"
    if kind in {"plan_ready"}:
        return "plan"
    if kind in {"waiting_for_human"}:
        return "approval"
    if kind in {"run_finished", "pr_closed"}:
        return "done"
    if kind == "run_started":
        return "trigger"
    return None


def _cost_breakdown(items: list[dict[str, Any]]) -> dict[str, Any]:
    phases: dict[str, dict[str, Any]] = {}
    for item in items:
        if item["type"] != "pi":
            continue
        phase = phases.setdefault(item["phase"], {"cost": 0.0, "tokens": 0, "runs": 0, "seconds": 0.0})
        phase["cost"] += item["cost"]
        phase["tokens"] += item["input"] + item["output"] + item["cache_read"]
        phase["runs"] += 1
        a, b = parse_time(item["at"]), parse_time(item["end"])
        if a and b:
            phase["seconds"] += (b - a).total_seconds()
    total = sum(phase["cost"] for phase in phases.values())
    return {"total": total, "phases": phases}


def build_stages(run: dict[str, Any], items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    current = stage_of(run["status"])
    order = [key for key, _ in STAGES]
    failed = run["status"] in {"failed", "stopped", "superseded"}
    spans: dict[str, dict[str, Any]] = {}
    for item in items:
        key = item.get("stage")
        if key not in spans:
            spans[key] = {"start": item["at"], "end": item.get("end") or item.get("resolved_at") or item["at"],
                          "cost": 0.0, "count": 0}
        span = spans[key]
        span["end"] = max(span["end"], item.get("end") or item.get("resolved_at") or item["at"],
                          key=lambda v: parse_time(v) or datetime.min.replace(tzinfo=timezone.utc))
        span["cost"] += item.get("cost", 0) if item["type"] == "pi" else 0
        span["count"] += 1
    # Older runs have no events rows: infer from run fields what must have happened.
    if run.get("verified_revision") and "verify" not in spans:
        spans["verify"] = {"start": None, "end": None, "cost": 0.0, "count": 0}
    if run.get("pr_number") and "pr" not in spans:
        spans["pr"] = {"start": None, "end": None, "cost": 0.0, "count": 0}
    reached = max([order.index(key) for key in spans if key in order] + [order.index(current)])
    if failed:
        # Nothing after the trigger means it broke while planning.
        current = order[max(reached, order.index("plan"))]
    out = []
    for index, (key, label) in enumerate(STAGES):
        span = spans.get(key)
        if run["status"] in {"merged", "closed", "published", "no_changes"}:
            state = "done" if span or key == "done" else "skipped"
        elif failed and key == current:
            state = "failed"
        elif key == current and not failed:
            state = "waiting" if key in {"approval", "review"} and run["status"] in {"waiting_for_human", "reviewing"} else "active"
        elif index < order.index(current):
            state = "done" if span else "skipped"
        else:
            state = "pending"
        rounds = {item.get("round") for item in items if item.get("stage") == key and item.get("round")}
        sessions = sum(1 for item in items if item["type"] == "pi" and item.get("stage") == key)
        out.append({
            "key": key, "label": label, "state": state,
            "iterations": len(rounds) if key == "review" else sum(
                1 for item in items if item.get("stage") == "verify" and item["type"] == "event"
            ) if key == "verify" else sessions,
            "start": (span or {}).get("start"), "end": (span or {}).get("end"),
            "cost": (span or {}).get("cost", 0.0), "count": (span or {}).get("count", 0),
        })
    return out


def router(db_path: Callable[[], Path]) -> APIRouter:
    api = APIRouter()

    def db() -> DashboardDB:
        return DashboardDB(db_path())

    @api.get("/dashboard", response_class=HTMLResponse)
    async def page() -> str:
        return PAGE.read_text(encoding="utf-8")

    @api.get("/api/runs")
    async def runs() -> list[dict[str, Any]]:
        return await asyncio.to_thread(db().runs)

    @api.get("/api/runs/{run_id}")
    async def timeline(run_id: str) -> dict[str, Any]:
        result = await asyncio.to_thread(db().timeline, run_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return result

    @api.get("/api/runs/{run_id}/pi")
    async def pi(run_id: str, first: int, last: int) -> list[dict[str, Any]]:
        return await asyncio.to_thread(db().pi_steps, run_id, first, last)

    @api.get("/api/stream")
    async def stream(request: Request) -> StreamingResponse:
        async def changes():
            last = None
            ticks = 0
            while not await request.is_disconnected():
                try:
                    version = await asyncio.to_thread(db().version)
                except sqlite3.Error:
                    version = last
                if version != last:
                    last = version
                    yield f"event: change\ndata: {json.dumps({'version': version})}\n\n"
                elif ticks % 15 == 0:
                    yield ": keepalive\n\n"
                ticks += 1
                await asyncio.sleep(1)

        return StreamingResponse(
            changes(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return api


async def _backfill(path: Path) -> None:
    from config import Settings
    from github_client import GitHubClient
    from service import backfill_comments
    from storage import AgentStore

    store = AgentStore(path)
    await store.open()
    github = GitHubClient(Settings.from_env().github_token)
    try:
        count = await backfill_comments(store, github)
        print(f"Backfilled {count} comment(s)")
    finally:
        await github.close()
        await store.close()


def main() -> None:
    import uvicorn
    from dotenv import load_dotenv
    from fastapi import FastAPI
    from fastapi.responses import RedirectResponse

    from config import Settings

    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, help="Override the SQLite database path")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--backfill", action="store_true", help="Fetch comments and titles from GitHub first")
    args = parser.parse_args()
    path = (args.db or Settings.from_env().database_path).resolve()
    if args.backfill:
        asyncio.run(_backfill(path))
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # The graph page reads LangGraph checkpoints from the same database.
        import aiosqlite
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        from agent_graph import CodingAgentGraph

        conn = await aiosqlite.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        saver = AsyncSqliteSaver(conn)
        app.state.graph = CodingAgentGraph(None, None, None, None, None, saver).graph  # type: ignore[arg-type]
        try:
            yield
        finally:
            await conn.close()

    app = FastAPI(title="agent-dashboard", lifespan=lifespan)
    app.include_router(router(lambda: path))

    @app.get("/graph", response_class=HTMLResponse)
    async def graph(run_id: str | None = None) -> str:
        import graph_view

        db = DashboardDB(path)
        runs = await asyncio.to_thread(db.runs, 20)
        if not run_id:
            return await graph_view.render(None, None, runs)
        found = next((r for r in runs if r["run_id"].startswith(run_id)), None)
        if found is None:
            raise HTTPException(status_code=404, detail="Run not found")
        run = {**found, "thread_id": await asyncio.to_thread(db.thread_id, found["run_id"])}
        return await graph_view.render(app.state.graph, run, [])

    app.add_api_route("/", lambda: RedirectResponse("/dashboard"), include_in_schema=False)
    print(f"Dashboard on http://{args.host}:{args.port}/dashboard  (db: {path})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
