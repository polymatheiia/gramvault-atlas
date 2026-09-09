import { useEffect, useState, type FormEvent, type ReactNode } from 'react'
import { api, getAuthToken, setAuthToken, setUnauthorizedHandler } from '../api/client'

/**
 * Wraps the app. If the backend has `auth.token` set and this browser has
 * no valid token, a 401 from any request flips this into a token-entry
 * screen. On submit the token is stored and the page reloads so every
 * query re-runs with the new `Authorization` header.
 */
export function AuthGate({ children }: { children: ReactNode }) {
  const [locked, setLocked] = useState(false)
  const [value, setValue] = useState('')
  // A 401 while a token is already stored means the stored one is wrong.
  const rejected = locked && getAuthToken() !== null

  useEffect(() => {
    setUnauthorizedHandler(() => setLocked(true))
    // Proactive probe so we show the prompt before any page renders an error.
    api.get('/api/jobs', { limit: 1 }).catch(() => {})
    return () => setUnauthorizedHandler(null)
  }, [])

  function submit(e: FormEvent) {
    e.preventDefault()
    if (!value.trim()) return
    setAuthToken(value.trim())
    window.location.reload()
  }

  if (!locked) return <>{children}</>

  return (
    <div className="flex min-h-screen items-center justify-center bg-surface px-4">
      <form onSubmit={submit} className="card flex w-full max-w-sm flex-col gap-3 p-6">
        <h1 className="text-lg font-semibold text-slate-100">API token required</h1>
        <p className="text-sm text-slate-400">
          This GramVault Atlas server is protected. Paste its <code>auth.token</code> to continue.
        </p>
        <input
          className="input"
          type="password"
          autoFocus
          placeholder="Bearer token"
          value={value}
          onChange={(e) => setValue(e.target.value)}
        />
        {rejected && <p className="text-sm text-red-300">The stored token was rejected.</p>}
        <button type="submit" className="btn-primary">
          Unlock
        </button>
      </form>
    </div>
  )
}
