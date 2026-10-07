from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from cloud_sandbox import (
    HELPER,
    REMOTE_EXTENSION,
    CloudPiRunner,
    CloudWorkspaceManager,
    SandboxClient,
    sandbox_name,
)
from config import Settings
from test_agent_core import FakePiProcess, _submit_event, settings
from workspace import WorkspaceError


def cloud_settings(tmp_path: Path) -> Settings:
    return replace(settings(tmp_path), execution_mode="cloud", sbx_command="sbx")


class FakeSandbox(SandboxClient):
    """Records sbx calls instead of running them."""

    def __init__(self, settings: Settings, outputs: dict[str, str] | None = None):
        super().__init__(settings)
        self.outputs = outputs or {}
        self.ensured: list[str] = []
        self.calls: list[tuple[str, list[str]]] = []

    async def ensure(self, name: str) -> None:
        self.ensured.append(name)

    async def ensure_helpers(self, name: str) -> None:
        pass

    async def ensure_running(self, name: str) -> None:
        pass

    async def exec(self, name, args, *, input=None, timeout=300, check=True):
        self.calls.append((name, list(args)))
        subcommand = args[2] if len(args) > 2 and args[1] == HELPER else args[0]
        return 0, self.outputs.get(subcommand, ""), ""


def test_execution_mode_is_read_and_validated(monkeypatch):
    monkeypatch.setenv("EXECUTION_MODE", "Cloud")
    monkeypatch.setenv("SANDBOX_ALLOW_NETWORK", "github.com, openrouter.ai")
    loaded = Settings.from_env()
    assert loaded.execution_mode == "cloud"
    assert loaded.sandbox_allow_network == ("github.com", "openrouter.ai")

    monkeypatch.setenv("EXECUTION_MODE", "kubernetes")
    with pytest.raises(ValueError, match="EXECUTION_MODE"):
        Settings.from_env()


def test_execution_mode_defaults_to_local(monkeypatch):
    monkeypatch.delenv("EXECUTION_MODE", raising=False)
    assert Settings.from_env().execution_mode == "local"


@pytest.mark.asyncio
async def test_cloud_prepare_creates_sandbox_and_clones_branch(tmp_path):
    sandbox = FakeSandbox(cloud_settings(tmp_path))
    manager = CloudWorkspaceManager(cloud_settings(tmp_path), sandbox)

    branch, worktree = await manager.prepare("abcdef1234", 12, "acme/todo")

    assert branch == "agent/issue-12-abcdef12"
    assert sandbox.ensured == ["agent-issue-12-abcdef12"]
    assert sandbox.calls == [
        ("agent-issue-12-abcdef12", ["bash", HELPER, "setup", "acme/todo", branch, "main"])
    ]
    assert sandbox_name(worktree) == "agent-issue-12-abcdef12"
    assert not worktree.exists()


@pytest.mark.asyncio
async def test_cloud_prepare_requires_repository(tmp_path):
    manager = CloudWorkspaceManager(cloud_settings(tmp_path), FakeSandbox(cloud_settings(tmp_path)))
    with pytest.raises(WorkspaceError, match="repository"):
        await manager.prepare("abcdef1234", 12)


@pytest.mark.asyncio
async def test_cloud_verify_runs_checks_inside_sandbox_and_enforces_boundary(tmp_path):
    sandbox = FakeSandbox(
        cloud_settings(tmp_path),
        {"changed-files": "todo-app/src/App.tsx\n", "revision": "abc123\n"},
    )
    manager = CloudWorkspaceManager(cloud_settings(tmp_path), sandbox)
    worktree = tmp_path / "issue-1-run"

    result = await manager.verify(worktree)

    assert result.passed is True
    assert result.revision == "abc123"
    checks = [args for _, args in sandbox.calls if args[2] == "check"]
    assert checks == [
        ["bash", HELPER, "check", "todo-app", "npm", "run", "lint"],
        ["bash", HELPER, "check", "todo-app", "npm", "run", "build"],
    ]

    sandbox.outputs["changed-files"] = "todo-app/src/App.tsx\n.github/workflows/deploy.yml\n"
    escaped = await manager.verify(worktree)
    assert escaped.passed is False
    assert ".github/workflows/deploy.yml" in escaped.summary


