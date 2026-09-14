import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { Schemas } from '../api/schema'

type ItemIdListResponse = Schemas['ItemIdListResponse']
type SemanticSearchResponse = Schemas['SemanticSearchResponse']

/** Gallery filter params that Gallery / Feed / ItemDetail all share. The
 * gallery's own `page` is deliberately excluded — Feed and prev/next span
 * the whole filtered set. `mode` picks semantic vs keyword search and only
 * matters alongside `search`, but travels with the other filters so a
 * shared link reproduces the same result set. */
const FILTER_KEYS = ['author', 'media_type', 'tag', 'category', 'date_from', 'date_to', 'search', 'mode'] as const

export const UNCATEGORIZED = '__uncategorized__'

export type SearchMode = 'semantic' | 'keyword'

export function searchModeOf(params: URLSearchParams): SearchMode {
  return params.get('mode') === 'keyword' ? 'keyword' : 'semantic'
}

/** The filter portion of a URLSearchParams as a `?a=b&c=d` string (empty
 * string when there are no filters), for carrying context between pages. */
export function filterSearch(params: URLSearchParams): string {
  const next = new URLSearchParams()
  for (const key of FILTER_KEYS) {
    const value = params.get(key)
    if (value) next.set(key, value)
  }
  const s = next.toString()
  return s ? `?${s}` : ''
}

/** Facet params shared by `/api/library/items`, `/api/library/item-ids`,
 * and (author/media_type/tag/date_from/date_to only — see
 * `fetchSiblingIds`) `/api/chat/search`. `category` here is passed through
 * verbatim, including the `UNCATEGORIZED` sentinel — callers that hit
 * semantic search have to handle that sentinel specially, since
 * `SearchFilters` on the backend doesn't understand it the way
 * `_item_filter_sql` does. */
function facetParams(params: URLSearchParams): Record<string, string> {
  const out: Record<string, string> = {}
  for (const key of ['author', 'media_type', 'tag', 'category', 'date_from', 'date_to'] as const) {
    const value = params.get(key)
    if (value) out[key] = value
  }
  return out
}

/** The ordered item ids for the current gallery filter — the sequence the
 * feed scrolls through and prev/next steps along. Mirrors whichever
 * search mode the gallery grid is showing (R13 — semantic search now
 * honours the same author/media_type/tag/date facets as browsing; a
 * plain category filter is forwarded as a facet too, but "uncategorized"
 * still needs a client-side post-filter since the backend's semantic
 * `SearchFilters` has no sentinel for it). */
export async function fetchSiblingIds(
  params: URLSearchParams,
  signal?: AbortSignal,
): Promise<number[]> {
  const search = params.get('search')?.trim()
  const category = params.get('category')

  if (search && searchModeOf(params) === 'semantic') {
    const facets = facetParams(params)
    if (category === UNCATEGORIZED) delete facets.category
    const res = await api.get<SemanticSearchResponse>(
      '/api/chat/search',
      { q: search, top_k: 100, ...facets },
      signal,
    )
    let results = res.results
    if (category === UNCATEGORIZED) results = results.filter((r) => r.item.category === null)
    return results.map((r) => r.item.id).filter((id): id is number => id != null)
  }

  const res = await api.get<ItemIdListResponse>(
    '/api/library/item-ids',
    { ...facetParams(params), ...(search ? { q: search } : {}) },
    signal,
  )
  return res.ids
}

export function useSiblingIds(params: URLSearchParams): { ids: number[]; loading: boolean } {
  const key = filterSearch(params)
  const [ids, setIds] = useState<number[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    fetchSiblingIds(params, controller.signal)
      .then(setIds)
      .catch(() => {
        if (!controller.signal.aborted) setIds([])
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
    return () => controller.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])

  return { ids, loading }
}
