# The LangGraph workflow (generated)

Generated from the compiled graph by `python graph_view.py --write`; do not edit by hand.
![LangGraph's own rendering](langgraph.png)

Solid edges are fixed, dotted edges are conditional (the label is the router's choice).

```mermaid
---
config:
  flowchart:
    curve: linear
---
graph TD;
	__start__([<p>__start__</p>]):::first
	load_issue(load_issue)
	plan_with_pi(plan_with_pi)
	human_input(human_input)
	implement_with_pi(implement_with_pi)
	review(review)
	implement_review(implement_review)
	verify(verify)
	finish(finish)
	publish_pr(publish_pr)
	push_review(push_review)
	__end__([<p>__end__</p>]):::last
	__start__ --> load_issue;
	human_input -.-> finish;
	human_input -. &nbsp;review&nbsp; .-> implement_review;
	human_input -. &nbsp;implement&nbsp; .-> implement_with_pi;
	human_input -. &nbsp;plan&nbsp; .-> plan_with_pi;
	implement_review -.-> finish;
	implement_review -. &nbsp;human&nbsp; .-> human_input;
	implement_review -.-> verify;
	implement_with_pi -.-> finish;
	implement_with_pi -. &nbsp;human&nbsp; .-> human_input;
	implement_with_pi -.-> verify;
	load_issue -.-> finish;
	load_issue -. &nbsp;plan&nbsp; .-> plan_with_pi;
	load_issue -.-> review;
	plan_with_pi -.-> finish;
	plan_with_pi -. &nbsp;human&nbsp; .-> human_input;
	plan_with_pi -. &nbsp;implement&nbsp; .-> implement_with_pi;
	review -.-> finish;
	review -. &nbsp;implement&nbsp; .-> implement_review;
	verify -.-> finish;
	verify -. &nbsp;review_retry&nbsp; .-> implement_review;
	verify -. &nbsp;retry&nbsp; .-> implement_with_pi;
	verify -. &nbsp;publish&nbsp; .-> publish_pr;
	verify -. &nbsp;push&nbsp; .-> push_review;
	finish --> __end__;
	publish_pr --> __end__;
	push_review --> __end__;
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
	classDef visited fill:#d3f9d8,stroke:#2b8a3e
	classDef current fill:#ffd43b,stroke:#e67700,stroke-width:3px
```
