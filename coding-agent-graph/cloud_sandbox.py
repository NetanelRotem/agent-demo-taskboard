"""Run Pi and repository commands inside Docker Cloud Sandboxes (`sbx --cloud`).

Each run gets its own sandbox, named after its worktree. The sandbox clones the
repository, runs Pi and the checks, and pushes the branch itself; GitHub and
OpenRouter credentials stay in the Docker cloud secret store and are injected
by the sandbox proxy, so the real values never enter the sandbox.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from pathlib import Path
from typing import Any

from config import Settings
from events import NullEvents
from agent_models import PiResult
from pi_runner import PiError, PiRunner, RESULT_EXTENSION
from workspace import WorkspaceError, WorkspaceManager, _command_environment

logger = logging.getLogger(__name__)

AGENT_DIR = "/home/agent/.coding-agent"
HELPER = f"{AGENT_DIR}/agent.sh"
REMOTE_EXTENSION = f"{AGENT_DIR}/submit_result.ts"
REMOTE_SESSIONS = f"{AGENT_DIR}/pi-sessions"
HELPER_SOURCE = Path(__file__).resolve().parent / "sandbox" / "agent.sh"
LOCAL_SECRETS = {"GITHUB_TOKEN", "GH_TOKEN", "GITHUB_WEBHOOK_SECRET"}
SETTLE_SECONDS = 300
# Refusals while the sandbox is between states; retry once it settles.
BUSY_ERRORS = ("is stopping", "failed_precondition", "workflow settles", "retry once it stops")
# Failures that happen before sbx reaches the sandbox, so a retry is safe.
TRANSIENT_ERRORS = (
    "resolve access token",
    "Client.Timeout exceeded",
    "TLS handshake timeout",
    "connection reset by peer",
    "no such host",
)
RETRY_DELAYS = (3, 10, 30)


def sandbox_name(worktree: Path) -> str:
    return f"agent-{worktree.name}"


class SandboxClient:
    def __init__(self, settings: Settings, events: Any = None):
        self.settings = settings
        self.events = events or NullEvents()
        self._helpers_uploaded: set[str] = set()
        # Sandboxes this process saw running; cleared when it stops them.
        self._running: set[str] = set()

    def command(self, *args: str) -> list[str]:
        executable = shutil.which(self.settings.sbx_command) or self.settings.sbx_command
        return [executable, "--cloud", *args]

    @staticmethod
    def environment() -> dict[str, str]:
        env = _command_environment(None)
        for key in LOCAL_SECRETS:
            env.pop(key, None)
        return env

    async def run(
        self,
        *args: str,
        input: bytes | None = None,
        timeout: int = 300,
        check: bool = True,
    ) -> tuple[int, str, str]:
        for delay in RETRY_DELAYS:
            code, out, err = await self._run_once(*args, input=input, timeout=timeout)
            if code == 0 or not any(marker in err for marker in TRANSIENT_ERRORS):
                break
            logger.warning("Transient sbx failure, retrying in %ss: %s", delay, err.strip()[-300:])
            await self.events.emit(
                "sbx_retry",
                f"Docker sbx `{args[0]}` failed transiently; retrying in {delay}s: {err.strip()[-300:]}",
                sandbox=next((a for a in args if a.startswith("agent-issue-")), None),
            )
            await asyncio.sleep(delay)
        else:
            code, out, err = await self._run_once(*args, input=input, timeout=timeout)
        if check and code != 0:
            raise WorkspaceError(f"sbx failed ({code}): {' '.join(args[:3])}\n{err or out}")
        return code, out, err

    async def _run_once(
        self, *args: str, input: bytes | None, timeout: int
    ) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            *self.command(*args),
            env=self.environment(),
            stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(input), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise WorkspaceError(f"sbx timed out: {' '.join(args[:3])}")
        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace")
        return process.returncode or 0, out, err

    async def status(self, name: str) -> str | None:
        _, out, _ = await self.run("ls", "--json", timeout=60)
        for sandbox in json.loads(out or "{}").get("sandboxes") or []:
            if sandbox.get("name") == name:
                return str(sandbox.get("status") or "")
        return None

    @staticmethod
    def is_transitional(status: str | None) -> bool:
        # Docker reports in-between states as gerunds (stopping, hibernating,
        # resuming, ...); exec and rm are refused until they finish.
        return bool(status) and status.endswith("ing") and status != "running"

    async def wait_until_settled(self, name: str) -> str | None:
        """Wait out transitional states such as stopping or hibernating."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SETTLE_SECONDS
        while True:
            status = await self.status(name)
            if not self.is_transitional(status) or loop.time() > deadline:
                return status
            await asyncio.sleep(3)

    async def exec(
        self,
        name: str,
        args: list[str],
        *,
        input: bytes | None = None,
        timeout: int = 300,
        check: bool = True,
    ) -> tuple[int, str, str]:
        flags = ["-i"] if input is not None else []
        try:
            return await self.run(
                "exec", *flags, name, *args, input=input, timeout=timeout, check=check
            )
        except WorkspaceError as exc:
            if not any(marker in str(exc) for marker in BUSY_ERRORS):
                raise
        await self.wait_until_settled(name)
        return await self.run("exec", *flags, name, *args, input=input, timeout=timeout, check=check)

    async def upload(self, name: str, remote_path: str, content: bytes) -> None:
        # Normalize line endings: files checked out on Windows may carry CRLF.
        await self.exec(
            name,
            ["bash", "-c", f"cat > {remote_path}"],
            input=content.replace(b"\r\n", b"\n"),
            timeout=120,
        )

    async def ensure_running(self, name: str) -> None:
        """Start a stopped sandbox explicitly (exec would too) so the start is visible."""
        if name in self._running:
            return
        status = await self.wait_until_settled(name)
        if status is None:
            raise WorkspaceError(f"Cloud sandbox {name} no longer exists; start a new run")
        if status != "running":
            started = time.monotonic()
            await self.exec(name, ["true"], timeout=300)
            await self.events.emit(
                "sandbox_started",
                f"Sandbox {name} started ({time.monotonic() - started:.0f}s, was {status})",
                sandbox=name,
            )
        self._running.add(name)

    async def stop(self, name: str) -> None:
        self._running.discard(name)
        if await self.status(name) == "running":
            await self.run("stop", name, timeout=120)
            await self.events.emit("sandbox_stopped", f"Sandbox {name} stopped (idle, not billed)", sandbox=name)

    async def remove(self, name: str) -> None:
        self._running.discard(name)
        self._helpers_uploaded.discard(name)
        for attempt in range(3):
            if await self.wait_until_settled(name) is None:
                return
            try:
                await self.run("rm", "-f", name, timeout=300)
                break
            except WorkspaceError as exc:
                # e.g. stopped by suspend() moments before a PR merge: still hibernating.
                if attempt == 2 or not any(marker in str(exc) for marker in BUSY_ERRORS):
                    raise
                await asyncio.sleep(5)
        await self.events.emit("sandbox_deleted", f"Sandbox {name} deleted", sandbox=name)

    async def ensure(self, name: str) -> None:
        status = await self.wait_until_settled(name)
        if status is None:
            started = time.monotonic()
            await self.run(
                "create",
                "--name", name,
                "--ttl", self.settings.sandbox_ttl,
                "--on-timeout", "stop",
                "-q",
                self.settings.sandbox_kit,
                timeout=900,
            )
            await self.run(
                "policy", "allow", "network", "--sandbox", name,
                ",".join(self.settings.sandbox_allow_network),
                timeout=120,
            )
            self._running.add(name)
            await self.events.emit(
                "sandbox_created",
                f"Sandbox {name} created ({time.monotonic() - started:.0f}s, "
                f"kit {self.settings.sandbox_kit}, ttl {self.settings.sandbox_ttl})",
                sandbox=name,
            )
        await self.ensure_running(name)
        self._helpers_uploaded.discard(name)
        await self.ensure_helpers(name)

    async def ensure_helpers(self, name: str) -> None:
        """Upload this server's helper files once per sandbox, so code updates reach
        sandboxes created by an earlier server process."""
        if name in self._helpers_uploaded:
            return
        await self.exec(name, ["mkdir", "-p", AGENT_DIR, REMOTE_SESSIONS], timeout=300)
        await self.upload(name, HELPER, HELPER_SOURCE.read_bytes())
        await self.upload(name, REMOTE_EXTENSION, RESULT_EXTENSION.read_bytes())
        self._helpers_uploaded.add(name)


