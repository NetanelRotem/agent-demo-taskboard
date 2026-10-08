import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

import main

SECRET = "webhook-secret"


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class FakeService:
    def __init__(self):
        self.items = []
        self.deliveries = set()

    async def enqueue(self, item, delivery_id):
        if item.user != "alice":
            return "ignored" if item.command.name == "review" else "forbidden"
        if delivery_id in self.deliveries:
            return "duplicate"
        self.deliveries.add(delivery_id)
        self.items.append(item)
        return "accepted"


@pytest.fixture
def service():
    return FakeService()


@pytest.fixture
def client(service, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    app = main.create_app()
    app.state.agent_service = service
    return TestClient(app)


def make_payload(body="/agent start", user="alice", **overrides) -> dict:
    payload = {
        "action": "created",
        "issue": {"number": 7, "title": "Add dark mode", "html_url": "https://example/7"},
        "comment": {
            "id": 99,
            "body": body,
            "user": {"login": user, "type": "User"},
        },
        "repository": {"full_name": "acme/todo-app"},
        "sender": {"login": user, "type": "User"},
    }
    payload.update(overrides)
    return payload


def make_review_payload(state="changes_requested", user="alice") -> dict:
    return {
        "action": "submitted",
        "review": {
            "id": 501,
            "state": state,
            "body": "Please handle the inline comments",
            "user": {"login": user, "type": "User"},
        },
        "pull_request": {"number": 42},
        "repository": {"full_name": "acme/todo-app"},
        "sender": {"login": user, "type": "User"},
    }


def post(client, payload, event="issue_comment", signature=None, delivery="delivery-1"):
    body = json.dumps(payload).encode()
    if signature is None:
        signature = sign(body)
    return client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery,
            "X-Hub-Signature-256": signature,
            "Content-Type": "application/json",
        },
    )


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_signed_webhook_enqueues_command(client, service):
    response = post(client, make_payload())
    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    assert service.items[0].command.name == "start"


@pytest.mark.parametrize(
    "signature",
    ["", "sha256=wrong", "sha1=abc", sign(b"another body"), sign(b"x", "other-secret")],
)
def test_bad_signature_is_rejected(client, service, signature):
    response = post(client, make_payload(), signature=signature)
    assert response.status_code == 401
    assert service.items == []


def test_missing_secret_refuses_webhooks(client, service, monkeypatch):
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET")
    response = post(client, make_payload())
    assert response.status_code == 500
    assert service.items == []


def test_unauthorized_user_is_rejected(client):
    response = post(client, make_payload(user="mallory"))
    assert response.status_code == 403


def test_duplicate_delivery_is_not_enqueued_twice(client, service):
    assert post(client, make_payload()).json() == {"status": "accepted"}
    assert post(client, make_payload()).json() == {"status": "duplicate"}
    assert len(service.items) == 1


@pytest.mark.parametrize("state", ["changes_requested", "commented"])
def test_submitted_review_is_enqueued(client, service, state):
    response = post(
        client,
        make_review_payload(state),
        event="pull_request_review",
        delivery=f"review-{state}",
    )
    assert response.json() == {"status": "accepted"}
    item = service.items[-1]
    assert item.command.name == "review"
    assert item.pull_number == 42
    assert item.review_id == 501


def test_approved_review_is_ignored(client):
    response = post(
        client,
        make_review_payload("approved"),
        event="pull_request_review",
        delivery="review-approved",
    )
    assert response.json() == {"status": "ignored"}


def test_review_from_unauthorized_user_is_ignored(client):
    response = post(
        client,
        make_review_payload("changes_requested", user="mallory"),
        event="pull_request_review",
        delivery="review-unauthorized",
    )
    assert response.status_code == 202
    assert response.json() == {"status": "ignored"}


@pytest.mark.parametrize(
    "event,payload",
    [
        ("ping", {"zen": "hi"}),
        ("issue_comment", make_payload(action="edited")),
        ("issue_comment", make_payload(issue={"number": 1, "pull_request": {"url": "x"}})),
        (
            "issue_comment",
            make_payload(
                comment={"id": 2, "body": "/agent start", "user": {"login": "ci[bot]", "type": "Bot"}}
            ),
        ),
        ("issue_comment", make_payload(body="ordinary comment")),
    ],
)
def test_irrelevant_events_are_ignored(client, event, payload):
    response = post(client, payload, event=event, delivery=f"ignored-{event}-{id(payload)}")
    assert response.status_code == 202
    assert response.json() == {"status": "ignored"}


@pytest.mark.parametrize("merged,outcome", [(True, "merged"), (False, "closed")])
def test_closed_pull_request_enqueues_cleanup(client, service, merged, outcome):
    payload = {
        "action": "closed",
        "pull_request": {"number": 7, "id": 4242, "merged": merged},
        "repository": {"full_name": "acme/todo"},
        "sender": {"login": "alice", "type": "User"},
    }
    response = post(client, payload, event="pull_request", delivery=f"close-{outcome}")
    assert response.json() == {"status": "accepted"}
    item = service.items[-1]
    assert (item.command.name, item.command.text, item.pull_number) == ("cleanup", outcome, 7)


def test_other_pull_request_actions_are_ignored(client, service):
    payload = {
        "action": "opened",
        "pull_request": {"number": 7, "id": 4242},
        "repository": {"full_name": "acme/todo"},
        "sender": {"login": "alice", "type": "User"},
    }
    response = post(client, payload, event="pull_request", delivery="open-1")
    assert response.json() == {"status": "ignored"}
    assert service.items == []
