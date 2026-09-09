import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { ApiError, api } from '../api/client'
import type { Schemas } from '../api/schema'

type PullSession = Schemas['PullSessionResponse']
type PullProgress = Schemas['PullProgress']
type PullRunResponse = Schemas['PullRunResponse']

function formatDate(iso: string | null | undefined): string {
  if (!iso) return 'never'
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString()
}

const CAUTION =
  'Pulling walks your saved feed through Instagram’s private endpoints with a logged-in ' +
  'session. Instagram rate-limits this and may flag or challenge an account that pulls too ' +
  'hard — keep runs small and infrequent. Your session cookie is a live credential: it is ' +
  'written only to a local session file (chmod 600), never to the database.'

function DisabledState() {
  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-6 px-4 py-6">
      <section className="flex flex-col gap-2">
        <h1 className="text-lg font-semibold text-slate-100">Pull from Instagram</h1>
        <p className="text-sm text-slate-400">
          Fetch your own <span className="text-slate-200">saved</span> posts straight from
          Instagram, so a reel shows up here minutes after you save it instead of on your next
          data-export request.
        </p>
      </section>
      <div className="card flex flex-col gap-3 px-4 py-4 text-sm">
        <p className="font-medium text-slate-200">This feature is off.</p>
        <p className="text-slate-400">To turn it on:</p>
        <ol className="ml-4 list-decimal space-y-1 text-slate-400">
          <li>
            Install the optional dependency:{' '}
            <code className="rounded bg-surface-overlay px-1.5 py-0.5 text-xs text-slate-300">
              pip install -e &quot;.[instagram]&quot;
            </code>
          </li>
          <li>
            Add a{' '}
            <code className="rounded bg-surface-overlay px-1.5 py-0.5 text-xs text-slate-300">
              pull:
            </code>{' '}
            block with{' '}
            <code className="rounded bg-surface-overlay px-1.5 py-0.5 text-xs text-slate-300">
              enabled: true
            </code>{' '}
            to <code className="text-xs text-slate-300">config.yaml</code>, then restart the server.
          </li>
        </ol>
        <p className="text-xs text-slate-500">{CAUTION}</p>
      </div>
    </div>
  )
}

function Bar({ label, value }: { label: string; value: number }) {
  return (
    <div className="card flex flex-col px-3 py-2">
      <span className="text-lg font-semibold text-slate-100">{value}</span>
      <span className="text-xs text-slate-500">{label}</span>
    </div>
  )
}

