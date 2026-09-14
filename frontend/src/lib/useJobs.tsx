import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import { api } from '../api/client'
import type { Job, JobKind } from '../types'
import { useToast } from './toast'

const ACTIVE_POLL_MS = 3000
const IDLE_POLL_MS = 10000
const RECENT_LIMIT = 20

/** Where the strip and the finish toast should send you for each job kind. */
export const JOB_KIND_ROUTE: Record<JobKind, string> = {
  enrich: '/enrich',
  categorize: '/categorize',
  digest: '/digest',
  pull: '/pull',
  model_pull: '/settings',
  reembed: '/enrich',
}

export const JOB_KIND_LABEL: Record<JobKind, string> = {
  enrich: 'Enrichment',
  categorize: 'Categorization',
  digest: 'Digest',
  pull: 'Instagram pull',
  model_pull: 'Model download',
  reembed: 'Re-embedding',
}

interface JobsContextValue {
  /** Newest-first, capped at RECENT_LIMIT — running/pending jobs plus
   * whatever recently finished ones still fit in that window. */
  jobs: Job[]
  active: Job[]
  cancel: (jobId: number) => Promise<void>
}

const JobsContext = createContext<JobsContextValue | null>(null)

/**
 * Polls GET /api/jobs (no status filter — one unfiltered page carries
 * running, pending, and just-finished jobs together, which is what both
 * the strip and the completion toasts need) and fires a toast whenever a
 * job's status flips into done/failed/cancelled.
 *
 * This is purely additive: it gives ambient awareness of background work
 * from anywhere in the app. Enrich/Categorize/Digest/Pull keep their own
 * polling of their domain-specific progress endpoints (item counts, review
 * queue refresh, etc.) — that detail doesn't exist on the generic Job
 * record, so there's nothing here for those pages to retire.
 */
export function JobsProvider({ children }: { children: ReactNode }) {
  const [jobs, setJobs] = useState<Job[]>([])
  const { push } = useToast()
  const prevStatus = useRef<Map<number, Job['status']> | null>(null)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const poll = useCallback(async (): Promise<Job[] | null> => {
    let list: Job[]
    try {
      list = await api.get<Job[]>('/api/jobs', { limit: RECENT_LIMIT })
    } catch {
      return null
    }
    setJobs(list)

    if (prevStatus.current === null) {
      // First tick: seed the map without toasting, or every job that
      // finished while the tab was closed/backgrounded would toast at once.
      prevStatus.current = new Map(list.filter((j) => j.id != null).map((j) => [j.id as number, j.status]))
      return list
    }

    for (const job of list) {
      if (job.id == null) continue
      const before = prevStatus.current.get(job.id)
      prevStatus.current.set(job.id, job.status)
      if (before === job.status) continue
      if (job.status === 'done' || job.status === 'failed' || job.status === 'cancelled') {
        const label = JOB_KIND_LABEL[job.kind]
        push({
          tone: job.status === 'done' ? 'success' : job.status === 'failed' ? 'error' : 'info',
          message:
            job.status === 'done'
              ? `${label} finished.`
              : job.status === 'failed'
                ? `${label} failed${job.error_message ? `: ${job.error_message}` : '.'}`
                : `${label} cancelled.`,
          href: JOB_KIND_ROUTE[job.kind],
        })
      }
    }
    return list
  }, [push])

  useEffect(() => {
    let cancelled = false

    async function tick() {
      const list = await poll()
      if (cancelled) return
      const stillActive = (list ?? []).some((j) => j.status === 'running' || j.status === 'pending')
      timerRef.current = setTimeout(tick, stillActive ? ACTIVE_POLL_MS : IDLE_POLL_MS)
    }

    void tick()
    return () => {
      cancelled = true
      if (timerRef.current) clearTimeout(timerRef.current)
    }
  }, [poll])

  const cancel = useCallback(
    async (jobId: number) => {
      await api.post(`/api/jobs/${jobId}/cancel`)
      await poll()
    },
    [poll],
  )

  const active = jobs.filter((j) => j.status === 'running' || j.status === 'pending')

  return <JobsContext.Provider value={{ jobs, active, cancel }}>{children}</JobsContext.Provider>
}

export function useJobs(): JobsContextValue {
  const ctx = useContext(JobsContext)
  if (!ctx) throw new Error('useJobs must be used within a JobsProvider')
  return ctx
}
