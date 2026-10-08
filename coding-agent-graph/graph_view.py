"""Draw the LangGraph workflow, optionally with one run's progress on it.

    python graph_view.py                   # print the Mermaid diagram
    python graph_view.py --write ../docs/langgraph.md

The server also serves it at GET /graph (and /graph?run_id=<id> for a live run).
"""

from __future__ import annotations

import argparse
import html
from pathlib import Path
from typing import Any

from agent_graph import CodingAgentGraph

INTERNAL_NODES = {"__start__", "__end__"}
STYLES = (
    "\tclassDef visited fill:#d3f9d8,stroke:#2b8a3e\n"
    "\tclassDef current fill:#ffd43b,stroke:#e67700,stroke-width:3px\n"
)


def static_graph() -> Any:
    """The compiled graph only needs its node methods to be drawn, not their dependencies."""
    return CodingAgentGraph(None, None, None, None, None, None).graph  # type: ignore[arg-type]


def mermaid(graph: Any | None = None, visited: set[str] = frozenset(), current: set[str] = frozenset()) -> str:
    text = (graph or static_graph()).get_graph().draw_mermaid()
    lines = [STYLES]
    for name in sorted(visited - current - INTERNAL_NODES):
        lines.append(f"\tclass {name} visited\n")
    for name in sorted(current - INTERNAL_NODES):
        lines.append(f"\tclass {name} current\n")
    return text.rstrip("\n") + "\n" + "".join(lines)


async def run_progress(graph: Any, thread_id: str) -> tuple[set[str], set[str]]:
    """Nodes the run already executed, and the node(s) it will run next (or waits in)."""
    config = {"configurable": {"thread_id": thread_id}}
    current = set((await graph.aget_state(config)).next)
    visited: set[str] = set()
    async for snapshot in graph.aget_state_history(config):
        visited.update(snapshot.next)
    return visited, current


def page(diagram: str, title: str, details: str = "", refresh: bool = False) -> str:
    meta = '<meta http-equiv="refresh" content="5">' if refresh else ""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{meta}
<title>Agent Graph</title>
<style>
  :root {{ --bg: #ffffff; --fg: #1f2328; --muted: #656d76; --border: #d0d7de; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg: #0d1117; --fg: #e6edf3; --muted: #8d96a0; --border: #30363d; }}
  }}
  body {{ margin: 0; padding: 24px 16px; background: var(--bg); color: var(--fg);
         font: 15px/1.5 system-ui, sans-serif; }}
  main {{ max-width: 1100px; margin: 0 auto; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .details {{ color: var(--muted); margin-bottom: 16px; }}
  .legend span {{ display: inline-block; padding: 1px 8px; margin-right: 8px; border-radius: 4px; color: #1f2328; }}
  .diagram {{ border: 1px solid var(--border); border-radius: 8px; padding: 16px; overflow-x: auto; background: #fff; }}
  a {{ color: inherit; }}
</style>
</head>
<body>
<main>
  <h1>{html.escape(title)}</h1>
  <div class="details">{details}</div>
  <div class="diagram"><pre class="mermaid">{html.escape(diagram)}</pre></div>
</main>
<script type="module">
  import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";
  mermaid.initialize({{ startOnLoad: true, theme: "default" }});
</script>
</body>
</html>"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--write", type=Path, help="Write a Markdown file with the diagram")
    args = parser.parse_args()
    diagram = mermaid()
    if args.write is None:
        print(diagram)
        return
    args.write.write_text(
        "# The LangGraph workflow (generated)\n\n"
        "Generated from the compiled graph by `python graph_view.py --write`; do not edit by hand.\n"
        "![LangGraph's own rendering](langgraph.png)\n\n"
        "Solid edges are fixed, dotted edges are conditional (the label is the router's choice).\n\n"
        f"```mermaid\n{diagram}```\n",
        encoding="utf-8",
    )
    print(f"Wrote {args.write}")


if __name__ == "__main__":
    main()