export function Pull() {
  const [session, setSession] = useState<PullSession | null>(null)
  const [progress, setProgress] = useState<PullProgress | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const [cookies, setCookies] = useState('')
  const [showPaste, setShowPaste] = useState(false)
  const [maxCount, setMaxCount] = useState(400)
  const [full, setFull] = useState(false)
  const [busy, setBusy] = useState(false)

  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const refreshSession = useCallback(async () => {
    try {
      const s = await api.get<PullSession>('/api/pull/session')
      setSession(s)
      return s
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load Instagram status')
      return null
    }
  }, [])

  const refreshProgress = useCallback(async () => {
    try {
      const p = await api.get<PullProgress>('/api/pull/progress')
      setProgress(p)
      return p
    } catch {
      return null
    }
  }, [])

  useEffect(() => {
    void refreshSession()
    void refreshProgress()
  }, [refreshSession, refreshProgress])

  // Poll while a pull job is active.
  useEffect(() => {
    const active = busy || progress?.job_id != null
    if (!active) {
      if (pollRef.current) clearTimeout(pollRef.current)
      return
    }
    pollRef.current = setTimeout(async () => {
      const p = await refreshProgress()
      if (p && p.job_id == null) {
        setBusy(false)
        if (p.last_status === 'failed') {
          setError(p.error_message || 'Pull failed.')
        } else if (p.last_status === 'done' || p.last_status === 'cancelled') {
          setNotice(
            `Pull ${p.stopped_reason ?? p.last_status}: ${p.new} new, ${p.imported} imported, ` +
              `${p.linked} with media${p.failed ? `, ${p.failed} failed` : ''}.`,
          )
        }
      }
    }, 2000)
    return () => {
      if (pollRef.current) clearTimeout(pollRef.current)
    }
  }, [busy, progress, refreshProgress])

  async function connect(kind: 'paste' | 'local') {
    setError(null)
    setNotice(null)
    setBusy(true)
    try {
      if (kind === 'paste') {
        await api.post<PullSession>('/api/pull/connect', { cookies })
        setCookies('')
        setShowPaste(false)
      } else {
        await api.post<PullSession>('/api/pull/connect-local')
      }
      await refreshSession()
      setNotice('Connected.')
    } catch (err) {
      if (err instanceof ApiError) setError(typeof err.detail === 'string' ? err.detail : err.message)
      else setError(err instanceof Error ? err.message : 'Connect failed')
    } finally {
      setBusy(false)
    }
  }

  async function disconnect() {
    setError(null)
    setNotice(null)
    try {
      await api.delete('/api/pull/session')
      await refreshSession()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to disconnect')
    }
  }

  async function run() {
    setError(null)
    setNotice(null)
    setBusy(true)
    try {
      const res = await api.post<PullRunResponse>('/api/pull/run', {
        max_count: maxCount,
        full,
      })
      setProgress((p) => ({ ...(p as PullProgress), job_id: res.job_id }))
      await refreshProgress()
    } catch (err) {
      setBusy(false)
      if (err instanceof ApiError) setError(typeof err.detail === 'string' ? err.detail : err.message)
      else setError(err instanceof Error ? err.message : 'Failed to start')
    }
  }

  async function cancel() {
    if (progress?.job_id == null) return
    try {
      await api.post(`/api/jobs/${progress.job_id}/cancel`)
      await refreshProgress()
    } catch {
      /* the poll will catch up */
    }
  }

  if (!session) {
    return <p className="mx-auto max-w-3xl px-4 py-12 text-center text-sm text-slate-500">Loading…</p>
  }
  if (!session.enabled) return <DisabledState />

  const jobActive = busy || progress?.job_id != null
  const newIds = progress?.new_item_ids ?? []

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-8 px-4 py-6">
      <section className="flex flex-col gap-1">
        <h1 className="text-lg font-semibold text-slate-100">Pull from Instagram</h1>
        <p className="text-sm text-slate-400">
          Walks your <span className="text-slate-200">saved</span> feed newest-first, downloads
          anything not already in the library, and runs the same import + media-link steps as a
          data-export import.
        </p>
      </section>

      {error && (
        <p className="card border-red-900/60 bg-red-950/40 px-4 py-2 text-sm text-red-300">{error}</p>
      )}
      {notice && (
        <p className="card border-emerald-900/60 bg-emerald-950/30 px-4 py-2 text-sm text-emerald-300">
          {notice}
        </p>
      )}

      {!session.configured ? (
        <section className="flex flex-col gap-4">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">Connect</h2>
          <p className="text-xs text-slate-500">{CAUTION}</p>

          <div className="card flex flex-col gap-3 px-4 py-4 text-sm">
            <p className="text-slate-300">
              1. Make sure you&apos;re logged in at{' '}
              <a
                href="https://www.instagram.com/"
                target="_blank"
                rel="noreferrer"
                className="text-accent no-underline hover:underline"
              >
                instagram.com
              </a>
              .
            </p>
            <p className="text-slate-300">2. Give it the session, one of two ways:</p>

            <div className="flex flex-col gap-2">
              <button
                type="button"
                className="btn-secondary w-fit"
                disabled={busy}
                onClick={() => void connect('local')}
              >
                Use this computer&apos;s Firefox session
              </button>
              <p className="text-xs text-slate-500">
                Works only if the browser you logged in with runs on the same machine as the
                server.
              </p>
            </div>

            <button
              type="button"
              className="w-fit text-xs text-accent no-underline hover:underline"
              onClick={() => setShowPaste((v) => !v)}
            >
              {showPaste ? 'Hide' : 'Paste a cookie export instead'}
            </button>
            {showPaste && (
              <div className="flex flex-col gap-2">
                <p className="text-xs text-slate-500">
                  Paste instagram.com cookies — a JSON object, a Cookie-Editor array, or a
                  Netscape <code className="text-slate-400">cookies.txt</code> all work. At
                  minimum <code className="text-slate-400">sessionid</code>,{' '}
                  <code className="text-slate-400">ds_user_id</code> and{' '}
                  <code className="text-slate-400">csrftoken</code>. Quick way without an
                  extension: DevTools → Network → click any instagram.com request → Cookies.
                </p>
                <textarea
                  className="input h-32 font-mono text-xs"
                  placeholder='{"sessionid": "...", "csrftoken": "...", "ds_user_id": "..."}'
                  value={cookies}
                  onChange={(e) => setCookies(e.target.value)}
                />
                <button
                  type="button"
                  className="btn-primary w-fit"
                  disabled={busy || cookies.trim().length === 0}
                  onClick={() => void connect('paste')}
                >
                  {busy ? 'Connecting…' : 'Connect'}
                </button>
              </div>
            )}
          </div>
        </section>
      ) : (
        <>
          <section className="card flex items-center justify-between gap-3 px-4 py-3 text-sm">
            <div className="flex flex-col">
              <span className="text-slate-200">
                Connected as <span className="font-medium">@{session.username}</span>
              </span>
              <span className="text-xs text-slate-500">
                session verified {formatDate(session.last_verified_at)}
              </span>
            </div>
            <button type="button" className="btn-secondary" onClick={() => void disconnect()}>
              Disconnect
            </button>
          </section>

          <section className="flex flex-col gap-3">
            <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">Pull</h2>
            <label className="flex items-center gap-2 text-sm text-slate-300">
              Walk back through
              <input
                type="number"
                min={1}
                max={5000}
                className="input w-24"
                value={maxCount}
                onChange={(e) => setMaxCount(Math.max(1, Number(e.target.value) || 1))}
              />
              saved posts
            </label>
            <label className="flex items-center gap-2 text-sm text-slate-300">
              <input type="checkbox" checked={full} onChange={(e) => setFull(e.target.checked)} />
              Pull everything in range, not just posts saved since the last run
            </label>
            <div className="flex items-center gap-3">
              <button
                type="button"
                className="btn-primary w-fit"
                disabled={jobActive}
                onClick={() => void run()}
              >
                {jobActive ? 'Pulling…' : 'Run pull'}
              </button>
              {jobActive && progress?.job_id != null && (
                <button type="button" className="btn-secondary w-fit" onClick={() => void cancel()}>
                  Cancel
                </button>
              )}
            </div>
            <p className="text-xs text-slate-500">
              There&apos;s a 3–7s pause between downloads to stay under Instagram&apos;s rate
              limits, so a large pull takes a while.
            </p>
          </section>

          {progress && (progress.scanned > 0 || jobActive || progress.last_status === 'done') && (
            <section className="flex flex-col gap-3">
              <div className="grid grid-cols-3 gap-3 sm:grid-cols-6">
                <Bar label="Scanned" value={progress.scanned} />
                <Bar label="New" value={progress.new} />
                <Bar label="Downloaded" value={progress.downloaded} />
                <Bar label="Imported" value={progress.imported} />
                <Bar label="Linked" value={progress.linked} />
                <Bar label="Failed" value={progress.failed} />
              </div>
              {!jobActive && progress.stopped_reason && (
                <p className="text-xs text-slate-500">Last run: {progress.stopped_reason}.</p>
              )}
              {!jobActive && newIds.length > 0 && (
                <p className="text-sm text-slate-300">
                  {newIds.length} new item{newIds.length === 1 ? '' : 's'} —{' '}
                  <Link to="/enrich" className="text-accent no-underline hover:underline">
                    enrich
                  </Link>{' '}
                  and{' '}
                  <Link to="/categorize" className="text-accent no-underline hover:underline">
                    categorize
                  </Link>{' '}
                  them next.
                </p>
              )}
            </section>
          )}
        </>
      )}
    </div>
  )
}
