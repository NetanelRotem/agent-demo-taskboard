import { FormEvent, useEffect, useState } from 'react'

type Todo = {
  id: number
  text: string
  completed: boolean
}

type Theme = 'light' | 'dark'

const THEME_STORAGE_KEY = 'theme'

function getInitialTheme(): Theme {
  try {
    return localStorage.getItem(THEME_STORAGE_KEY) === 'dark' ? 'dark' : 'light'
  } catch {
    return 'light'
  }
}

function App() {
  const [todos, setTodos] = useState<Todo[]>([])
  const [text, setText] = useState('')
  const [theme, setTheme] = useState<Theme>(getInitialTheme)

  useEffect(() => {
    document.documentElement.dataset.theme = theme

    try {
      localStorage.setItem(THEME_STORAGE_KEY, theme)
    } catch {
      // Storage unavailable; theme still applies for this session.
    }
  }, [theme])

  function toggleTheme() {
    setTheme((current) => (current === 'dark' ? 'light' : 'dark'))
  }

  function addTodo(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const value = text.trim()

    if (!value) return

    setTodos((current) => [
      ...current,
      { id: Date.now(), text: value, completed: false },
    ])
    setText('')
  }

  function toggleTodo(id: number) {
    setTodos((current) =>
      current.map((todo) =>
        todo.id === id ? { ...todo, completed: !todo.completed } : todo,
      ),
    )
  }

  function deleteTodo(id: number) {
    setTodos((current) => current.filter((todo) => todo.id !== id))
  }

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
            {theme === 'dark' ? '☀️' : '🍎'}
          </button>
        </div>

        <form onSubmit={addTodo}>
          <label className="sr-only" htmlFor="new-todo">
            משימה חדשה
          </label>
          <input
            id="new-todo"
            value={text}
            onChange={(event) => setText(event.target.value)}
            placeholder="מה צריך לעשות?"
          />
          <button type="submit">הוספה</button>
        </form>

        {todos.length === 0 ? (
          <p className="empty">אין משימות עדיין</p>
        ) : (
          <ul>
            {todos.map((todo) => (
              <li key={todo.id}>
                <label>
                  <input
                    type="checkbox"
                    checked={todo.completed}
                    onChange={() => toggleTodo(todo.id)}
                  />
                  <span className={todo.completed ? 'completed' : ''}>
                    {todo.text}
                  </span>
                </label>
                <button
                  className="delete"
                  type="button"
                  onClick={() => deleteTodo(todo.id)}
                  aria-label={`מחיקת ${todo.text}`}
                >
                  מחיקה
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
    </main>
  )
}

export default App