@pytest.mark.asyncio
async def test_cloud_commit_and_push_happens_in_sandbox(tmp_path):
    sandbox = FakeSandbox(cloud_settings(tmp_path), {"commit-push": "deadbeef\n"})
    manager = CloudWorkspaceManager(cloud_settings(tmp_path), sandbox)

    sha = await manager.commit_and_push(tmp_path / "issue-3-run", "agent/issue-3-run", "Resolve #3")

    assert sha == "deadbeef"
    assert sandbox.calls == [
        (
            "agent-issue-3-run",
            ["bash", HELPER, "commit-push", "todo-app", "agent/issue-3-run", "Resolve #3"],
        )
    ]


@pytest.mark.asyncio
async def test_cloud_pi_runs_through_sbx_exec_and_ignores_progress_lines(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "local-secret")
    captured = {}
    details = {
        "status": "completed", "summary": "done", "plan": [],
        "questions": [], "claimed_checks": [],
    }

    class ProcessWithProgress(FakePiProcess):
        def __init__(self):
            super().__init__([_submit_event(details)])
            original = self.stdout.readline
            lines = [b"Starting cloud sandbox sbx_123 ...\n"]

            async def readline():
                return lines.pop(0) if lines else await original()

            self.stdout.readline = readline

    async def fake_exec(*args, **kwargs):
        captured["args"] = list(args)
        captured["env"] = kwargs["env"]
        captured["cwd"] = kwargs["cwd"]
        return ProcessWithProgress()

    async def no_upload(self, name):
        pass

    monkeypatch.setattr("pi_runner.asyncio.create_subprocess_exec", fake_exec)
    monkeypatch.setattr(SandboxClient, "ensure_helpers", no_upload)
    monkeypatch.setattr(SandboxClient, "ensure_running", no_upload)
    runner = CloudPiRunner(cloud_settings(tmp_path))
    result = await runner.run(
        run_id="run", phase="implement", session_id="s",
        worktree=tmp_path / "issue-5-run", prompt="p", read_only=False,
    )

    assert result.status == "completed"
    args = captured["args"]
    assert args[1:5] == ["--cloud", "exec", "-i", "agent-issue-5-run"]
    assert args[5:9] == ["bash", HELPER, "pi", "completed,needs_input,failed"]
    assert args[args.index("--extension") + 1] == REMOTE_EXTENSION
    assert captured["cwd"] is None
    assert "GITHUB_TOKEN" not in captured["env"]


@pytest.mark.asyncio
async def test_sandbox_exec_retries_while_sandbox_is_stopping(tmp_path, monkeypatch):
    client = SandboxClient(cloud_settings(tmp_path))
    attempts = []

    async def fake_run(*args, input=None, timeout=300, check=True):
        attempts.append(args)
        if args[0] == "ls":
            return 0, json.dumps({"sandboxes": [{"name": "box", "status": "stopped"}]}), ""
        if len([a for a in attempts if a[0] == "exec"]) == 1:
            raise WorkspaceError("sbx failed (1): exec box\nerror: sandbox 'sbx_1' is stopping")
        return 0, "ok", ""

    monkeypatch.setattr(client, "run", fake_run)
    code, out, _ = await client.exec("box", ["true"])

    assert (code, out) == (0, "ok")
    assert [a[0] for a in attempts] == ["exec", "ls", "exec"]


