import { FormEvent, useEffect, useRef, useState } from 'react'

type Priority = 'low' | 'medium' | 'high'

type Todo = {
  id: number
  text: string
  completed: boolean
  createdAt: number
  deletedAt?: number
  priority: Priority
}

type Theme = 'light' | 'dark'

const THEME_STORAGE_KEY = 'theme'

const TODOS_STORAGE_KEY = 'to_tooooooo'

const PRIORITY_LEVELS = ['low', 'medium', 'high'] as const

const PRIORITY_LABELS: Record<Priority, string> = {
  low: 'נמוכה',
  medium: 'בינונית',
  high: 'גבוהה',
}

const CONFETTI_COLORS = [
  '#ef4444',
  '#f97316',
  '#eab308',
  '#22c55e',
  '#3b82f6',
  '#a855f7',
]

type ConfettiPiece = {
  left: number
  color: string
  delay: number
  duration: number
  width: number
  height: number
}

const CONFETTI_PIECES: ConfettiPiece[] = Array.from(
  { length: 28 },
  () => ({
    left: Math.random() * 100,
    color: CONFETTI_COLORS[Math.floor(Math.random() * CONFETTI_COLORS.length)],
    delay: Math.random() * 0.3,
    duration: 1.5 + Math.random() * 0.6,
    width: 7 + Math.random() * 6,
    height: 5 + Math.random() * 4,
  }),
)

function formatTimestamp(timestamp: number): string {
  const date = new Date(timestamp)
  const day = String(date.getDate()).padStart(2, '0')
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const year = date.getFullYear()
  const hours = String(date.getHours()).padStart(2, '0')
  const minutes = String(date.getMinutes()).padStart(2, '0')

  return `${day}/${month}/${year} ${hours}:${minutes}`
}

function getInitialTheme(): Theme {
  try {
    return localStorage.getItem(THEME_STORAGE_KEY) === 'dark' ? 'dark' : 'light'
  } catch {
    return 'light'
  }
}

function isTodo(value: unknown): value is Todo {
  if (typeof value !== 'object' || value === null) return false

  const todo = value as Record<string, unknown>

  return (
    typeof todo.id === 'number' &&
    typeof todo.text === 'string' &&
    typeof todo.completed === 'boolean' &&
    typeof todo.createdAt === 'number' &&
    (todo.deletedAt === undefined || typeof todo.deletedAt === 'number') &&
    PRIORITY_LEVELS.includes(todo.priority as Priority)
  )
}

function getInitialTodos(): Todo[] {
  try {
    const stored = localStorage.getItem(TODOS_STORAGE_KEY)

    if (!stored) return []

    const parsed: unknown = JSON.parse(stored)

    if (!Array.isArray(parsed)) return []

    return parsed.filter(isTodo)
  } catch {
    // Storage unavailable or corrupted; start with an empty list.
    return []
  }
}

