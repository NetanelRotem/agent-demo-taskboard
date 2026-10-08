from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from agent_graph import CodingAgentGraph
from agent_models import AgentCommand
from config import Settings
from events import EventLog, TelegramNotifier
from github_client import GitHubClient
from pi_runner import PiRunner
from storage import AgentStore
from workspace import WorkspaceManager

logger = logging.getLogger(__name__)
pi_logger = logging.getLogger("pi")

SENSITIVE_KEY_PARTS = ("token", "secret", "api_key", "authorization", "password")


@dataclass(frozen=True)
class WorkItem:
    command: AgentCommand
    repo: str
    issue_number: int
    comment_id: int
    user: str
    pull_number: int | None = None
    review_id: int | None = None


class AgentService:
    def __init__(self, settings: Settings, github: GitHubClient | None = None):
        self.settings = settings
        self.store = AgentStore(settings.database_path)
        self.github = github or GitHubClient(settings.github_token)
        self.telegram = (
            TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id, self.store)
            if settings.telegram_enabled
            else None
        )
        self.events = EventLog(self.store, self.telegram, redact=self._redact)
        self.sandbox = None
        if settings.execution_mode == "cloud":
            from cloud_sandbox import CloudWorkspaceManager, SandboxClient

            self.sandbox = SandboxClient(settings, self.events)
            self.workspace = CloudWorkspaceManager(settings, self.sandbox)
        else:
            self.workspace = WorkspaceManager(settings)
        self.queue: asyncio.Queue[tuple[int, WorkItem]] = asyncio.Queue(
            maxsize=settings.queue_size
        )
        self.worker_task: asyncio.Task | None = None
        self.checkpoint_conn: aiosqlite.Connection | None = None
        self.checkpointer: AsyncSqliteSaver | None = None
        self.agent_graph: CodingAgentGraph | None = None
        self.langfuse_client: Any | None = None
        self.langfuse_handler: Any | None = None

    async def open(self) -> None:
        if sys.version_info < (3, 11):
            raise RuntimeError("Python 3.11+ is required for async LangGraph interrupts")
        if self.settings.langfuse_enabled:
            from langfuse import get_client
            from langfuse.langchain import CallbackHandler

            self.langfuse_client = get_client()
            self.langfuse_handler = CallbackHandler()
            logger.info("Langfuse tracing enabled for LangGraph runs")
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        await self.store.open()
        self.checkpoint_conn = await aiosqlite.connect(self.settings.database_path)
        self.checkpointer = AsyncSqliteSaver(self.checkpoint_conn)
        await self.checkpointer.setup()
        if self.sandbox is not None:
            from cloud_sandbox import CloudPiRunner

            pi: PiRunner = CloudPiRunner(
                self.settings,
                event_sink=self._record_pi_event,
                cancel_check=self._stop_requested,
                sandbox=self.sandbox,
            )
            logger.info("Execution mode: cloud (Docker Cloud Sandboxes via sbx)")
        else:
            pi = PiRunner(
                self.settings,
                event_sink=self._record_pi_event,
                cancel_check=self._stop_requested,
            )
        self.agent_graph = CodingAgentGraph(
            self.settings,
            self.store,
            self.github,
            pi,
            self.workspace,
            self.checkpointer,
            events=self.events,
        )
        if self.telegram:
            self.telegram.start()
            logger.info("Telegram notifications enabled")
        recovered = await self.store.recover_work_items(self.settings.queue_size)
        for job in recovered:
            self.queue.put_nowait((job["id"], self._deserialize_item(job["payload"])))
        if recovered:
            logger.warning("Recovered %s unfinished work item(s) from SQLite", len(recovered))
        self.worker_task = asyncio.create_task(self._worker(), name="coding-agent-worker")

    async def close(self) -> None:
        if self.worker_task:
            self.worker_task.cancel()
            await asyncio.gather(self.worker_task, return_exceptions=True)
        await self.github.close()
        if self.telegram:
            await self.telegram.close()
        if self.checkpoint_conn:
            await self.checkpoint_conn.close()
        await self.store.close()
        if self.langfuse_client:
            await asyncio.to_thread(self.langfuse_client.shutdown)

    async def _record_pi_event(self, run_id: str, phase: str, event: dict) -> None:
        event_type = str(event.get("type", "unknown"))
        visible_types = {
            "agent_start", "agent_end", "agent_settled", "message_end",
            "tool_execution_start", "tool_execution_update", "tool_execution_end",
            "auto_retry_start", "auto_retry_end", "compaction_start", "compaction_end",
        }
        if event_type not in visible_types:
            return

        safe_event = self._redact(event)
        await self.store.record_pi_event(run_id, phase, event_type, safe_event)
        prefix = f"[run={run_id[:8]} phase={phase}]"

        if event_type == "tool_execution_start":
            pi_logger.info(
                "%s tool start: %s %s",
                prefix,
                safe_event.get("toolName", "unknown"),
                self._compact(safe_event.get("args", {}), 1200),
            )
        elif event_type == "tool_execution_update":
            pi_logger.debug(
                "%s tool update: %s %s",
                prefix,
                safe_event.get("toolName", "unknown"),
                self._compact(safe_event.get("partialResult", {}), 1200),
            )
        elif event_type == "tool_execution_end":
            level = logging.ERROR if safe_event.get("isError") else logging.INFO
            pi_logger.log(
                level,
                "%s tool end: %s status=%s result=%s",
                prefix,
                safe_event.get("toolName", "unknown"),
                "error" if safe_event.get("isError") else "ok",
                self._compact(safe_event.get("result", {}), 1200),
            )
        elif event_type == "message_end":
            message = safe_event.get("message") or {}
            if message.get("role") == "assistant":
                pi_logger.info(
                    "%s assistant: %s", prefix, self._message_text(message)[:2000]
                )
        elif event_type.startswith("auto_retry"):
            pi_logger.warning("%s %s %s", prefix, event_type, self._compact(safe_event, 1000))
        elif event_type.startswith("compaction"):
            pi_logger.info("%s %s %s", prefix, event_type, self._compact(safe_event, 1000))
        else:
            pi_logger.info("%s %s", prefix, event_type)

    def _redact(self, value: Any, key: str = "") -> Any:
        if any(part in key.lower() for part in SENSITIVE_KEY_PARTS):
            return "[REDACTED]"
        if isinstance(value, dict):
            return {name: self._redact(item, str(name)) for name, item in value.items()}
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        if isinstance(value, str):
            secrets = [
                self.settings.github_token,
                os.getenv("OPENROUTER_API_KEY", ""),
                self.settings.telegram_bot_token,
            ]
            for secret in secrets:
                if secret:
                    value = value.replace(secret, "[REDACTED]")
        return value

    @staticmethod
    def _compact(value: Any, limit: int) -> str:
        text = json.dumps(value, ensure_ascii=False, default=str)
        return text if len(text) <= limit else text[:limit] + "…"

    @staticmethod
    def _message_text(message: dict) -> str:
        content = message.get("content") or []
        if isinstance(content, str):
            return content
        return "".join(
            item.get("text", "") for item in content if item.get("type") == "text"
        )

    async def _stop_requested(self, run_id: str) -> bool:
        run = await self.store.get_run(run_id)
        return bool(run and run.get("status") == "stop_requested")

    def is_authorized(self, user: str) -> bool:
        return user in self.settings.authorized_users

    async def enqueue(self, item: WorkItem, delivery_id: str) -> str:
        if not self.is_authorized(item.user):
            return "ignored" if item.command.name in {"review", "cleanup"} else "forbidden"
        if self.queue.full():
            return "busy"
        if item.command.name in {"answer", "approve", "reject"} and not item.command.request_id:
            item = await self._bind_pending_request(item)
        item_id = await self.store.persist_work_item(
            delivery_id, self._serialize_item(item)
        )
        if item_id is None:
            return "duplicate"
        if item.command.name == "stop":
            run = await self.store.get_active_run(item.repo, item.issue_number)
            if run:
                await self.store.update_run(run["run_id"], status="stop_requested")
        try:
            self.queue.put_nowait((item_id, item))
        except asyncio.QueueFull:
            await self.store.mark_work_pending(item_id)
        return "accepted"

    async def _bind_pending_request(self, item: WorkItem) -> WorkItem:
        """Pin a command typed without an id to the request pending right now, so a
        replay after a restart targets the same request rather than a newer one."""
        run = await self.store.get_active_run(item.repo, item.issue_number)
        request = run and await self.store.get_latest_human_request(run["run_id"], pending_only=True)
        if not request:
            return item
        return replace(item, command=replace(item.command, request_id=request["request_id"]))

    async def _worker(self) -> None:
        while True:
            item_id, item = await self.queue.get()
            attempts = await self.store.mark_work_processing(item_id)
            logger.info(
                "Processing work item id=%s command=%s repo=%s issue=%s pr=%s attempt=%s",
                item_id,
                item.command.name,
                item.repo,
                item.issue_number,
                item.pull_number,
                attempts,
            )
            try:
                await self._acknowledge(item)
                await self.handle(item)
            except asyncio.CancelledError:
                await self.store.mark_work_pending(item_id, "Worker cancelled during shutdown")
                raise
            except Exception as exc:
                logger.exception("Agent work item failed")
                if attempts < 3:
                    await self.store.mark_work_pending(item_id, str(exc))
                else:
                    await self.store.mark_work_failed(item_id, str(exc))
                    await self._fail_active_run(item)
                    await self.events.emit(
                        "work_item_failed",
                        f"`{item.command.name}` failed after 3 attempts: {str(exc)[:1500]}",
                        repo=item.repo,
                        issue_number=item.issue_number,
                    )
                    try:
                        await self.github.post_comment(
                            item.repo,
                            item.issue_number,
                            "The coding agent failed unexpectedly after 3 attempts. "
                            "Check the server logs; the worktree was preserved.",
                        )
                    except Exception:
                        logger.exception("Could not report agent failure to GitHub")
            else:
                await self.store.mark_work_done(item_id)
            finally:
                self.queue.task_done()
                await self._refill_queue()

    async def _acknowledge(self, item: WorkItem) -> None:
        """React with 👍 on the command comment so the user sees work has started."""
        if item.command.name in {"review", "cleanup"}:
            return
        try:
            await self.github.add_reaction(item.repo, item.comment_id, "+1")
        except Exception:
            logger.warning("Could not react to comment %s", item.comment_id, exc_info=True)

    async def _suspend_workspace(self, run_id: str) -> None:
        """The graph only returns when it waits for a human or has finished: nothing runs."""
        run = await self.store.get_run(run_id)
        if not run or not run.get("worktree"):
            return
        try:
            await self.workspace.suspend(Path(run["worktree"]))
        except Exception:
            logger.warning("Could not suspend workspace for run %s", run_id[:8], exc_info=True)

    async def _refill_queue(self) -> None:
        available = self.settings.queue_size - self.queue.qsize()
        if available <= 0:
            return
        for job in await self.store.claim_pending_work_items(available):
            self.queue.put_nowait((job["id"], self._deserialize_item(job["payload"])))

    @staticmethod
    def _serialize_item(item: WorkItem) -> dict[str, Any]:
        return {
            "command": {
                "name": item.command.name,
                "request_id": item.command.request_id,
                "text": item.command.text,
            },
            "repo": item.repo,
            "issue_number": item.issue_number,
            "comment_id": item.comment_id,
            "user": item.user,
            "pull_number": item.pull_number,
            "review_id": item.review_id,
        }

    @staticmethod
    def _deserialize_item(payload: dict[str, Any]) -> WorkItem:
        command = payload["command"]
        return WorkItem(
            command=AgentCommand(
                name=command["name"],
                request_id=command.get("request_id"),
                text=command.get("text", ""),
            ),
            repo=payload["repo"],
            issue_number=int(payload["issue_number"]),
            comment_id=int(payload["comment_id"]),
            user=payload["user"],
            pull_number=(
                int(payload["pull_number"])
                if payload.get("pull_number") is not None
                else None
            ),
            review_id=(
                int(payload["review_id"])
                if payload.get("review_id") is not None
                else None
            ),
        )

    async def _fail_active_run(self, item: WorkItem) -> None:
        run = (
            await self.store.get_run_by_pr(item.repo, item.pull_number)
            if item.pull_number is not None
            else await self.store.get_active_run(item.repo, item.issue_number)
        )
        if run:
            await self.store.update_run(run["run_id"], status="failed")

    async def handle(self, item: WorkItem) -> None:
        if self.agent_graph is None:
            raise RuntimeError("AgentService is not open")
        if item.command.name == "start":
            await self._start(item)
        elif item.command.name in {"answer", "approve", "reject"}:
            await self._resume(item)
        elif item.command.name == "stop":
            await self._stop(item)
        elif item.command.name == "review":
            await self._review(item)
        elif item.command.name == "cleanup":
            await self._cleanup(item)

    async def _start(self, item: WorkItem) -> None:
        active = await self.store.get_active_run(item.repo, item.issue_number)
        if active:
            await self.github.post_comment(
                item.repo,
                item.issue_number,
                f"A run is already active for this issue: `{active['run_id'][:8]}` ({active['status']}).",
            )
            return
        run_id = str(uuid.uuid4())
        thread_id = f"{item.repo}#{item.issue_number}:{run_id}"
        await self.store.create_run(
            {
                "run_id": run_id,
                "thread_id": thread_id,
                "repo": item.repo,
                "issue_number": item.issue_number,
                "status": "starting",
            }
        )
        await self.agent_graph.graph.ainvoke(
            {
                "repo": item.repo,
                "issue_number": item.issue_number,
                "run_id": run_id,
                "thread_id": thread_id,
                "command_name": "start",
                "triggering_comment_id": item.comment_id,
                "auto_approve": item.command.text == "auto"
                or not self.settings.require_plan_approval,
            },
            self._config(thread_id, run_id, item, "start"),
        )
        await self._suspend_workspace(run_id)

    async def _resume(self, item: WorkItem) -> None:
        if item.command.request_id:
            request_id = item.command.request_id
            request = await self.store.get_request(request_id)
        else:
            request = await self._issue_request(item)
            request_id = request["request_id"] if request else ""
        if (
            request
            and request.get("status") == "resolved"
            and await self._continue_interrupted_run(request, item)
        ):
            return
        if not request or request.get("status") != "pending":
            message = (
                f"Request `{request_id}` is missing, stale, or already resolved."
                if item.command.request_id
                else "No pending request for this issue."
            )
            await self.github.post_comment(item.repo, item.issue_number, message)
            return
        run = await self.store.get_run(request["run_id"])
        if not run or run["repo"] != item.repo or run["issue_number"] != item.issue_number:
            await self.github.post_comment(item.repo, item.issue_number, "That request belongs to another run.")
            return
        expected_kind = "clarification" if item.command.name == "answer" else "approval"
        if request["kind"] != expected_kind:
            await self.github.post_comment(item.repo, item.issue_number, "That command does not match the pending request.")
            return
        if request["plan_version"] != run["plan_version"]:
            await self.github.post_comment(item.repo, item.issue_number, "That approval is for an old plan version.")
            return

        decision = {"action": item.command.name, "text": item.command.text}
        if item.command.name == "approve":
            latest = await self.github.load_issue(item.repo, item.issue_number)
            if latest.requirements_version != run.get("requirements_version"):
                decision = {"action": "requirements_changed", "text": ""}

        if not await self.store.resolve_request(request_id, decision):
            return
        await self.agent_graph.graph.ainvoke(
            Command(resume=decision),
            self._config(run["thread_id"], run["run_id"], item, item.command.name),
        )
        await self._suspend_workspace(run["run_id"])

    async def _issue_request(self, item: WorkItem) -> dict | None:
        """The request a command without an id targets: the active run's pending one.
        With nothing pending, the newest request lets a replay after a restart
        reach _continue_interrupted_run, as it would with an explicit id."""
        run = await self.store.get_active_run(item.repo, item.issue_number)
        if not run:
            return None
        return await self.store.get_latest_human_request(
            run["run_id"], pending_only=True
        ) or await self.store.get_latest_human_request(run["run_id"])

    async def _continue_interrupted_run(self, request: dict, item: WorkItem) -> bool:
        """A restart can land between accepting a decision and finishing the work it
        started. The recovered work item then finds its request already resolved; if
        the graph is mid-step (not waiting on a human), continue from the checkpoint."""
        response = json.loads(request.get("response_json") or "{}")
        if response.get("action") not in {item.command.name, "requirements_changed"}:
            return False
        run = await self.store.get_run(request["run_id"])
        if not run or run["repo"] != item.repo or run["issue_number"] != item.issue_number:
            return False
        config = self._config(run["thread_id"], run["run_id"], item, item.command.name)
        snapshot = await self.agent_graph.graph.aget_state(config)
        if not snapshot.next or any(task.interrupts for task in snapshot.tasks):
            return False
        logger.warning("Continuing interrupted run %s from its checkpoint", run["run_id"][:8])
        await self.agent_graph.graph.ainvoke(None, config)
        await self._suspend_workspace(run["run_id"])
        return True

    async def _cleanup(self, item: WorkItem) -> None:
        """The PR was merged or closed: no review can follow, so drop the workspace."""
        if item.pull_number is None:
            return
        run = await self.store.get_run_by_pr(item.repo, item.pull_number)
        if not run:
            return
        if run.get("worktree"):
            await self.workspace.destroy(Path(run["worktree"]))
        outcome = "merged" if item.command.text == "merged" else "closed"
        await self.store.update_run(run["run_id"], status=outcome)
        await self.events.emit(
            "pr_closed",
            f"PR #{item.pull_number} {outcome}; workspace removed",
            run_id=run["run_id"],
            repo=item.repo,
            issue_number=int(run["issue_number"]),
        )
        logger.info("PR %s#%s %s; removed workspace of run %s", item.repo, item.pull_number, outcome, run["run_id"][:8])
        await self.github.post_comment(
            item.repo,
            item.pull_number,
            f"PR {outcome}. The agent's workspace for run `{run['run_id'][:8]}` was removed.",
        )

    async def _stop(self, item: WorkItem) -> None:
        run = await self.store.get_active_run(item.repo, item.issue_number)
        if not run:
            return
        snapshot = await self.agent_graph.graph.aget_state(
            self._config(run["thread_id"], run["run_id"], item, "stop")
        )
        if snapshot.next:
            await self.agent_graph.graph.ainvoke(
                Command(resume={"action": "stop", "text": ""}),
                self._config(run["thread_id"], run["run_id"], item, "stop"),
            )
        else:
            await self.store.update_run(run["run_id"], status="stopped")
        await self._suspend_workspace(run["run_id"])

    async def _review(self, item: WorkItem) -> None:
        if item.pull_number is None or item.review_id is None:
            logger.warning("Review work item is missing pull_number or review_id")
            return
        logger.info(
            "Resolving review id=%s for %s#%s",
            item.review_id,
            item.repo,
            item.pull_number,
        )
        run = await self.store.get_run_by_pr(item.repo, item.pull_number)
        if not run:
            logger.info(
                "Ignoring review %s for untracked PR %s#%s",
                item.review_id,
                item.repo,
                item.pull_number,
            )
            return
        logger.info(
            "Starting review graph run=%s thread=%s issue=%s pr=%s",
            run["run_id"][:8],
            run["thread_id"],
            run["issue_number"],
            item.pull_number,
        )
        review_thread_id = f"{run['run_id']}:review:{item.review_id}"
        await self.store.update_run(run["run_id"], thread_id=review_thread_id)
        trace_item = WorkItem(
            command=item.command,
            repo=item.repo,
            issue_number=int(run["issue_number"]),
            comment_id=item.comment_id,
            user=item.user,
            pull_number=item.pull_number,
            review_id=item.review_id,
        )
        await self.agent_graph.graph.ainvoke(
            {
                "repo": item.repo,
                "issue_number": int(run["issue_number"]),
                "run_id": run["run_id"],
                "thread_id": review_thread_id,
                "command_name": "review",
                "triggering_comment_id": item.comment_id,
                "pr_number": item.pull_number,
                "review_id": item.review_id,
                "workflow_phase": "review",
                "final_status": "",
            },
            self._config(review_thread_id, run["run_id"], trace_item, "review"),
        )
        logger.info("Review graph completed for run=%s", run["run_id"][:8])
        await self._suspend_workspace(run["run_id"])

    def _config(
        self,
        thread_id: str,
        run_id: str | None = None,
        item: WorkItem | None = None,
        action: str | None = None,
    ) -> dict[str, Any]:
        config: dict[str, Any] = {"configurable": {"thread_id": thread_id}}
        if self.langfuse_handler is None:
            return config

        tags = ["coding-agent-graph"]
        if action:
            tags.append(action)
        metadata: dict[str, Any] = {
            "langfuse_session_id": run_id or thread_id,
            "langfuse_tags": tags,
            "thread_id": thread_id,
        }
        if run_id:
            metadata["run_id"] = run_id
        if item:
            metadata.update(
                {
                    "langfuse_user_id": item.user,
                    "repository": item.repo,
                    "issue_number": item.issue_number,
                    "github_comment_id": item.comment_id,
                }
            )
            if item.pull_number is not None:
                metadata["pull_number"] = item.pull_number
            if item.review_id is not None:
                metadata["review_id"] = item.review_id
        if action:
            metadata["action"] = action

        config.update(
            {
                "callbacks": [self.langfuse_handler],
                "run_name": f"coding-agent:{action or 'state'}",
                "tags": tags,
                "metadata": metadata,
            }
        )
        return config