@pytest.mark.asyncio
async def test_sandbox_ensure_creates_with_ttl_policy_and_uploads_helpers(tmp_path, monkeypatch):
    client = SandboxClient(cloud_settings(tmp_path))
    runs: list[tuple] = []
    uploads: dict[str, bytes] = {}

    async def fake_run(*args, input=None, timeout=300, check=True):
        runs.append(args)
        if args[0] == "ls":
            return 0, json.dumps({"sandboxes": []}), ""
        if args[0] == "exec" and input is not None:
            uploads[args[-1].removeprefix("cat > ")] = input
        return 0, "", ""

    monkeypatch.setattr(client, "run", fake_run)
    await client.ensure("agent-issue-9-run")

    create = next(args for args in runs if args[0] == "create")
    assert create[create.index("--ttl") + 1] == "24h"
    assert create[create.index("--on-timeout") + 1] == "stop"
    assert create[-1] == "docker.io/docker/sbx-kit-pi:latest"
    policy = next(args for args in runs if args[0] == "policy")
    assert "api.github.com" in policy[-1] and "openrouter.ai" in policy[-1]
    assert set(uploads) == {HELPER, REMOTE_EXTENSION}
    assert b"\r\n" not in uploads[HELPER]


def test_failed_workspace_preparation_routes_to_finish():
    from agent_graph import CodingAgentGraph

    after_load = CodingAgentGraph.after_load
    assert after_load(None, {"command_name": "start"}) == "plan"
    assert after_load(None, {"command_name": "start", "final_status": "failed"}) == "finish"
    assert after_load(None, {"command_name": "review", "final_status": "failed"}) == "review"


@pytest.mark.asyncio
async def test_cloud_suspend_stops_only_a_running_sandbox(tmp_path):
    sandbox = FakeSandbox(cloud_settings(tmp_path))
    statuses = {"agent-issue-4-run": "running"}
    stopped = []

    async def status(name):
        return statuses.get(name)

    async def run(*args, input=None, timeout=300, check=True):
        stopped.append(args)
        return 0, "", ""

    sandbox.status = status
    sandbox.run = run
    manager = CloudWorkspaceManager(cloud_settings(tmp_path), sandbox)

    await manager.suspend(tmp_path / "issue-4-run")
    assert stopped == [("stop", "agent-issue-4-run")]

    statuses["agent-issue-4-run"] = "stopped"
    await manager.suspend(tmp_path / "issue-4-run")
    assert len(stopped) == 1


@pytest.mark.asyncio
async def test_service_reacts_to_commands_and_suspends_after_graph_returns(tmp_path):
    from agent_models import AgentCommand
    from service import AgentService, WorkItem
    from test_agent_core import FakeGitHub

    github = FakeGitHub()
    service = AgentService(settings(tmp_path), github=github)
    await service.store.open()
    try:
        await service._acknowledge(WorkItem(AgentCommand("approve", "r1"), "a/b", 7, 99, "alice"))
        await service._acknowledge(WorkItem(AgentCommand("review"), "a/b", 7, 55, "alice", 3, 4))
        assert github.reactions == [("a/b", 99, "+1")]

        suspended = []

        async def suspend(worktree):
            suspended.append(worktree)

        service.workspace.suspend = suspend
        await service.store.create_run(
            {"run_id": "run-1", "thread_id": "t", "repo": "a/b", "issue_number": 7, "status": "waiting_for_human"}
        )
        await service._suspend_workspace("run-1")
        assert suspended == []
        await service.store.update_run("run-1", worktree=str(tmp_path / "issue-7-run"))
        await service._suspend_workspace("run-1")
        assert suspended == [tmp_path / "issue-7-run"]
    finally:
        await service.store.close()


@pytest.mark.asyncio
async def test_github_reaction_and_ready_pr_requests():
    import httpx

    from github_client import GitHubClient

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, json.loads(request.content or b"null")))
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(201, json={"number": 1, "html_url": "u", "id": 5})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.github.test")
    github = GitHubClient("", client=http)
    await github.add_reaction("a/b", 99)
    await github.create_pr("a/b", "agent/x", "main", "Title", "Body")
    await http.aclose()

    assert seen[0] == ("POST", "/repos/a/b/issues/comments/99/reactions", {"content": "+1"})
    assert seen[-1][2]["draft"] is False


@pytest.mark.asyncio
async def test_helpers_are_uploaded_once_per_sandbox_per_process(tmp_path, monkeypatch):
    client = SandboxClient(cloud_settings(tmp_path))
    execs = []

    async def fake_exec(name, args, *, input=None, timeout=300, check=True):
        execs.append((name, args[0]))
        return 0, "", ""

    monkeypatch.setattr(client, "exec", fake_exec)
    await client.ensure_helpers("box")
    await client.ensure_helpers("box")
    assert execs == [("box", "mkdir"), ("box", "bash"), ("box", "bash")]


