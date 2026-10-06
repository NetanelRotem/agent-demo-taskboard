from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from config import Settings


class WorkspaceError(RuntimeError):
    pass


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


def _command_environment(env: dict[str, str] | None) -> dict[str, str]:
    command_env = dict(os.environ if env is None else env)
    path_value = next(
        (value for key, value in command_env.items() if key.upper() == "PATH"), ""
    )
    for key in [key for key in command_env if key.upper() == "PATH"]:
        del command_env[key]
    command_env["PATH"] = _deduplicate_path(path_value)
    return command_env


@dataclass
class VerificationResult:
    passed: bool
    summary: str
    changed_files: list[str] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)
    revision: str = ""

    def as_dict(self) -> dict:
        return {
            "passed": self.passed,
            "summary": self.summary,
            "changed_files": self.changed_files,
            "checks": self.checks,
            "revision": self.revision,
        }


async def _run(
    args: list[str],
    cwd: Path,
    timeout: int = 300,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    executable = shutil.which(args[0]) or args[0]
    process = await asyncio.create_subprocess_exec(
        executable,
        *args[1:],
        cwd=str(cwd),
        env=_command_environment(env),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise WorkspaceError(f"Command timed out: {' '.join(args)}")
    out = stdout.decode("utf-8", errors="replace")
    err = stderr.decode("utf-8", errors="replace")
    if check and process.returncode != 0:
        raise WorkspaceError(f"Command failed ({process.returncode}): {' '.join(args)}\n{err or out}")
    return process.returncode or 0, out, err


class WorkspaceManager:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _git_environment(self) -> dict[str, str] | None:
        if not self.settings.github_token:
            return None
        credentials = base64.b64encode(
            f"x-access-token:{self.settings.github_token}".encode("utf-8")
        ).decode("ascii")
        env = dict(os.environ)
        env.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {credentials}",
            }
        )
        return env

    async def prepare(self, run_id: str, issue_number: int) -> tuple[str, Path]:
        branch = f"agent/issue-{issue_number}-{run_id[:8]}"
        worktree = (self.settings.worktrees_dir / f"issue-{issue_number}-{run_id[:8]}").resolve()
        worktree.parent.mkdir(parents=True, exist_ok=True)
        if (worktree / ".git").exists():
            return branch, worktree

        await _run(
            ["git", "fetch", "origin", self.settings.main_branch],
            self.settings.repository_path,
            timeout=300,
            check=False,
            env=self._git_environment(),
        )

        base_ref = f"origin/{self.settings.main_branch}"
        code, _, _ = await _run(
            ["git", "rev-parse", "--verify", base_ref],
            self.settings.repository_path,
            check=False,
        )
        if code != 0:
            base_ref = self.settings.main_branch

        branch_exists, _, _ = await _run(
            ["git", "show-ref", "--verify", f"refs/heads/{branch}"],
            self.settings.repository_path,
            check=False,
        )
        args = ["git", "worktree", "add"]
        if branch_exists != 0:
            args += ["-b", branch]
        args += [str(worktree), branch if branch_exists == 0 else base_ref]
        await _run(args, self.settings.repository_path)
        return branch, worktree

    async def changed_files(self, worktree: Path) -> list[str]:
        base = f"origin/{self.settings.main_branch}"
        _, committed, _ = await _run(
            ["git", "diff", "--name-only", f"{base}...HEAD"], worktree, check=False
        )
        _, local, _ = await _run(["git", "diff", "--name-only", "HEAD"], worktree)
        _, untracked, _ = await _run(
            ["git", "ls-files", "--others", "--exclude-standard"], worktree
        )
        return sorted({line.strip().replace("\\", "/") for line in (committed + local + untracked).splitlines() if line.strip()})

    async def revision(self, worktree: Path) -> str:
        files = await self.changed_files(worktree)
        digest = hashlib.sha256()
        _, head, _ = await _run(["git", "rev-parse", "HEAD"], worktree)
        digest.update(head.strip().encode())
        for name in files:
            digest.update(name.encode("utf-8"))
            path = worktree / name
            if path.is_file():
                digest.update(path.read_bytes())
        return digest.hexdigest()

    async def verify(self, worktree: Path) -> VerificationResult:
        changed = await self.changed_files(worktree)
        if not changed:
            return VerificationResult(False, "Pi did not produce any code changes.")

        allowed_prefix = self.settings.todo_path.rstrip("/") + "/"
        forbidden = [name for name in changed if not name.startswith(allowed_prefix)]
        if forbidden:
            return VerificationResult(
                False,
                "Changes escaped the Todo application boundary: " + ", ".join(forbidden),
                changed_files=changed,
            )

        todo_dir = worktree / self.settings.todo_path
        checks: list[dict] = []
        for command in (["npm", "run", "lint"], ["npm", "run", "build"]):
            code, out, err = await _run(command, todo_dir, timeout=300, check=False)
            checks.append(
                {
                    "command": " ".join(command),
                    "passed": code == 0,
                    "output": (out + err)[-6000:],
                }
            )
            if code != 0:
                return VerificationResult(
                    False,
                    f"Verification failed: {' '.join(command)}",
                    changed_files=changed,
                    checks=checks,
                )

        revision = await self.revision(worktree)
        return VerificationResult(
            True,
            "All repository checks passed.",
            changed_files=changed,
            checks=checks,
            revision=revision,
        )

    async def commit_and_push(self, worktree: Path, branch: str, message: str) -> str:
        await _run(["git", "add", "--", self.settings.todo_path], worktree)
        code, _, _ = await _run(["git", "diff", "--cached", "--quiet"], worktree, check=False)
        if code != 0:
            await _run(["git", "commit", "-m", message], worktree)
        await _run(
            ["git", "push", "-u", "origin", branch],
            worktree,
            timeout=300,
            env=self._git_environment(),
        )
        _, sha, _ = await _run(["git", "rev-parse", "HEAD"], worktree)
        return sha.strip()
