from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


def _csv(name: str) -> frozenset[str]:
    return frozenset(value.strip() for value in os.getenv(name, "").split(",") if value.strip())


def _github_token() -> str:
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if token:
        return token
    gh = shutil.which("gh")
    if not gh:
        return ""
    try:
        result = subprocess.run(
            [gh, "auth", "token"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


@dataclass(frozen=True)
class Settings:
    github_token: str
    authorized_users: frozenset[str]
    repository_path: Path
    todo_path: str
    main_branch: str
    data_dir: Path
    pi_command: str
    openrouter_model: str
    pi_timeout_seconds: int
    max_attempts: int
    queue_size: int
    langfuse_enabled: bool = False
    require_plan_approval: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        project_root = Path(__file__).resolve().parent.parent
        data_dir_value = os.getenv("AGENT_DATA_DIR") or str(
            Path(__file__).resolve().parent / ".agent-data"
        )
        repository_value = os.getenv("AGENT_REPOSITORY_PATH") or str(project_root)
        data_dir = Path(data_dir_value)
        return cls(
            github_token=_github_token(),
            authorized_users=_csv("AUTHORIZED_GITHUB_USERS"),
            repository_path=Path(repository_value).resolve(),
            todo_path=os.getenv("TODO_APP_PATH", "todo-app"),
            main_branch=os.getenv("AGENT_MAIN_BRANCH", "main"),
            data_dir=data_dir.resolve(),
            pi_command=os.getenv("PI_COMMAND", "pi"),
            openrouter_model=os.getenv("OPENROUTER_MODEL", "z-ai/glm-5.3"),
            pi_timeout_seconds=int(os.getenv("PI_TIMEOUT_SECONDS", "900")),
            max_attempts=int(os.getenv("AGENT_MAX_ATTEMPTS", "3")),
            queue_size=int(os.getenv("AGENT_QUEUE_SIZE", "100")),
            langfuse_enabled=bool(
                os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")
            )
            and os.getenv("LANGFUSE_TRACING_ENABLED", "true").lower()
            not in {"0", "false", "no", "off"},
            require_plan_approval=os.getenv("REQUIRE_PLAN_APPROVAL", "true").lower()
            not in {"0", "false", "no", "off"},
        )

    @property
    def database_path(self) -> Path:
        return self.data_dir / "agent.sqlite3"

    @property
    def worktrees_dir(self) -> Path:
        return self.data_dir / "worktrees"

    @property
    def pi_sessions_dir(self) -> Path:
        return self.data_dir / "pi-sessions"
