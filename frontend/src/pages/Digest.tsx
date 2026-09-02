import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, api } from '../api/client'
import type {
  CategoryListResponse,
  Digest as DigestRow,
  DigestCreateResponse,
  DigestPreflightResponse,
  DigestTemplateInfo,
  Job,
} from '../types'

function formatCost(cost: number | null): string {
  if (cost == null) return 'n/a'
  if (cost === 0) return 'free (local)'
  return `~$${cost.toFixed(2)}`
}

function formatDate(iso: string | null): string {
  if (!iso) return ''
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString()
}

function downloadMarkdown(name: string, markdown: string) {
  const blob = new Blob([markdown], { type: 'text/markdown' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'digest'}.md`
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

const STATUS_STYLES: Record<string, string> = {
  done: 'text-emerald-400',
  running: 'text-accent',
  pending: 'text-slate-400',
  failed: 'text-red-400',
  cancelled: 'text-slate-500',
}

export function Digest() {
  const [templates, setTemplates] = useState<DigestTemplateInfo[]>([])
  const [categories, setCategories] = useState<CategoryListResponse | null>(null)
  const [history, setHistory] = useState<DigestRow[]>([])

  const [template, setTemplate] = useState('')
  const [category, setCategory] = useState('')
  const [query, setQuery] = useState('')
  const [name, setName] = useState('')
  const [showPrompts, setShowPrompts] = useState(false)

  const [preflight, setPreflight] = useState<DigestPreflightResponse | null>(null)
  const [preflighting, setPreflighting] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [jobBar, setJobBar] = useState<{ step: number; total: number } | null>(null)

  const [current, setCurrent] = useState<DigestRow | null>(null)
  const [tick, setTick] = useState(0)
  const activeIdRef = useRef<number | null>(null)
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const selected = templates.find((t) => t.name === template)
  const hasSelection = !!(category || query.trim())

  const refreshHistory = useCallback(() => {
    api.get<DigestRow[]>('/api/digests').then(setHistory).catch(() => undefined)
  }, [])

  useEffect(() => {
    api
      .get<DigestTemplateInfo[]>('/api/digests/templates')
      .then((list) => {
        setTemplates(list)
        setTemplate((t) => t || list[0]?.name || '')
      })
      .catch(() => undefined)
    api.get<CategoryListResponse>('/api/library/categories').then(setCategories).catch(() => undefined)
    refreshHistory()
  }, [refreshHistory])

  // Reset the estimate whenever the selection changes.
  useEffect(() => {
    setPreflight(null)
  }, [template, category, query])

  // Poll the running digest's job.
  useEffect(() => {
    if (!busy) {
      if (pollRef.current) clearTimeout(pollRef.current)
      return
    }
    pollRef.current = setTimeout(async () => {
      const id = activeIdRef.current
      if (id == null) return
      try {
        const digest = await api.get<DigestRow>(`/api/digests/${id}`)
        if (digest.job_id != null) {
          const job = await api.get<Job>(`/api/jobs/${digest.job_id}`)
          const prog = job.progress as { step?: number; total?: number } | null
          if (prog && typeof prog.total === 'number') {
            setJobBar({ step: prog.step ?? 0, total: prog.total })
          }
        }
        if (digest.status !== 'pending' && digest.status !== 'running') {
          setBusy(false)
          setJobBar(null)
          setCurrent(digest)
          refreshHistory()
          if (digest.status === 'failed') {
            setError(digest.error_message || 'The digest run failed.')
          }
        } else {
          setTick((n) => n + 1)
        }
      } catch {
        setTick((n) => n + 1)
      }
    }, 2000)
    return () => {
      if (pollRef.current) clearTimeout(pollRef.current)
    }
  }, [busy, tick, refreshHistory])

  async function runPreflight() {
    if (!template || !hasSelection) return
    setError(null)
    setPreflighting(true)
    try {
      const res = await api.post<DigestPreflightResponse>('/api/digests/preflight', {
        template,
        category: category || null,
        query: query.trim() || null,
      })
      setPreflight(res)
    } catch (err) {
      setError(
        err instanceof ApiError && typeof err.detail === 'string'
          ? err.detail
          : err instanceof Error
            ? err.message
            : 'Preflight failed',
      )
    } finally {
      setPreflighting(false)
    }
  }

  async function run() {
    if (!template || !hasSelection) return
    setError(null)
    setBusy(true)
    setCurrent(null)
    try {
      const res = await api.post<DigestCreateResponse>('/api/digests', {
        template,
        category: category || null,
        query: query.trim() || null,
        name: name.trim() || null,
      })
      activeIdRef.current = res.digest_id
      setJobBar({ step: 0, total: (preflight?.batches ?? 0) + 1 })
      refreshHistory()
    } catch (err) {
      setBusy(false)
      if (err instanceof ApiError && err.status === 503) {
        setError(
          typeof err.detail === 'string'
            ? `${err.detail} — set the digest model in Settings.`
            : 'The digest model is not ready — check Settings.',
        )
      } else if (err instanceof ApiError && typeof err.detail === 'string') {
        setError(err.detail)
      } else {
        setError(err instanceof Error ? err.message : 'Failed to start')
      }
    }
  }

  async function openDigest(id: number) {
    setError(null)
    try {
      setCurrent(await api.get<DigestRow>(`/api/digests/${id}`))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load digest')
    }
  }

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-8 px-4 py-6">
      <section className="flex flex-col gap-1">
        <h1 className="text-lg font-semibold text-slate-100">Digest</h1>
        <p className="text-sm text-slate-400">
          Distill a category or a search into one Markdown doc — a reading list, themed notes, a link
          list. Extract per batch, then one merge pass, with <code>[[item:id]]</code> citations
          resolved against your library.
        </p>
      </section>

      <section className="flex flex-col gap-3">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">Template</h2>
        <select className="input" value={template} onChange={(e) => setTemplate(e.target.value)}>
          {templates.map((t) => (
            <option key={t.name} value={t.name}>
              {t.name}
              {t.source === 'user' ? ' (custom)' : ''}
            </option>
          ))}
        </select>
        {selected && (
          <div className="flex flex-col gap-2 text-xs text-slate-500">
            <p>{selected.description}</p>
            <button
              type="button"
              className="w-fit text-accent no-underline hover:underline"
              onClick={() => setShowPrompts((v) => !v)}
            >
              {showPrompts ? 'Hide' : 'Show'} prompts
            </button>
            {showPrompts && (
              <div className="card flex flex-col gap-2 px-3 py-2">
                <p>
                  <span className="text-slate-400">Extract:</span> {selected.extract_prompt}
                </p>
                <p>
                  <span className="text-slate-400">Reduce:</span> {selected.reduce_prompt}
                </p>
              </div>
            )}
          </div>
        )}
      </section>

      <section className="flex flex-col gap-3">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">Selection</h2>
        <label className="flex flex-col gap-1 text-sm text-slate-300">
          Category
          <select className="input" value={category} onChange={(e) => setCategory(e.target.value)}>
            <option value="">— none —</option>
            {categories?.categories.map((c) => (
              <option key={c.id ?? c.name} value={c.name}>
                {c.name} ({c.count})
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-sm text-slate-300">
          Search query (optional — narrows to the semantic-search hits)
          <input
            className="input"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="e.g. nervous system regulation"
          />
        </label>
        <label className="flex flex-col gap-1 text-sm text-slate-300">
          Name (optional)
          <input
            className="input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="defaults to the template + item count"
          />
        </label>
      </section>

      <section className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center gap-3">
          <button
            type="button"
            className="btn-secondary w-fit"
            disabled={!hasSelection || preflighting || busy}
            onClick={() => void runPreflight()}
          >
            {preflighting ? 'Estimating…' : 'Estimate'}
          </button>
          <button
            type="button"
            className="btn-primary w-fit"
            disabled={!hasSelection || busy}
            onClick={() => void run()}
          >
            {busy ? 'Running…' : 'Generate'}
          </button>
        </div>

        {preflight && (
          <div className="card px-4 py-3 text-sm text-slate-300">
            <p>
              <span className="font-semibold text-slate-100">{preflight.item_count}</span> items ·{' '}
              {preflight.batches} extract {preflight.batches === 1 ? 'batch' : 'batches'} + 1 merge
            </p>
            <p className="text-xs text-slate-500">
              ~{(preflight.estimated_tokens_in + preflight.estimated_tokens_out).toLocaleString()}{' '}
              tokens · {formatCost(preflight.estimated_cost)} · {preflight.provider} / {preflight.model}
            </p>
          </div>
        )}

        {busy && jobBar && (
          <div className="card px-4 py-3">
            <div className="mb-1 flex justify-between text-xs text-slate-400">
              <span>
                {jobBar.step}/{jobBar.total || '?'}
              </span>
              <span>
                {jobBar.total ? Math.round((jobBar.step / jobBar.total) * 100) : 0}%
              </span>
            </div>
            <div className="h-2 overflow-hidden rounded-full bg-surface-overlay">
              <div
                className="h-full bg-accent transition-all"
                style={{ width: `${jobBar.total ? (jobBar.step / jobBar.total) * 100 : 5}%` }}
              />
            </div>
          </div>
        )}

        {error && (
          <p className="card border-red-900/60 bg-red-950/40 px-4 py-2 text-sm text-red-300">{error}</p>
        )}
      </section>

      {current && (
        <section className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h2 className="text-sm font-semibold text-slate-200">{current.name}</h2>
            <div className="flex items-center gap-3 text-xs text-slate-500">
              <span className={STATUS_STYLES[current.status] ?? ''}>{current.status}</span>
              {current.markdown && (
                <button
                  type="button"
                  className="text-accent no-underline hover:underline"
                  onClick={() => downloadMarkdown(current.name, current.markdown ?? '')}
                >
                  Download .md
                </button>
              )}
            </div>
          </div>
          {current.model && (
            <p className="text-xs text-slate-500">
              {current.provider} / {current.model} ·{' '}
              {(current.tokens_in + current.tokens_out).toLocaleString()} tokens ·{' '}
              {formatCost(current.cost_estimate)}
            </p>
          )}
          {current.error_message && <p className="text-sm text-red-300">{current.error_message}</p>}
          {current.markdown && (
            <pre className="card max-h-[32rem] overflow-auto whitespace-pre-wrap px-4 py-3 text-xs text-slate-200">
              {current.markdown}
            </pre>
          )}
        </section>
      )}

      {history.length > 0 && (
        <section className="flex flex-col gap-2">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">History</h2>
          <div className="flex flex-col gap-1">
            {history.map((d) => (
              <button
                key={d.id}
                type="button"
                className="card flex items-center justify-between gap-3 px-3 py-2 text-left text-sm hover:border-accent/60"
                onClick={() => d.id != null && void openDigest(d.id)}
              >
                <span className="min-w-0 flex-1 truncate text-slate-200">{d.name}</span>
                <span className="shrink-0 text-xs text-slate-500">{d.template}</span>
                <span className={`shrink-0 text-xs ${STATUS_STYLES[d.status] ?? ''}`}>{d.status}</span>
                <span className="shrink-0 text-xs text-slate-600">{formatDate(d.created_at)}</span>
              </button>
            ))}
          </div>
        </section>
      )}
    </div>
  )
}
