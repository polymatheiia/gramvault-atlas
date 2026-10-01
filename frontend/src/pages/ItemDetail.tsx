import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { api, isNotFound, mediaUrl } from '../api/client'
import { CaptionTrack } from '../components/CaptionTrack'
import { ItemCard } from '../components/ItemCard'
import { TagBadge } from '../components/TagBadge'
import { filterSearch, useSiblingIds } from '../lib/gallery'
import { useToast } from '../lib/toast'
import type {
  CategoryListResponse,
  CategoryWithCount,
  EnrichmentRunResponse,
  EnrichSteps,
  Item,
  MediaFile,
} from '../types'

const ENRICHMENT_LABEL: Record<Item['enrichment_status'], string> = {
  pending: 'Enrichment pending',
  running: 'Enrichment running…',
  done: 'Enriched',
  failed: 'Enrichment failed',
}

const DEFAULT_RERUN_STEPS: EnrichSteps = {
  transcribe: true,
  ocr: true,
  vision_caption: true,
  embed: true,
}

function formatDate(iso: string | null): string {
  if (!iso) return 'Unknown date'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleString(undefined, { year: 'numeric', month: 'long', day: 'numeric' })
}

async function copyToClipboard(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text)
    return true
  } catch {
    return false
  }
}

function MediaBlock({ file, onCopyTranscript }: { file: MediaFile; onCopyTranscript: (text: string) => void }) {
  return (
    <div className="flex flex-col gap-2">
      <div className="overflow-hidden rounded-lg bg-black">
        {file.media_type === 'video' ? (
          <video src={mediaUrl(file.file_path)} controls className="max-h-[70vh] w-full">
            {file.id != null && file.transcript && (
              <CaptionTrack mediaFileId={file.id} />
            )}
          </video>
        ) : (
          <img src={mediaUrl(file.file_path)} alt="" className="max-h-[70vh] w-full object-contain" />
        )}
      </div>
      {file.vision_caption && (
        <p className="card px-3 py-2 text-sm text-slate-300">
          <span className="label mr-2">AI caption</span>
          {file.vision_caption}
          {file.vision_model && <span className="ml-2 text-xs text-slate-500">({file.vision_model})</span>}
        </p>
      )}
      {file.ocr_text && (
        <p className="card px-3 py-2 text-sm text-slate-300">
          <span className="label mr-2">On-screen text</span>
          {file.ocr_text}
          {file.ocr_model && <span className="ml-2 text-xs text-slate-500">({file.ocr_model})</span>}
        </p>
      )}
      {file.transcript && (
        <p className="card flex items-start justify-between gap-3 px-3 py-2 text-sm text-slate-300">
          <span>
            <span className="label mr-2">Transcript</span>
            {file.transcript}
            {file.transcript_model && <span className="ml-2 text-xs text-slate-500">({file.transcript_model})</span>}
          </span>
          <button
            type="button"
            className="btn-secondary shrink-0"
            onClick={() => onCopyTranscript(file.transcript ?? '')}
          >
            Copy
          </button>
        </p>
      )}
    </div>
  )
}

