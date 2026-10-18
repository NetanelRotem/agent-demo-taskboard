import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import dashboard
import main
from github_client import comment_record
from storage import AgentStore

RUN = "abcdef12-0000-0000-0000-000000000000"


def message_end(cost, text="", tokens=100):
    return {
        "type": "message_end",
        "message": {
            "role": "assistant",
            "model": "test-model",
            "content": [{"type": "text", "text": text}],
            "usage": {"input": tokens, "output": 10, "cacheRead": 0, "cost": {"total": cost}},
        },
    }


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "agent.sqlite3"

    async def seed():
        store = AgentStore(path)
        await store.open()
        await store.create_run({
            "run_id": RUN, "thread_id": RUN, "repo": "acme/app", "issue_number": 7, "status": "planning",
        })
        await store.set_run_title(RUN, "Add dark mode")
        await store.persist_work_item("d1", {
            "command": {"name": "start", "request_id": None, "text": ""},
            "repo": "acme/app", "issue_number": 7, "comment_id": 11, "user": "alice",
            "pull_number": None, "review_id": None,
        })
        await store.record_comment(comment_record("issue", "acme/app", 7, {
            "id": 11, "body": "/agent start", "user": {"login": "alice", "type": "User"},
        }))
        await store.record_comment(comment_record("issue", "acme/app", 7, {
            "id": 12, "body": "<!-- coding-agent-graph -->\nWorking on it", "user": {"login": "alice"},
        }))
        await store.record_pi_event(RUN, "plan", "agent_start", {"type": "agent_start"})
        await store.record_pi_event(RUN, "plan", "tool_execution_start",
                                    {"toolName": "bash", "toolCallId": "t1", "args": {"command": "ls"}})
        await store.record_pi_event(RUN, "plan", "tool_execution_end",
                                    {"toolName": "bash", "toolCallId": "t1", "isError": False,
                                     "result": {"content": [{"type": "text", "text": "src"}]}})
        await store.record_pi_event(RUN, "plan", "message_end", message_end(0.25, "Plan ready"))
        await store.record_pi_event(RUN, "plan", "message_end", message_end(0.5))
        await store.record_event("plan_ready", "Plan v1 ready", run_id=RUN, repo="acme/app", issue_number=7)
        await store.close()

    asyncio.run(seed())
    return path


def test_runs_include_cost_title_and_comments(db_path):
    [run] = dashboard.DashboardDB(db_path).runs()
    assert run["title"] == "Add dark mode"
    assert run["cost"] == pytest.approx(0.75)
    assert run["comments"] == 2
    assert run["stage"] == "plan" and not run["terminal"]


def test_timeline_merges_sources_without_duplicates(db_path):
    timeline = dashboard.DashboardDB(db_path).timeline(RUN[:8])
    types = [item["type"] for item in timeline["items"]]
    # The /agent start comment is folded into its command; the agent comment stays.
    assert types.count("command") == 1 and types.count("comment") == 1
    command = next(item for item in timeline["items"] if item["type"] == "command")
    assert command["actor"] == "alice" and command["body"] == "/agent start"
    comment = next(item for item in timeline["items"] if item["type"] == "comment")
    assert comment["from_agent"] and comment["body"] == "Working on it"
    [segment] = [item for item in timeline["items"] if item["type"] == "pi"]
    assert segment["cost"] == pytest.approx(0.75)
    assert segment["tools"] == 1 and segment["summary"] == "Plan ready" and segment["running"]
    assert timeline["costs"]["phases"]["plan"]["cost"] == pytest.approx(0.75)
    states = {stage["key"]: stage["state"] for stage in timeline["stages"]}
    assert states["trigger"] == "done" and states["plan"] == "active" and states["pr"] == "pending"


def test_pi_steps_pair_tool_start_and_end(db_path):
    steps = dashboard.DashboardDB(db_path).pi_steps(RUN[:8], 0, 10**9)
    tool = next(step for step in steps if step["type"] == "tool")
    assert tool["tool"] == "bash" and tool["ok"] is True and tool["result"] == "src"
    assert [step["text"] for step in steps if step["type"] == "message"] == ["Plan ready", ""]


def test_api_and_page(db_path):
    app = FastAPI()
    app.include_router(dashboard.router(lambda: db_path))
    client = TestClient(app)
    assert client.get("/dashboard").status_code == 200
    assert client.get("/api/runs").json()[0]["short_id"] == RUN[:8]
    assert client.get(f"/api/runs/{RUN[:8]}").json()["run"]["title"] == "Add dark mode"
    assert client.get("/api/runs/ffffffff").status_code == 404


def test_missing_database_is_empty(tmp_path):
    db = dashboard.DashboardDB(tmp_path / "none.sqlite3")
    assert db.runs() == [] and db.timeline("x") is None and db.version() == "none"


def test_webhook_review_comment_is_extracted():
    payload = {
        "action": "created",
        "pull_request": {"number": 9},
        "repository": {"full_name": "acme/app"},
        "comment": {"id": 5, "body": "nit", "path": "a.ts", "user": {"login": "bob"},
                    "created_at": "2026-01-01T00:00:00Z", "html_url": "u"},
    }
    record = main.extract_comment("pull_request_review_comment", payload)
    assert record["source"] == "review_comment" and record["number"] == 9
    assert record["path"] == "a.ts" and not record["from_agent"]
    assert main.extract_comment("push", payload) is None


def test_each_pr_review_is_its_own_round():
    def item(type_, **extra):
        return {"type": type_, "stage": "review", "at": "", **extra}

    items = [
        item("comment", source="review_comment"),
        item("command", title="Review submitted"),
        item("event", kind="review_started"),
        item("pi"),
        item("event", kind="review_pushed"),
        item("comment", source="review"),
        item("command", title="Review submitted"),
        item("event", kind="review_started"),
        item("pi"),
    ]
    dashboard._number_review_rounds(items)
    assert [i["round"] for i in items] == [1, 1, 1, 1, 1, 2, 2, 2, 2]
