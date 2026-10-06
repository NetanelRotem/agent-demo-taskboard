from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import httpx
import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from typing_extensions import TypedDict

from agent_graph import CodingAgentGraph
from agent_models import AgentCommand, PiResult, IssueContext, requirements_hash
from commands import CommandError, parse_command
from config import Settings, _github_token
from github_client import GitHubClient
from pi_runner import PiError, PiRunner, _parse_result
from pi_logs import format_event
from service import AgentService, WorkItem
from storage import AgentStore
from workspace import WorkspaceManager, _command_environment


def settings(tmp_path: Path, users=frozenset({"alice"})) -> Settings:
    return Settings(
        github_token="not-validated",
        authorized_users=users,
        repository_path=tmp_path,
        todo_path="todo-app",
        main_branch="main",
        data_dir=tmp_path / "data",
        pi_command="pi",
        openrouter_model="z-ai/glm-5.3",
        pi_timeout_seconds=10,
        max_attempts=2,
        queue_size=10,
    )


def test_parse_result_accepts_json_after_explanatory_text():
    result = _parse_result(
        'Work completed.\n{"status":"completed","summary":"done",'
        '"plan":[],"questions":[],"claimed_checks":["lint"]}'
    )
    assert result.status == "completed"
    assert result.claimed_checks == ["lint"]


@pytest.mark.parametrize(
    "text,name,request_id,answer",
    [
        ("/agent start", "start", None, ""),
        ("/agent start auto", "start", None, "auto"),
        ("/agent approve r1", "approve", "r1", ""),
        ("/agent answer r1 use blue", "answer", "r1", "use blue"),
        ("/agent reject r1 too broad", "reject", "r1", "too broad"),
        ("/agent stop", "stop", None, ""),
    ],
)
def test_command_parser(text, name, request_id, answer):
    parsed = parse_command(text)
    assert (parsed.name, parsed.request_id, parsed.text) == (name, request_id, answer)


def test_command_parser_rejects_invalid_command():
    with pytest.raises(CommandError):
        parse_command("/agent approve")


def test_start_rejects_unknown_option():
    with pytest.raises(CommandError, match="Usage: /agent start"):
        parse_command("/agent start fast")


def test_publish_command_has_been_removed():
    with pytest.raises(CommandError, match="Unknown command: publish"):
        parse_command("/agent publish")


