from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from agent_models import AgentState, IssueContext
from config import Settings
from github_client import GitHubClient
from pi_runner import PiError, PiRunner, PiStopped
from storage import AgentStore
from workspace import VerificationResult, WorkspaceError, WorkspaceManager


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _format_plan(plan: list[str]) -> str:
    return "\n".join(f"{index}. {item}" for index, item in enumerate(plan, 1))


class CodingAgentGraph:
    def __init__(
        self,
        settings: Settings,
        store: AgentStore,
        github: GitHubClient,
        pi: PiRunner,
        workspace: WorkspaceManager,
        checkpointer: Any,
    ):
        self.settings = settings
        self.store = store
        self.github = github
        self.pi = pi
        self.workspace = workspace
        self.graph = self._build().compile(checkpointer=checkpointer)

    def _build(self) -> StateGraph:
        builder = StateGraph(AgentState)
        builder.add_node("load_issue", self.load_issue)
        builder.add_node("plan_with_pi", self.plan_with_pi)
        builder.add_node("human_input", self.human_input)
        builder.add_node("implement_with_pi", self.implement_with_pi)
        builder.add_node("review", self.review)
        builder.add_node("implement_review", self.implement_review)
        builder.add_node("verify", self.verify)
        builder.add_node("finish", self.finish)
        builder.add_node("publish_pr", self.publish_pr)
        builder.add_node("push_review", self.push_review)
        builder.add_edge(START, "load_issue")
        builder.add_conditional_edges(
            "load_issue", self.after_load, {"plan": "plan_with_pi", "review": "review"}
        )
        builder.add_conditional_edges(
            "plan_with_pi",
            self.after_plan,
            {"human": "human_input", "implement": "implement_with_pi", "finish": "finish"},
        )
        builder.add_conditional_edges(
            "human_input",
            self.after_human,
            {
                "plan": "plan_with_pi",
                "implement": "implement_with_pi",
                "review": "implement_review",
                "finish": "finish",
            },
        )
        builder.add_conditional_edges(
            "implement_with_pi",
            self.after_implement,
            {"verify": "verify", "human": "human_input", "finish": "finish"},
        )
        builder.add_conditional_edges(
            "review",
            self.after_review_loaded,
            {"implement": "implement_review", "finish": "finish"},
        )
        builder.add_conditional_edges(
            "implement_review",
            self.after_review_implementation,
            {"verify": "verify", "human": "human_input", "finish": "finish"},
        )
        builder.add_conditional_edges(
            "verify",
            self.after_verify,
            {
                "retry": "implement_with_pi",
                "review_retry": "implement_review",
                "publish": "publish_pr",
                "push": "push_review",
                "finish": "finish",
            },
        )
        builder.add_edge("finish", END)
        builder.add_edge("publish_pr", END)
        builder.add_edge("push_review", END)
        return builder

    async def load_issue(self, state: AgentState) -> dict:
        context = await self.github.load_issue(state["repo"], state["issue_number"])
        update: dict[str, Any] = {
            "issue_context": context.model_dump(),
            "requirements_version": context.requirements_version,
        }
        if state.get("command_name") == "review":
            run = await self.store.get_run(state["run_id"])
            if not run or not run.get("worktree") or not run.get("branch"):
                return {
                    **update,
                    "final_status": "failed",
                    "final_summary": "Cannot process review: the original worktree is unavailable.",
                }
            update.update(
                {
                    "branch": run["branch"],
                    "worktree": run["worktree"],
                    "pi_session_id": state.get("pi_session_id") or state["run_id"],
                    "workflow_phase": "review",
                }
            )
            await self.store.update_run(state["run_id"], status="reading_review")
            return update

        branch, worktree = await self.workspace.prepare(state["run_id"], state["issue_number"])
        update.update(
            {
                "branch": branch,
                "worktree": str(worktree),
                "pi_session_id": state.get("pi_session_id") or state["run_id"],
                "plan_version": state.get("plan_version", 0),
                "attempt": state.get("attempt", 0),
                "decisions": state.get("decisions", []),
                "workflow_phase": "initial",
            }
        )
        await self.store.update_run(
            state["run_id"],
            status="planning",
            branch=branch,
            worktree=str(worktree),
            requirements_version=context.requirements_version,
        )
        return update

    def after_load(self, state: AgentState) -> Literal["plan", "review"]:
        return "review" if state.get("command_name") == "review" else "plan"

    async def plan_with_pi(self, state: AgentState) -> dict:
        context = IssueContext.model_validate(state["issue_context"])
        decisions = "\n".join(f"- {item}" for item in state.get("decisions", [])) or "(none)"
        prompt = f"""You are planning a change to the Todo application at {self.settings.todo_path}.
Investigate the repository using only the enabled read/search/list tools. Do not edit files and do not run shell commands.

Requirements:
{context.prompt_text()}

Prior human decisions:
{decisions}

Finish by calling the submit_result tool with status ready, needs_input, or failed.
The plan must be short and include acceptance criteria and concrete repository checks. Ask only questions that materially change behavior; infer ordinary technical choices from the code. Use status needs_input only for such questions.
"""
        try:
            result = await self.pi.run(
                run_id=state["run_id"],
                phase="plan",
                session_id=state["pi_session_id"],
                worktree=Path(state["worktree"]),
                prompt=prompt,
                read_only=True,
            )
        except PiStopped:
            return {"final_status": "stopped", "final_summary": "Stopped by an authorized user."}
        except PiError as exc:
            return {
                "final_status": "failed",
                "final_summary": f"Pi planning failed: {exc}",
            }

        if result.status == "failed":
            return {"final_status": "failed", "final_summary": result.summary or "Pi planning failed."}

        version = state.get("plan_version", 0) + 1
        pending_kind = "clarification" if result.status == "needs_input" or result.questions else "approval"
        auto_approved = pending_kind == "approval" and state.get("auto_approve", False)
        if auto_approved:
            await self._announce_auto_approved_plan(state, version, result.plan)
        await self.store.update_run(
            state["run_id"],
            status="implementing" if auto_approved else "waiting_for_human",
            plan_version=version,
            requirements_version=state["requirements_version"],
        )
        return {
            "plan_version": version,
            "plan": result.plan,
            "questions": result.questions,
            "claimed_checks": result.claimed_checks,
            "pi_summary": result.summary,
            "pending_kind": pending_kind,
            "human_decision": {},
        }

    async def _announce_auto_approved_plan(
        self, state: AgentState, version: int, plan: list[str]
    ) -> None:
        content = (
            f"### Plan (v{version}) — implementing without approval\n\n{_format_plan(plan)}\n\n"
            "Plan approval is disabled for this run; a draft PR will follow once checks pass. "
            "Use `/agent stop` to cancel."
        )
        request_id = f"{state['run_id'][:8]}-p{version}-auto"
        request = await self.store.ensure_request(
            request_id, state["run_id"], "notice", version, _content_hash(content)
        )
        if request.get("github_comment_id") is None:
            comment_id = await self.github.post_comment(state["repo"], state["issue_number"], content)
            await self.store.set_request_comment(request_id, comment_id)

    def after_plan(self, state: AgentState) -> Literal["human", "implement", "finish"]:
        if state.get("final_status"):
            return "finish"
        if state.get("pending_kind") == "approval" and state.get("auto_approve"):
            return "implement"
        return "human"

    async def human_input(self, state: AgentState) -> dict:
        kind = state.get("pending_kind", "approval")
        version = state.get("plan_version", 0)
        request_id = f"{state['run_id'][:8]}-p{version}-{'q' if kind == 'clarification' else 'a'}"
        if kind == "clarification":
            questions = "\n".join(f"- {item}" for item in state.get("questions", []))
            content = (
                f"### Agent needs input\n\n{questions}\n\n"
                f"Reply with `/agent answer {request_id} <answer>`."
            )
        else:
            content = (
                f"### Proposed plan (v{version})\n\n{_format_plan(state.get('plan', []))}\n\n"
                f"Approve with `/agent approve {request_id}` or reject with "
                f"`/agent reject {request_id} <reason>`."
            )

        request = await self.store.ensure_request(
            request_id,
            state["run_id"],
            kind,
            version,
            _content_hash(content),
        )
        if request.get("github_comment_id") is None:
            comment_id = await self.github.post_comment(state["repo"], state["issue_number"], content)
            await self.store.set_request_comment(request_id, comment_id)

        decision = interrupt(
            {
                "request_id": request_id,
                "kind": kind,
                "plan_version": version,
                "message": content,
            }
        )
        action = str(decision.get("action", ""))
        text = str(decision.get("text", "")).strip()
        decisions = list(state.get("decisions", []))
        if action == "answer":
            decisions.append(f"Clarification answer: {text}")
        elif action == "reject":
            decisions.append(f"Plan rejected: {text}")
        elif action == "requirements_changed":
            decisions.append("Issue requirements changed while waiting; re-plan against the latest issue.")
        return {
            "pending_request_id": request_id,
            "human_decision": decision,
            "decisions": decisions,
        }

    def after_human(
        self, state: AgentState
    ) -> Literal["plan", "implement", "review", "finish"]:
        action = state.get("human_decision", {}).get("action")
        if action == "approve":
            return "implement"
        if action == "answer" and state.get("workflow_phase") == "review":
            return "review"
        if action == "stop":
            return "finish"
        return "plan"

    async def implement_with_pi(self, state: AgentState) -> dict:
        if state.get("human_decision", {}).get("action") == "stop":
            return {"final_status": "stopped", "final_summary": "Stopped by an authorized user."}

        attempt = state.get("attempt", 0) + 1
        verification = state.get("verification", {})
        feedback = verification.get("summary", "")
        if verification.get("checks"):
            feedback += "\n" + "\n".join(
                f"{item['command']}: {item['output'][-2000:]}" for item in verification["checks"] if not item["passed"]
            )
        prompt = f"""Continue this work session and implement the approved plan in {self.settings.todo_path}.

Approved plan v{state.get('plan_version')}:
{_format_plan(state.get('plan', []))}

Verification feedback from a previous attempt:
{feedback or '(none)'}

Read and edit the real files, then run the relevant checks. Stay strictly inside {self.settings.todo_path}; do not edit secrets, {Path(__file__).resolve().parent.name}, .github, or deployment files. Do not use GitHub credentials, push, open a PR, or merge.
If a new product decision is genuinely required, stop and return needs_input.
Finish by calling the submit_result tool with status completed, needs_input, or failed.
"""
        try:
            result = await self.pi.run(
                run_id=state["run_id"],
                phase="implement",
                session_id=state["pi_session_id"],
                worktree=Path(state["worktree"]),
                prompt=prompt,
                read_only=False,
            )
        except PiStopped:
            return {
                "attempt": attempt,
                "final_status": "stopped",
                "final_summary": "Stopped by an authorized user.",
            }
        except PiError as exc:
            return {
                "attempt": attempt,
                "verification": {"passed": False, "summary": f"Pi failed: {exc}"},
                "pi_summary": str(exc),
            }

        if result.status == "needs_input":
            await self.store.update_run(state["run_id"], status="waiting_for_human")
            return {
                "attempt": attempt,
                "pending_kind": "clarification",
                "questions": result.questions,
                "pi_summary": result.summary,
            }
        if result.status != "completed":
            summary = result.summary or "Pi reported implementation failure."
            return {
                "attempt": attempt,
                "verification": {"passed": False, "summary": f"Pi failed: {summary}"},
                "pi_summary": summary,
            }
        await self.store.update_run(state["run_id"], status="verifying")
        return {
            "attempt": attempt,
            "pi_summary": result.summary,
            "claimed_checks": result.claimed_checks,
            "questions": [],
        }

    def after_implement(self, state: AgentState) -> Literal["verify", "human", "finish"]:
        if state.get("final_status"):
            return "finish"
        if state.get("pending_kind") == "clarification" and state.get("questions"):
            return "human"
        if state.get("verification", {}).get("summary", "").startswith("Pi failed"):
            return "verify"
        return "verify"

    async def review(self, state: AgentState) -> dict:
        if state.get("final_status") == "failed":
            return {}
        feedback = await self.github.load_review_feedback(
            state["repo"], state["pr_number"], state["review_id"]
        )
        base_revision = await self.workspace.revision(Path(state["worktree"]))
        await self.store.update_run(state["run_id"], status="implementing_review")
        return {
            "review_feedback": feedback,
            "review_base_revision": base_revision,
            "attempt": 0,
            "verification": {},
            "questions": [],
            "pending_kind": "",
            "human_decision": {},
            "final_status": "",
            "final_summary": "",
            "workflow_phase": "review",
        }

    async def implement_review(self, state: AgentState) -> dict:
        attempt = state.get("attempt", 0) + 1
        feedback = state.get("review_feedback", {})
        inline = "\n\n".join(
            (
                f"File: {item.get('path') or '(unknown)'}"
                f"{f':{item.get('line')}' if item.get('line') else ''}\n"
                f"Comment: {item.get('body') or '(empty)'}\n"
                f"Diff context:\n{item.get('diff_hunk') or '(not provided)'}"
            )
            for item in feedback.get("comments", [])
        ) or "(no inline comments)"
        verification = state.get("verification", {})
        verification_feedback = verification.get("summary", "")
        if verification.get("checks"):
            verification_feedback += "\n" + "\n".join(
                f"{item['command']}: {item['output'][-2000:]}"
                for item in verification["checks"]
                if not item["passed"]
            )
        prompt = f"""Address the submitted review on draft PR #{state.get('pr_number')}.

Review by {feedback.get('author', 'unknown')} ({feedback.get('state', 'commented')}):
{feedback.get('body') or '(no summary)'}

Inline review comments:
{inline}

Verification feedback from a previous attempt:
{verification_feedback or '(none)'}

Inspect the current worktree and implement only the requested review changes in {self.settings.todo_path}. Run relevant checks. Stay strictly inside {self.settings.todo_path}; do not edit secrets, {Path(__file__).resolve().parent.name}, .github, or deployment files. Do not use GitHub credentials, commit, push, open or merge a PR.
If a product decision is genuinely required, return needs_input with a precise question.
Finish by calling the submit_result tool with status completed, needs_input, or failed.
"""
        try:
            result = await self.pi.run(
                run_id=state["run_id"],
                phase="implement_review",
                session_id=state["pi_session_id"],
                worktree=Path(state["worktree"]),
                prompt=prompt,
                read_only=False,
            )
        except PiStopped:
            return {
                "attempt": attempt,
                "final_status": "stopped",
                "final_summary": "Stopped by an authorized user.",
            }
        except PiError as exc:
            return {
                "attempt": attempt,
                "verification": {"passed": False, "summary": f"Pi failed: {exc}"},
                "pi_summary": str(exc),
            }

        if result.status == "needs_input":
            await self.store.update_run(state["run_id"], status="waiting_for_human")
            return {
                "attempt": attempt,
                "pending_kind": "clarification",
                "questions": result.questions,
                "pi_summary": result.summary,
            }
        if result.status != "completed":
            summary = result.summary or "Pi reported review implementation failure."
            return {
                "attempt": attempt,
                "verification": {"passed": False, "summary": f"Pi failed: {summary}"},
                "pi_summary": summary,
            }
        await self.store.update_run(state["run_id"], status="verifying_review")
        return {
            "attempt": attempt,
            "pi_summary": result.summary,
            "claimed_checks": result.claimed_checks,
            "questions": [],
        }

    def after_review_loaded(self, state: AgentState) -> Literal["implement", "finish"]:
        return "finish" if state.get("final_status") else "implement"

    def after_review_implementation(
        self, state: AgentState
    ) -> Literal["verify", "human", "finish"]:
        if state.get("final_status"):
            return "finish"
        if state.get("pending_kind") == "clarification" and state.get("questions"):
            return "human"
        return "verify"

    async def verify(self, state: AgentState) -> dict:
        try:
            result = await self.workspace.verify(Path(state["worktree"]))
        except WorkspaceError as exc:
            result = VerificationResult(False, f"Verification infrastructure failed: {exc}")
        if (
            result.passed
            and state.get("workflow_phase") == "review"
            and result.revision == state.get("review_base_revision")
        ):
            result = VerificationResult(
                False,
                "Review implementation did not produce any new code changes.",
                changed_files=result.changed_files,
                checks=result.checks,
                revision=result.revision,
            )
        update: dict[str, Any] = {"verification": result.as_dict()}
        if result.passed:
            update.update(
                {
                    "verified_revision": result.revision,
                    "final_status": "verified",
                    "final_summary": result.summary,
                }
            )
            await self.store.update_run(
                state["run_id"], status="verified", verified_revision=result.revision
            )
        elif state.get("attempt", 0) >= self.settings.max_attempts:
            update.update(
                {
                    "final_status": "failed",
                    "final_summary": f"Verification failed after {state.get('attempt')} attempts: {result.summary}",
                }
            )
        return update

    def after_verify(
        self, state: AgentState
    ) -> Literal["retry", "review_retry", "publish", "push", "finish"]:
        if state.get("verification", {}).get("passed"):
            return "push" if state.get("workflow_phase") == "review" else "publish"
        if state.get("attempt", 0) >= self.settings.max_attempts:
            return "finish"
        return "review_retry" if state.get("workflow_phase") == "review" else "retry"

    async def finish(self, state: AgentState) -> dict:
        action = state.get("human_decision", {}).get("action")
        status = "stopped" if action == "stop" else state.get("final_status", "failed")
        verification = state.get("verification", {})
        files = verification.get("changed_files", [])
        checks = verification.get("checks", [])
        files_text = "\n".join(f"- `{name}`" for name in files) or "- None"
        checks_text = "\n".join(
            f"- {item['command']}: {'passed' if item['passed'] else 'failed'}"
            for item in checks
        ) or "- Not run"
        body = (
            f"### Agent run `{state['run_id'][:8]}` — {status}\n\n"
            f"{state.get('final_summary') or state.get('pi_summary') or 'Run finished.'}\n\n"
            f"**Branch:** `{state.get('branch', 'not created')}`\n\n"
            f"**Changed files:**\n{files_text}\n\n"
            f"**Checks:**\n{checks_text}\n\n"
            "No merge was performed. Inspect the preserved worktree and logs before retrying."
        )
        request_id = f"{state['run_id'][:8]}-finish-{_content_hash(body)[:8]}"
        request = await self.store.ensure_request(
            request_id, state["run_id"], "finish", state.get("plan_version", 0), _content_hash(body)
        )
        if request.get("github_comment_id") is None:
            target_number = (
                state.get("pr_number")
                if state.get("workflow_phase") == "review"
                else state["issue_number"]
            )
            comment_id = await self.github.post_comment(state["repo"], target_number, body)
            await self.store.set_request_comment(request_id, comment_id)
        await self.store.update_run(state["run_id"], status=status)
        return {"final_status": status}

    async def publish_pr(self, state: AgentState) -> dict:
        run = await self.store.get_run(state["run_id"])
        if not run or run.get("status") != "verified":
            await self.github.post_comment(
                state["repo"],
                state["issue_number"],
                "Cannot create the draft PR: the latest run is not verified.",
            )
            return {
                "final_status": "failed",
                "final_summary": "Draft PR creation refused: run is not verified.",
            }

        worktree = Path(run["worktree"])
        current_revision = await self.workspace.revision(worktree)
        if current_revision != run.get("verified_revision"):
            verification = await self.workspace.verify(worktree)
            if not verification.passed:
                await self.github.post_comment(
                    state["repo"], state["issue_number"],
                    f"Draft PR creation refused because the code changed after verification: {verification.summary}",
                )
                return {"final_status": "failed", "verification": verification.as_dict()}
            await self.store.update_run(
                state["run_id"], verified_revision=verification.revision, status="verified"
            )

        context = IssueContext.model_validate(state["issue_context"])
        await self.workspace.commit_and_push(
            worktree, run["branch"], f"Resolve issue #{state['issue_number']}"
        )
        pr = await self.github.create_pr(
            state["repo"],
            run["branch"],
            self.settings.main_branch,
            f"{context.title} (#{state['issue_number']})",
            f"Closes #{state['issue_number']}\n\nCreated by the coding agent after local verification.",
        )
        pr_url = str(pr.get("html_url") or "")
        pr_number = int(pr["number"])
        await self.store.update_run(
            state["run_id"],
            status="reviewing",
            pr_url=pr_url,
            pr_number=pr_number,
        )
        await self.github.post_comment(
            state["repo"],
            state["issue_number"],
            f"Draft pull request ready for review: {pr_url}",
        )
        return {
            "final_status": "reviewing",
            "pr_url": pr_url,
            "pr_number": pr_number,
        }

    async def push_review(self, state: AgentState) -> dict:
        run = await self.store.get_run(state["run_id"])
        if not run or not run.get("pr_number"):
            return {
                "final_status": "failed",
                "final_summary": "Cannot push review changes: PR metadata is missing.",
            }
        worktree = Path(run["worktree"])
        current_revision = await self.workspace.revision(worktree)
        if current_revision != state.get("verified_revision"):
            return {
                "final_status": "failed",
                "final_summary": "Review changes changed after verification; push refused.",
            }
        sha = await self.workspace.commit_and_push(
            worktree,
            run["branch"],
            f"Address review on PR #{run['pr_number']}",
        )
        await self.store.update_run(state["run_id"], status="reviewing")
        await self.github.post_comment(
            state["repo"],
            int(run["pr_number"]),
            f"Review feedback addressed in `{sha[:12]}`. Checks passed; ready for another review.",
        )
        return {
            "final_status": "reviewing",
            "final_summary": "Review feedback implemented, verified, and pushed.",
        }
