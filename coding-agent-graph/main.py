"""Minimal FastAPI server that receives GitHub webhooks.

Stage 1: verify the signature, filter for `/agent` commands on issue comments,
and print the command to the terminal. No model calls, no code changes, no replies.
"""

import hashlib
import hmac
import json
import os
import sys

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

load_dotenv()

# Print non-ASCII comment text (e.g. Hebrew) correctly even when stdout is redirected on Windows.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

COMMAND_PREFIX = "/agent"

app = FastAPI(title="coding-agent-graph")


def verify_signature(secret: str, body: bytes, signature_header: str | None) -> bool:
    """Check X-Hub-Signature-256 against an HMAC-SHA256 of the raw request body."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def is_bot(payload: dict) -> bool:
    user = payload.get("comment", {}).get("user") or {}
    sender = payload.get("sender") or {}
    for account in (user, sender):
        if account.get("type") == "Bot" or account.get("login", "").endswith("[bot]"):
            return True
    return False


def extract_command(event: str | None, payload: dict) -> dict | None:
    """Return command details if this event is an `/agent` comment on an issue, else None."""
    if event != "issue_comment" or payload.get("action") != "created":
        return None
    issue = payload.get("issue") or {}
    if "pull_request" in issue:  # PR comments also arrive as issue_comment
        return None
    if is_bot(payload):
        return None
    body = (payload.get("comment") or {}).get("body") or ""
    if not body.lstrip().startswith(COMMAND_PREFIX):
        return None
    return {
        "repo": (payload.get("repository") or {}).get("full_name"),
        "issue_number": issue.get("number"),
        "issue_title": issue.get("title"),
        "user": (payload["comment"].get("user") or {}).get("login"),
        "comment": body,
        "issue_url": issue.get("html_url"),
    }


def print_command(command: dict) -> None:
    print(
        "\n=== /agent command received ===\n"
        f"Repo:    {command['repo']}\n"
        f"Issue:   #{command['issue_number']} {command['issue_title']}\n"
        f"User:    {command['user']}\n"
        f"Comment: {command['comment']}\n"
        f"URL:     {command['issue_url']}\n"
        "===============================",
        flush=True,
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/webhooks/github")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_github_event: str | None = Header(default=None),
    x_hub_signature_256: str | None = Header(default=None),
) -> dict:
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET")
    if not secret:
        raise HTTPException(status_code=500, detail="GITHUB_WEBHOOK_SECRET is not configured")

    body = await request.body()  # raw bytes, exactly as signed by GitHub
    if not verify_signature(secret, body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    command = extract_command(x_github_event, payload)
    if command is None:
        return {"status": "ignored"}

    # Respond right away; handling runs after the response is sent.
    background_tasks.add_task(print_command, command)
    return {"status": "accepted"}