@pytest.mark.asyncio
async def test_pi_output_reader_failure_fails_fast_instead_of_hanging(tmp_path, monkeypatch):
    from pi_runner import EVENT_LINE_LIMIT, PiError, PiRunner

    captured = {}

    class StuckProcess(FakePiProcess):
        def __init__(self):
            super().__init__([])

            async def readline():
                raise ValueError("Separator is found, but chunk is longer than limit")

            self.stdout.readline = readline
            self.killed = False

        async def wait(self):
            while not self.killed:
                await __import__("asyncio").sleep(0.01)
            self.returncode = -9
            return self.returncode

        def kill(self):
            self.killed = True

    async def fake_exec(*args, **kwargs):
        captured["limit"] = kwargs.get("limit")
        return StuckProcess()

    monkeypatch.setattr("pi_runner.asyncio.create_subprocess_exec", fake_exec)
    with pytest.raises(PiError, match="Reading Pi output failed"):
        await PiRunner(settings(tmp_path)).run(
            run_id="run", phase="implement", session_id="s",
            worktree=tmp_path, prompt="p", read_only=False,
        )
    assert captured["limit"] == EVENT_LINE_LIMIT >= 16 * 1024 * 1024


@pytest.mark.asyncio
async def test_replayed_approval_continues_a_run_interrupted_mid_step(tmp_path):
    from types import SimpleNamespace

    from agent_models import AgentCommand
    from service import AgentService, WorkItem
    from test_agent_core import FakeGitHub

    class Graph:
        def __init__(self, next_nodes, interrupts):
            self.snapshot = SimpleNamespace(
                next=next_nodes, tasks=[SimpleNamespace(interrupts=interrupts)]
            )
            self.invoked = []

        async def aget_state(self, config):
            return self.snapshot

        async def ainvoke(self, value, config):
            self.invoked.append(value)

    github = FakeGitHub()
    service = AgentService(settings(tmp_path), github=github)
    await service.store.open()
    try:
        await service.store.create_run(
            {"run_id": "run-1", "thread_id": "t", "repo": "a/b", "issue_number": 6, "status": "waiting_for_human"}
        )
        await service.store.ensure_request("run-1-p1-a", "run-1", "approval", 1, "h")
        await service.store.resolve_request("run-1-p1-a", {"action": "approve", "text": ""})
        approve = WorkItem(AgentCommand("approve", "run-1-p1-a"), "a/b", 6, 9, "alice")

        mid_step = Graph(("implement_with_pi",), ())
        service.agent_graph = SimpleNamespace(graph=mid_step)
        await service._resume(approve)
        assert mid_step.invoked == [None]
        assert github.comments == []

        waiting = Graph(("human_input",), ("pending question",))
        service.agent_graph = SimpleNamespace(graph=waiting)
        await service._resume(approve)
        assert waiting.invoked == []
        assert "already resolved" in github.comments[-1]
    finally:
        await service.store.close()


@pytest.mark.asyncio
async def test_cleanup_destroys_sandbox_and_closes_run(tmp_path):
    from agent_models import AgentCommand
    from service import AgentService, WorkItem
    from test_agent_core import FakeGitHub

    github = FakeGitHub()
    service = AgentService(settings(tmp_path), github=github)
    await service.store.open()
    try:
        destroyed = []

        async def destroy(worktree):
            destroyed.append(worktree)

        service.workspace.destroy = destroy
        await service.store.create_run(
            {"run_id": "run-7", "thread_id": "t", "repo": "a/b", "issue_number": 6, "status": "reviewing"}
        )
        await service.store.update_run("run-7", pr_number=7, worktree=str(tmp_path / "issue-6-run"))

        await service._cleanup(WorkItem(AgentCommand("cleanup", text="merged"), "a/b", 7, 1, "alice", 7))

        assert destroyed == [tmp_path / "issue-6-run"]
        assert (await service.store.get_run("run-7"))["status"] == "merged"
        assert await service.store.get_active_run("a/b", 6) is None
        assert "workspace" in github.comments[-1]
        assert await service.enqueue(
            WorkItem(AgentCommand("cleanup", text="closed"), "a/b", 7, 2, "mallory", 7), "d-1"
        ) == "ignored"
    finally:
        await service.store.close()


