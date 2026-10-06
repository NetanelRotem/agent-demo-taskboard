# coding-agent-graph

Minimal FastAPI server that receives GitHub webhooks and prints `/agent` commands from issue comments.
At this stage it does **not** call a model, change code or post comments.

## Endpoints

| Method | Path               | Description                                   |
|--------|--------------------|-----------------------------------------------|
| GET    | `/health`          | Health check → `{"status": "ok"}`             |
| POST   | `/webhooks/github` | GitHub webhook receiver                       |

`/webhooks/github` behavior:

1. Verifies `X-Hub-Signature-256` (HMAC-SHA256 of the raw body with `GITHUB_WEBHOOK_SECRET`). Invalid → `401`, secret not set → `500`.
2. Handles only `issue_comment` events with `action=created` on an Issue (not a PR).
3. Ignores comments from bots and comments that don't start with `/agent`. → `{"status": "ignored"}`
4. For a command: returns `{"status": "accepted"}` immediately and prints the repo, issue number and title, user, comment and issue URL to the terminal.

## Run

```bash
cd coding-agent-graph
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env        # then set GITHUB_WEBHOOK_SECRET in .env
uvicorn main:app --host 0.0.0.0 --port 8000
```

`.env` is loaded automatically and is ignored by Git — never commit it.

## Connect GitHub

To expose the local server, use a tunnel (e.g. `ngrok http 8000` or `smee.io`). Then, in the repo, go to
**Settings → Webhooks → Add webhook**:

- Payload URL: `https://<tunnel-host>/webhooks/github`
- Content type: `application/json`
- Secret: same value as `GITHUB_WEBHOOK_SECRET`
- Events: *Let me select individual events* → **Issue comments**

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```
