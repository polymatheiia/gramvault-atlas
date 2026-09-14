import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api/client'
import type {
  CategoryListResponse,
  EnrichmentProgress,
  EnrichmentRunRequest,
  EnrichmentRunResponse,
  EnrichSteps,
  EnrichStepName,
  Job,
  ModelsOverview,
  OcrScope,
} from '../types'

interface Failure {
  item_id: number
  caption: string | null
  error: string | null
}

const STEP_LABELS: Record<EnrichStepName, string> = {
  transcribe: 'Transcribe audio',
  ocr: 'Read on-screen text (OCR)',
  vision_caption: 'Describe frames (vision)',
  embed: 'Embed for search',
}

const STEP_HINT: Record<EnrichStepName, string> = {
  transcribe: 'faster-whisper over videos with no transcript yet',
  ocr: 'the text overlay — recipes, book titles, punchlines',
  vision_caption: "the vision model's guess at what a frame shows",
  embed: 'rebuild the content document and its vector chunks',
}

const OCR_SCOPES: { value: OcrScope; label: string }[] = [
  { value: 'silent_thin_caption', label: 'Silent videos with a thin caption (default)' },
  { value: 'all_silent', label: 'Every silent video' },
  { value: 'all_media', label: 'Every photo, slide and video' },
  { value: 'retry_discarded', label: 'Retry reads discarded by a weaker model' },
]

function Bar({ done, pending }: { done: number; pending: number }) {
  const total = done + pending
  const pct = total > 0 ? Math.round((done / total) * 100) : 0
  return (
    <div>
      <div className="mb-1 flex justify-between text-xs text-slate-400">
        <span>
          {done}/{total}
        </span>
        <span>{pct}%</span>
      </div>
      <div className="h-2 overflow-hidden rounded-full bg-surface-overlay">
        <div className="h-full bg-accent transition-all" style={{ width: `${pct}%` }} />
      </div>
    </div>
  )
}