export function ItemDetail() {
  const { id } = useParams<{ id: string }>()
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const { push } = useToast()
  const [item, setItem] = useState<Item | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const { ids: siblingIds } = useSiblingIds(searchParams)
  const ctx = filterSearch(searchParams)
  const { prevId, nextId, position } = useMemo(() => {
    const idx = id ? siblingIds.indexOf(Number(id)) : -1
    if (idx === -1) return { prevId: null, nextId: null, position: null }
    return {
      prevId: idx > 0 ? siblingIds[idx - 1] : null,
      nextId: idx < siblingIds.length - 1 ? siblingIds[idx + 1] : null,
      position: `${idx + 1} / ${siblingIds.length}`,
    }
  }, [id, siblingIds])

  const [showShortcuts, setShowShortcuts] = useState(false)
  const [categoryFocusRequest, setCategoryFocusRequest] = useState(0)

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.target instanceof HTMLElement && ['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName)) return
      if (e.key === 'ArrowLeft' && prevId != null) navigate(`/items/${prevId}${ctx}`)
      if (e.key === 'ArrowRight' && nextId != null) navigate(`/items/${nextId}${ctx}`)
      if (e.key === 'c') setCategoryFocusRequest((n) => n + 1)
      if (e.key === 'f' && item) void saveMeta({ favourite: !item.favourite })
      if (e.key === '?') setShowShortcuts((v) => !v)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [prevId, nextId, ctx, navigate, item])

  const [tagInput, setTagInput] = useState('')
  const [savingTags, setSavingTags] = useState(false)

  const [categories, setCategories] = useState<CategoryWithCount[]>([])
  const [savingCategory, setSavingCategory] = useState(false)
  const [categorySelect, setCategorySelect] = useState<HTMLSelectElement | null>(null)

  useEffect(() => {
    if (categoryFocusRequest > 0) categorySelect?.focus()
  }, [categoryFocusRequest, categorySelect])

  const [rerunOpen, setRerunOpen] = useState(false)
  const [rerunSteps, setRerunSteps] = useState<EnrichSteps>(DEFAULT_RERUN_STEPS)
  const [rerunning, setRerunning] = useState(false)

  const [similarOpen, setSimilarOpen] = useState(false)
  const [similarItems, setSimilarItems] = useState<Item[] | null>(null)
  const [loadingSimilar, setLoadingSimilar] = useState(false)

  const [deleting, setDeleting] = useState(false)

  const [noteInput, setNoteInput] = useState('')
  const [savingMeta, setSavingMeta] = useState(false)

  useEffect(() => {
    api
      .get<CategoryListResponse>('/api/library/categories')
      .then((r) => setCategories(r.categories))
      .catch(() => setCategories([]))
  }, [])

  useEffect(() => {
    if (!id) return
    const controller = new AbortController()
    setLoading(true)
    setError(null)
    api
      .get<Item>(`/api/library/items/${id}`, undefined, controller.signal)
      .then((i) => {
        setItem(i)
        setNoteInput(i.user_note ?? '')
      })
      .catch((err) => {
        if (controller.signal.aborted) return
        setError(isNotFound(err) ? 'Item not found.' : err instanceof Error ? err.message : 'Failed to load item')
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
    return () => controller.abort()
  }, [id])

  async function saveTags(nextManualTags: string[]) {
    if (!id) return
    setSavingTags(true)
    try {
      const updated = await api.patch<Item>(`/api/library/items/${id}/tags`, { tags: nextManualTags })
      setItem(updated)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update tags')
    } finally {
      setSavingTags(false)
    }
  }

  function addTag(e: FormEvent) {
    e.preventDefault()
    const name = tagInput.trim()
    if (!name || !item) return
    const manual = item.tags.filter((t) => t.kind === 'manual').map((t) => t.name)
    if (manual.includes(name)) return
    setTagInput('')
    void saveTags([...manual, name])
  }

  function removeTag(name: string) {
    if (!item) return
    const manual = item.tags.filter((t) => t.kind === 'manual' && t.name !== name).map((t) => t.name)
    void saveTags(manual)
  }

  async function saveMeta(body: { favourite?: boolean; user_note?: string }) {
    if (!id) return
    setSavingMeta(true)
    try {
      const updated = await api.patch<Item>(`/api/library/items/${id}/meta`, body)
      setItem(updated)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save')
    } finally {
      setSavingMeta(false)
    }
  }

  async function saveCategory(categoryId: number | null) {
    if (!id) return
    setSavingCategory(true)
    try {
      const updated = await api.patch<Item>(`/api/library/items/${id}`, { category_id: categoryId })
      setItem(updated)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update category')
    } finally {
      setSavingCategory(false)
    }
  }

  /** The classifier's suggestion is already on the item — "confirming" it
   * just re-PATCHes the same category_id, which the backend always marks
   * `source: 'manual'` (see routes_library.update_item_category). */
  function confirmCategory() {
    if (item?.category_id != null) void saveCategory(item.category_id)
  }

  async function copyTranscript(text: string) {
    const ok = await copyToClipboard(text)
    push({ tone: ok ? 'success' : 'error', message: ok ? 'Transcript copied.' : 'Could not copy to clipboard.' })
  }

  async function runEnrichment() {
    if (!id) return
    setRerunning(true)
    try {
      const res = await api.post<EnrichmentRunResponse>('/api/enrich/run', {
        scope: { item_ids: [Number(id)], only_missing: false },
        steps: rerunSteps,
        ocr_scope: 'all_media',
      })
      if (res.queued_count === 0) {
        push({ tone: 'info', message: 'Nothing to run — no step was selected.' })
      } else {
        push({ tone: 'info', message: 'Enrichment queued — watch the strip above for progress.', href: '/enrich' })
        setRerunOpen(false)
      }
    } catch (err) {
      push({ tone: 'error', message: err instanceof Error ? err.message : 'Failed to queue enrichment' })
    } finally {
      setRerunning(false)
    }
  }

  async function loadSimilar() {
    if (!id) return
    setSimilarOpen((open) => !open)
    if (similarItems != null) return
    setLoadingSimilar(true)
    try {
      setSimilarItems(await api.get<Item[]>(`/api/library/items/${id}/similar`))
    } catch {
      setSimilarItems([])
    } finally {
      setLoadingSimilar(false)
    }
  }

  async function deleteItem() {
    if (!id || !item) return
    if (!confirm('Delete this item from the library? The underlying media file is not removed from disk.')) return
    setDeleting(true)
    try {
      await api.delete(`/api/library/items/${id}`)
      push({ tone: 'success', message: 'Item deleted.' })
      navigate(`/${ctx}`)
    } catch (err) {
      push({ tone: 'error', message: err instanceof Error ? err.message : 'Failed to delete item' })
      setDeleting(false)
    }
  }

  if (loading) return <p className="px-4 py-6 text-sm text-slate-500">Loading…</p>
  if (error) return <p className="mx-auto max-w-3xl px-4 py-6 text-sm text-red-300">{error}</p>
  if (!item) return null

  const manualTags = item.tags.filter((t) => t.kind === 'manual')
  const otherTags = item.tags.filter((t) => t.kind !== 'manual')

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-6 px-4 py-6">
      <div className="flex items-center justify-between gap-3">
        <Link to={`/${ctx}`} className="text-sm text-accent no-underline hover:underline">
          ← Back to gallery
        </Link>
        <div className="flex items-center gap-2 text-sm">
          {position && <span className="text-slate-500">{position}</span>}
          <button
            type="button"
            className={`btn-secondary ${item.favourite ? 'text-amber-300' : ''}`}
            title="Favourite (f)"
            aria-label={item.favourite ? 'Remove favourite' : 'Add favourite'}
            aria-pressed={item.favourite}
            disabled={savingMeta}
            onClick={() => void saveMeta({ favourite: !item.favourite })}
          >
            {item.favourite ? '★' : '☆'}
          </button>
          {prevId != null ? (
            <Link to={`/items/${prevId}${ctx}`} className="btn-secondary" title="Previous (←)" aria-label="Previous item">
              ←
            </Link>
          ) : (
            <span className="btn-secondary opacity-40" aria-hidden="true">
              ←
            </span>
          )}
          {nextId != null ? (
            <Link to={`/items/${nextId}${ctx}`} className="btn-secondary" title="Next (→)" aria-label="Next item">
              →
            </Link>
          ) : (
            <span className="btn-secondary opacity-40" aria-hidden="true">
              →
            </span>
          )}
          <Link to={`/feed${ctx}`} className="btn-secondary" title="Open the full-screen feed" aria-label="Open full-screen feed">
            ▶ Feed
          </Link>
          <button
            type="button"
            className="btn-secondary"
            title="Keyboard shortcuts (?)"
            aria-label="Keyboard shortcuts"
            onClick={() => setShowShortcuts((v) => !v)}
          >
            ?
          </button>
        </div>
      </div>

      {showShortcuts && (
        <div className="card flex flex-wrap gap-x-6 gap-y-1 px-4 py-3 text-sm text-slate-400">
          <span><kbd className="badge bg-surface-overlay">←</kbd> / <kbd className="badge bg-surface-overlay">→</kbd> previous / next item</span>
          <span><kbd className="badge bg-surface-overlay">c</kbd> focus category picker</span>
          <span><kbd className="badge bg-surface-overlay">f</kbd> toggle favourite</span>
          <span><kbd className="badge bg-surface-overlay">?</kbd> toggle this panel</span>
        </div>
      )}

      <div className="flex flex-col gap-4">
        {item.media_files.length === 0 ? (
          <p className="card px-4 py-6 text-center text-sm text-slate-500">No media files on this item.</p>
        ) : (
          item.media_files.map((file) => (
            <MediaBlock key={file.id ?? file.file_path} file={file} onCopyTranscript={(text) => void copyTranscript(text)} />
          ))
        )}
      </div>

      <div className="card flex flex-col gap-3 px-4 py-4">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <span className="badge bg-surface-overlay text-slate-300">{item.media_type}</span>
            <span className="badge bg-surface-overlay text-slate-400">{ENRICHMENT_LABEL[item.enrichment_status]}</span>
          </div>
          {item.permalink && (
            <a href={item.permalink} target="_blank" rel="noreferrer" className="btn-secondary">
              View on Instagram
            </a>
          )}
        </div>

        {item.caption && <p className="whitespace-pre-wrap text-sm text-slate-100">{item.caption}</p>}

        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="label">Category</span>
          <select
            ref={setCategorySelect}
            className="input w-auto"
            value={item.category_id ?? ''}
            disabled={savingCategory}
            onChange={(e) => void saveCategory(e.target.value ? Number(e.target.value) : null)}
          >
            <option value="">— none —</option>
            {categories.map((c) => (
              <option key={c.id ?? c.name} value={c.id ?? ''}>
                {c.name}
              </option>
            ))}
          </select>
          {item.category_source && item.category_source !== 'manual' && (
            <>
              <span className="text-xs text-slate-500">
                auto ({item.category_source}
                {item.category_confidence != null && `, ${Math.round(item.category_confidence * 100)}%`})
              </span>
              {item.category_id != null && (
                <button type="button" className="btn-secondary" disabled={savingCategory} onClick={confirmCategory}>
                  Confirm
                </button>
              )}
            </>
          )}
        </div>
        {item.category_reason && item.category_source !== 'manual' && (
          <p className="text-xs text-slate-500">
            <span className="label mr-2">Why</span>
            {item.category_reason}
          </p>
        )}

        <div className="flex items-center justify-between text-sm text-slate-400">
          <span>
            {item.author ? (
              item.author.profile_url ? (
                <a href={item.author.profile_url} target="_blank" rel="noreferrer" className="text-accent no-underline hover:underline">
                  @{item.author.username}
                </a>
              ) : (
                `@${item.author.username}`
              )
            ) : (
              'Unknown author'
            )}
          </span>
          <span>{formatDate(item.taken_at)}</span>
        </div>

        <div className="flex flex-col gap-2 border-t border-surface-border pt-3">
          <span className="label">Tags</span>
          <div className="flex flex-wrap gap-1.5">
            {otherTags.map((t) => (
              <TagBadge key={`${t.kind}-${t.name}`} tag={t} />
            ))}
            {manualTags.map((t) => (
              <span key={`manual-${t.name}`} className="badge bg-accent-soft text-accent">
                {t.name}
                <button
                  type="button"
                  aria-label={`Remove tag ${t.name}`}
                  className="ml-1.5 text-accent/70 hover:text-accent"
                  onClick={() => removeTag(t.name)}
                  disabled={savingTags}
                >
                  ×
                </button>
              </span>
            ))}
            {otherTags.length === 0 && manualTags.length === 0 && (
              <span className="text-sm text-slate-500">No tags yet.</span>
            )}
          </div>
          <form onSubmit={addTag} className="flex gap-2">
            <input
              className="input"
              placeholder="Add a manual tag…"
              value={tagInput}
              onChange={(e) => setTagInput(e.target.value)}
              disabled={savingTags}
            />
            <button type="submit" className="btn-secondary" disabled={savingTags || !tagInput.trim()}>
              Add
            </button>
          </form>
        </div>

        <div className="flex flex-col gap-2 border-t border-surface-border pt-3">
          <span className="label">Note</span>
          <textarea
            className="input min-h-16"
            placeholder="Private note — not shared with the model, exported into your Obsidian note's own tail"
            value={noteInput}
            onChange={(e) => setNoteInput(e.target.value)}
            onBlur={() => {
              if (noteInput !== (item.user_note ?? '')) void saveMeta({ user_note: noteInput })
            }}
            disabled={savingMeta}
          />
        </div>
      </div>

      <div className="card flex flex-col gap-3 px-4 py-4">
        <span className="label">Actions</span>
        <div className="flex flex-wrap gap-2">
          <button type="button" className="btn-secondary" onClick={() => setRerunOpen((v) => !v)}>
            Re-run enrichment
          </button>
          <button type="button" className="btn-secondary" onClick={() => void loadSimilar()}>
            Find similar
          </button>
          <button
            type="button"
            className="btn-secondary text-red-300 hover:text-red-200"
            onClick={() => void deleteItem()}
            disabled={deleting}
          >
            Delete item
          </button>
        </div>

        {rerunOpen && (
          <div className="flex flex-col gap-2 border-t border-surface-border pt-3">
            <div className="flex flex-wrap gap-3 text-sm text-slate-300">
              {(
                [
                  ['transcribe', 'Transcribe audio'],
                  ['ocr', 'On-screen text (OCR)'],
                  ['vision_caption', 'Describe frames (vision)'],
                  ['embed', 'Re-embed for search'],
                ] as const
              ).map(([key, label]) => (
                <label key={key} className="flex items-center gap-1.5">
                  <input
                    type="checkbox"
                    checked={rerunSteps[key]}
                    onChange={(e) => setRerunSteps((s) => ({ ...s, [key]: e.target.checked }))}
                  />
                  {label}
                </label>
              ))}
            </div>
            <div>
              <button
                type="button"
                className="btn-primary"
                disabled={rerunning || !Object.values(rerunSteps).some(Boolean)}
                onClick={() => void runEnrichment()}
              >
                {rerunning ? 'Queuing…' : 'Run'}
              </button>
            </div>
          </div>
        )}

        {similarOpen && (
          <div className="flex flex-col gap-2 border-t border-surface-border pt-3">
            <span className="label">Similar items</span>
            {loadingSimilar ? (
              <p className="text-sm text-slate-500">Loading…</p>
            ) : !similarItems || similarItems.length === 0 ? (
              <p className="text-sm text-slate-500">
                {item.enrichment_status === 'done'
                  ? 'No similar items found.'
                  : "Nothing yet — this item hasn't been embedded for search."}
              </p>
            ) : (
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
                {similarItems.map((similar) => (
                  <ItemCard key={similar.id} item={similar} />
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
