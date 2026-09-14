import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { ApiError, api, mediaUrl } from '../api/client'
import type {
  CategorizeMethod,
  CategorizeProgress,
  CategorizeRunRequest,
  CategorizeRunResponse,
  CategorizeScopeName,
  CategoryListResponse,
  Item,
  ItemListResponse,
  Job,
  ModelsOverview,
} from '../types'

const METHODS: { value: CategorizeMethod; label: string; hint: string }[] = [
  {
    value: 'keyword_then_llm',
    label: 'Keyword vote, then LLM on the unsure ones (recommended)',
    hint: 'Free deterministic pass, then the model only re-checks low-confidence guesses',
  },
  {
    value: 'keyword',
    label: 'Keyword vote only',
    hint: 'Instant and offline — hashtag/keyword scoring, no model needed',
  },
  {
    value: 'llm',
    label: 'LLM on every item',
    hint: 'Most accurate, slowest, and costs tokens on a hosted provider',
  },
]

const SCOPES: { value: CategorizeScopeName; label: string }[] = [
  { value: 'uncategorized', label: 'Items with no category yet' },
  { value: 'needs_review', label: 'Re-run the low-confidence ones (review queue)' },
  { value: 'all', label: 'Every item (skips anything set by hand)' },
]

const REVIEW_PAGE_SIZE = 24

