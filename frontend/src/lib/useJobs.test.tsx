import { act, renderHook, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { setAuthToken } from '../api/client'
import type { Job } from '../types'
import { ToastProvider, useToast } from './toast'
import { JobsProvider, useJobs } from './useJobs'

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } })
}

function job(overrides: Partial<Job>): Job {
  return {
    id: 1,
    kind: 'enrich',
    status: 'running',
    params: null,
    progress: null,
    result: null,
    error_message: null,
    cancel_requested: false,
    started_at: new Date().toISOString(),
    finished_at: null,
    created_at: new Date().toISOString(),
    ...overrides,
  }
}

function useCombined() {
  return { jobs: useJobs(), toasts: useToast() }
}

describe('JobsProvider', () => {
  beforeEach(() => {
    setAuthToken(null)
    vi.useFakeTimers({ shouldAdvanceTime: true })
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  it('does not toast for jobs already finished on the first poll', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse([job({ id: 1, status: 'done' })]))
    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(useCombined, {
      wrapper: ({ children }) => (
        <MemoryRouter>
          <ToastProvider>
            <JobsProvider>{children}</JobsProvider>
          </ToastProvider>
        </MemoryRouter>
      ),
    })

    await waitFor(() => expect(result.current.jobs.jobs).toHaveLength(1))
    expect(result.current.toasts.toasts).toHaveLength(0)
  })

  it('toasts exactly once when a job transitions to done', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse([job({ id: 1, status: 'running' })]))
      .mockResolvedValue(jsonResponse([job({ id: 1, status: 'done' })]))
    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(useCombined, {
      wrapper: ({ children }) => (
        <MemoryRouter>
          <ToastProvider>
            <JobsProvider>{children}</JobsProvider>
          </ToastProvider>
        </MemoryRouter>
      ),
    })

    await waitFor(() => expect(result.current.jobs.jobs[0]?.status).toBe('running'))
    expect(result.current.toasts.toasts).toHaveLength(0)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000)
    })

    await waitFor(() => expect(result.current.jobs.jobs[0]?.status).toBe('done'))
    expect(result.current.toasts.toasts).toHaveLength(1)
    expect(result.current.toasts.toasts[0].message).toBe('Enrichment finished.')

    // A further poll with the same "done" status must not toast again
    // (checked before the 8s auto-dismiss would clear it either way).
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000)
    })
    expect(result.current.toasts.toasts).toHaveLength(1)
  })

  it('toasts once for a failed job with its error message', async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse([job({ id: 2, kind: 'digest', status: 'running' })]))
      .mockResolvedValue(
        jsonResponse([job({ id: 2, kind: 'digest', status: 'failed', error_message: 'boom' })]),
      )
    vi.stubGlobal('fetch', fetchMock)

    const { result } = renderHook(useCombined, {
      wrapper: ({ children }) => (
        <MemoryRouter>
          <ToastProvider>
            <JobsProvider>{children}</JobsProvider>
          </ToastProvider>
        </MemoryRouter>
      ),
    })

    await waitFor(() => expect(result.current.jobs.jobs[0]?.status).toBe('running'))

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000)
    })

    await waitFor(() => expect(result.current.toasts.toasts).toHaveLength(1))
    expect(result.current.toasts.toasts[0].message).toBe('Digest failed: boom')
  })
})