def test_github_token_falls_back_to_local_gh_credentials(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr("config.shutil.which", lambda name: "gh.exe")
    monkeypatch.setattr(
        "config.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="local-token\n"),
    )
    assert _github_token() == "local-token"


def test_langfuse_is_enabled_only_with_both_keys(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AGENT_REPOSITORY_PATH", str(tmp_path))
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    assert Settings.from_env().langfuse_enabled is False

    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    assert Settings.from_env().langfuse_enabled is True

    monkeypatch.setenv("LANGFUSE_TRACING_ENABLED", "false")
    assert Settings.from_env().langfuse_enabled is False


def test_langfuse_config_adds_trace_context(tmp_path):
    service = AgentService(settings(tmp_path), github=FakeGitHub())
    handler = object()
    service.langfuse_handler = handler
    item = WorkItem(AgentCommand("start"), "acme/todo", 7, 99, "alice")

    config = service._config("thread-1", "run-1", item, "start")

    assert config["callbacks"] == [handler]
    assert config["run_name"] == "coding-agent:start"
    assert config["metadata"]["langfuse_session_id"] == "run-1"
    assert config["metadata"]["langfuse_user_id"] == "alice"
    assert config["metadata"]["issue_number"] == 7


def test_langfuse_callback_integration_is_importable():
    from langfuse.langchain import CallbackHandler

    assert CallbackHandler is not None


def test_agent_comments_do_not_change_requirements_or_enter_prompt():
    human = {"id": 1, "body": "Use blue", "user": {"login": "alice", "type": "User"}}
    agent = {
        "id": 2,
        "body": "<!-- coding-agent-graph -->\n### Proposed plan (v1)",
        "user": {"login": "alice", "type": "User"},
    }
    legacy = {
        "id": 3,
        "body": "The coding agent failed unexpectedly. Check the server logs.",
        "user": {"login": "alice", "type": "User"},
    }
    baseline = requirements_hash("Title", "Body", [human])
    assert requirements_hash("Title", "Body", [human, agent, legacy]) == baseline
    context = IssueContext(
        repo="a/b",
        number=1,
        title="Title",
        body="Body",
        comments=[human, agent, legacy],
        requirements_version=baseline,
    )
    prompt = context.prompt_text()
    assert "Use blue" in prompt
    assert "Proposed plan" not in prompt
    assert "failed unexpectedly" not in prompt


@pytest.mark.asyncio
async def test_github_issue_loader_follows_comment_pagination():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/issues/3"):
            return httpx.Response(200, json={"title": "Task", "body": "Details", "html_url": "u"})
        page = int(request.url.params.get("page", "1"))
        comments = [
            {"id": index, "body": f"note {index}", "user": {"login": "alice", "type": "User"}}
            for index in range(100 if page == 1 else 1)
        ]
        return httpx.Response(200, json=comments)

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.github.test"
    )
    github = GitHubClient("", client=http)
    context = await github.load_issue("a/b", 3)
    assert len(context.comments) == 101
    await http.aclose()


@pytest.mark.asyncio
async def test_github_creates_draft_pr_and_loads_review_comments():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/repos/a/b/pulls" and request.method == "GET":
            return httpx.Response(200, json=[])
        if request.url.path == "/repos/a/b/pulls" and request.method == "POST":
            payload = json.loads(request.content)
            assert payload["draft"] is True
            return httpx.Response(
                201, json={"number": 42, "html_url": "https://example.test/pr/42"}
            )
        if request.url.path == "/repos/a/b/pulls/42/reviews/501":
            return httpx.Response(
                200,
                json={
                    "state": "CHANGES_REQUESTED",
                    "body": "Please fix this",
                    "user": {"login": "alice"},
                },
            )
        if request.url.path == "/repos/a/b/pulls/42/reviews/501/comments":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 9,
                        "path": "todo-app/src/App.tsx",
                        "line": 12,
                        "body": "Handle the empty state",
                        "diff_hunk": "@@ -10,2 +10,3 @@",
                    }
                ],
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.github.test"
    )
    github = GitHubClient("", client=http)
    pr = await github.create_pr("a/b", "agent/branch", "main", "Title", "Body")
    feedback = await github.load_review_feedback("a/b", 42, 501)

    assert pr["number"] == 42
    assert feedback["state"] == "changes_requested"
    assert feedback["comments"][0]["path"] == "todo-app/src/App.tsx"
    assert len(requests) == 4
    await http.aclose()


@pytest.mark.asyncio
async def test_delivery_and_stop_state_survive_store_restart(tmp_path):
    path = tmp_path / "state.sqlite3"
    first = AgentStore(path)
    await first.open()
    assert await first.claim_delivery("same") is True
    await first.create_run(
        {"run_id": "run", "thread_id": "thread", "repo": "a/b", "issue_number": 1}
    )
    await first.update_run("run", status="stop_requested")
    await first.close()

    second = AgentStore(path)
    await second.open()
    assert await second.claim_delivery("same") is False
    assert (await second.get_run("run"))["status"] == "stop_requested"
    await second.close()


@pytest.mark.asyncio
async def test_pr_number_survives_store_restart_and_supports_lookup(tmp_path):
    path = tmp_path / "state.sqlite3"
    first = AgentStore(path)
    await first.open()
    await first.create_run(
        {"run_id": "run", "thread_id": "thread", "repo": "a/b", "issue_number": 7}
    )
    await first.update_run(
        "run", status="reviewing", pr_number=42, pr_url="https://example.test/pr/42"
    )
    await first.close()

    second = AgentStore(path)
    await second.open()
    run = await second.get_run_by_pr("a/b", 42)
    assert run is not None
    assert run["issue_number"] == 7
    assert run["status"] == "reviewing"
    await second.close()


