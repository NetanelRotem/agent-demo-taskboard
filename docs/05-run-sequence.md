# 5. ריצה מקצה לקצה

> **בקצרה:** כל webhook הופך לפריט עבודה שמור. ה־worker מריץ את הגרף עד שהוא נעצר, משהה את ה־sandbox ומשתחרר.

## מ־`/agent start` עד תוכנית

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
    PI-->>G: submit_result {status: ready, plan}
    G->>GH: comment "Proposed plan (v1)"
    G->>G: interrupt() → checkpoint
    W->>SB: sbx stop (not billed while waiting)
    W->>DB: work item done
```

## מאישור עד PR

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

## Review ו־merge

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

## איך Pi מחזיר תוצאה

Pi לא מסיים בטקסט חופשי. הוא קורא לכלי `submit_result`, והכלי:
- אוכף סכמה: `status`, ‏`summary`, ‏`plan`, ‏`questions`, ‏`claimed_checks`.
- מגביל את הסטטוסים לפי השלב: `ready | needs_input | failed` בתכנון, ‏`completed | needs_input | failed` במימוש.
- מחזיר `terminate: true`, כך שאין סבב LLM מיותר אחרי ההגשה.

השרת קורא את התוצאה מאירוע `tool_execution_end`. חילוץ JSON מטקסט נשאר רק כ־fallback למודלים שלא קוראים לכלי.
