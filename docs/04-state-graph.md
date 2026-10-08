# 4. גרף המצבים

> **בקצרה:** כל run הוא מעבר בין nodes. בכל node הגרף שומר checkpoint, ובנקודות ההחלטה הוא נעצר עד שאדם עונה.

## בתמונה אחת (לשקף)

![The agent graph, simplified](graph-slide.png)

כחול: Pi עובד. צהוב: הגרף עוצר ומחכה לאדם. ירוק: השרת מריץ בדיקות. סגול: עבודה מול GitHub. היציאות ל־`finish` (כשל או עצירה) הושמטו. המקור: [graph-slide.mmd](graph-slide.mmd).

## הגרף

הדיאגרמה כאן מצוירת ביד ומסבירה את המעברים. הגרסה שנוצרת אוטומטית מהקוד נמצאת ב־[langgraph.md](langgraph.md), והגרסה החיה בכתובת `/graph` של השרת (ראו [08](08-observability.md)).

```mermaid
stateDiagram-v2
    [*] --> load_issue

    load_issue --> plan_with_pi: start
    load_issue --> review: PR review arrived
    load_issue --> finish: workspace failed

    plan_with_pi --> human_input: plan ready / question
    plan_with_pi --> implement_with_pi: auto approve
    plan_with_pi --> finish: failed / stopped

    human_input --> implement_with_pi: approve
    human_input --> plan_with_pi: reject / answer / issue changed
    human_input --> implement_review: answer (review phase)
    human_input --> finish: stop

    implement_with_pi --> verify: done (or Pi error)
    implement_with_pi --> human_input: needs_input
    implement_with_pi --> finish: stopped

    review --> implement_review
    implement_review --> verify
    implement_review --> human_input: needs_input
    implement_review --> finish: stopped

    verify --> publish_pr: passed (initial)
    verify --> push_review: passed (review)
    verify --> implement_with_pi: failed, attempts left
    verify --> implement_review: failed, attempts left (review)
    verify --> finish: failed, no attempts left

    publish_pr --> [*]
    push_review --> [*]
    finish --> [*]

    note right of human_input
        interrupt(): graph pauses,
        checkpoint saved,
        sandbox suspended
    end note
```

## ה־nodes

| Node | מה הוא עושה | כלים של Pi |
|------|-------------|------------|
| `load_issue` | טוען את ה־Issue והתגובות, מחשב `requirements_version` (hash של הדרישות), ויוצר branch ו־sandbox. | — |
| `plan_with_pi` | Pi חוקר את הקוד ומגיש תוכנית, שאלות או כשל. | `read, grep, find, ls` |
| `human_input` | מפרסם תוכנית או שאלה כתגובה ונעצר (`interrupt`). כשמגיעה תשובה, היא נרשמת ב־`decisions`. | — |
| `implement_with_pi` | Pi מממש את התוכנית המאושרת. בניסיון חוזר הוא מקבל את הפלט של הבדיקות שנכשלו. | `read, bash, edit, write, grep, find, ls` |
| `verify` | השרת בודק שהשינויים בתוך התיקייה המותרת ומריץ lint ו־build. | — |
| `publish_pr` | בודק שהקוד לא השתנה מאז הבדיקות, ואז commit, push ו־PR. | — |
| `review` | טוען את סיכום ה־review ואת ה־inline comments. | — |
| `implement_review` | Pi מתקן לפי ה־review באותו sandbox ובאותו session. | כמו במימוש |
| `push_review` | commit נוסף לאותו PR. | — |
| `finish` | תגובת סיכום: סטטוס, קבצים שהשתנו ובדיקות. | — |

## לולאת התיקון

![Fix loop](fix-loop.png)

```mermaid
flowchart LR
    I["implement<br/>attempt N"] --> V{verify}
    V -->|pass| P[publish PR]
    V -->|"fail, N < 3"| F["feed failing output<br/>back to Pi"] --> I
    V -->|"fail, N = 3"| X[finish: failed]
```

`AGENT_MAX_ATTEMPTS` (ברירת מחדל 3) מגביל את מספר הניסיונות. בכל ניסיון Pi מקבל את 2,000 התווים האחרונים מהפלט של כל בדיקה שנכשלה, וממשיך באותו session, כך שהוא זוכר מה כבר ניסה.

## הגנות מובנות בגרף

- **דרישות שהשתנו:** אם ה־Issue נערך בזמן שהתוכנית חיכתה לאישור, `approve` הופך ל־`requirements_changed` והסוכן מתכנן מחדש.
- **אישור ישן:** אישור של גרסת תוכנית קודמת נדחה ("That approval is for an old plan version").
- **קוד שהשתנה אחרי בדיקה:** ‏`publish_pr` ו־`push_review` משווים את ה־revision (‏hash של HEAD ושל הקבצים) למה שנבדק. אם יש פער, הם מריצים בדיקות שוב או מסרבים.
- **review בלי שינוי:** אם Pi "תיקן" review בלי לשנות קוד, הבדיקה נכשלת.