function Bar({ done, total }: { done: number; total: number }) {
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

function ReviewCard({
  item,
  categories,
  focused,
  keyHint,
  onFocusCard,
  onReassign,
  onConfirm,
}: {
  item: Item
  categories: CategoryListResponse['categories']
  focused: boolean
  keyHint: number | null
  onFocusCard: () => void
  onReassign: (item: Item, categoryId: number) => Promise<void>
  onConfirm: (item: Item) => Promise<void>
}) {
  const [saving, setSaving] = useState(false)
  const first = item.media_files[0]
  const confidence = item.category_confidence != null ? `${Math.round(item.category_confidence * 100)}%` : '—'

  return (
    <div
      className={`card flex flex-col overflow-hidden ${focused ? 'ring-2 ring-accent' : ''}`}
      onClick={onFocusCard}
      onFocus={onFocusCard}
      tabIndex={0}
    >
      <div className="relative aspect-square w-full overflow-hidden bg-surface-overlay">
        {keyHint != null && (
          <span className="absolute left-1.5 top-1.5 z-10 badge bg-black/60 text-slate-200">{keyHint}</span>
        )}
        <Link to={`/items/${item.id}`} className="block h-full w-full">
          {first ? (
            first.media_type === 'video' ? (
              <video src={mediaUrl(first.file_path)} className="h-full w-full object-cover" muted preload="metadata" />
            ) : (
              <img src={mediaUrl(first.file_path)} alt="" className="h-full w-full object-cover" loading="lazy" />
            )
          ) : (
            <div className="flex h-full w-full items-center justify-center text-xs text-slate-600">No media</div>
          )}
        </Link>
      </div>
      <div className="flex flex-1 flex-col gap-2 p-3">
        <p className="line-clamp-2 text-xs text-slate-300">
          {item.caption || <span className="text-slate-500">No caption</span>}
        </p>
        <p className="text-xs text-slate-500">
          <span className="text-slate-400">{item.category ?? 'uncategorized'}</span>
          {' · '}
          {item.category_source ?? 'auto'} {confidence}
        </p>
        {item.category_reason && (
          <p className="line-clamp-2 text-xs italic text-slate-500">{item.category_reason}</p>
        )}
        <div className="mt-auto flex gap-1.5">
          <select
            className="input flex-1 text-xs"
            value={item.category_id ?? ''}
            disabled={saving}
            onChange={(e) => {
              const id = Number(e.target.value)
              if (!id || id === item.category_id) return
              setSaving(true)
              onReassign(item, id).catch(() => setSaving(false))
            }}
          >
            <option value="">— uncategorized —</option>
            {categories.map((c) => (
              <option key={c.id ?? c.name} value={c.id ?? ''}>
                {c.name}
              </option>
            ))}
          </select>
          {item.category_id != null && (
            <button
              type="button"
              className="btn-secondary shrink-0 text-xs"
              disabled={saving}
              title="Accept the suggestion (Enter)"
              onClick={() => {
                setSaving(true)
                onConfirm(item).catch(() => setSaving(false))
              }}
            >
              ✓
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

export function Categorize() {
  const [progress, setProgress] = useState<CategorizeProgress | null>(null)
  const [categories, setCategories] = useState<CategoryListResponse | null>(null)
  const [models, setModels] = useState<ModelsOverview | null>(null)

  const [method, setMethod] = useState<CategorizeMethod>('keyword_then_llm')
  const [scope, setScope] = useState<CategorizeScopeName>('uncategorized')

  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [jobBar, setJobBar] = useState<{ done: number; total: number } | null>(null)

  const [review, setReview] = useState<Item[]>([])
  const [reviewTotal, setReviewTotal] = useState(0)
  const [reviewPage, setReviewPage] = useState(1)
  const [focusedIndex, setFocusedIndex] = useState(0)
  const [acceptThreshold, setAcceptThreshold] = useState(0.5)
  const [bulkAccepting, setBulkAccepting] = useState(false)
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const refreshProgress = useCallback(async () => {
    try {
      const p = await api.get<CategorizeProgress>('/api/categorize/progress')
      setProgress(p)
      return p
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load progress')
      return null
    }
  }, [])

  const refreshReview = useCallback((page = reviewPage) => {
    api
      .get<ItemListResponse>('/api/library/items', { needs_review: 1, page, page_size: REVIEW_PAGE_SIZE })
      .then((res) => {
        setReview(res.items)
        setReviewTotal(res.total)
        setFocusedIndex(0)
      })
      .catch(() => undefined)
  }, [reviewPage])

  function goToReviewPage(page: number) {
    setReviewPage(page)
    refreshReview(page)
  }

  useEffect(() => {
    void refreshProgress()
    refreshReview()
    api.get<CategoryListResponse>('/api/library/categories').then(setCategories).catch(() => undefined)
    api.get<ModelsOverview>('/api/models').then(setModels).catch(() => undefined)
  }, [refreshProgress, refreshReview])

  // Poll while a job is active.
  useEffect(() => {
    const jobId = progress?.job_id ?? null
    const active = busy || jobId != null
    if (!active) {
      if (pollRef.current) clearTimeout(pollRef.current)
      setJobBar(null)
      return
    }
    pollRef.current = setTimeout(async () => {
      const p = await refreshProgress()
      const id = p?.job_id ?? jobId
      if (id != null) {
        try {
          const job = await api.get<Job>(`/api/jobs/${id}`)
          const prog = job.progress as { done?: number; total?: number } | null
          if (prog && typeof prog.total === 'number') {
            setJobBar({ done: prog.done ?? 0, total: prog.total })
          }
        } catch {
          /* non-fatal */
        }
      }
      if (p && p.job_id == null) {
        setBusy(false)
        setJobBar(null)
        refreshReview()
        try {
          const jobs = await api.get<Job[]>('/api/jobs', { kind: 'categorize', limit: 1 })
          const last = jobs[0]
          if (last && (last.status === 'failed' || last.status === 'cancelled')) {
            setError(last.error_message || `Categorize ${last.status}.`)
          }
        } catch {
          /* non-fatal */
        }
      }
    }, 2000)
    return () => {
      if (pollRef.current) clearTimeout(pollRef.current)
    }
  }, [busy, progress, refreshProgress, refreshReview])

  async function run() {
    setError(null)
    setBusy(true)
    const body: CategorizeRunRequest = { method, scope }
    try {
      const res = await api.post<CategorizeRunResponse>('/api/categorize/run', body)
      if (res.queued_count === 0) {
        setBusy(false)
        setError('Nothing matched that scope.')
      }
      await refreshProgress()
    } catch (err) {
      setBusy(false)
      if (err instanceof ApiError && err.status === 503) {
        setError(
          typeof err.detail === 'string'
            ? `${err.detail} — check the categorize model in Settings.`
            : 'The categorize model is not ready — check Settings.',
        )
      } else {
        setError(err instanceof Error ? err.message : 'Failed to start')
      }
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

  const reassign = useCallback(
    async (item: Item, categoryId: number) => {
      try {
        await api.patch<Item>(`/api/library/items/${item.id}`, { category_id: categoryId })
        setReview((list) => list.filter((i) => i.id !== item.id))
        setReviewTotal((n) => Math.max(0, n - 1))
        void refreshProgress()
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to reassign')
        throw err
      }
    },
    [refreshProgress],
  )

  /** "Accept the suggestion" — PATCH with the item's own current
   * category_id, which the backend always marks `source: 'manual'` (same
   * pattern as ItemDetail's Confirm button). */
  const confirmSuggestion = useCallback(
    async (item: Item) => {
      if (item.category_id != null) await reassign(item, item.category_id)
    },
    [reassign],
  )

  async function acceptAllAboveThreshold() {
    const targets = review.filter(
      (i) => i.category_id != null && (i.category_confidence ?? 0) >= acceptThreshold,
    )
    if (targets.length === 0) return
    setBulkAccepting(true)
    setError(null)
    try {
      await Promise.all(targets.map((i) => api.patch(`/api/library/items/${i.id}`, { category_id: i.category_id })))
      const targetIds = new Set(targets.map((i) => i.id))
      setReview((list) => list.filter((i) => !targetIds.has(i.id)))
      setReviewTotal((n) => Math.max(0, n - targets.length))
      void refreshProgress()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Bulk accept failed')
    } finally {
      setBulkAccepting(false)
    }
  }

  const skipFocused = useCallback(() => {
    setFocusedIndex((i) => Math.min(i + 1, Math.max(0, review.length - 1)))
  }, [review.length])

  // Clamp focus back into range after items leave the current page
  // (reassigned, confirmed, or bulk-accepted).
  useEffect(() => {
    setFocusedIndex((i) => Math.min(i, Math.max(0, review.length - 1)))
  }, [review.length])

  // Keyboard triage: 1-9 assigns the nth category, Enter accepts the
  // focused card's suggestion, S skips to the next one without changing
  // anything.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.target instanceof HTMLElement && ['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName)) return
      const item = review[focusedIndex]
      if (!item) return
      if (e.key >= '1' && e.key <= '9') {
        const cat = (categories?.categories ?? [])[Number(e.key) - 1]
        if (cat?.id != null) void reassign(item, cat.id).catch(() => undefined)
      } else if (e.key === 'Enter') {
        void confirmSuggestion(item).catch(() => undefined)
      } else if (e.key.toLowerCase() === 's') {
        skipFocused()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [review, focusedIndex, categories, reassign, confirmSuggestion, skipFocused])

  const usesLlm = method !== 'keyword'
  const categorize = models?.tasks.categorize
  const jobActive = busy || progress?.job_id != null
  const reviewPageCount = Math.max(1, Math.ceil(reviewTotal / REVIEW_PAGE_SIZE))

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-8 px-4 py-6">
      <section className="flex flex-col gap-1">
        <div className="flex items-center justify-between gap-3">
          <h1 className="text-lg font-semibold text-slate-100">Categorize</h1>
          <Link to="/categories" className="text-sm text-accent no-underline hover:underline">
            Manage categories →
          </Link>
        </div>
        <p className="text-sm text-slate-400">
          Sort items into {categories?.categories.length ?? 'the'} categories. The keyword vote is the free
          floor; the LLM pass is where the accuracy comes from. Anything you set by hand is never overwritten.
        </p>
      </section>

      {progress && (
        <section className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          {[
            ['Total', progress.total],
            ['Categorized', progress.categorized],
            ['Uncategorized', progress.uncategorized],
            ['Needs review', progress.needs_review],
          ].map(([label, value]) => (
            <div key={label} className="card flex flex-col px-3 py-2">
              <span className="text-lg font-semibold text-slate-100">{value}</span>
              <span className="text-xs text-slate-500">{label}</span>
            </div>
          ))}
        </section>
      )}

      <section className="flex flex-col gap-3">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">Method</h2>
        {METHODS.map((m) => (
          <label key={m.value} className="card flex items-start gap-3 px-4 py-3">
            <input
              type="radio"
              className="mt-1"
              checked={method === m.value}
              onChange={() => setMethod(m.value)}
            />
            <div className="flex flex-col">
              <span className="text-sm text-slate-200">{m.label}</span>
              <span className="text-xs text-slate-500">{m.hint}</span>
            </div>
          </label>
        ))}
        {usesLlm && (
          <p className="text-xs text-slate-500">
            Categorize model:{' '}
            <span className="text-slate-300">
              {categorize ? `${categorize.provider} / ${categorize.model}` : '…'}
            </span>{' '}
            — <Link to="/settings" className="text-accent no-underline hover:underline">change</Link>
          </p>
        )}
      </section>

      <section className="flex flex-col gap-3">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">Scope</h2>
        <div className="flex flex-col gap-2 text-sm text-slate-300">
          {SCOPES.map((s) => (
            <label key={s.value} className="flex items-center gap-2">
              <input type="radio" checked={scope === s.value} onChange={() => setScope(s.value)} />
              {s.label}
            </label>
          ))}
        </div>
      </section>

      <section className="flex flex-col gap-3">
        <div className="flex items-center gap-3">
          <button type="button" className="btn-primary w-fit" disabled={jobActive} onClick={() => void run()}>
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
        {jobActive && jobBar && (
          <div className="card px-4 py-3">
            <Bar done={jobBar.done} total={jobBar.total} />
          </div>
        )}
        {progress && Object.keys(progress.by_source).length > 0 && (
          <p className="text-xs text-slate-500">
            By source:{' '}
            {Object.entries(progress.by_source)
              .map(([source, n]) => `${n} ${source}`)
              .join(' · ')}
          </p>
        )}
      </section>

      <section className="flex flex-col gap-3">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-500">
          Review queue ({reviewTotal})
        </h2>
        <p className="text-xs text-slate-500">
          Automatic guesses the classifier isn&apos;t confident about. Pick the right category and it&apos;s
          locked in as a manual label.
        </p>
        <p className="text-xs text-slate-500">
          Keyboard: <kbd className="badge bg-surface-overlay">1</kbd>–<kbd className="badge bg-surface-overlay">9</kbd>{' '}
          assign the nth category to the focused card, <kbd className="badge bg-surface-overlay">Enter</kbd> accepts
          its suggestion, <kbd className="badge bg-surface-overlay">S</kbd> skips it. Click a card to focus it.
        </p>

        {review.length > 0 && (
          <div className="card flex flex-wrap items-center gap-3 px-4 py-3">
            <label className="flex items-center gap-2 text-xs text-slate-400">
              Accept all suggestions ≥
              <input
                type="range"
                min={0}
                max={1}
                step={0.05}
                value={acceptThreshold}
                onChange={(e) => setAcceptThreshold(Number(e.target.value))}
              />
              {Math.round(acceptThreshold * 100)}%
            </label>
            <button
              type="button"
              className="btn-secondary text-xs"
              disabled={bulkAccepting}
              onClick={() => void acceptAllAboveThreshold()}
            >
              {bulkAccepting ? 'Accepting…' : 'Accept matching (this page)'}
            </button>
          </div>
        )}

        {review.length === 0 ? (
          <p className="text-sm text-slate-500">Nothing waiting for review.</p>
        ) : (
          <>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
              {review.map((item, idx) => (
                <ReviewCard
                  key={item.id}
                  item={item}
                  categories={categories?.categories ?? []}
                  focused={idx === focusedIndex}
                  keyHint={idx < 9 ? idx + 1 : null}
                  onFocusCard={() => setFocusedIndex(idx)}
                  onReassign={reassign}
                  onConfirm={confirmSuggestion}
                />
              ))}
            </div>
            {reviewPageCount > 1 && (
              <div className="flex items-center justify-between text-xs text-slate-500">
                <button
                  type="button"
                  className="btn-secondary text-xs disabled:opacity-40"
                  disabled={reviewPage <= 1}
                  onClick={() => goToReviewPage(reviewPage - 1)}
                >
                  ← Prev
                </button>
                <span>
                  Page {reviewPage} of {reviewPageCount} — {reviewTotal} total
                </span>
                <button
                  type="button"
                  className="btn-secondary text-xs disabled:opacity-40"
                  disabled={reviewPage >= reviewPageCount}
                  onClick={() => goToReviewPage(reviewPage + 1)}
                >
                  Next →
                </button>
              </div>
            )}
          </>
        )}
      </section>
    </div>
  )
}