class CloudWorkspaceManager(WorkspaceManager):
    def __init__(self, settings: Settings, sandbox: SandboxClient | None = None):
        super().__init__(settings)
        self.sandbox = sandbox or SandboxClient(settings)

    async def _helper(
        self, worktree: Path, *args: str, timeout: int = 300, check: bool = True
    ) -> tuple[int, str, str]:
        name = sandbox_name(worktree)
        await self.sandbox.ensure_running(name)
        await self.sandbox.ensure_helpers(name)
        return await self.sandbox.exec(
            name, ["bash", HELPER, *args], timeout=timeout, check=check
        )

    async def prepare(self, run_id: str, issue_number: int, repo: str = "") -> tuple[str, Path]:
        if not repo:
            raise WorkspaceError("Cloud execution needs the GitHub repository (owner/name)")
        branch = f"agent/issue-{issue_number}-{run_id[:8]}"
        # Never created locally: the name identifies the run's sandbox.
        worktree = (self.settings.worktrees_dir / f"issue-{issue_number}-{run_id[:8]}").resolve()
        await self.sandbox.ensure(sandbox_name(worktree))
        await self._helper(
            worktree, "setup", repo, branch, self.settings.main_branch, timeout=900
        )
        return branch, worktree

    async def changed_files(self, worktree: Path) -> list[str]:
        _, out, _ = await self._helper(worktree, "changed-files", self.settings.main_branch)
        return sorted({line.strip() for line in out.splitlines() if line.strip()})

    async def revision(self, worktree: Path) -> str:
        _, out, _ = await self._helper(worktree, "revision", self.settings.main_branch)
        return out.strip()

    async def suspend(self, worktree: Path) -> None:
        # A stopped sandbox is not billed; the next exec restarts it with files intact.
        await self.sandbox.stop(sandbox_name(worktree))

    async def destroy(self, worktree: Path) -> None:
        await self.sandbox.remove(sandbox_name(worktree))

    async def _run_check(self, command: list[str], worktree: Path) -> tuple[int, str, str]:
        return await self._helper(
            worktree, "check", self.settings.todo_path, *command, timeout=900, check=False
        )

    async def commit_and_push(self, worktree: Path, branch: str, message: str) -> str:
        _, out, _ = await self._helper(
            worktree, "commit-push", self.settings.todo_path, branch, message, timeout=300
        )
        lines = out.strip().splitlines()
        if not lines:
            raise WorkspaceError("Sandbox push did not report a commit SHA")
        return lines[-1].strip()


