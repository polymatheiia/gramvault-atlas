import { useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api, isNotImplemented } from '../api/client'
import { ItemCard } from '../components/ItemCard'
import { filterSearch, itemSortOf, searchModeOf, UNCATEGORIZED, type SearchMode } from '../lib/gallery'
import { useToast } from '../lib/toast'
import type {
  Author,
  CategoryListResponse,
  EnrichSteps,
  Item,
  ItemListResponse,
  MediaType,
  SemanticSearchResponse,
  Tag,
} from '../types'

const MEDIA_TYPES: MediaType[] = ['photo', 'video', 'reel', 'carousel']
const PAGE_SIZE = 48
const SEMANTIC_TOP_K_CAP = 100

const DENSITY_KEY = 'gv_gallery_density'
type Density = 'comfortable' | 'compact'

function loadDensity(): Density {
  try {
    return localStorage.getItem(DENSITY_KEY) === 'compact' ? 'compact' : 'comfortable'
  } catch {
    return 'comfortable'
  }
}

const DENSITY_GRID: Record<Density, string> = {
  comfortable: 'grid-cols-2 gap-4 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5',
  compact: 'grid-cols-3 gap-2 sm:grid-cols-4 md:grid-cols-6 lg:grid-cols-8',
}

const BULK_ENRICH_STEPS: EnrichSteps = { transcribe: true, ocr: true, vision_caption: true, embed: true }

