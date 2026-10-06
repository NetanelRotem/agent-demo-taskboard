from __future__ import annotations

from agent_models import AgentCommand


class CommandError(ValueError):
    pass


def parse_command(body: str) -> AgentCommand:
    line = body.strip().splitlines()[0].strip()
    parts = line.split(maxsplit=3)
    if len(parts) < 2 or parts[0] != "/agent":
        raise CommandError("Not an agent command")

    name = parts[1].lower()
    if name == "start":
        if len(parts) == 2:
            return AgentCommand(name="start")
        if len(parts) == 3 and parts[2].lower() == "auto":
            return AgentCommand(name="start", text="auto")
        raise CommandError("Usage: /agent start [auto]")

    if name == "stop":
        if len(parts) != 2:
            raise CommandError("Usage: /agent stop")
        return AgentCommand(name="stop")

    if name == "approve":
        if len(parts) != 3:
            raise CommandError("Usage: /agent approve <request-id>")
        return AgentCommand(name="approve", request_id=parts[2])

    if name in {"answer", "reject"}:
        if len(parts) != 4 or not parts[3].strip():
            raise CommandError(f"Usage: /agent {name} <request-id> <text>")
        return AgentCommand(name=name, request_id=parts[2], text=parts[3].strip())  # type: ignore[arg-type]

    raise CommandError(f"Unknown command: {name}")