function App() {
  const [todos, setTodos] = useState<Todo[]>(getInitialTodos)
  const [text, setText] = useState('')
  const [priority, setPriority] = useState<Priority>('medium')
  const [theme, setTheme] = useState<Theme>(getInitialTheme)
  const [celebrating, setCelebrating] = useState(false)

  // Tracks whether the previous render already had every task done, so the
  // celebration fires only on the transition to "all done".
  const wasAllDone = useRef(false)

  // Marks that the latest todos change came from a completion toggle, so
  // deleting the last open todo never triggers the celebration.
  const toggled = useRef(false)

  useEffect(() => {
    const activeTodos = todos.filter((todo) => !todo.deletedAt)
    const allDone =
      activeTodos.length > 0 && activeTodos.every((todo) => todo.completed)

    if (allDone && !wasAllDone.current && toggled.current) {
      setCelebrating(true)
    }

    wasAllDone.current = allDone
    toggled.current = false
  }, [todos])

  useEffect(() => {
    if (!celebrating) return

    const timer = setTimeout(() => setCelebrating(false), 2000)

    return () => clearTimeout(timer)
  }, [celebrating])

  useEffect(() => {
    document.documentElement.dataset.theme = theme

    try {
      localStorage.setItem(THEME_STORAGE_KEY, theme)
    } catch {
      // Storage unavailable; theme still applies for this session.
    }
  }, [theme])

  useEffect(() => {
    try {
      localStorage.setItem(TODOS_STORAGE_KEY, JSON.stringify(todos))
    } catch {
      // Storage unavailable; todos still apply for this session.
    }
  }, [todos])

  function toggleTheme() {
    setTheme((current) => (current === 'dark' ? 'light' : 'dark'))
  }

  function addTodo(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const value = text.trim()

    if (!value) return

    const createdAt = Date.now()

    setTodos((current) => [
      ...current,
      { id: createdAt, text: value, completed: false, createdAt, priority },
    ])
    setText('')
  }

  function toggleTodo(id: number) {
    toggled.current = true

    setTodos((current) =>
      current.map((todo) =>
        todo.id === id && !todo.deletedAt
          ? { ...todo, completed: !todo.completed }
          : todo,
      ),
    )
  }

  function deleteTodo(id: number) {
    setTodos((current) =>
      current.map((todo) =>
        todo.id === id && !todo.deletedAt
          ? { ...todo, deletedAt: Date.now() }
          : todo,
      ),
    )
  }

  function clearCompleted() {
    setTodos((current) =>
      current.map((todo) =>
        !todo.deletedAt && todo.completed
          ? { ...todo, deletedAt: Date.now() }
          : todo,
      ),
    )
  }

  const openCount = todos.filter(
    (todo) => !todo.deletedAt && !todo.completed,
  ).length

  const hasCompleted = todos.some((todo) => !todo.deletedAt && todo.completed)

  return (
    <main className="app">
      <section className="card" aria-labelledby="title">
        <div className="card-header">
          <h1 id="title">המשימות שלי</h1>
          <button
            className="theme-toggle"
            type="button"
            onClick={toggleTheme}
            aria-label={
              theme === 'dark'
                ? 'החלפה למצב בהיר'
                : 'החלפה למצב כהה'
            }
          >
            {theme === 'dark' ? '☀️' : '🥇'}
          </button>
        </div>

        <p className="open-count">
          {openCount > 0
            ? `${openCount} משימות פתוחות`
            : 'אין משימות פתוחות'}
        </p>

        <form onSubmit={addTodo}>
          <label className="sr-only" htmlFor="new-todo">
            משימה חדשה
          </label>
          <input
            id="new-todo"
            value={text}
            onChange={(event) => setText(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Escape') setText('')
            }}
            placeholder="מה צריך לעשות?"
          />
          <label className="sr-only" htmlFor="new-todo-priority">
            עדיפות
          </label>
          <select
            id="new-todo-priority"
            value={priority}
            onChange={(event) => setPriority(event.target.value as Priority)}
          >
            {PRIORITY_LEVELS.map((level) => (
              <option key={level} value={level}>
                {PRIORITY_LABELS[level]}
              </option>
            ))}
          </select>
          <button type="submit">הוספה</button>
        </form>

        {todos.length === 0 ? (
          <p className="empty">אין משימות עדיין</p>
        ) : (
          <ul>
            {todos.map((todo) => {
              const deletedAt = todo.deletedAt

              const content = (
                <span className="todo-text">
                  <span
                    className={`priority-badge priority-${todo.priority}`}
                  >
                    {PRIORITY_LABELS[todo.priority]}
                  </span>
                  <span className="timestamps">
                    נוצרה {formatTimestamp(todo.createdAt)}
                    {deletedAt
                      ? ` · נמחקה ${formatTimestamp(deletedAt)}`
                      : ''}
                    {' · '}
                  </span>
                  <span
                    className={
                      deletedAt
                        ? 'deleted-text'
                        : todo.completed
                          ? 'completed'
                          : ''
                    }
                  >
                    {todo.text}
                  </span>
                </span>
              )

              return (
                <li
                  key={todo.id}
                  className={deletedAt ? 'deleted' : undefined}
                >
                  {deletedAt ? (
                    <div className="todo-item">{content}</div>
                  ) : (
                    <label className="todo-item">
                      <input
                        type="checkbox"
                        checked={todo.completed}
                        onChange={() => toggleTodo(todo.id)}
                      />
                      {content}
                    </label>
                  )}
                  {!deletedAt && (
                    <button
                      className="delete"
                      type="button"
                      onClick={() => deleteTodo(todo.id)}
                      aria-label={`מחיקת ${todo.text}`}
                    >
                      🗑️ מחיקה
                    </button>
                  )}
                </li>
              )
            })}
          </ul>
        )}

        {hasCompleted && (
          <button
            className="clear-completed"
            type="button"
            onClick={clearCompleted}
          >
            clear dones
          </button>
        )}
      </section>

      {celebrating && (
        <div className="celebration">
          <div aria-hidden="true">
            {CONFETTI_PIECES.map((piece, index) => (
              <span
                key={index}
                className="confetti-piece"
                style={{
                  left: `${piece.left}%`,
                  width: piece.width,
                  height: piece.height,
                  backgroundColor: piece.color,
                  animationDelay: `${piece.delay}s`,
                  animationDuration: `${piece.duration}s`,
                }}
              />
            ))}
          </div>
          <p className="celebration-message" role="status">
            כל הכבוד! סיימת את כל המשימות 🎉
          </p>
        </div>
      )}
    </main>
  )
}

export default App
