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

  it('under an active search, filters results by category client-side', async () => {
    vi.mocked(api.get).mockResolvedValue({
      query: 'pasta',
      total: 2,
      results: [
        { item: { id: 1, category: 'recipes' }, score: 0.9 },
        { item: { id: 2, category: 'travel' }, score: 0.8 },
      ],
    })

    const ids = await fetchSiblingIds(new URLSearchParams('search=pasta&category=recipes'))

    expect(ids).toEqual([1])
  })

  it('under an active search, an uncategorized filter keeps only null-category results', async () => {
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

    expect(ids).toEqual([1])
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