@pytest.mark.asyncio
async def test_existing_database_is_migrated_with_pr_number(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    conn = await aiosqlite.connect(path)
    await conn.execute(
        """CREATE TABLE runs (
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
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    await conn.commit()
    await conn.close()

    store = AgentStore(path)
    await store.open()
    cursor = await store.conn.execute("PRAGMA table_info(runs)")
    columns = {row[1] for row in await cursor.fetchall()}
    assert "pr_number" in columns
    await store.close()


@pytest.mark.asyncio
async def test_processing_work_item_is_recovered_after_restart(tmp_path):
    path = tmp_path / "durable-queue.sqlite3"
    payload = {
        "command": {"name": "review", "request_id": None, "text": "fix it"},
        "repo": "a/b",
        "issue_number": 42,
        "comment_id": 501,
        "user": "alice",
        "pull_number": 42,
        "review_id": 501,
    }
    first = AgentStore(path)
    await first.open()
    item_id = await first.persist_work_item("delivery-1", payload)
    assert item_id is not None
    assert await first.mark_work_processing(item_id) == 1
    await first.close()

    second = AgentStore(path)
    await second.open()
    recovered = await second.recover_work_items(10)
    assert recovered == [{"id": item_id, "payload": payload}]
    await second.mark_work_done(item_id)
    await second.close()

    third = AgentStore(path)
    await third.open()
    assert await third.recover_work_items(10) == []
    await third.close()


def test_work_item_serialization_round_trip():
    original = WorkItem(
        AgentCommand("review", text="fix it"),
        "a/b",
        42,
        501,
        "alice",
        pull_number=42,
        review_id=501,
    )
    restored = AgentService._deserialize_item(AgentService._serialize_item(original))
    assert restored == original


class ResumeState(TypedDict, total=False):
    answer: str


async def wait_for_answer(state: ResumeState):
    value = interrupt({"question": "continue?"})
    return {"answer": value}


@pytest.mark.asyncio
async def test_stop_resumes_interrupt_after_database_reopen(tmp_path):
    db = tmp_path / "checkpoints.sqlite3"
    config = {"configurable": {"thread_id": "restart-test"}}
    builder = StateGraph(ResumeState)
    builder.add_node("human", wait_for_answer)
    builder.add_edge(START, "human")
    builder.add_edge("human", END)

    conn1 = await aiosqlite.connect(db)
    saver1 = AsyncSqliteSaver(conn1)
    await saver1.setup()
    graph1 = builder.compile(checkpointer=saver1)
    await graph1.ainvoke({}, config)
    assert (await graph1.aget_state(config)).next == ("human",)
    await conn1.close()

    conn2 = await aiosqlite.connect(db)
    saver2 = AsyncSqliteSaver(conn2)
    await saver2.setup()
    graph2 = builder.compile(checkpointer=saver2)
    result = await graph2.ainvoke(Command(resume={"action": "stop"}), config)
    assert result["answer"] == {"action": "stop"}
    await conn2.close()


class FakeGitHub:
    def __init__(self):
        self.comments = []

    async def post_comment(self, repo, issue_number, body):
        self.comments.append(body)
        return len(self.comments)

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_service_builds_graph_with_async_sqlite(tmp_path):
    service = AgentService(settings(tmp_path), github=FakeGitHub())
    await service.open()
    assert service.agent_graph is not None
    assert "human_input" in service.agent_graph.graph.get_graph().nodes
    await service.close()


@pytest.mark.asyncio
async def test_review_event_resumes_original_issue_run(tmp_path):
    class CapturingGraph:
        def __init__(self):
            self.input = None

        async def ainvoke(self, value, config):
            self.input = value

    github = FakeGitHub()
    service = AgentService(settings(tmp_path), github=github)
    await service.store.open()
    await service.store.create_run(
        {
            "run_id": "run",
            "thread_id": "thread",
            "repo": "a/b",
            "issue_number": 7,
            "status": "reviewing",
        }
    )
    await service.store.update_run("run", pr_number=42)
    compiled = CapturingGraph()
    service.agent_graph = SimpleNamespace(graph=compiled)

    await service._review(
        WorkItem(AgentCommand("review"), "a/b", 42, 501, "alice", 42, 501)
    )

    assert compiled.input["issue_number"] == 7
    assert compiled.input["pr_number"] == 42
    assert compiled.input["review_id"] == 501
    assert compiled.input["thread_id"] == "run:review:501"
    assert (await service.store.get_run("run"))["thread_id"] == "run:review:501"
    await service.store.close()
    await github.close()


@pytest.mark.asyncio
async def test_verified_run_automatically_creates_draft_pr_and_saves_number(tmp_path):
    class Store:
        def __init__(self):
            self.values = {}

        async def get_run(self, run_id):
            return {
                "run_id": run_id,
                "status": "verified",
                "worktree": str(tmp_path),
                "branch": "agent/issue-7-run",
                "verified_revision": "verified-revision",
            }

        async def update_run(self, run_id, **values):
            self.values.update(values)

    class Workspace:
        async def revision(self, worktree):
            return "verified-revision"

        async def commit_and_push(self, worktree, branch, message):
            return "abc123"

    class GitHub:
        def __init__(self):
            self.comments = []

        async def create_pr(self, repo, branch, base, title, body):
            return {"number": 42, "html_url": "https://example.test/pr/42", "draft": True}

        async def post_comment(self, repo, number, body):
            self.comments.append((number, body))
            return 1

    graph = object.__new__(CodingAgentGraph)
    graph.settings = settings(tmp_path)
    graph.store = Store()
    graph.workspace = Workspace()
    graph.github = GitHub()
    context = IssueContext(
        repo="a/b",
        number=7,
        title="Add dark mode",
        requirements_version="hash",
    )

    result = await graph.publish_pr(
        {
            "run_id": "run",
            "repo": "a/b",
            "issue_number": 7,
            "issue_context": context.model_dump(),
        }
    )

    assert result == {
        "final_status": "reviewing",
        "pr_url": "https://example.test/pr/42",
        "pr_number": 42,
    }
    assert graph.store.values["status"] == "reviewing"
    assert graph.store.values["pr_number"] == 42
    assert "Draft pull request" in graph.github.comments[0][1]


def test_graph_routes_verified_work_to_pr_or_review_push(tmp_path):
    graph = object.__new__(CodingAgentGraph)
    graph.settings = settings(tmp_path)

    assert graph.after_verify(
        {"workflow_phase": "initial", "verification": {"passed": True}}
    ) == "publish"
    assert graph.after_verify(
        {"workflow_phase": "review", "verification": {"passed": True}}
    ) == "push"


@pytest.mark.asyncio
async def test_old_plan_approval_is_rejected(tmp_path):
    github = FakeGitHub()
    service = AgentService(settings(tmp_path), github=github)
    await service.store.open()
    await service.store.create_run(
        {
            "run_id": "run",
            "thread_id": "thread",
            "repo": "a/b",
            "issue_number": 1,
            "plan_version": 2,
        }
    )
    await service.store.ensure_request("old", "run", "approval", 1, "hash")
    await service._resume(
        WorkItem(AgentCommand("approve", "old"), "a/b", 1, 10, "alice")
    )
    assert "old plan version" in github.comments[-1]
    await service.store.close()
    await github.close()


def test_pi_result_contract_rejects_free_text_and_accepts_json():
    with pytest.raises(PiError):
        _parse_result("looks good")
    result = _parse_result(
        '{"status":"completed","summary":"done","plan":[],"questions":[],"claimed_checks":[]}'
    )
    assert result.status == "completed"


@pytest.mark.asyncio
async def test_pi_failure_is_retried_then_reported(tmp_path):
    class FailedPi:
        async def run(self, **kwargs):
            return PiResult(status="failed", summary="provider failed")

    class Store:
        async def update_run(self, *args, **kwargs):
            pass

    graph = object.__new__(CodingAgentGraph)
    graph.settings = settings(tmp_path)
    graph.pi = FailedPi()
    graph.store = Store()
    state = {
        "run_id": "run",
        "pi_session_id": "session",
        "worktree": str(tmp_path),
        "plan": ["change code"],
        "plan_version": 1,
        "attempt": 0,
    }
    first = await graph.implement_with_pi(state)
    assert first["attempt"] == 1
    assert "final_status" not in first
    second = await graph.implement_with_pi({**state, "attempt": 1})
    assert second["attempt"] == 2
    assert second["verification"]["summary"].startswith("Pi failed:")
    assert "final_status" not in second


def test_pi_environment_removes_github_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "secret-token")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "webhook-secret")
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    environment = PiRunner(settings(tmp_path))._environment()
    assert "GITHUB_TOKEN" not in environment
    assert "GITHUB_WEBHOOK_SECRET" not in environment
    assert environment["OPENROUTER_API_KEY"] == "openrouter-key"
    assert environment["GH_CONFIG_DIR"].endswith("pi-no-github-auth")
    assert environment["GIT_CONFIG_KEY_0"] == "credential.helper"
    assert environment["GIT_CONFIG_VALUE_0"] == ""


def test_pi_environment_deduplicates_windows_path(tmp_path, monkeypatch):
    repeated = [r"C:\Windows\System32", r"C:\Program Files\nodejs"] * 100
    monkeypatch.setenv("PATH", ";".join(repeated))
    environment = PiRunner(settings(tmp_path))._environment()
    assert environment["PATH"].split(";") == [
        r"C:\Windows\System32",
        r"C:\Program Files\nodejs",
    ]


def test_workspace_commands_deduplicate_windows_path(monkeypatch):
    repeated = [r"C:\Windows\System32", r"C:\Program Files\nodejs"] * 100
    monkeypatch.setenv("PATH", ";".join(repeated))
    environment = _command_environment(None)
    assert environment["PATH"].split(";") == [
        r"C:\Windows\System32",
        r"C:\Program Files\nodejs",
    ]


def test_pi_log_redacts_known_secrets_and_formats_tool_activity(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-secret")
    service = AgentService(settings(tmp_path), github=FakeGitHub())
    safe = service._redact(
        {"args": {"authorization": "Bearer value", "command": "echo openrouter-secret"}}
    )
    assert safe["args"]["authorization"] == "[REDACTED]"
    assert safe["args"]["command"] == "echo [REDACTED]"

    line = format_event(
        {
            "created_at": "2026-01-01T00:00:00Z",
            "run_id": "12345678-abcd",
            "phase": "implement",
            "event_type": "tool_execution_start",
            "payload_json": '{"toolName":"bash","args":{"command":"npm run lint"}}',
        }
    )
    assert "run=12345678" in line
    assert "bash" in line
    assert "npm run lint" in line

    malformed = format_event(
        {
            "created_at": "2026-01-01T00:00:00Z",
            "run_id": "12345678-abcd",
            "phase": "implement",
            "event_type": "message_end",
            "payload_json": '{"message":{"content":"cut off',
        }
    )
    assert "[malformed stored payload]" in malformed


@pytest.mark.asyncio
async def test_pi_event_truncation_preserves_valid_json(tmp_path):
    store = AgentStore(tmp_path / "agent.sqlite3")
    await store.open()
    try:
        await store.record_pi_event(
            "run-id",
            "implement",
            "message_end",
            {"type": "message_end", "data": "x" * 25_000},
        )
        cursor = await store.conn.execute("SELECT payload_json FROM pi_events")
        row = await cursor.fetchone()
        payload = json.loads(row["payload_json"])
        assert payload["_truncated"] is True
        assert len(payload["preview"]) == 19_000
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_verification_reports_failing_repository_check(tmp_path, monkeypatch):
    manager = WorkspaceManager(settings(tmp_path))

    async def changed_files(_worktree):
        return ["todo-app/src/App.tsx"]

    calls = 0

    async def fake_run(args, cwd, timeout=300, check=True):
        nonlocal calls
        calls += 1
        return (1, "", "lint failed")

    monkeypatch.setattr(manager, "changed_files", changed_files)
    monkeypatch.setattr("workspace._run", fake_run)
    result = await manager.verify(tmp_path)
    assert result.passed is False
    assert result.checks[0]["command"] == "npm run lint"
    assert calls == 1


class FakePiProcess:
    def __init__(self, events: list[dict], returncode: int = 0):
        lines = [json.dumps(event).encode("utf-8") + b"\n" for event in events]
        self.stdin = SimpleNamespace(
            write=lambda data: None, drain=self._noop, close=lambda: None
        )
        self.stdout = SimpleNamespace(readline=self._readline(lines))
        self.stderr = SimpleNamespace(read=self._read_empty)
        self._returncode = returncode
        self.returncode = None

    @staticmethod
    async def _noop():
        return None

    @staticmethod
    async def _read_empty():
        return b""

    @staticmethod
    def _readline(lines):
        async def readline():
            return lines.pop(0) if lines else b""
        return readline

    async def wait(self):
        self.returncode = self._returncode
        return self.returncode

    def kill(self):
        pass


def _submit_event(details: dict, is_error: bool = False) -> dict:
    return {
        "type": "tool_execution_end",
        "toolName": "submit_result",
        "isError": is_error,
        "result": {"content": [], "details": details},
    }


def _assistant_event(text: str) -> dict:
    return {
        "type": "message_end",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}], "stopReason": "stop"},
    }


async def _run_fake_pi(tmp_path, monkeypatch, events, read_only):
    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs["env"]
        return FakePiProcess(events)

    monkeypatch.setattr("pi_runner.asyncio.create_subprocess_exec", fake_exec)
    result = await PiRunner(settings(tmp_path)).run(
        run_id="run", phase="plan" if read_only else "implement", session_id="s",
        worktree=tmp_path, prompt="p", read_only=read_only,
    )
    return result, captured


@pytest.mark.asyncio
async def test_pi_result_comes_from_submit_result_tool(tmp_path, monkeypatch):
    details = {
        "status": "ready", "summary": "plan ready", "plan": ["edit App.tsx"],
        "questions": [], "claimed_checks": ["npm run build"],
    }
    result, captured = await _run_fake_pi(
        tmp_path, monkeypatch, [_submit_event(details)], read_only=True
    )
    assert result.status == "ready"
    assert result.plan == ["edit App.tsx"]
    args = list(captured["args"])
    assert args[args.index("--tools") + 1].endswith(",submit_result")
    assert args[args.index("--extension") + 1].endswith("submit_result.ts")
    assert captured["env"]["CODING_AGENT_RESULT_STATUSES"] == "ready,needs_input,failed"


@pytest.mark.asyncio
async def test_failed_submit_result_call_is_ignored_in_favor_of_retry(tmp_path, monkeypatch):
    events = [
        _submit_event({"status": "ready"}, is_error=True),
        _submit_event({
            "status": "completed", "summary": "done", "plan": [],
            "questions": [], "claimed_checks": [],
        }),
    ]
    result, _ = await _run_fake_pi(tmp_path, monkeypatch, events, read_only=False)
    assert result.status == "completed"


@pytest.mark.asyncio
async def test_pi_falls_back_to_text_json_when_tool_is_not_called(tmp_path, monkeypatch):
    text = '{"status":"completed","summary":"done","plan":[],"questions":[],"claimed_checks":[]}'
    result, _ = await _run_fake_pi(
        tmp_path, monkeypatch, [_assistant_event(text)], read_only=False
    )
    assert result.status == "completed"


@pytest.mark.asyncio
async def test_pi_rejects_status_from_another_phase(tmp_path, monkeypatch):
    details = {
        "status": "completed", "summary": "", "plan": [],
        "questions": [], "claimed_checks": [],
    }
    with pytest.raises(PiError, match="status 'completed' for phase plan"):
        await _run_fake_pi(tmp_path, monkeypatch, [_submit_event(details)], read_only=True)


@pytest.mark.asyncio
async def test_pi_without_result_raises(tmp_path, monkeypatch):
    with pytest.raises(PiError, match="without calling submit_result"):
        await _run_fake_pi(tmp_path, monkeypatch, [], read_only=False)


def test_plan_routes_to_implement_only_when_auto_approved(tmp_path):
    graph = object.__new__(CodingAgentGraph)
    graph.settings = settings(tmp_path)
    assert graph.after_plan({"pending_kind": "approval"}) == "human"
    assert graph.after_plan({"pending_kind": "approval", "auto_approve": True}) == "implement"
    assert graph.after_plan({"pending_kind": "clarification", "auto_approve": True}) == "human"
    assert graph.after_plan({"final_status": "failed", "auto_approve": True}) == "finish"


@pytest.mark.asyncio
async def test_auto_approved_plan_is_announced_once(tmp_path):
    class ReadyPi:
        async def run(self, **kwargs):
            return PiResult(status="ready", summary="ok", plan=["change title"])

    github = FakeGitHub()
    store = AgentStore(tmp_path / "agent.sqlite3")
    await store.open()
    await store.create_run({"run_id": "run-1234", "thread_id": "t", "repo": "a/b", "issue_number": 1})
    graph = object.__new__(CodingAgentGraph)
    graph.settings = settings(tmp_path)
    graph.pi = ReadyPi()
    graph.store = store
    graph.github = github
    state = {
        "run_id": "run-1234", "repo": "a/b", "issue_number": 1,
        "pi_session_id": "s", "worktree": str(tmp_path), "requirements_version": "v",
        "auto_approve": True,
        "issue_context": IssueContext(
            repo="a/b", number=1, title="t", requirements_version="v"
        ).model_dump(),
    }
    update = await graph.plan_with_pi(state)
    await graph.plan_with_pi(state)  # replay after a crash must not post twice
    assert graph.after_plan({**state, **update}) == "implement"
    assert len(github.comments) == 1
    assert "implementing without approval" in github.comments[0]
    assert (await store.get_run("run-1234"))["status"] == "implementing"
    await store.close()
