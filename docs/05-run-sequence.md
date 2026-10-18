# 5. End-to-end run

> **In short:** every webhook becomes a stored work item. The worker runs the graph until it stops, pauses the sandbox, and lets go.

## From `/agent start` to a plan

```mermaid
sequenceDiagram
    participant GH as GitHub
    participant API as FastAPI
    participant DB as SQLite
    participant W as Worker
    participant G as LangGraph
    participant SB as Sandbox
    participant PI as Pi

    GH->>API: POST /webhooks/github (issue_comment)
    API->>API: parse "/agent start", check user
    API->>DB: persist work item (dedupe by delivery id)
    API-->>GH: 202 accepted
    W->>DB: claim work item
    W->>GH: 👍 reaction
    W->>G: ainvoke(start)
    G->>GH: load issue + comments
    G->>SB: sbx create --ttl 24h + network policy
    G->>SB: agent.sh setup (clone, branch)
    G->>SB: agent.sh pi (read-only tools)
    SB->>PI: prompt via stdin
    PI-->>G: JSONL events (streamed)
    PI-->>G: submit_result {status: ready, plan, complexity}
    alt approval needed (complex, long plan, or mode=true)
        G->>GH: comment "Proposed plan (v1)" + why approval is needed
        G->>G: interrupt() → checkpoint
    else simple plan in auto mode
        G->>GH: comment "Plan (v1) — implementing without approval"
        Note over G: continues straight to implement_with_pi
    end
    W->>SB: sbx stop (not billed while waiting)
    W->>DB: work item done
```

## From approval to PR

```mermaid
sequenceDiagram
    participant GH as GitHub
    participant W as Worker
    participant G as LangGraph
    participant SB as Sandbox
    participant PI as Pi

    GH->>W: /agent approve (via queue)
    W->>W: validate: pending, right kind, current plan, issue unchanged
    W->>G: Command(resume=approve)
    G->>SB: agent.sh pi (edit + bash tools)  — sandbox auto-starts
    SB->>PI: implement approved plan
    PI-->>G: submit_result {status: completed}
    G->>SB: changed-files → must be inside todo-app/
    G->>SB: check npm run lint
    G->>SB: check npm run build
    alt checks fail and attempts left
        G->>SB: agent.sh pi (with failing output)
    end
    G->>SB: revision (hash) == verified?
    G->>SB: commit-push
    G->>GH: create PR "Closes #N"
    G->>GH: comment "Pull request ready for review"
    W->>SB: sbx stop
```

## Review and merge

```mermaid
sequenceDiagram
    participant GH as GitHub
    participant W as Worker
    participant G as LangGraph
    participant SB as Sandbox

    GH->>W: pull_request_review (changes_requested / commented)
    W->>G: new thread run_id:review:{id}, same sandbox & Pi session
    G->>GH: load review body + inline comments
    G->>SB: Pi fixes → verify → commit-push
    G->>GH: comment on PR "Review feedback addressed in {sha}"
    W->>SB: sbx stop
    GH->>W: pull_request closed (merged)
    W->>SB: sbx rm
    W->>GH: comment "workspace was removed"
```

## How Pi returns a result

Pi doesn't finish with free text. It calls the `submit_result` tool, which:
- Enforces a schema: `status`, `summary`, `plan`, `questions`, `claimed_checks`, and while planning also `complexity` and `complexity_reason`.
- Limits the statuses by phase: `ready | needs_input | failed` while planning, `completed | needs_input | failed` while implementing.
- Returns `terminate: true`, so there is no wasted LLM turn after the submission.

The server reads the result from the `tool_execution_end` event. Extracting JSON from text remains only as a fallback for models that don't call the tool.