class CloudPiRunner(PiRunner):
    def __init__(self, settings: Settings, *args, sandbox: SandboxClient | None = None, **kwargs):
        super().__init__(settings, *args, **kwargs)
        self.sandbox = sandbox or SandboxClient(settings)
        self._events_seen = 0

    async def run(self, *, worktree: Path, **kwargs) -> PiResult:
        await self.sandbox.ensure_running(sandbox_name(worktree))
        await self.sandbox.ensure_helpers(sandbox_name(worktree))
        for delay in (*RETRY_DELAYS, None):
            self._events_seen = 0
            try:
                return await super().run(worktree=worktree, **kwargs)
            except PiError as exc:
                # Retry only when sbx failed before Pi produced anything.
                transient = any(marker in str(exc) for marker in TRANSIENT_ERRORS)
                if delay is None or self._events_seen or not transient:
                    raise
                logger.warning("Transient sbx failure starting Pi, retrying in %ss: %s", delay, exc)
                await self.sandbox.events.emit(
                    "sbx_retry",
                    f"Starting Pi failed transiently; retrying in {delay}s: {str(exc)[-300:]}",
                    sandbox=sandbox_name(worktree),
                )
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    def _parse_event(self, raw: bytes) -> dict | None:
        try:
            event = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            # sbx may print progress (e.g. while starting a stopped sandbox).
            logger.debug("Ignoring non-JSON sbx output: %r", raw[:200])
            return None
        if not isinstance(event, dict):
            return None
        self._events_seen += 1
        return event

    def _command(
        self, *, session_id: str, worktree: Path, tools: str, statuses: tuple[str, ...]
    ) -> tuple[list[str], str | None, dict[str, str]]:
        args = self.sandbox.command(
            "exec", "-i", sandbox_name(worktree), "bash", HELPER, "pi", ",".join(statuses)
        ) + self._pi_arguments(
            session_id=session_id,
            tools=tools,
            session_dir=REMOTE_SESSIONS,
            extension=REMOTE_EXTENSION,
        )
        return args, None, self.sandbox.environment()

    async def _after_kill(self, worktree: Path) -> None:
        try:
            await self.sandbox.exec(
                sandbox_name(worktree), ["bash", HELPER, "stop-pi"], timeout=60, check=False
            )
        except (WorkspaceError, OSError) as exc:
            logger.warning("Could not stop Pi inside the sandbox: %s", exc)

