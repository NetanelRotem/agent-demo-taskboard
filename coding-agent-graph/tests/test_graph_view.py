from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import graph_view
import main

NODES = (
    "load_issue", "plan_with_pi", "human_input", "implement_with_pi", "review",
    "implement_review", "verify", "finish", "publish_pr", "push_review",
)


def test_static_diagram_contains_every_node():
    diagram = graph_view.mermaid()
    for node in NODES:
        assert f"\t{node}(" in diagram
    assert "class " not in diagram.replace("classDef", "")


def test_progress_is_highlighted():
    diagram = graph_view.mermaid(
        visited={"__start__", "load_issue", "plan_with_pi", "human_input"},
        current={"human_input"},
    )
    assert "\tclass load_issue visited\n" in diagram
    assert "\tclass plan_with_pi visited\n" in diagram
    assert "\tclass human_input current\n" in diagram
    assert "class human_input visited" not in diagram
    assert "class __start__" not in diagram


class FakeGraph:
    def __init__(self, history):
        self.history = history

    async def aget_state(self, config):
        return self.history[0]

    async def aget_state_history(self, config):
        for snapshot in self.history:
            yield snapshot


@pytest.mark.asyncio
async def test_run_progress_collects_visited_and_current():
    history = [
        SimpleNamespace(next=("human_input",)),
        SimpleNamespace(next=("plan_with_pi",)),
        SimpleNamespace(next=("load_issue",)),
        SimpleNamespace(next=("__start__",)),
    ]
    visited, current = await graph_view.run_progress(FakeGraph(history), "thread")
    assert current == {"human_input"}
    assert visited == {"__start__", "load_issue", "plan_with_pi", "human_input"}


class FakeStore:
    def __init__(self, run):
        self.run = run

    async def recent_runs(self):
        return [self.run]

    async def find_run(self, prefix):
        return self.run if self.run["run_id"].startswith(prefix) else None


@pytest.fixture
def client():
    run = {
        "run_id": "abcd1234-0000", "thread_id": "t", "repo": "acme/todo",
        "issue_number": 7, "status": "waiting_for_human", "pr_url": None,
    }
    service = SimpleNamespace(
        store=FakeStore(run),
        agent_graph=SimpleNamespace(graph=graph_view.static_graph()),
    )
    app = main.create_app()
    app.state.agent_service = service
    return TestClient(app)


def test_graph_page_lists_recent_runs(client):
    response = client.get("/graph")
    assert response.status_code == 200
    assert "plan_with_pi" in response.text
    assert "?run_id=abcd1234" in response.text


def test_unknown_run_is_404(client):
    assert client.get("/graph?run_id=ffffffff").status_code == 404