export function Enrich() {
  const [progress, setProgress] = useState<EnrichmentProgress | null>(null)
  const [categories, setCategories] = useState<CategoryListResponse | null>(null)
  const [models, setModels] = useState<ModelsOverview | null>(null)
  const [failures, setFailures] = useState<Failure[]>([])

  const [steps, setSteps] = useState<EnrichSteps>({
    transcribe: true,
    ocr: true,
    vision_caption: false,
    embed: true,
  })
  const [ocrScope, setOcrScope] = useState<OcrScope>('silent_thin_caption')
  const [scopeMode, setScopeMode] = useState<'all' | 'category'>('all')
  const [category, setCategory] = useState<string>('')
  const [onlyMissing, setOnlyMissing] = useState(true)

  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const refreshProgress = useCallback(async () => {
    try {
      const p = await api.get<EnrichmentProgress>('/api/enrich/progress')
      setProgress(p)
      return p
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load progress')
      return null
    }
  }, [])

  const refreshFailures = useCallback(() => {
    api
      .get<Failure[]>('/api/enrich/failures')
      .then(setFailures)
      .catch(() => undefined)
  }, [])

  useEffect(() => {
    void refreshProgress()
    refreshFailures()
    api
      .get<CategoryListResponse>('/api/library/categories')
      .then(setCategories)
      .catch(() => undefined)
    api
      .get<ModelsOverview>('/api/models')
      .then(setModels)
      .catch(() => undefined)
  }, [refreshProgress, refreshFailures])

  // Poll while a job is active.
  useEffect(() => {
    const active = busy || (progress?.job_id != null)
    if (!active) {
      if (pollRef.current) clearTimeout(pollRef.current)
      return
    }
    pollRef.current = setTimeout(async () => {
      const p = await refreshProgress()
      if (p && p.job_id == null) {
        setBusy(false)
        refreshFailures()
        // The job just ended — surface its outcome if it failed or was cancelled.
        try {
          const jobs = await api.get<Job[]>('/api/jobs', { kind: 'enrich', limit: 1 })
          const last = jobs[0]
          if (last && (last.status === 'failed' || last.status === 'cancelled')) {
            setError(last.error_message || `Enrichment ${last.status}.`)
          }
        } catch {
          /* non-fatal */
        }
      }
    }, 2000)
    return () => {
      if (pollRef.current) clearTimeout(pollRef.current)
    }
  }, [busy, progress, refreshProgress, refreshFailures])

  const anyStep = steps.transcribe || steps.ocr || steps.vision_caption || steps.embed

  async function run(overrideItemIds?: number[]) {
    setError(null)
    setNotice(null)
    setBusy(true)
    const body: EnrichmentRunRequest = {
      scope: {
        item_ids: overrideItemIds ?? null,
        category: overrideItemIds ? null : scopeMode === 'category' ? category || null : null,
        only_missing: overrideItemIds ? false : onlyMissing,
      },
      steps,
      ocr_scope: ocrScope,
    }
    try {
      const res = await api.post<EnrichmentRunResponse>('/api/enrich/run', body)
      if (res.queued_count === 0) {
        setBusy(false)
        setNotice('Nothing matched — every item in scope already has this done.')
      }
      await refreshProgress()
    } catch (err) {
      setBusy(false)
      setError(err instanceof Error ? err.message : 'Failed to start')
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

  const vision = models?.tasks.vision
  const jobActive = busy || progress?.job_id != null

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-8 px-4 py-6">
      <section className="flex flex-col gap-1">
        <h1 className="text-lg font-semibold text-slate-100">Enrichment pipeline</h1>
        <p className="text-sm text-slate-400">
          Each pass is independent and resumable — run OCR today, captions next week. The count
          next to a pass is how many media files still need it.
        </p>
      </section>

      <section className="flex flex-col gap-3">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">Passes</h2>
        {(Object.keys(STEP_LABELS) as EnrichStepName[]).map((name) => {
          const c = progress?.steps[name]
          return (
            <label key={name} className="card flex items-start gap-3 px-4 py-3">
              <input
                type="checkbox"
                className="mt-1"
                checked={steps[name]}
                onChange={(e) => setSteps((s) => ({ ...s, [name]: e.target.checked }))}
              />
              <div className="flex flex-col">
                <span className="text-sm text-slate-200">
                  {STEP_LABELS[name]}
                  {c != null && (
                    <span className="ml-2 text-xs text-slate-500">
                      {c.pending} need this · {c.done} done
                    </span>
                  )}
                </span>
                <span className="text-xs text-slate-500">{STEP_HINT[name]}</span>
                {name === 'ocr' && steps.ocr && (
                  <div className="mt-2 flex flex-col gap-1">
                    <select
                      className="input text-xs"
                      value={ocrScope}
                      onChange={(e) => setOcrScope(e.target.value as OcrScope)}
                    >
                      {OCR_SCOPES.map((o) => (
                        <option key={o.value} value={o.value}>
                          {o.label}
                        </option>
                      ))}
                    </select>
                  </div>
                )}
              </div>
            </label>
          )
        })}
        {(steps.ocr || steps.vision_caption) && (
          <p className="text-xs text-slate-500">
            Vision model:{' '}
            <span className="text-slate-300">
              {vision ? `${vision.provider} / ${vision.model}` : '…'}
            </span>{' '}
            — <Link to="/settings" className="text-accent no-underline hover:underline">change</Link>
          </p>
        )}
      </section>

      <section className="flex flex-col gap-3">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">Scope</h2>
        <div className="flex flex-col gap-2 text-sm text-slate-300">
          <label className="flex items-center gap-2">
            <input
              type="radio"
              checked={scopeMode === 'all'}
              onChange={() => setScopeMode('all')}
            />
            Whole library
          </label>
          <label className="flex items-center gap-2">
            <input
              type="radio"
              checked={scopeMode === 'category'}
              onChange={() => setScopeMode('category')}
            />
            Only category
            <select
              className="input text-xs"
              disabled={scopeMode !== 'category'}
              value={category}
              onChange={(e) => setCategory(e.target.value)}
            >
              <option value="">Choose…</option>
              {categories?.categories.map((c) => (
                <option key={c.id} value={c.name}>
                  {c.name} ({c.count})
                </option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={onlyMissing}
              onChange={(e) => setOnlyMissing(e.target.checked)}
            />
            Skip items that already have every selected pass done
          </label>
        </div>
      </section>

      <section className="flex flex-col gap-3">
        <div className="flex items-center gap-3">
          <button
            type="button"
            className="btn-primary w-fit"
            disabled={jobActive || !anyStep || (scopeMode === 'category' && !category)}
            onClick={() => void run()}
          >
            {jobActive ? 'Running…' : 'Run'}
          </button>
          {jobActive && progress?.job_id != null && (
            <button type="button" className="btn-secondary w-fit" onClick={() => void cancel()}>
              Cancel
            </button>
          )}
        </div>
        {error && (
          <p className="card border-red-900/60 bg-red-950/40 px-4 py-2 text-sm text-red-300">{error}</p>
        )}
        {notice && (
          <p className="card border-accent/40 bg-accent-soft/40 px-4 py-2 text-sm text-slate-200">{notice}</p>
        )}
        {progress && (
          <div className="card flex flex-col gap-3 px-4 py-3">
            {(Object.keys(STEP_LABELS) as EnrichStepName[]).map((name) => (
              <div key={name}>
                <span className="text-xs text-slate-400">{STEP_LABELS[name]}</span>
                <Bar done={progress.steps[name].done} pending={progress.steps[name].pending} />
              </div>
            ))}
            <p className="text-xs text-slate-500">
              Items: {progress.done} done · {progress.pending} pending · {progress.running} running ·{' '}
              {progress.failed} failed
            </p>
          </div>
        )}
      </section>

      {failures.length > 0 && (
        <section className="flex flex-col gap-3">
          <div className="flex items-center justify-between gap-2">
            <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
              Failed ({failures.length})
            </h2>
            <button
              type="button"
              className="btn-secondary text-xs"
              disabled={jobActive}
              onClick={() => void run(failures.map((f) => f.item_id))}
            >
              Retry all
            </button>
          </div>
          <div className="flex flex-col gap-2">
            {failures.map((f) => (
              <div key={f.item_id} className="card flex items-start justify-between gap-3 px-3 py-2 text-sm">
                <div className="flex min-w-0 flex-col">
                  <Link
                    to={`/items/${f.item_id}`}
                    className="truncate text-slate-200 no-underline hover:underline"
                  >
                    {f.caption?.slice(0, 80) || `Item ${f.item_id}`}
                  </Link>
                  {f.error && <span className="text-xs text-red-300">{f.error}</span>}
                </div>
                <button
                  type="button"
                  className="btn-secondary shrink-0 text-xs"
                  disabled={jobActive}
                  onClick={() => void run([f.item_id])}
                >
                  Retry
                </button>
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  )
}
