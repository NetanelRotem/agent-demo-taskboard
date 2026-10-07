"""FastAPI webhook ingress for the issue-driven coding agent."""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request, status

load_dotenv()

from agent_models import AgentCommand
from commands import CommandError, parse_command
from config import Settings
from service import AgentService, WorkItem

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

COMMAND_PREFIX = "/agent"


def is_bot(payload: dict) -> bool:
    user = payload.get("comment", {}).get("user") or {}
    sender = payload.get("sender") or {}
    return any(
        account.get("type") == "Bot" or account.get("login", "").endswith("[bot]")
        for account in (user, sender)
    )


def extract_command(event: str | None, payload: dict) -> dict | None:
    if is_bot(payload):
        return None
    if event == "issue_comment" and payload.get("action") == "created":
        issue = payload.get("issue") or {}
        if "pull_request" in issue:
            return None
        comment = payload.get("comment") or {}
        body = comment.get("body") or ""
        if not body.lstrip().startswith(COMMAND_PREFIX):
            return None
        return {
            "repo": (payload.get("repository") or {}).get("full_name"),
            "issue_number": issue.get("number"),
            "user": (comment.get("user") or {}).get("login"),
            "comment_id": comment.get("id"),
            "body": body,
        }
    if event == "pull_request_review" and payload.get("action") == "submitted":
        review = payload.get("review") or {}
        review_state = str(review.get("state") or "").lower()
        if review_state not in {"changes_requested", "commented"}:
            return None
        pull_request = payload.get("pull_request") or {}
        return {
            "repo": (payload.get("repository") or {}).get("full_name"),
            "issue_number": pull_request.get("number"),
            "pull_number": pull_request.get("number"),
            "review_id": review.get("id"),
            "user": (review.get("user") or {}).get("login"),
            "comment_id": review.get("id"),
            "command": AgentCommand("review", text=str(review.get("body") or "")),
        }
    if event == "pull_request" and payload.get("action") == "closed":
        pull_request = payload.get("pull_request") or {}
        return {
            "repo": (payload.get("repository") or {}).get("full_name"),
            "issue_number": pull_request.get("number"),
            "pull_number": pull_request.get("number"),
            "user": (payload.get("sender") or {}).get("login"),
            "comment_id": pull_request.get("id"),
            "command": AgentCommand(
                "cleanup", text="merged" if pull_request.get("merged") else "closed"
            ),
        }
    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_env()
    service = AgentService(settings)
    await service.open()
    app.state.agent_service = service
    try:
        yield
    finally:
        await service.close()


def create_app() -> FastAPI:
    app = FastAPI(title="coding-agent-graph", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.post("/webhooks/github", status_code=status.HTTP_202_ACCEPTED)
    async def github_webhook(
        request: Request,
        x_github_event: str | None = Header(default=None),
        x_github_delivery: str | None = Header(default=None),
    ) -> dict:
        body = await request.body()

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON") from exc

        details = extract_command(x_github_event, payload)
        if details is None:
            return {"status": "ignored"}
        if not all((details["repo"], details["issue_number"], details["user"], details["comment_id"])):
            raise HTTPException(status_code=400, detail="Incomplete GitHub event")
        command = details.get("command")
        if command is None:
            try:
                command = parse_command(details["body"])
            except CommandError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

        delivery_id = x_github_delivery or f"comment-{details['comment_id']}"
        result = await request.app.state.agent_service.enqueue(
            WorkItem(
                command=command,
                repo=details["repo"],
                issue_number=int(details["issue_number"]),
                comment_id=int(details["comment_id"]),
                user=details["user"],
                pull_number=(
                    int(details["pull_number"]) if details.get("pull_number") else None
                ),
                review_id=int(details["review_id"]) if details.get("review_id") else None,
            ),
            delivery_id,
        )
        if result == "forbidden":
            raise HTTPException(status_code=403, detail="User is not authorized")
        if result == "busy":
            raise HTTPException(status_code=503, detail="Agent queue is full")
        return {"status": result}

    return app


app = create_app()