@pytest.mark.asyncio
async def test_cloud_destroy_removes_existing_sandbox(tmp_path):
    sandbox = FakeSandbox(cloud_settings(tmp_path))
    removed = []

    async def status(name):
        return "stopped" if name == "agent-issue-6-run" else None

    async def run(*args, input=None, timeout=300, check=True):
        removed.append(args)
        return 0, "", ""

    sandbox.status = status
    sandbox.run = run
    manager = CloudWorkspaceManager(cloud_settings(tmp_path), sandbox)

    await manager.destroy(tmp_path / "issue-6-run")
    await manager.destroy(tmp_path / "issue-9-gone")
    assert removed == [("rm", "-f", "agent-issue-6-run")]


@pytest.mark.asyncio
async def test_cloud_pi_retries_transient_sbx_failure_only_before_any_event(tmp_path, monkeypatch):
    import cloud_sandbox
    from pi_runner import PiError, PiRunner

    monkeypatch.setattr(cloud_sandbox, "RETRY_DELAYS", (0, 0))
    calls = []
    transient = PiError("Pi exited with code 1: error: cloud request timed out: unauthenticated: resolve access token")

    async def no_upload(self, name):
        pass

    async def flaky_run(self, **kwargs):
        calls.append(kwargs["phase"])
        if len(calls) == 1:
            raise transient
        return "result"

    monkeypatch.setattr(SandboxClient, "ensure_helpers", no_upload)
    monkeypatch.setattr(SandboxClient, "ensure_running", no_upload)
    monkeypatch.setattr(PiRunner, "run", flaky_run)
    runner = CloudPiRunner(cloud_settings(tmp_path))
    result = await runner.run(
        run_id="r", phase="plan", session_id="s", worktree=tmp_path / "issue-8-run",
        prompt="p", read_only=True,
    )
    assert result == "result" and calls == ["plan", "plan"]

    async def fails_after_output(self, **kwargs):
        calls.append("late")
        self._events_seen = 3
        raise transient

    monkeypatch.setattr(PiRunner, "run", fails_after_output)
    with pytest.raises(PiError):
        await runner.run(
            run_id="r", phase="plan", session_id="s", worktree=tmp_path / "issue-8-run",
            prompt="p", read_only=True,
        )
    assert calls.count("late") == 1


def test_transitional_states_include_hibernating_but_not_running():
    assert SandboxClient.is_transitional("hibernating")
    assert SandboxClient.is_transitional("stopping")
    assert not SandboxClient.is_transitional("running")
    assert not SandboxClient.is_transitional("stopped")
    assert not SandboxClient.is_transitional(None)


@pytest.mark.asyncio
async def test_remove_waits_for_hibernation_and_retries_refused_delete(tmp_path, monkeypatch):
    import cloud_sandbox

    monkeypatch.setattr(cloud_sandbox.asyncio, "sleep", _no_sleep)
    client = SandboxClient(cloud_settings(tmp_path))
    statuses = iter(["hibernating", "stopped", "stopped"])
    calls = []

    async def fake_run(*args, input=None, timeout=300, check=True):
        calls.append(args[0])
        if args[0] == "ls":
            return 0, json.dumps({"sandboxes": [{"name": "box", "status": next(statuses)}]}), ""
        if calls.count("rm") == 1:
            raise WorkspaceError(
                'sbx failed (1): rm -f box\nfailed_precondition: sandbox is "hibernating"; '
                "retry delete after the workflow settles"
            )
        return 0, "", ""

    monkeypatch.setattr(client, "run", fake_run)
    await client.remove("box")
    assert calls == ["ls", "ls", "rm", "ls", "rm"]


async def _no_sleep(_seconds):
    return None
