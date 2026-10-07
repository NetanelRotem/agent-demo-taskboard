from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import pytest_asyncio

from config import Settings
from events import Event, EventLog, TelegramNotifier, format_message
from storage import AgentStore


class FakeTelegram:
    """Records Telegram Bot API calls; behavior is configurable per test."""

    def __init__(self, forum: bool = True, rate_limit_once: bool = False):
        self.forum = forum
        self.rate_limit_once = rate_limit_once
        self.calls: list[tuple[str, dict]] = []
        self.next_thread = 100

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content)
        self.calls.append((method, payload))
        if method == "createForumTopic":
            if not self.forum:
                return httpx.Response(400, json={"ok": False, "description": "Bad Request: the chat is not a forum"})
            self.next_thread += 1
            return httpx.Response(200, json={"ok": True, "result": {"message_thread_id": self.next_thread}})
        if self.rate_limit_once:
            self.rate_limit_once = False
            return httpx.Response(
                429, json={"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 0}}
            )
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler), base_url="https://tg.test/botX/")

    def sent(self) -> list[dict]:
        return [payload for method, payload in self.calls if method == "sendMessage"]


@pytest_asyncio.fixture
async def store(tmp_path):
    store = AgentStore(tmp_path / "agent.sqlite3")
    await store.open()
    yield store
    await store.close()


async def drain(notifier: TelegramNotifier) -> None:
    await asyncio.wait_for(notifier.queue.join(), timeout=5)


def test_telegram_is_enabled_only_with_both_variables(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100123")
    assert Settings.from_env().telegram_enabled is False
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    assert Settings.from_env().telegram_enabled is True


@pytest.mark.asyncio
async def test_events_go_to_sqlite_and_one_topic_per_issue(store):
    fake = FakeTelegram()
    notifier = TelegramNotifier("X", "-100123", store, client=fake.client())
    notifier.start()
    events = EventLog(store, notifier)

    await events.emit("run_started", "Run started: Add priority", run_id="d83d7e04-aaaa",
                      repo="a/b", issue_number=8, data={"title": "Add priority"})
    await events.emit("plan_ready", "Plan v1", run_id="d83d7e04-aaaa", repo="a/b", issue_number=8)
    await events.emit("run_started", "Run started: Other", run_id="ffff0000-bbbb",
                      repo="a/b", issue_number=9, data={"title": "Other"})
    await drain(notifier)
    await notifier.close()

    topics = [payload for method, payload in fake.calls if method == "createForumTopic"]
    assert [t["name"] for t in topics] == ["#8 Add priority", "#9 Other"]
    threads = [payload["message_thread_id"] for payload in fake.sent()]
    assert threads == [101, 101, 102]
    assert await store.get_telegram_topic("a/b", 8) == 101

    cursor = await store.conn.execute("SELECT kind, run_id, issue_number FROM events ORDER BY id")
    rows = [tuple(row) for row in await cursor.fetchall()]
    assert rows[0] == ("run_started", "d83d7e04-aaaa", 8)
    assert len(rows) == 3


@pytest.mark.asyncio
async def test_without_forum_topics_messages_go_to_the_main_chat(store):
    fake = FakeTelegram(forum=False)
    notifier = TelegramNotifier("X", "-100123", store, client=fake.client())
    notifier.start()
    events = EventLog(store, notifier)

    await events.emit("run_started", "one", run_id="r1", repo="a/b", issue_number=8)
    await events.emit("plan_ready", "two", run_id="r1", repo="a/b", issue_number=8)
    await drain(notifier)
    await notifier.close()

    assert [m for m, _ in fake.calls].count("createForumTopic") == 1
    assert all("message_thread_id" not in payload for payload in fake.sent())
    assert len(fake.sent()) == 2


@pytest.mark.asyncio
async def test_rate_limit_is_retried_and_failures_never_raise(store):
    fake = FakeTelegram(rate_limit_once=True)
    notifier = TelegramNotifier("X", "-100123", store, client=fake.client())
    notifier.start()
    events = EventLog(store, notifier)

    await events.emit("sbx_retry", "retrying", repo="a/b", issue_number=8)
    await drain(notifier)
    assert len(fake.sent()) == 2  # 429, then success

    def broken(request):
        raise httpx.ConnectError("telegram down")

    notifier.client = httpx.AsyncClient(transport=httpx.MockTransport(broken), base_url="https://tg.test/")
    await events.emit("run_finished", "still recorded", repo="a/b", issue_number=8)
    await drain(notifier)
    await notifier.close()

    cursor = await store.conn.execute("SELECT kind FROM events ORDER BY id")
    assert [row[0] for row in await cursor.fetchall()] == ["sbx_retry", "run_finished"]


@pytest.mark.asyncio
async def test_sandbox_events_resolve_their_run_and_issue(store):
    await store.create_run(
        {"run_id": "d83d7e04-6423", "thread_id": "t", "repo": "a/b", "issue_number": 8, "status": "planning"}
    )
    events = EventLog(store)
    await events.emit("sandbox_stopped", "stopped", sandbox="agent-issue-8-d83d7e04")

    cursor = await store.conn.execute("SELECT run_id, repo, issue_number FROM events")
    assert tuple(await cursor.fetchone()) == ("d83d7e04-6423", "a/b", 8)


@pytest.mark.asyncio
async def test_emit_redacts_and_survives_a_closed_store(tmp_path):
    store = AgentStore(tmp_path / "agent.sqlite3")
    events = EventLog(store, redact=lambda value: value.replace("sk-secret", "[REDACTED]") if isinstance(value, str) else value)
    await events.emit("run_finished", "failed with sk-secret")  # store never opened: must not raise

    await store.open()
    try:
        await events.emit("run_finished", "failed with sk-secret")
        cursor = await store.conn.execute("SELECT message FROM events")
        assert (await cursor.fetchone())[0] == "failed with [REDACTED]"
    finally:
        await store.close()


def test_message_format_has_icon_run_and_issue_link():
    text = format_message(
        Event("pr_opened", "Opened PR #9", run_id="d83d7e04-6423", repo="a/b", issue_number=8)
    )
    assert text.splitlines()[0] == "🔀 Opened PR #9"
    assert "run d83d7e04" in text and "https://github.com/a/b/issues/8" in text
    assert len(format_message(Event("run_finished", "x" * 10_000))) < 4096
