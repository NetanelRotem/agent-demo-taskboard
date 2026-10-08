# 2. מסע המשתמש

> **מה המשתמש רואה:** הכול קורה בתגובות על ה־Issue וה־PR. אין ממשק נוסף ללמוד.

## המסלול המלא

```mermaid
sequenceDiagram
    actor U as Developer
    participant GH as GitHub
    participant AG as Agent
    participant TG as Telegram

    U->>GH: Open issue "Add due dates to todos"
    U->>GH: Comment /agent start
    GH->>AG: webhook
    AG-->>GH: 👍 reaction on the comment
    AG-->>TG: 🚀 Run started
    Note over AG: Pi reads the code (read-only)
    AG->>GH: "Proposed plan (v1) ... /agent approve"
    AG-->>TG: 📋 Plan ready
    U->>GH: /agent approve
    Note over AG: Pi edits code in the sandbox
    Note over AG: Server runs lint + build
    AG-->>TG: ✅ Checks passed
    AG->>GH: Push branch + open PR
    AG->>GH: "Pull request ready for review"
    AG-->>TG: 🔀 PR opened
    U->>GH: Review: "Request changes" + inline comments
    Note over AG: Pi fixes, server re-checks
    AG->>GH: New commit on the same PR
    U->>GH: Merge
    AG-->>TG: 📦 PR merged, sandbox deleted
```

## הפקודות

כל פקודה היא תגובה על ה־Issue. רק משתמשים שמופיעים ב־`AUTHORIZED_GITHUB_USERS` יכולים להפעיל את הסוכן.

| פקודה | מתי | מה קורה |
|-------|-----|---------|
| `/agent start` | על Issue חדש | פותח run: תכנון, ואז המתנה לאישור |
| `/agent start auto` | כשסומכים על הסוכן במשימה | מדלג על אישור התוכנית. שאלות הבהרה עדיין עוצרות |
| `/agent approve` | אחרי שהתוכנית פורסמה | מתחיל לכתוב קוד |
| `/agent reject <reason>` | התוכנית לא טובה | הסוכן מתכנן מחדש עם הסיבה |
| `/agent answer <text>` | הסוכן שאל שאלה | התשובה נכנסת להחלטות, והסוכן ממשיך |
| `/agent stop` | בכל שלב | עוצר את ה־run, כולל Pi שרץ באותו רגע |

ל־`approve`, `answer` ו־`reject` אפשר להוסיף מזהה בקשה (למשל `/agent approve a1b2c3d4-p1-a`). בלי מזהה, הפקודה הולכת לבקשה שממתינה ב־Issue.

## שתי נקודות ההחלטה של האדם

```mermaid
flowchart LR
    subgraph G1["Gate 1: before any code"]
        P[Plan posted] --> D1{approve / reject / answer}
    end
    subgraph G2["Gate 2: before main"]
        PR[PR opened] --> D2{review / merge}
    end
    D1 -->|approve| W[Agent writes code] --> PR
    D1 -->|"reject / answer"| P
    D2 -->|"request changes"| W
```

- **שער 1** חוסך זמן: אם הסוכן הבין לא נכון, עוצרים אותו לפני שכתב קוד. עם `auto` מוותרים על השער הזה ומסתמכים על שער 2.
- **שער 2** תמיד קיים: הסוכן לא ממזג. גם reviews נכנסים לעבודה רק ממשתמשים מורשים.

## מה הסוכן יכול לשאול

אם Pi נתקל בהחלטה שמשנה את התנהגות המוצר, הוא מחזיר `needs_input` במקום לנחש. בחירות טכניות רגילות הוא אמור להסיק מהקוד. השאלה מתפרסמת כתגובה, וה־run מחכה. בזמן ההמתנה ה־sandbox מושהה ולא עולה כסף.
