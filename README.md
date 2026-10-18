# Agent Demo Taskboard

A coding agent that takes a task from a GitHub Issue, plans it, writes the code in a cloud sandbox, runs the checks and opens a Pull Request. The app it works on in the demo is a Todo app.

| Folder | What's in it |
|---|---|
| [coding-agent-graph](coding-agent-graph/README.md) | The agent: a FastAPI server, a LangGraph workflow, Pi running in Docker Cloud Sandboxes, and a live dashboard at `/dashboard` |
| [todo-app](todo-app/README.md) | A Todo app in React, TypeScript and Vite, published to GitHub Pages on every merge to `main` |
| [docs](docs/README.md) | How the agent works, from the overview down to the details, with diagrams ready for slides |

**Getting started:** installation and setup are in [coding-agent-graph/README.md](coding-agent-graph/README.md#installation), and daily use is at the top of the same file ([Daily use](coding-agent-graph/README.md#daily-use-quick-start)).