export function Gallery() {
  const [searchParams, setSearchParams] = useSearchParams()
  const { push } = useToast()
  const author = searchParams.get('author') ?? ''
  const mediaType = (searchParams.get('media_type') as MediaType | null) ?? ''
  const tag = searchParams.get('tag') ?? ''
  const category = searchParams.get('category') ?? ''
  const dateFrom = searchParams.get('date_from') ?? ''
  const dateTo = searchParams.get('date_to') ?? ''
  const search = searchParams.get('search') ?? ''
  const mode = searchModeOf(searchParams)
  const sort = itemSortOf(searchParams)

  const [searchInput, setSearchInput] = useState(search)
  const [authors, setAuthors] = useState<Author[]>([])
  const [tags, setTags] = useState<Tag[]>([])
  const [categoryData, setCategoryData] = useState<CategoryListResponse | null>(null)

  const [items, setItems] = useState<Item[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [searchScores, setSearchScores] = useState<Map<number, { score: number; snippet: string | null }>>(new Map())

  // Browse/keyword pagination cursor; semantic mode has no offset API, so
  // "load more" there just re-runs with a bigger top_k (see `runQuery`).
  const [page, setPage] = useState(1)
  const [semanticTopK, setSemanticTopK] = useState(PAGE_SIZE)

  const [density, setDensity] = useState<Density>(loadDensity)
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set())
  const [lastClickedIndex, setLastClickedIndex] = useState<number | null>(null)
  const [bulkBusy, setBulkBusy] = useState(false)
  const [bulkTagInput, setBulkTagInput] = useState('')

  useEffect(() => {
    try {
      localStorage.setItem(DENSITY_KEY, density)
    } catch {
      /* private mode / storage disabled — the toggle just won't persist */
    }
  }, [density])

  useEffect(() => {
    api
      .get<Author[]>('/api/library/authors')
      .then(setAuthors)
      .catch(() => setAuthors([]))
    api
      .get<Tag[]>('/api/library/tags')
      .then(setTags)
      .catch(() => setTags([]))
    api
      .get<CategoryListResponse>('/api/library/categories')
      .then(setCategoryData)
      .catch(() => setCategoryData(null))
  }, [])

  // Keep the search box in sync when navigation changes the URL directly.
  useEffect(() => setSearchInput(search), [search])

  // Debounce the search box -> `search` query param.
  useEffect(() => {
    const handle = setTimeout(() => {
      if (searchInput === search) return
      setSearchParams((prev) => {
        const next = new URLSearchParams(prev)
        if (searchInput) next.set('search', searchInput)
        else next.delete('search')
        return next
      })
    }, 350)
    return () => clearTimeout(handle)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchInput])

  function setMode(next: SearchMode) {
    setSearchParams((prev) => {
      const params = new URLSearchParams(prev)
      if (next === 'keyword') params.set('mode', 'keyword')
      else params.delete('mode')
      return params
    })
  }

  async function runQuery(opts: { replace: boolean; page?: number; topK?: number; signal?: AbortSignal }) {
    const signal = opts.signal
    if (opts.replace) setLoading(true)
    else setLoadingMore(true)
    setError(null)

    try {
      if (search && mode === 'semantic') {
        const topK = opts.topK ?? semanticTopK
        const facets: Record<string, string> = {}
        if (author) facets.author = author
        if (mediaType) facets.media_type = mediaType
        if (tag) facets.tag = tag
        if (dateFrom) facets.date_from = dateFrom
        if (dateTo) facets.date_to = dateTo
        // SearchFilters has no sentinel for "uncategorized" the way
        // /items and /item-ids do — filter that case client-side instead.
        if (category && category !== UNCATEGORIZED) facets.category = category

        const res = await api.get<SemanticSearchResponse>(
          '/api/chat/search',
          { q: search, top_k: topK, ...facets },
          signal,
        )
        let results = res.results
        if (category === UNCATEGORIZED) results = results.filter((r) => r.item.category === null)
        setItems(results.map((r) => r.item))
        setTotal(res.total)
        setSearchScores(new Map(results.map((r) => [r.item.id ?? -1, { score: r.score, snippet: r.snippet }])))
      } else {
        const pageNum = opts.page ?? page
        const res = await api.get<ItemListResponse>(
          '/api/library/items',
          {
            author: author || undefined,
            media_type: mediaType || undefined,
            tag: tag || undefined,
            category: category || undefined,
            date_from: dateFrom || undefined,
            date_to: dateTo || undefined,
            q: search || undefined,
            sort,
            page: pageNum,
            page_size: PAGE_SIZE,
          },
          signal,
        )
        setItems((prev) => (opts.replace ? res.items : [...prev, ...res.items]))
        setTotal(res.total)
        setSearchScores(new Map())
      }
    } catch (err) {
      if (signal?.aborted) return
      if (isNotImplemented(err)) setError('This feature is not implemented on the backend yet.')
      else setError(err instanceof Error ? err.message : 'Failed to load items')
      if (opts.replace) {
        setItems([])
        setTotal(0)
      }
    } finally {
      if (!signal?.aborted) {
        setLoading(false)
        setLoadingMore(false)
      }
    }
  }

  // Reset to page 1 / a fresh top_k and refetch whenever the filters,
  // search text, or mode change. Selection is scoped to "whatever's on
  // screen right now", so it's cleared too rather than silently keeping
  // stale ids the new result set doesn't contain.
  useEffect(() => {
    setPage(1)
    setSemanticTopK(PAGE_SIZE)
    setSelectedIds(new Set())
    setLastClickedIndex(null)
    const controller = new AbortController()
    void runQuery({ replace: true, page: 1, topK: PAGE_SIZE, signal: controller.signal })
    return () => controller.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [author, mediaType, tag, category, dateFrom, dateTo, search, mode, sort])

  function loadMore() {
    if (search && mode === 'semantic') {
      const nextTopK = Math.min(SEMANTIC_TOP_K_CAP, semanticTopK + PAGE_SIZE)
      setSemanticTopK(nextTopK)
      void runQuery({ replace: true, topK: nextTopK })
    } else {
      const nextPage = page + 1
      setPage(nextPage)
      void runQuery({ replace: false, page: nextPage })
    }
  }

  const hasMore =
    search && mode === 'semantic'
      ? semanticTopK < SEMANTIC_TOP_K_CAP && items.length >= semanticTopK
      : items.length < total

  const atSemanticCap = search && mode === 'semantic' && semanticTopK >= SEMANTIC_TOP_K_CAP && items.length >= SEMANTIC_TOP_K_CAP

  function updateFilter(key: string, value: string) {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      if (value) next.set(key, value)
      else next.delete(key)
      return next
    })
  }

  const hasFilters = !!(author || mediaType || tag || category || dateFrom || dateTo || search || sort !== 'saved_date')

  function toggleSelect(index: number, item: Item, shiftKey: boolean) {
    if (item.id == null) return
    const id = item.id
    setSelectedIds((prev) => {
      const next = new Set(prev)
      if (shiftKey && lastClickedIndex != null) {
        const [start, end] = [Math.min(lastClickedIndex, index), Math.max(lastClickedIndex, index)]
        for (let i = start; i <= end; i++) {
          const otherId = items[i]?.id
          if (otherId != null) next.add(otherId)
        }
      } else if (next.has(id)) {
        next.delete(id)
      } else {
        next.add(id)
      }
      return next
    })
    setLastClickedIndex(index)
  }

  function clearSelection() {
    setSelectedIds(new Set())
    setLastClickedIndex(null)
  }

  const selectedItems = useMemo(() => items.filter((i) => i.id != null && selectedIds.has(i.id)), [items, selectedIds])

  async function bulkSetCategory(categoryId: number | null) {
    const targets = selectedItems
    if (targets.length === 0) return
    setBulkBusy(true)
    try {
      await Promise.all(
        targets.map((i) => api.patch(`/api/library/items/${i.id}`, { category_id: categoryId })),
      )
      setItems((list) =>
        list.map((i) =>
          i.id != null && selectedIds.has(i.id)
            ? { ...i, category_id: categoryId, category_source: categoryId != null ? 'manual' : null, category_confidence: categoryId != null ? 1 : null }
            : i,
        ),
      )
      push({ tone: 'success', message: `Category set on ${targets.length} item(s).` })
    } catch {
      push({ tone: 'error', message: 'Failed to set category on some items' })
    } finally {
      setBulkBusy(false)
    }
  }

  async function bulkAddTag() {
    const name = bulkTagInput.trim()
    const targets = selectedItems
    if (!name || targets.length === 0) return
    setBulkBusy(true)
    try {
      await Promise.all(
        targets.map((item) => {
          const manual = item.tags.filter((t) => t.kind === 'manual').map((t) => t.name)
          if (manual.includes(name)) return Promise.resolve()
          return api.patch(`/api/library/items/${item.id}/tags`, { tags: [...manual, name] })
        }),
      )
      setBulkTagInput('')
      push({ tone: 'success', message: `Tag "${name}" added to ${targets.length} item(s).` })
    } catch {
      push({ tone: 'error', message: 'Failed to add the tag to some items' })
    } finally {
      setBulkBusy(false)
    }
  }

  async function bulkEnrich() {
    const ids = [...selectedIds]
    if (ids.length === 0) return
    setBulkBusy(true)
    try {
      await api.post('/api/enrich/run', {
        scope: { item_ids: ids, only_missing: false },
        steps: BULK_ENRICH_STEPS,
        ocr_scope: 'all_media',
      })
      push({ tone: 'info', message: `Enrichment queued for ${ids.length} item(s).`, href: '/enrich' })
    } catch (err) {
      push({ tone: 'error', message: err instanceof Error ? err.message : 'Failed to queue enrichment' })
    } finally {
      setBulkBusy(false)
    }
  }

  async function bulkExport() {
    const ids = [...selectedIds]
    if (ids.length === 0) return
    setBulkBusy(true)
    try {
      await api.post('/api/export/obsidian', { item_ids: ids })
      push({ tone: 'info', message: `Export to Obsidian queued for ${ids.length} item(s).` })
    } catch (err) {
      push({ tone: 'error', message: err instanceof Error ? err.message : 'Failed to queue export' })
    } finally {
      setBulkBusy(false)
    }
  }

  return (
    <div className="mx-auto flex max-w-6xl flex-col gap-6 px-4 py-6">
      <div className="flex flex-col gap-3">
        <div className="flex gap-2">
          <input
            className="input"
            placeholder="Search across captions, transcripts, and vision captions…"
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
          />
          {search && (
            <div className="flex shrink-0 overflow-hidden rounded-lg border border-surface-border">
              <button
                type="button"
                className={`px-3 py-1.5 text-xs ${mode === 'semantic' ? 'bg-surface-overlay text-slate-100' : 'text-slate-400 hover:bg-surface-raised'}`}
                onClick={() => setMode('semantic')}
              >
                Semantic
              </button>
              <button
                type="button"
                className={`px-3 py-1.5 text-xs ${mode === 'keyword' ? 'bg-surface-overlay text-slate-100' : 'text-slate-400 hover:bg-surface-raised'}`}
                onClick={() => setMode('keyword')}
              >
                Keyword
              </button>
            </div>
          )}
          <button
            type="button"
            className="btn-secondary shrink-0"
            title={density === 'comfortable' ? 'Switch to compact grid' : 'Switch to comfortable grid'}
            onClick={() => setDensity((d) => (d === 'comfortable' ? 'compact' : 'comfortable'))}
          >
            {density === 'comfortable' ? '⊞' : '⊟'}
          </button>
          <Link
            to={`/feed${filterSearch(searchParams)}`}
            className="btn-secondary shrink-0 whitespace-nowrap"
            title="Scroll through these as a full-screen feed"
          >
            ▶ Feed
          </Link>
        </div>
        <div className="flex flex-wrap gap-2">
          <select
            className="input w-auto"
            value={category}
            onChange={(e) => updateFilter('category', e.target.value)}
          >
            <option value="">All categories</option>
            {categoryData?.categories.map((c) => (
              <option key={c.id ?? c.name} value={c.name}>
                {c.name} ({c.count})
              </option>
            ))}
            {categoryData && categoryData.uncategorized_count > 0 && (
              <option value={UNCATEGORIZED}>Uncategorized ({categoryData.uncategorized_count})</option>
            )}
          </select>
          <select
            className="input w-auto"
            value={mediaType}
            onChange={(e) => updateFilter('media_type', e.target.value)}
          >
            <option value="">All types</option>
            {MEDIA_TYPES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
          <select
            className="input w-auto"
            value={author}
            onChange={(e) => updateFilter('author', e.target.value)}
          >
            <option value="">All authors</option>
            {authors.map((a) => (
              <option key={a.username} value={a.username}>
                {a.username}
              </option>
            ))}
          </select>
          <select
            className="input w-auto"
            value={tag}
            onChange={(e) => updateFilter('tag', e.target.value)}
          >
            <option value="">All tags</option>
            {tags.map((t) => (
              <option key={t.name} value={t.name}>
                {t.name}
              </option>
            ))}
          </select>
          <input
            type="date"
            className="input w-auto"
            value={dateFrom}
            onChange={(e) => updateFilter('date_from', e.target.value)}
          />
          <span className="self-center text-slate-500">to</span>
          <input
            type="date"
            className="input w-auto"
            value={dateTo}
            onChange={(e) => updateFilter('date_to', e.target.value)}
          />
          <select
            className="input w-auto"
            value={sort}
            disabled={!!search && mode === 'semantic'}
            title={search && mode === 'semantic' ? 'Semantic search is already ordered by relevance' : 'Sort'}
            onChange={(e) => updateFilter('sort', e.target.value === 'saved_date' ? '' : e.target.value)}
          >
            <option value="saved_date">Sort: date saved</option>
            <option value="posted_date">Sort: date posted</option>
            <option value="author">Sort: author</option>
            {search && <option value="relevance">Sort: relevance</option>}
          </select>
          {hasFilters && (
            <button type="button" className="btn-ghost" onClick={() => setSearchParams({})}>
              Clear filters
            </button>
          )}
        </div>
      </div>

      {selectedIds.size > 0 && (
        <div className="card sticky top-16 z-10 flex flex-wrap items-center gap-2 px-4 py-3">
          <span className="text-sm text-slate-300">{selectedIds.size} selected</span>
          <button type="button" className="btn-ghost text-xs" onClick={clearSelection}>
            Clear
          </button>
          <div className="mx-2 h-5 w-px bg-surface-border" />
          <select
            className="input w-auto text-xs"
            defaultValue=""
            disabled={bulkBusy}
            onChange={(e) => {
              const value = e.target.value
              e.target.value = ''
              if (value === '__none__') void bulkSetCategory(null)
              else if (value) void bulkSetCategory(Number(value))
            }}
          >
            <option value="" disabled>
              Set category…
            </option>
            <option value="__none__">— uncategorized —</option>
            {categoryData?.categories.map((c) => (
              <option key={c.id ?? c.name} value={c.id ?? ''}>
                {c.name}
              </option>
            ))}
          </select>
          <form
            className="flex gap-1"
            onSubmit={(e) => {
              e.preventDefault()
              void bulkAddTag()
            }}
          >
            <input
              className="input w-32 text-xs"
              placeholder="Add tag…"
              value={bulkTagInput}
              onChange={(e) => setBulkTagInput(e.target.value)}
              disabled={bulkBusy}
            />
            <button type="submit" className="btn-secondary text-xs" disabled={bulkBusy || !bulkTagInput.trim()}>
              Add
            </button>
          </form>
          <button type="button" className="btn-secondary text-xs" disabled={bulkBusy} onClick={() => void bulkEnrich()}>
            Enrich these
          </button>
          <button type="button" className="btn-secondary text-xs" disabled={bulkBusy} onClick={() => void bulkExport()}>
            Export to Obsidian
          </button>
        </div>
      )}

      {error && <p className="card border-red-900/60 bg-red-950/40 px-4 py-3 text-sm text-red-300">{error}</p>}

      {loading ? (
        <p className="text-sm text-slate-500">Loading…</p>
      ) : items.length === 0 ? (
        <p className="text-sm text-slate-500">No items found.</p>
      ) : (
        <div className={`grid ${DENSITY_GRID[density]}`}>
          {items.map((item, idx) => {
            const meta = item.id !== null ? searchScores.get(item.id) : undefined
            const selected = item.id != null && selectedIds.has(item.id)
            return (
              <div key={item.id} className="group relative flex flex-col gap-1">
                <div
                  role="checkbox"
                  aria-checked={selected}
                  aria-label={selected ? 'Deselect item' : 'Select item'}
                  tabIndex={0}
                  className={`absolute left-2 top-2 z-10 flex h-6 w-6 cursor-pointer items-center justify-center rounded-md border border-surface-border bg-black/60 text-sm text-accent transition-opacity ${
                    selected ? 'opacity-100' : 'opacity-0 group-hover:opacity-100'
                  }`}
                  onClick={(e) => {
                    e.preventDefault()
                    e.stopPropagation()
                    toggleSelect(idx, item, e.shiftKey)
                  }}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault()
                      toggleSelect(idx, item, e.shiftKey)
                    }
                  }}
                >
                  {selected && '✓'}
                </div>
                <ItemCard item={item} contextSearch={filterSearch(searchParams)} />
                {search && meta && (
                  <p className="line-clamp-1 px-1 text-xs text-slate-500">
                    match {(meta.score * 100).toFixed(0)}%{meta.snippet ? ` — ${meta.snippet}` : ''}
                  </p>
                )}
              </div>
            )
          })}
        </div>
      )}

      {!loading && items.length > 0 && (
        <div className="flex flex-col items-center gap-2 pt-2">
          {hasMore && (
            <button type="button" className="btn-secondary" disabled={loadingMore} onClick={loadMore}>
              {loadingMore ? 'Loading…' : 'Load more'}
            </button>
          )}
          {atSemanticCap && (
            <p className="text-xs text-slate-500">Showing the top {SEMANTIC_TOP_K_CAP} semantic matches.</p>
          )}
          {!hasMore && !atSemanticCap && (
            <p className="text-xs text-slate-500">
              {items.length} of {total}
            </p>
          )}
        </div>
      )}
    </div>
  )
}
