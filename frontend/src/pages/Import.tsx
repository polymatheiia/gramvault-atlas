import { useCallback, useEffect, useRef, useState, type DragEvent } from 'react'
import { Link } from 'react-router-dom'
import { ApiError, api, uploadFile } from '../api/client'
import type { EnrichmentProgress, EnrichmentRunResponse, ImportJob, ImportJobListResponse } from '../types'
import type { Schemas } from '../api/schema'

type LinkMediaResponse = Schemas['LinkMediaResponse']

function JobRow({ job }: { job: ImportJob }) {
  return (
    <div className="card flex items-center justify-between gap-3 px-3 py-2 text-sm">
      <div className="flex flex-col">
        <span className="text-slate-200">{job.source_path.split(/[/\\]/).pop()}</span>
        <span className="text-xs text-slate-500">
          {job.processed_items}/{job.total_items} items{job.failed_items ? `, ${job.failed_items} failed` : ''}
        </span>
      </div>
      <span
        className={`badge ${
          job.status === 'done'
            ? 'bg-emerald-950 text-emerald-300'
            : job.status === 'failed'
              ? 'bg-red-950 text-red-300'
              : 'bg-surface-overlay text-slate-300'
        }`}
      >
        {job.status}
      </span>
    </div>
  )
}

export function Import() {
  const [jobs, setJobs] = useState<ImportJob[]>([])
  const [uploadPct, setUploadPct] = useState<number | null>(null)
  const [currentJob, setCurrentJob] = useState<ImportJob | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [dragging, setDragging] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)

  const [enrichRunning, setEnrichRunning] = useState(false)
  const [progress, setProgress] = useState<EnrichmentProgress | null>(null)
  const [enrichError, setEnrichError] = useState<string | null>(null)

  const [linkDir, setLinkDir] = useState('')
  const [linkCopy, setLinkCopy] = useState(false)
  const [linking, setLinking] = useState(false)
  const [linkResult, setLinkResult] = useState<LinkMediaResponse | null>(null)
  const [linkError, setLinkError] = useState<string | null>(null)

  function refreshJobs() {
    api
      .get<ImportJobListResponse>('/api/import/jobs')
      .then((res) => setJobs(res.jobs))
      .catch(() => undefined)
  }

  useEffect(refreshJobs, [])

  const handleFile = useCallback(async (file: File) => {
    setError(null)
    setUploadPct(0)
    setCurrentJob(null)
    try {
      const job = await uploadFile<ImportJob>('/api/import/upload', file, 'file', setUploadPct)
      setCurrentJob(job)
      // Import currently runs synchronously (see routes_import.py), so `job`
      // is already in its final state — poll anyway in case that changes.
      if (job.status === 'pending' || job.status === 'running') {
        void pollJob(job.id ?? undefined)
      }
      refreshJobs()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Upload failed')
    } finally {
      setUploadPct(null)
    }
  }, [])

  async function pollJob(jobId: number | undefined) {
    if (jobId === undefined) return
    for (let i = 0; i < 60; i++) {
      await new Promise((r) => setTimeout(r, 1500))
      try {
        const job = await api.get<ImportJob>(`/api/import/jobs/${jobId}`)
        setCurrentJob(job)
        if (job.status === 'done' || job.status === 'failed') break
      } catch {
        break
      }
    }
    refreshJobs()
  }

  function onDrop(e: DragEvent) {
    e.preventDefault()
    setDragging(false)
    const file = e.dataTransfer.files[0]
    if (file) void handleFile(file)
  }

  async function runEnrichment() {
    setEnrichError(null)
    setEnrichRunning(true)
    try {
      await api.post<EnrichmentRunResponse>('/api/enrich/run', { item_ids: null })
      await pollEnrichmentProgress()
    } catch (err) {
      setEnrichError(err instanceof Error ? err.message : 'Failed to start enrichment')
      setEnrichRunning(false)
    }
  }

  async function pollEnrichmentProgress() {
    for (let i = 0; i < 600; i++) {
      try {
        const p = await api.get<EnrichmentProgress>('/api/enrich/progress')
        setProgress(p)
        if (p.pending === 0 && p.running === 0) break
      } catch (err) {
        setEnrichError(err instanceof Error ? err.message : 'Failed to poll enrichment progress')
        break
      }
      await new Promise((r) => setTimeout(r, 1500))
    }
    setEnrichRunning(false)
  }

  async function runLinkMedia() {
    if (!linkDir.trim()) return
    setLinking(true)
    setLinkError(null)
    setLinkResult(null)
    try {
      const res = await api.post<LinkMediaResponse>('/api/import/link-media', {
        source_dir: linkDir.trim(),
        copy_files: linkCopy,
      })
      setLinkResult(res)
    } catch (err) {
      setLinkError(
        err instanceof ApiError && typeof err.detail === 'string'
          ? err.detail
          : err instanceof Error
            ? err.message
            : 'Failed to link media',
      )
    } finally {
      setLinking(false)
    }
  }

  const enrichTotal = progress ? progress.total : 0
  const enrichDoneCount = progress ? progress.done + progress.failed : 0
  const enrichPct = enrichTotal > 0 ? Math.round((enrichDoneCount / enrichTotal) * 100) : 0

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-8 px-4 py-6">
      <section className="flex flex-col gap-3">
        <h1 className="text-lg font-semibold text-slate-100">Import an Instagram export</h1>
        <div
          className={`card flex flex-col items-center justify-center gap-2 border-dashed px-6 py-12 text-center transition-colors ${
            dragging ? 'border-accent bg-accent-soft/20' : ''
          }`}
          onDragOver={(e) => {
            e.preventDefault()
            setDragging(true)
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
        >
          <p className="text-sm text-slate-300">Drag & drop your Instagram data export ZIP here</p>
          <p className="text-xs text-slate-500">or</p>
          <button type="button" className="btn-secondary" onClick={() => fileInputRef.current?.click()}>
            Choose file
          </button>
          <input
            ref={fileInputRef}
            type="file"
            accept=".zip"
            className="hidden"
            onChange={(e) => {
              const file = e.target.files?.[0]
              if (file) void handleFile(file)
              e.target.value = ''
            }}
          />
        </div>

        {uploadPct !== null && (
          <div className="card px-3 py-2">
            <div className="mb-1 flex justify-between text-xs text-slate-400">
              <span>Uploading…</span>
              <span>{uploadPct}%</span>
            </div>
            <div className="h-2 overflow-hidden rounded-full bg-surface-overlay">
              <div className="h-full bg-accent transition-all" style={{ width: `${uploadPct}%` }} />
            </div>
          </div>
        )}

        {error && <p className="card border-red-900/60 bg-red-950/40 px-4 py-2 text-sm text-red-300">{error}</p>}

        {currentJob && (
          <div className="card flex flex-col gap-1 px-4 py-3 text-sm">
            <span className="text-slate-200">
              Import {currentJob.status}: {currentJob.processed_items}/{currentJob.total_items} items
              {currentJob.failed_items ? `, ${currentJob.failed_items} failed` : ''}
            </span>
            {currentJob.error_message && <span className="text-red-300">{currentJob.error_message}</span>}
            {currentJob.status === 'done' && (
              <Link to="/" className="text-accent no-underline hover:underline">
                View in gallery →
              </Link>
            )}
          </div>
        )}
      </section>

      <section className="flex flex-col gap-3">
        <h2 className="text-lg font-semibold text-slate-100">Link separately downloaded media</h2>
        <p className="text-sm text-slate-400">
          Instagram&apos;s export has no media for other people&apos;s saved posts. Point this at a
          directory downloaded with <code className="text-xs text-slate-300">instaloader</code> (or
          anything whose filenames carry the post shortcode) to match those items to their media by
          shortcode — the server-side version of <code className="text-xs text-slate-300">gramvault link-media</code>.
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <input
            className="input flex-1"
            placeholder="/path/to/downloaded/media"
            value={linkDir}
            onChange={(e) => setLinkDir(e.target.value)}
            disabled={linking}
          />
          <label className="flex items-center gap-1.5 text-sm text-slate-300">
            <input type="checkbox" checked={linkCopy} onChange={(e) => setLinkCopy(e.target.checked)} disabled={linking} />
            Copy instead of hardlink
          </label>
          <button type="button" className="btn-secondary" disabled={linking || !linkDir.trim()} onClick={() => void runLinkMedia()}>
            {linking ? 'Linking…' : 'Link media'}
          </button>
        </div>
        {linkError && <p className="card border-red-900/60 bg-red-950/40 px-4 py-2 text-sm text-red-300">{linkError}</p>}
        {linkResult && (
          <div className="card flex flex-col gap-1 px-4 py-3 text-sm text-slate-300">
            <span>{linkResult.summary}</span>
            {linkResult.unmatched_examples.length > 0 && (
              <span className="text-xs text-slate-500">
                Unmatched, e.g.: {linkResult.unmatched_examples.join(', ')}
              </span>
            )}
          </div>
        )}
      </section>

      <section className="flex flex-col gap-3">
        <h2 className="text-lg font-semibold text-slate-100">Run AI enrichment</h2>
        <p className="text-sm text-slate-400">
          Generates AI captions/transcripts and embeddings for items that haven&apos;t been enriched yet.{' '}
          <Link to="/enrich" className="text-accent no-underline hover:underline">
            The Enrich page
          </Link>{' '}
          has per-pass control (OCR, transcribe-only, by category).
        </p>
        <button type="button" className="btn-primary w-fit" onClick={() => void runEnrichment()} disabled={enrichRunning}>
          {enrichRunning ? 'Enriching…' : 'Run enrichment on pending items'}
        </button>
        {progress && (
          <div className="card px-3 py-2">
            <div className="mb-1 flex justify-between text-xs text-slate-400">
              <span>
                {enrichDoneCount}/{enrichTotal} ({progress.failed} failed)
              </span>
              <span>{enrichPct}%</span>
            </div>
            <div className="h-2 overflow-hidden rounded-full bg-surface-overlay">
              <div className="h-full bg-accent transition-all" style={{ width: `${enrichPct}%` }} />
            </div>
          </div>
        )}
        {enrichError && <p className="card border-red-900/60 bg-red-950/40 px-4 py-2 text-sm text-red-300">{enrichError}</p>}
      </section>

      <section className="flex flex-col gap-3">
        <h2 className="text-lg font-semibold text-slate-100">Past import jobs</h2>
        <div className="flex flex-col gap-2">
          {jobs.length === 0 && <p className="text-sm text-slate-500">No imports yet.</p>}
          {jobs.map((job) => (
            <JobRow key={job.id} job={job} />
          ))}
        </div>
      </section>
    </div>
  )
}
