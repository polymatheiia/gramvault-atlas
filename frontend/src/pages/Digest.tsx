import { useCallback, useEffect, useRef, useState } from 'react'
import { useLocation } from 'react-router-dom'
import { ApiError, api } from '../api/client'
import { ChatMarkdown } from '../lib/markdown'
import type {
  CategoryListResponse,
  Digest as DigestRow,
  DigestCreateResponse,
  DigestExportResponse,
  DigestPreflightResponse,
  DigestTemplateInfo,
  DigestTemplateWriteRequest,
  Job,
  ModelsOverview,
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
  const location = useLocation()
  const [templates, setTemplates] = useState<DigestTemplateInfo[]>([])
  const [categories, setCategories] = useState<CategoryListResponse | null>(null)
  const [models, setModels] = useState<ModelsOverview | null>(null)
  const [history, setHistory] = useState<DigestRow[]>([])

  const [template, setTemplate] = useState('')
  const [category, setCategory] = useState('')
  const [query, setQuery] = useState('')
  const [itemIds, setItemIds] = useState<number[]>([])
  const [name, setName] = useState('')
  const [showPrompts, setShowPrompts] = useState(false)
  const [showTemplateEditor, setShowTemplateEditor] = useState(false)

  const [overrideProvider, setOverrideProvider] = useState('')
  const [overrideModel, setOverrideModel] = useState('')

  const [preflight, setPreflight] = useState<DigestPreflightResponse | null>(null)
  const [preflighting, setPreflighting] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [jobBar, setJobBar] = useState<{ step: number; total: number } | null>(null)

  const [current, setCurrent] = useState<DigestRow | null>(null)
  const [exportedPath, setExportedPath] = useState<string | null>(null)
  const [exporting, setExporting] = useState(false)
  const [tick, setTick] = useState(0)
  const activeIdRef = useRef<number | null>(null)
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const selected = templates.find((t) => t.name === template)
  const hasSelection = !!(category || query.trim() || itemIds.length > 0)

  const refreshHistory = useCallback(() => {
    api.get<DigestRow[]>('/api/digests').then(setHistory).catch(() => undefined)
  }, [])

  const refreshTemplates = useCallback(() => {
    api
      .get<DigestTemplateInfo[]>('/api/digests/templates')
      .then((list) => {
        setTemplates(list)
        setTemplate((t) => t || list[0]?.name || '')
      })
      .catch(() => undefined)
  }, [])

  useEffect(() => {
    refreshTemplates()
    api.get<CategoryListResponse>('/api/library/categories').then(setCategories).catch(() => undefined)
    api.get<ModelsOverview>('/api/models').then(setModels).catch(() => undefined)
    refreshHistory()
  }, [refreshHistory, refreshTemplates])

  // A gallery selection ("Digest these") hands off its item ids via
  // router state — take it once, on arrival, rather than re-reading on
  // every render (location.state is stable per navigation but would
  // otherwise keep re-triggering this if it were a dependency alongside
  // other state this effect might reasonably want to read).
  useEffect(() => {
    const ids = (location.state as { itemIds?: number[] } | null)?.itemIds
    if (ids && ids.length > 0) setItemIds(ids)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Reset the estimate whenever the selection changes.
  useEffect(() => {
    setPreflight(null)
  }, [template, category, query, itemIds, overrideProvider, overrideModel])

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

  function selectionPayload() {
    return {
      category: itemIds.length > 0 ? null : category || null,
      query: itemIds.length > 0 ? null : query.trim() || null,
      item_ids: itemIds.length > 0 ? itemIds : null,
      provider: overrideProvider && overrideModel.trim() ? overrideProvider : null,
      model: overrideProvider && overrideModel.trim() ? overrideModel.trim() : null,
    }
  }

  async function runPreflight() {
    if (!template || !hasSelection) return
    setError(null)
    setPreflighting(true)
    try {
      const res = await api.post<DigestPreflightResponse>('/api/digests/preflight', {
        template,
        ...selectionPayload(),
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
    setExportedPath(null)
    try {
      const res = await api.post<DigestCreateResponse>('/api/digests', {
        template,
        ...selectionPayload(),
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
    setExportedPath(null)
    try {
      setCurrent(await api.get<DigestRow>(`/api/digests/${id}`))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load digest')
    }
  }

  async function exportToVault(id: number) {
    setError(null)
    setExporting(true)
    try {
      const res = await api.post<DigestExportResponse>(`/api/digests/${id}/export`)
      setExportedPath(res.path)
    } catch (err) {
      if (err instanceof ApiError && err.status === 400) {
        setError('Configure an Obsidian vault in Settings first.')
      } else {
        setError(err instanceof Error ? err.message : 'Export failed')
      }
    } finally {
      setExporting(false)
    }
  }

  async function deleteDigest(id: number) {
    if (!confirm('Delete this digest?')) return
    await api.delete(`/api/digests/${id}`)
    if (current?.id === id) setCurrent(null)
    refreshHistory()
  }

  async function deleteTemplate(name: string) {
    if (!confirm(`Delete template "${name}"?`)) return
    await api.delete(`/api/digests/templates/${encodeURIComponent(name)}`)
    setTemplate('')
    refreshTemplates()
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
        <div className="flex items-center justify-between gap-2">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">Template</h2>
          <button
            type="button"
            className="text-xs text-accent no-underline hover:underline"
            onClick={() => setShowTemplateEditor((v) => !v)}
          >
            {showTemplateEditor ? 'Close editor' : 'New / edit templates'}
          </button>
        </div>
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
            <div className="flex gap-3">
              <button
                type="button"
                className="w-fit text-accent no-underline hover:underline"
                onClick={() => setShowPrompts((v) => !v)}
              >
                {showPrompts ? 'Hide' : 'Show'} prompts
              </button>
              {selected.source === 'user' && (
                <button
                  type="button"
                  className="w-fit text-red-400 no-underline hover:underline"
                  onClick={() => void deleteTemplate(selected.name)}
                >
                  Delete template
                </button>
              )}
            </div>
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
        {showTemplateEditor && <TemplateEditor onSaved={() => { refreshTemplates(); setShowTemplateEditor(false) }} />}
      </section>

      <section className="flex flex-col gap-3">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">Selection</h2>
        {itemIds.length > 0 ? (
          <div className="card flex items-center justify-between gap-2 px-3 py-2 text-sm text-slate-300">
            <span>
              <span className="font-semibold text-slate-100">{itemIds.length}</span> item(s) from your gallery
              selection
            </span>
            <button type="button" className="btn-ghost text-xs" onClick={() => setItemIds([])}>
              Clear, use filters instead
            </button>
          </div>
        ) : (
          <>
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
          </>
        )}
        <label className="flex flex-col gap-1 text-sm text-slate-300">
          Name (optional)
          <input
            className="input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="defaults to the template + item count"
          />
        </label>
        <label className="flex flex-col gap-1 text-sm text-slate-300">
          Model override (optional — defaults to the template's routed provider/model)
          <div className="flex gap-2">
            <select className="input w-auto" value={overrideProvider} onChange={(e) => setOverrideProvider(e.target.value)}>
              <option value="">— default —</option>
              {models?.providers.map((p) => (
                <option key={p.name} value={p.name}>
                  {p.name}
                </option>
              ))}
            </select>
            <input
              className="input flex-1"
              value={overrideModel}
              onChange={(e) => setOverrideModel(e.target.value)}
              placeholder="model name"
              disabled={!overrideProvider}
            />
          </div>
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
                <>
                  <button
                    type="button"
                    className="text-accent no-underline hover:underline"
                    onClick={() => downloadMarkdown(current.name, current.markdown ?? '')}
                  >
                    Download .md
                  </button>
                  {current.id != null && (
                    <button
                      type="button"
                      className="text-accent no-underline hover:underline disabled:opacity-50"
                      disabled={exporting}
                      onClick={() => void exportToVault(current.id as number)}
                    >
                      {exporting ? 'Exporting…' : 'Export to Obsidian'}
                    </button>
                  )}
                </>
              )}
              {current.id != null && (
                <button
                  type="button"
                  className="text-red-400 no-underline hover:underline"
                  onClick={() => void deleteDigest(current.id as number)}
                >
                  Delete
                </button>
              )}
            </div>
          </div>
          {exportedPath && (
            <p className="text-xs text-emerald-400">Written to {exportedPath}</p>
          )}
          {current.model && (
            <p className="text-xs text-slate-500">
              {current.provider} / {current.model} ·{' '}
              {(current.tokens_in + current.tokens_out).toLocaleString()} tokens ·{' '}
              {formatCost(current.cost_estimate)}
            </p>
          )}
          {current.error_message && <p className="text-sm text-red-300">{current.error_message}</p>}
          {current.markdown && (
            <div className="card max-h-[32rem] overflow-auto px-4 py-3 text-sm text-slate-200">
              <ChatMarkdown content={current.markdown} />
            </div>
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

function TemplateEditor({ onSaved }: { onSaved: () => void }) {
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [extractPrompt, setExtractPrompt] = useState('')
  const [reducePrompt, setReducePrompt] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function save() {
    if (!name.trim() || !extractPrompt.trim() || !reducePrompt.trim()) return
    setSaving(true)
    setError(null)
    try {
      const body: DigestTemplateWriteRequest = {
        name: name.trim(),
        description: description.trim(),
        extract_prompt: extractPrompt.trim(),
        reduce_prompt: reducePrompt.trim(),
      }
      await api.post('/api/digests/templates', body)
      setName('')
      setDescription('')
      setExtractPrompt('')
      setReducePrompt('')
      onSaved()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save template')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="card flex flex-col gap-2 px-4 py-3">
      <p className="text-xs text-slate-500">
        A name matching a built-in template shadows it with your own version — the built-in stays available
        under the same name once you delete your copy.
      </p>
      <label className="flex flex-col gap-1 text-sm text-slate-300">
        Name
        <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="my-template" />
      </label>
      <label className="flex flex-col gap-1 text-sm text-slate-300">
        Description
        <input className="input" value={description} onChange={(e) => setDescription(e.target.value)} />
      </label>
      <label className="flex flex-col gap-1 text-sm text-slate-300">
        Extract prompt
        <textarea
          className="input min-h-24"
          value={extractPrompt}
          onChange={(e) => setExtractPrompt(e.target.value)}
        />
      </label>
      <label className="flex flex-col gap-1 text-sm text-slate-300">
        Reduce prompt
        <textarea
          className="input min-h-24"
          value={reducePrompt}
          onChange={(e) => setReducePrompt(e.target.value)}
        />
      </label>
      {error && <p className="text-sm text-red-300">{error}</p>}
      <button type="button" className="btn-primary w-fit" disabled={saving} onClick={() => void save()}>
        {saving ? 'Saving…' : 'Save template'}
      </button>
    </div>
  )
}
