from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from pathlib import Path
from typing import Awaitable, Callable

from agent_models import PiResult
from config import Settings


class PiError(RuntimeError):
    pass


class PiStopped(PiError):
    pass


EventSink = Callable[[str, str, dict], Awaitable[None]]

RESULT_TOOL = "submit_result"
RESULT_EXTENSION = Path(__file__).resolve().parent / "pi_extensions" / "submit_result.ts"
PLAN_STATUSES = ("ready", "needs_input", "failed")
IMPLEMENT_STATUSES = ("completed", "needs_input", "failed")
CancelCheck = Callable[[str], Awaitable[bool]]
EVENT_LINE_LIMIT = 64 * 1024 * 1024


def _assistant_text(message: dict) -> str:
    if message.get("role") != "assistant":
        return ""
    content = message.get("content") or []
    if isinstance(content, str):
        return content
    return "".join(item.get("text", "") for item in content if item.get("type") == "text")


def _parse_result(text: str) -> PiResult:
    candidates = [text.strip()]
    match = re.fullmatch(r"\s*```(?:json)?\s*(\{.*\})\s*```\s*", text, re.DOTALL)
    if match:
        candidates.append(match.group(1))
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            candidates.append(json.dumps(value))
    for candidate in reversed(candidates):
        try:
            return PiResult.model_validate(json.loads(candidate))
        except (json.JSONDecodeError, ValueError):
            continue
    raise PiError("Pi final response did not match the required JSON contract")


