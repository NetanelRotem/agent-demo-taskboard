import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

import main

SECRET = "test-secret"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    return TestClient(main.app)


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def make_payload(**overrides) -> dict:
    payload = {
        "action": "created",
        "issue": {
            "number": 7,
            "title": "Add dark mode",
            "html_url": "https://github.com/acme/todo-app/issues/7",
        },
        "comment": {"body": "/agent please fix this", "user": {"login": "alice", "type": "User"}},
        "repository": {"full_name": "acme/todo-app"},
        "sender": {"login": "alice", "type": "User"},
    }
    payload.update(overrides)
    return payload


def post(client, payload, event="issue_comment", signature=None):
    body = json.dumps(payload).encode()
    return client.post(
        "/webhooks/github",
        content=body,
        headers={
            "X-GitHub-Event": event,
            "X-Hub-Signature-256": signature if signature is not None else sign(body),
            "Content-Type": "application/json",
        },
    )


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_valid_signature_command_is_accepted_and_printed(client, capsys):
    resp = post(client, make_payload())
    assert resp.status_code == 200
    assert resp.json() == {"status": "accepted"}
    out = capsys.readouterr().out
    for expected in ["acme/todo-app", "#7 Add dark mode", "alice", "/agent please fix this",
                     "https://github.com/acme/todo-app/issues/7"]:
        assert expected in out


def test_wrong_secret_rejected(client, capsys):
    body = json.dumps(make_payload()).encode()
    resp = post(client, make_payload(), signature=sign(body, "wrong-secret"))
    assert resp.status_code == 401
    assert capsys.readouterr().out == ""


def test_missing_signature_rejected(client):
    assert post(client, make_payload(), signature="").status_code == 401


def test_tampered_body_rejected(client):
    original = json.dumps(make_payload()).encode()
    tampered = make_payload(comment={"body": "/agent rm -rf", "user": {"login": "eve", "type": "User"}})
    assert post(client, tampered, signature=sign(original)).status_code == 401


def test_signature_uses_raw_body(client):
    # Same JSON, different whitespace: signature over these exact bytes must still pass.
    body = b'{ "action" :  "created", "issue": {"number": 1, "title": "t", "html_url": "u"},' \
           b' "comment": {"body": "/agent x", "user": {"login": "a", "type": "User"}},' \
           b' "repository": {"full_name": "r"}, "sender": {"login": "a", "type": "User"} }'
    resp = client.post("/webhooks/github", content=body,
                       headers={"X-GitHub-Event": "issue_comment", "X-Hub-Signature-256": sign(body)})
    assert resp.json() == {"status": "accepted"}


def test_missing_secret_env_returns_500(client, monkeypatch):
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET")
    assert post(client, make_payload()).status_code == 500


@pytest.mark.parametrize(
    "event,payload",
    [
        ("issues", make_payload()),
        ("ping", {"zen": "hi"}),
        ("issue_comment", make_payload(action="edited")),
        ("issue_comment", make_payload(action="deleted")),
        ("issue_comment", make_payload(issue={"number": 1, "title": "PR", "html_url": "u",
                                              "pull_request": {"url": "x"}})),
        ("issue_comment", make_payload(comment={"body": "/agent hi",
                                                "user": {"login": "ci[bot]", "type": "Bot"}},
                                       sender={"login": "ci[bot]", "type": "Bot"})),
        ("issue_comment", make_payload(comment={"body": "/agent hi",
                                                "user": {"login": "helper[bot]", "type": "User"}})),
        ("issue_comment", make_payload(comment={"body": "just a comment /agent",
                                                "user": {"login": "alice", "type": "User"}})),
        ("issue_comment", make_payload(comment={"body": "", "user": {"login": "alice", "type": "User"}})),
    ],
    ids=["issues-event", "ping", "edited", "deleted", "pull-request", "bot-type",
         "bot-login", "no-prefix", "empty-body"],
)
def test_ignored_events(client, capsys, event, payload):
    resp = post(client, payload, event=event)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ignored"}
    assert capsys.readouterr().out == ""
