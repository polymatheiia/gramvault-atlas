import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { fetchSiblingIds, filterSearch } from './gallery'

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return { ...actual, api: { ...actual.api, get: vi.fn() } }
})

describe('filterSearch', () => {
  it('carries only the known filter keys forward, in a stable order', () => {
    const params = new URLSearchParams(
      'author=alice&media_type=reel&page=3&page_size=50&unrelated=x',
    )
    expect(filterSearch(params)).toBe('?author=alice&media_type=reel')
  })

  it('is empty when no filter params are set', () => {
    expect(filterSearch(new URLSearchParams('page=2'))).toBe('')
  })

  it('carries the search (semantic query) key too', () => {
    expect(filterSearch(new URLSearchParams('search=pasta'))).toBe('?search=pasta')
  })

  it('carries the search mode alongside search', () => {
    expect(filterSearch(new URLSearchParams('search=pasta&mode=keyword'))).toBe('?search=pasta&mode=keyword')
  })
})

describe('fetchSiblingIds', () => {
  beforeEach(() => {
    vi.mocked(api.get).mockReset()
  })

  it('requests top_k=100 for a semantic search, never the 120 that used to 422 (audit R1)', async () => {
    vi.mocked(api.get).mockResolvedValue({ results: [], query: 'pasta', total: 0 })

    await fetchSiblingIds(new URLSearchParams('search=pasta'))

    expect(api.get).toHaveBeenCalledWith(
      '/api/chat/search',
      expect.objectContaining({ top_k: 100 }),
      undefined,
    )
  })

  it('forwards a real category as a server-side facet for semantic search (R13)', async () => {
    vi.mocked(api.get).mockResolvedValue({
      query: 'pasta',
      total: 1,
      results: [{ item: { id: 1, category: 'recipes' }, score: 0.9 }],
    })

    const ids = await fetchSiblingIds(new URLSearchParams('search=pasta&category=recipes'))

    expect(api.get).toHaveBeenCalledWith(
      '/api/chat/search',
      expect.objectContaining({ category: 'recipes' }),
      undefined,
    )
    expect(ids).toEqual([1])
  })

  it('forwards author/media_type/tag/date facets to semantic search too (R13)', async () => {
    vi.mocked(api.get).mockResolvedValue({ results: [], query: 'pasta', total: 0 })

    await fetchSiblingIds(
      new URLSearchParams('search=pasta&author=alice&media_type=reel&tag=travel&date_from=2024-01-01'),
    )

    expect(api.get).toHaveBeenCalledWith(
      '/api/chat/search',
      expect.objectContaining({ author: 'alice', media_type: 'reel', tag: 'travel', date_from: '2024-01-01' }),
      undefined,
    )
  })

  it('under an active search, an uncategorized filter is NOT sent as a facet (SearchFilters has no sentinel for it) and is applied client-side instead', async () => {
    vi.mocked(api.get).mockResolvedValue({
      query: 'pasta',
      total: 2,
      results: [
        { item: { id: 1, category: null }, score: 0.9 },
        { item: { id: 2, category: 'travel' }, score: 0.8 },
      ],
    })

    const ids = await fetchSiblingIds(
      new URLSearchParams('search=pasta&category=__uncategorized__'),
    )

    const [, calledParams] = vi.mocked(api.get).mock.calls[0] as [string, Record<string, unknown>]
    expect(calledParams.category).toBeUndefined()
    expect(ids).toEqual([1])
  })

  it('under keyword mode, an active search goes to item-ids with q + facets instead of chat/search', async () => {
    vi.mocked(api.get).mockResolvedValue({ ids: [5], total: 1 })

    const ids = await fetchSiblingIds(new URLSearchParams('search=pasta&mode=keyword&author=alice'))

    expect(api.get).toHaveBeenCalledWith(
      '/api/library/item-ids',
      { author: 'alice', q: 'pasta' },
      undefined,
    )
    expect(ids).toEqual([5])
  })

  it('without a search, calls the plain item-ids endpoint with the browse filters', async () => {
    vi.mocked(api.get).mockResolvedValue({ ids: [3, 4], total: 2 })

    const ids = await fetchSiblingIds(new URLSearchParams('author=alice&tag=travel'))

    expect(api.get).toHaveBeenCalledWith(
      '/api/library/item-ids',
      { author: 'alice', tag: 'travel' },
      undefined,
    )
    expect(ids).toEqual([3, 4])
  })
})
