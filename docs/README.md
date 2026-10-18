# Documentation: how the agent works

A coding agent that takes a task from a GitHub Issue, plans it, waits for approval when the change is complex, writes the code in an isolated cloud environment, runs the checks and opens a Pull Request. Whoever writes the Issue never has to leave GitHub (or Telegram).

The documentation is layered, from the general to the detailed. Each file stands on its own, and every diagram in it can serve as a slide.

| # | Layer | The question it answers | Audience |
|---|-------|--------------------------|----------|
| 1 | [Overview](01-overview.md) | What is this, and why is it interesting? | Everyone |
| 2 | [User journey](02-user-journey.md) | What do I do in GitHub, and what do I get? | Users, managers |
| 3 | [Architecture](03-architecture.md) | What is it made of, and how do the parts talk? | Developers |
| 4 | [State graph](04-state-graph.md) | How does a run move forward, and where does it stop? | Developers |
| 5 | [End-to-end run](05-run-sequence.md) | What actually happens, second by second? | Developers |
| 6 | [Sandbox and security](06-sandbox-security.md) | Why is it safe to let the agent write code? | Developers, security |
| 7 | [Reliability](07-reliability.md) | What happens when something fails? | Developers, operations |
| 8 | [Observability](08-observability.md) | How do you know what the agent is doing right now, and what it cost? | Everyone |
| — | [langgraph.md](langgraph.md) | The graph as LangGraph draws it (generated) | Developers |

## Suggested presentation order

1. **The problem and the idea:** the opening slide from [01](01-overview.md).
2. **The demo:** the user journey from [02](02-user-journey.md), run live.
3. **Under the hood:** the architecture from [03](03-architecture.md) and the state graph from [04](04-state-graph.md).
4. **Why you can trust it:** isolation from [06](06-sandbox-security.md) and reliability from [07](07-reliability.md).
5. **Wrap-up:** the live dashboard, Telegram and Langfuse from [08](08-observability.md), and what comes next.

## About the diagrams

The diagrams are written in [Mermaid](https://mermaid.js.org) and render directly on GitHub and in VS Code. The PNG versions used as slides are rendered from the same sources with the Mermaid CLI.
