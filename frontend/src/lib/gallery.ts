import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { ItemIdListResponse, SemanticSearchResponse } from '../types'

/** Gallery filter params that Gallery / Feed / ItemDetail all share. The
 * gallery's own `page` is deliberately excluded — Feed and prev/next span
 * the whole filtered set. */
const FILTER_KEYS = ['author', 'media_type', 'tag', 'category', 'date_from', 'date_to', 'search'] as const

const UNCATEGORIZED = '__uncategorized__'

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

/** Query object for `/api/library/item-ids` / `/items` from gallery params.
 * The gallery's free-text box (`search`) maps to the backend's caption
 * filter (`q`); true semantic ordering is handled separately. */
function browseQuery(params: URLSearchParams): Record<string, string> {
  const out: Record<string, string> = {}
  const author = params.get('author')
  const mediaType = params.get('media_type')
  const tag = params.get('tag')
  const category = params.get('category')
  const dateFrom = params.get('date_from')
  const dateTo = params.get('date_to')
  if (author) out.author = author
  if (mediaType) out.media_type = mediaType
  if (tag) out.tag = tag
  if (category) out.category = category
  if (dateFrom) out.date_from = dateFrom
  if (dateTo) out.date_to = dateTo
  return out
}

/** The ordered item ids for the current gallery filter — the sequence the
 * feed scrolls through and prev/next steps along. When a semantic search
 * is active it uses the ranked search results (client-filtered by
 * category, matching Gallery). */
export async function fetchSiblingIds(
  params: URLSearchParams,
  signal?: AbortSignal,
): Promise<number[]> {
  const search = params.get('search')?.trim()
  if (search) {
    const res = await api.get<SemanticSearchResponse>(
      '/api/chat/search',
      { q: search, top_k: 120 },
      signal,
    )
    const category = params.get('category') ?? ''
    let results = res.results
    if (category === UNCATEGORIZED) results = results.filter((r) => r.item.category === null)
    else if (category) results = results.filter((r) => r.item.category === category)
    return results.map((r) => r.item.id).filter((id): id is number => id !== null)
  }
  const res = await api.get<ItemIdListResponse>('/api/library/item-ids', browseQuery(params), signal)
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
