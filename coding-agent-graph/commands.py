from __future__ import annotations

import re

from agent_models import AgentCommand

# Matches the ids human_input generates: f"{run_id[:8]}-p{version}-{'q'|'a'}".
REQUEST_ID = re.compile(r"^[0-9a-f]{8}-p\d+-[qa]$")


class CommandError(ValueError):
    pass


def parse_command(body: str) -> AgentCommand:
    line = body.strip().splitlines()[0].strip()
    parts = line.split(maxsplit=2)
    if len(parts) < 2 or parts[0] != "/agent":
        raise CommandError("Not an agent command")

    name = parts[1].lower()
    rest = parts[2].strip() if len(parts) == 3 else ""
    if name == "start":
        if not rest:
            return AgentCommand(name="start")
        if rest.lower() == "auto":
            return AgentCommand(name="start", text="auto")
        raise CommandError("Usage: /agent start [auto]")

    if name == "stop":
        if rest:
            raise CommandError("Usage: /agent stop")
        return AgentCommand(name="stop")

    # The request id is optional: without one the command targets the issue's
    # pending request (an issue has at most one active run and one pending request).
    request_id, text = _split_request_id(rest)

    if name == "approve":
        if text:
            raise CommandError("Usage: /agent approve [request-id]")
        return AgentCommand(name="approve", request_id=request_id)

    if name in {"answer", "reject"}:
        if not text:
            label = "answer" if name == "answer" else "reason"
            raise CommandError(f"Usage: /agent {name} [request-id] <{label}>")
        return AgentCommand(name=name, request_id=request_id, text=text)  # type: ignore[arg-type]

    raise CommandError(f"Unknown command: {name}")


def _split_request_id(rest: str) -> tuple[str | None, str]:
    tokens = rest.split(maxsplit=1)
    if tokens and REQUEST_ID.match(tokens[0]):
        return tokens[0], tokens[1].strip() if len(tokens) == 2 else ""
    return None, rest
