import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import type { Job } from '../types'
import { JOB_KIND_LABEL, JOB_KIND_ROUTE, useJobs } from '../lib/useJobs'

function elapsedLabel(startedAt: string | null): string {
  if (!startedAt) return ''
  const started = Date.parse(startedAt)
  if (Number.isNaN(started)) return ''
  const seconds = Math.max(0, Math.floor((Date.now() - started) / 1000))
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`
}

function progressFraction(job: Job): number | null {
  const p = job.progress as { done?: number; total?: number; step?: number } | null
  if (!p || typeof p.total !== 'number' || p.total <= 0) return null
  const done = typeof p.done === 'number' ? p.done : typeof p.step === 'number' ? p.step : 0
  return Math.min(1, Math.max(0, done / p.total))
}

function JobRow({ job }: { job: Job }) {
  const { cancel } = useJobs()
  const [cancelling, setCancelling] = useState(false)
  const fraction = progressFraction(job)

  async function handleCancel() {
    if (job.id == null) return
    setCancelling(true)
    try {
      await cancel(job.id)
    } finally {
      setCancelling(false)
    }
  }

  return (
    <div className="flex items-center gap-3 text-sm">
      <Link to={JOB_KIND_ROUTE[job.kind]} className="font-medium text-slate-200 no-underline hover:underline">
        {JOB_KIND_LABEL[job.kind]}
      </Link>
      <div className="h-1.5 w-28 overflow-hidden rounded-full bg-surface-overlay">
        {fraction != null ? (
          <div className="h-full bg-accent transition-all" style={{ width: `${Math.round(fraction * 100)}%` }} />
        ) : (
          <div className="h-full w-full animate-pulse bg-surface-border" />
        )}
      </div>
      {job.status === 'pending' ? (
        <span className="text-xs text-slate-500">queued</span>
      ) : (
        <span className="text-xs text-slate-500">{elapsedLabel(job.started_at)}</span>
      )}
      <button
        type="button"
        className="text-xs text-slate-400 hover:text-slate-200 disabled:opacity-50"
        onClick={handleCancel}
        disabled={cancelling || job.id == null}
      >
        Cancel
      </button>
    </div>
  )
}

/** A slim strip under the NavBar with ambient awareness of every running
 * or queued job, from any page. See `lib/useJobs.tsx` for the polling and
 * toast logic; this component just renders `active`. */
export function JobStrip() {
  const { active } = useJobs()
  const [, setNow] = useState(0)

  // Re-render every few seconds so `elapsedLabel` keeps ticking even
  // between polls.
  useEffect(() => {
    if (active.length === 0) return
    const id = setInterval(() => setNow((n) => n + 1), 5000)
    return () => clearInterval(id)
  }, [active.length])

  if (active.length === 0) return null

  return (
    <div className="border-b border-surface-border bg-surface-raised/60">
      <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-x-6 gap-y-2 px-4 py-2">
        {active.map((job) => (
          <JobRow key={job.id ?? `${job.kind}-${job.created_at}`} job={job} />
        ))}
      </div>
    </div>
  )
}