class PiRunner:
    def __init__(
        self,
        settings: Settings,
        event_sink: EventSink | None = None,
        cancel_check: CancelCheck | None = None,
    ):
        self.settings = settings
        self.event_sink = event_sink
        self.cancel_check = cancel_check

    def _environment(self, statuses: tuple[str, ...] = ()) -> dict[str, str]:
        blocked = {
            "GITHUB_TOKEN", "GH_TOKEN", "GITHUB_WEBHOOK_SECRET",
            "AUTHORIZED_GITHUB_USERS",
        }
        env = {key: value for key, value in os.environ.items() if key not in blocked}
        path_value = next(
            (value for key, value in env.items() if key.upper() == "PATH"), ""
        )
        for key in [key for key in env if key.upper() == "PATH"]:
            del env[key]
        env["PATH"] = self._deduplicate_path(path_value)
        isolated_gh_config = self.settings.data_dir / "pi-no-github-auth"
        isolated_gh_config.mkdir(parents=True, exist_ok=True)
        env.update(
            {
                "GH_CONFIG_DIR": str(isolated_gh_config),
                "GH_PROMPT_DISABLED": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "GCM_INTERACTIVE": "never",
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "credential.helper",
                "GIT_CONFIG_VALUE_0": "",
                "CODING_AGENT_RESULT_STATUSES": ",".join(statuses),
            }
        )
        return env

    @staticmethod
    def _deduplicate_path(value: str) -> str:
        seen: set[str] = set()
        unique: list[str] = []
        for entry in value.split(os.pathsep):
            entry = entry.strip()
            if not entry:
                continue
            key = os.path.normcase(os.path.normpath(entry))
            if key in seen:
                continue
            seen.add(key)
            unique.append(entry)
        return os.pathsep.join(unique)

    def _pi_arguments(
        self, *, session_id: str, tools: str, session_dir: str, extension: str
    ) -> list[str]:
        return [
            "--mode", "json",
            "--provider", "openrouter",
            "--model", self.settings.openrouter_model,
            "--session-id", session_id,
            "--session-dir", session_dir,
            "--tools", f"{tools},{RESULT_TOOL}",
            "--no-extensions",
            "--extension", extension,
            "--no-skills",
            "--no-prompt-templates",
            "--no-context-files",
        ]

    def _command(
        self, *, session_id: str, worktree: Path, tools: str, statuses: tuple[str, ...]
    ) -> tuple[list[str], str | None, dict[str, str]]:
        """Return (argv, cwd, env) for the process that runs Pi."""
        executable = shutil.which(self.settings.pi_command) or self.settings.pi_command
        self.settings.pi_sessions_dir.mkdir(parents=True, exist_ok=True)
        args = [executable] + self._pi_arguments(
            session_id=session_id,
            tools=tools,
            session_dir=str(self.settings.pi_sessions_dir),
            extension=str(RESULT_EXTENSION),
        )
        return args, str(worktree), self._environment(statuses)

    async def _after_kill(self, worktree: Path) -> None:
        """Hook for runners whose Pi process outlives the local one."""

    def _parse_event(self, raw: bytes) -> dict | None:
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PiError(f"Invalid JSONL event from Pi: {exc}") from exc

    async def run(
        self,
        *,
        run_id: str,
        phase: str,
        session_id: str,
        worktree: Path,
        prompt: str,
        read_only: bool,
    ) -> PiResult:
        tools = "read,grep,find,ls" if read_only else "read,bash,edit,write,grep,find,ls"
        statuses = PLAN_STATUSES if read_only else IMPLEMENT_STATUSES
        args, cwd, env = self._command(
            session_id=session_id, worktree=worktree, tools=tools, statuses=statuses
        )
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # One JSONL event can carry a whole file; the 64 KiB default would
            # make readline fail and leave Pi blocked on a full stdout pipe.
            limit=EVENT_LINE_LIMIT,
        )
        assert process.stdin and process.stdout and process.stderr
        process.stdin.write(prompt.encode("utf-8"))
        await process.stdin.drain()
        process.stdin.close()

        final_text = ""
        final_stop_reason = ""
        submitted: dict | None = None

        async def consume_stdout() -> None:
            nonlocal final_text, final_stop_reason, submitted
            while True:
                raw = await process.stdout.readline()
                if not raw:
                    break
                event = self._parse_event(raw)
                if event is None:
                    continue
                event_type = str(event.get("type", "unknown"))
                if self.event_sink:
                    await self.event_sink(run_id, phase, event)
                if (
                    event_type == "tool_execution_end"
                    and event.get("toolName") == RESULT_TOOL
                    and not event.get("isError")
                ):
                    details = (event.get("result") or {}).get("details")
                    if isinstance(details, dict):
                        submitted = details
                if event_type == "message_end":
                    message = event.get("message") or {}
                    text = _assistant_text(message)
                    if text:
                        final_text = text
                        final_stop_reason = str(message.get("stopReason") or "")

        stdout_task = asyncio.create_task(consume_stdout())
        stderr_task = asyncio.create_task(process.stderr.read())
        process_task = asyncio.create_task(process.wait())
        deadline = asyncio.get_running_loop().time() + self.settings.pi_timeout_seconds
        try:
            while not process_task.done():
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    process.kill()
                    await process.wait()
                    await self._after_kill(worktree)
                    await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
                    raise PiError(f"Pi timed out after {self.settings.pi_timeout_seconds} seconds")
                if stdout_task.done() and stdout_task.exception() is not None:
                    # Nobody reads stdout any more, so Pi would block forever.
                    process.kill()
                    await process.wait()
                    await self._after_kill(worktree)
                    stderr_task.cancel()
                    await asyncio.gather(stderr_task, return_exceptions=True)
                    raise PiError(f"Reading Pi output failed: {stdout_task.exception()}")
                if self.cancel_check and await self.cancel_check(run_id):
                    process.kill()
                    await process.wait()
                    await self._after_kill(worktree)
                    await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
                    raise PiStopped("Stop requested by an authorized user")
                try:
                    await asyncio.wait_for(asyncio.shield(process_task), timeout=min(1, remaining))
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
                await process.wait()
                await asyncio.shield(self._after_kill(worktree))
            stdout_task.cancel()
            stderr_task.cancel()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise
        await stdout_task
        stderr = (await stderr_task).decode("utf-8", errors="replace")
        if process.returncode != 0:
            raise PiError(f"Pi exited with code {process.returncode}: {stderr[-2000:]}")
        if final_stop_reason in {"error", "aborted"}:
            raise PiError(f"Pi stopped with {final_stop_reason}: {stderr[-2000:]}")
        if submitted is not None:
            try:
                result = PiResult.model_validate(submitted)
            except ValueError as exc:
                raise PiError(f"Invalid {RESULT_TOOL} payload: {exc}") from exc
        elif final_text:
            # Fallback for models that answer in text instead of calling the tool.
            result = _parse_result(final_text)
        else:
            raise PiError(f"Pi completed without calling {RESULT_TOOL}")
        if result.status not in statuses:
            raise PiError(f"Pi returned status {result.status!r} for phase {phase}")
        return result
