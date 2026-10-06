from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Literal, TypedDict

from pydantic import BaseModel, Field

AGENT_COMMENT_MARKER = "<!-- coding-agent-graph -->"
LEGACY_AGENT_COMMENT_PREFIXES = (
    "### Proposed plan",
    "### Agent needs input",
    "### Agent run",
    "The coding agent failed unexpectedly.",
    "Pull request ready for manual review:",
    "Cannot publish:",
    "Publish refused",
    "A run is already active for this issue:",
    "Request `",
    "That request belongs to another run.",
    "That command does not match the pending request.",
    "That approval is for an old plan version.",
    "There is no verified run available to publish.",
)


def is_agent_generated_comment(comment: dict) -> bool:
    body = str(comment.get("body") or "").lstrip()
    return AGENT_COMMENT_MARKER in body or body.startswith(LEGACY_AGENT_COMMENT_PREFIXES)


CommandName = Literal["start", "answer", "approve", "reject", "stop", "review"]


@dataclass(frozen=True)
class AgentCommand:
    name: CommandName
    request_id: str | None = None
    text: str = ""


class PiResult(BaseModel):
    status: Literal["ready", "needs_input", "completed", "failed"]
    summary: str = ""
    plan: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    claimed_checks: list[str] = Field(default_factory=list)


class IssueContext(BaseModel):
    repo: str
    number: int
    title: str
    body: str = ""
    html_url: str = ""
    comments: list[dict] = Field(default_factory=list)
    requirements_version: str

    def prompt_text(self) -> str:
        history = []
        for comment in self.comments:
            if is_agent_generated_comment(comment):
                continue
            body = str(comment.get("body") or "")
            if body.lstrip().startswith("/agent"):
                continue
            login = ((comment.get("user") or {}).get("login") or "unknown")
            history.append(f"- {login}: {body}")
        joined = "\n".join(history) if history else "(no non-command comments)"
        return (
            f"Issue #{self.number}: {self.title}\n\n"
            f"Description:\n{self.body or '(empty)'}\n\n"
            f"Human discussion:\n{joined}"
        )


def requirements_hash(title: str, body: str, comments: list[dict]) -> str:
    relevant = [
        {
            "id": item.get("id"),
            "author": (item.get("user") or {}).get("login"),
            "body": item.get("body") or "",
        }
        for item in comments
        if not str(item.get("body") or "").lstrip().startswith("/agent")
        and not is_agent_generated_comment(item)
        and (item.get("user") or {}).get("type") != "Bot"
    ]
    raw = json.dumps(
        {"title": title, "body": body, "comments": relevant},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class AgentState(TypedDict, total=False):
    repo: str
    issue_number: int
    run_id: str
    thread_id: str
    command_name: str
    triggering_comment_id: int
    issue_context: dict
    requirements_version: str
    plan_version: int
    plan: list[str]
    questions: list[str]
    decisions: list[str]
    pending_request_id: str
    pending_kind: str
    human_decision: dict
    pi_session_id: str
    worktree: str
    branch: str
    attempt: int
    pi_summary: str
    claimed_checks: list[str]
    verification: dict
    verified_revision: str
    final_status: str
    final_summary: str
    pr_url: str
    pr_number: int
    review_id: int
    review_feedback: dict
    review_base_revision: str
    workflow_phase: str
    auto_approve: bool
