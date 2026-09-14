import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import type { Schemas } from '../api/schema'
import type {
  AiTask,
  ExportJobStatus,
  ExportLayout,
  ExportRequest,
  ExportSettings,
  Job,
  JobStartResponse,
  ModelsOverview,
  ProviderKind,
  SuggestedModel,
  TaskRoutingUpdate,
  TaskRoutingUpdateResponse,
  TestTaskResult,
  VaultPathCheckRequest,
  VaultPathCheckResponse,
} from '../types'

type SystemInfo = Schemas['SystemInfo']
type PullSession = Schemas['PullSessionResponse']

const AI_TASKS: AiTask[] = ['chat', 'vision', 'embedding', 'categorize', 'digest']
const PROVIDER_KINDS: ProviderKind[] = ['ollama', 'openai', 'anthropic']

const TABS = [
  { id: 'library', label: 'Library & Obsidian' },
  { id: 'models', label: 'AI Models' },
  { id: 'instagram', label: 'Instagram' },
  { id: 'security', label: 'Security' },
  { id: 'advanced', label: 'Advanced' },
] as const
type TabId = (typeof TABS)[number]['id']

function humanBytes(n: number | null): string {
  if (!n) return ''
  const units = ['B', 'KB', 'MB', 'GB']
  let v = n
  let i = 0
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024
    i++
  }
  return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${units[i]}`
}

export function Settings() {
  const [tab, setTab] = useState<TabId>('library')

  return (
    <div className="mx-auto flex max-w-2xl flex-col gap-6 px-4 py-6">
      <div role="tablist" aria-label="Settings sections" className="flex flex-wrap gap-1 border-b border-surface-border pb-2">
        {TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={tab === t.id}
            className={`rounded-lg px-3 py-1.5 text-sm font-medium transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent ${
              tab === t.id
                ? 'bg-surface-overlay text-slate-100'
                : 'text-slate-400 hover:bg-surface-raised hover:text-slate-200'
            }`}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === 'library' && <LibrarySettings />}
      {tab === 'models' && <ModelSettings />}
      {tab === 'instagram' && <InstagramSettings />}
      {tab === 'security' && <SecuritySettings />}
      {tab === 'advanced' && <AdvancedSettings />}
    </div>
  )
}

function LibrarySettings() {
  const [vaultPath, setVaultPath] = useState('')
  const [subfolder, setSubfolder] = useState('')
  const [validation, setValidation] = useState<VaultPathCheckResponse | null>(null)
  const [validating, setValidating] = useState(false)

  const [job, setJob] = useState<ExportJobStatus | null>(null)
  const [exporting, setExporting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)

  const [layout, setLayout] = useState<ExportLayout>('flat')

  useEffect(() => {
    api
      .get<ExportSettings>('/api/export/settings')
      .then((s) => setLayout(s.layout))
      .catch(() => undefined)
  }, [])

  async function saveLayout(next: ExportLayout) {
    setLayout(next)
    try {
      await api.put<ExportSettings>('/api/export/settings', { layout: next })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save layout')
    }
  }

  async function validate() {
    setValidating(true)
    setValidation(null)
    try {
      const body: VaultPathCheckRequest = { vault_dir: vaultPath.trim() || null }
      const res = await api.post<VaultPathCheckResponse>('/api/export/validate-vault', body)
      setValidation(res)
    } catch (err) {
      setValidation({ valid: false, reason: err instanceof Error ? err.message : 'Validation failed' })
    } finally {
      setValidating(false)
    }
  }

  // The export endpoint always reads paths.obsidian_vault_dir from
  // config.yaml — it never accepts a vault path in its own request body.
  // So the typed path has to be persisted here first, or "Export" would
  // silently export to whatever vault (if any) is already on disk.
  async function saveVaultPath(): Promise<boolean> {
    setSaving(true)
    setSaved(false)
    try {
      const body: VaultPathCheckRequest = { vault_dir: vaultPath.trim() || null }
      const res = await api.post<VaultPathCheckResponse>('/api/export/vault-path', body)
      setValidation(res)
      setSaved(res.valid)
      return res.valid
    } catch (err) {
      setValidation({ valid: false, reason: err instanceof Error ? err.message : 'Save failed' })
      return false
    } finally {
      setSaving(false)
    }
  }

  async function pollJob(jobId: number) {
    for (let i = 0; i < 600; i++) {
      const status = await api.get<ExportJobStatus>(`/api/export/jobs/${jobId}`)
      setJob(status)
      if (status.status === 'done' || status.status === 'failed') break
      await new Promise((r) => setTimeout(r, 1500))
    }
  }

  async function runExport() {
    setError(null)
    setExporting(true)
    setJob(null)
    try {
      // Only overwrite the saved vault path if the user actually typed
      // something — leaving it blank means "use what's already configured".
      if (vaultPath.trim() && !(await saveVaultPath())) {
        setError('Vault path is invalid — fix it before exporting.')
        return
      }
      const body: ExportRequest = { item_ids: null, vault_subfolder: subfolder.trim() || null }
      const started = await api.post<ExportJobStatus>('/api/export/obsidian', body)
      setJob(started)
      await pollJob(started.id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Export failed')
    } finally {
      setExporting(false)
    }
  }

  return (
    <section className="flex flex-col gap-3">
      <h1 className="text-lg font-semibold text-slate-100">Obsidian export</h1>

      <label className="flex flex-col gap-1">
        <span className="label">Vault folder path</span>
        <span className="text-xs text-slate-500">
          Leave blank to validate/use the path already configured in <code>config.yaml</code> (
          <code>paths.obsidian_vault_dir</code>).
        </span>
        <input
          className="input"
          placeholder="C:\Users\you\ObsidianVault"
          value={vaultPath}
          onChange={(e) => setVaultPath(e.target.value)}
        />
      </label>

      <label className="flex flex-col gap-1">
        <span className="label">Subfolder within vault (optional)</span>
        <input
          className="input"
          placeholder="GramVault"
          value={subfolder}
          onChange={(e) => setSubfolder(e.target.value)}
        />
      </label>

      <label className="flex flex-col gap-1">
        <span className="label">Note layout</span>
        <span className="text-xs text-slate-500">
          How notes are foldered. Changing this moves notes on the next export; your notes below the{' '}
          <code>%% gramvault:end %%</code> marker are always kept.
        </span>
        <select
          className="input w-auto"
          value={layout}
          onChange={(e) => void saveLayout(e.target.value as ExportLayout)}
        >
          <option value="flat">Flat — one folder</option>
          <option value="by-category">By category — {'<category>/<note>'}</option>
          <option value="by-date">By date — {'<YYYY-MM>/<note>'}</option>
        </select>
      </label>

      <div className="flex gap-2">
        <button type="button" className="btn-secondary" onClick={() => void validate()} disabled={validating}>
          {validating ? 'Validating…' : 'Validate path'}
        </button>
        <button type="button" className="btn-secondary" onClick={() => void saveVaultPath()} disabled={saving}>
          {saving ? 'Saving…' : 'Save vault path'}
        </button>
        <button type="button" className="btn-primary" onClick={() => void runExport()} disabled={exporting}>
          {exporting ? 'Exporting…' : 'Export to Obsidian'}
        </button>
      </div>

      {validation && (
        <p className={`text-sm ${validation.valid ? 'text-emerald-300' : 'text-red-300'}`}>
          {validation.valid ? (saved ? 'Saved.' : 'Valid vault path.') : validation.reason}
        </p>
      )}

      {error && <p className="card border-red-900/60 bg-red-950/40 px-4 py-2 text-sm text-red-300">{error}</p>}

      {job && (
        <div className="card flex flex-col gap-1 px-4 py-3 text-sm text-slate-300">
          <span>
            Export {job.status}: {job.processed_items}/{job.total_items} items
          </span>
          <span className="text-xs text-slate-500">
            {job.notes_written} notes written, {job.notes_updated} updated, {job.media_files_copied} media files copied
          </span>
          {job.failed_items > 0 && <span className="text-amber-300">{job.failed_items} items skipped</span>}
          {job.skipped.length > 0 && (
            <ul className="list-disc pl-5 text-xs text-slate-500">
              {job.skipped.map((s, i) => (
                <li key={i}>
                  {s.item_id !== null ? `Item #${s.item_id}` : 'Unknown item'}: {s.reason}
                </li>
              ))}
            </ul>
          )}
          {job.error_message && <span className="text-red-300">{job.error_message}</span>}
        </div>
      )}
    </section>
  )
}

function InstagramSettings() {
  const [session, setSession] = useState<PullSession | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api
      .get<PullSession>('/api/pull/session')
      .then(setSession)
      .catch((err) => setError(err instanceof Error ? err.message : 'Failed to load'))
  }, [])

  return (
    <section className="flex flex-col gap-3">
      <h1 className="text-lg font-semibold text-slate-100">Instagram</h1>
      {error && <p className="card border-red-900/60 bg-red-950/40 px-4 py-2 text-sm text-red-300">{error}</p>}
      {!session && !error && <p className="text-sm text-slate-500">Loading…</p>}
      {session && !session.enabled && (
        <div className="card flex flex-col gap-2 px-4 py-3 text-sm text-slate-300">
          <p>Pulling from Instagram is off.</p>
          <p className="text-xs text-slate-500">
            Enable it by adding a <code>pull: {'{'}enabled: true{'}'}</code> block to{' '}
            <code>config.yaml</code> and restarting the server — see the{' '}
            <a href="/pull" className="text-accent underline">
              Pull page
            </a>{' '}
            for details.
          </p>
        </div>
      )}
      {session?.enabled && (
        <div className="card flex flex-col gap-2 px-4 py-3 text-sm text-slate-300">
          <p>
            {session.configured ? (
              <>
                Connected as <span className="text-slate-100">{session.username}</span>.
              </>
            ) : (
              'Not connected yet.'
            )}
          </p>
          {session.last_verified_at && (
            <p className="text-xs text-slate-500">Last verified {new Date(session.last_verified_at).toLocaleString()}</p>
          )}
          <a href="/pull" className="btn-secondary w-fit no-underline">
            Manage on the Pull page
          </a>
        </div>
      )}
    </section>
  )
}

function SecuritySettings() {
  const [info, setInfo] = useState<SystemInfo | null>(null)
  const [ov, setOv] = useState<ModelsOverview | null>(null)
  const [tokenInput, setTokenInput] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [status, setStatus] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    try {
      const [i, o] = await Promise.all([
        api.get<SystemInfo>('/api/system/info'),
        api.get<ModelsOverview>('/api/models'),
      ])
      setInfo(i)
      setOv(o)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load')
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  function generate() {
    const bytes = new Uint8Array(24)
    crypto.getRandomValues(bytes)
    const token = btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
    setTokenInput(token)
  }

  async function saveToken() {
    setError(null)
    setStatus(null)
    try {
      await api.put('/api/models/secrets', { set_auth_token: true, auth_token: tokenInput || null })
      setStatus(tokenInput ? 'Token saved — store it somewhere safe, it is never shown again.' : 'Token cleared.')
      setTokenInput('')
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save')
    }
  }

  return (
    <section className="flex flex-col gap-4">
      <h1 className="text-lg font-semibold text-slate-100">Security</h1>
      {error && <p className="card border-red-900/60 bg-red-950/40 px-4 py-2 text-sm text-red-300">{error}</p>}
      {!info && !error && <p className="text-sm text-slate-500">Loading…</p>}

      <div className="flex flex-col gap-2">
        <span className="label">API auth token</span>
        <p className="text-xs text-slate-500">
          When set, every request to this server needs <code>Authorization: Bearer &lt;token&gt;</code>. Only bind to{' '}
          <code>0.0.0.0</code> with a token set.{' '}
          {ov?.auth_token_set ? <span className="text-emerald-300">Currently set.</span> : 'Currently off.'}
        </p>
        <div className="flex flex-wrap gap-2">
          <input
            className="input"
            type="password"
            placeholder={ov?.auth_token_set ? '•••••• (enter a new token to change)' : 'set a long random token'}
            value={tokenInput}
            onChange={(e) => setTokenInput(e.target.value)}
          />
          <button type="button" className="btn-ghost" onClick={generate}>
            Generate
          </button>
          <button type="button" className="btn-secondary" onClick={() => void saveToken()}>
            {tokenInput ? 'Save' : ov?.auth_token_set ? 'Clear' : 'Save'}
          </button>
        </div>
        {status && <p className="text-sm text-emerald-300">{status}</p>}
      </div>

      {info && (
        <div className="flex flex-col gap-2">
          <span className="label">Bind address & hosts</span>
          <div className="card flex flex-col gap-1 px-4 py-3 text-sm text-slate-300">
            <span>
              Bound to <code className="text-slate-100">{info.server_host}:{info.server_port}</code>
            </span>
            <span className="text-xs text-slate-500">
              Accepted Host headers: localhost, 127.0.0.1, [::1]
              {info.allowed_hosts.length > 0 ? `, ${info.allowed_hosts.join(', ')}` : ''}
            </span>
            {info.server_host !== '127.0.0.1' && info.server_host !== 'localhost' && !info.auth_enabled && (
              <span className="text-amber-300">
                Bound to a non-loopback host with no auth token set — anyone who can reach this address has full
                access.
              </span>
            )}
          </div>
        </div>
      )}
    </section>
  )
}

function AdvancedSettings() {
  const [info, setInfo] = useState<SystemInfo | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api
      .get<SystemInfo>('/api/system/info')
      .then(setInfo)
      .catch((err) => setError(err instanceof Error ? err.message : 'Failed to load'))
  }, [])

  return (
    <section className="flex flex-col gap-4">
      <h1 className="text-lg font-semibold text-slate-100">Advanced</h1>
      {error && <p className="card border-red-900/60 bg-red-950/40 px-4 py-2 text-sm text-red-300">{error}</p>}
      {!info && !error && <p className="text-sm text-slate-500">Loading…</p>}
      {info && (
        <>
          <div className="flex flex-col gap-2">
            <span className="label">Transcription (Whisper)</span>
            <div className="card flex flex-col gap-1 px-4 py-3 text-sm text-slate-300">
              <span>
                Model <code className="text-slate-100">{info.transcription_model_size}</code> on{' '}
                <code className="text-slate-100">{info.transcription_device}</code> (
                {info.transcription_compute_type})
              </span>
              <span className="text-xs text-slate-500">
                Set via <code>transcription:</code> in <code>config.yaml</code>. The first transcription downloads the
                model (~1.6 GB for the default <code>turbo</code> size) from Hugging Face.
              </span>
            </div>
          </div>

          <div className="flex flex-col gap-2">
            <span className="label">Privacy</span>
            <div className="card flex flex-col gap-1 px-4 py-3 text-sm text-slate-300">
              <span>
                Chroma anonymised telemetry:{' '}
                {info.chroma_telemetry_disabled ? (
                  <span className="text-emerald-300">disabled</span>
                ) : (
                  <span className="text-amber-300">enabled</span>
                )}
              </span>
              <span className="text-xs text-slate-500">No other network calls happen by default except to a local Ollama.</span>
            </div>
          </div>

          <div className="flex flex-col gap-2">
            <span className="label">Data locations</span>
            <div className="card flex flex-col gap-1 px-4 py-3 text-sm text-slate-300">
              <span>
                Config: <code className="text-xs text-slate-400">{info.config_path}</code>
              </span>
              <span>
                Library: <code className="text-xs text-slate-400">{info.library_dir}</code>
              </span>
              <span>
                Database: <code className="text-xs text-slate-400">{info.db_path}</code>
              </span>
              <span>
                Vectors: <code className="text-xs text-slate-400">{info.chroma_dir}</code>
              </span>
            </div>
          </div>
        </>
      )}
    </section>
  )
}

function ModelSettings() {
  const [ov, setOv] = useState<ModelsOverview | null>(null)
  const [suggested, setSuggested] = useState<SuggestedModel[]>([])
  const [error, setError] = useState<string | null>(null)
  const [needsReembed, setNeedsReembed] = useState(false)
  const [pullJob, setPullJob] = useState<Job | null>(null)
  const [reembedJob, setReembedJob] = useState<Job | null>(null)
  const [pullName, setPullName] = useState('')
  const [tests, setTests] = useState<Partial<Record<AiTask, TestTaskResult | 'running'>>>({})

  const refresh = useCallback(async () => {
    try {
      const [o, s] = await Promise.all([
        api.get<ModelsOverview>('/api/models'),
        api.get<SuggestedModel[]>('/api/models/suggested'),
      ])
      setOv(o)
      setSuggested(s)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load models')
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  // Poll a running pull / reembed job.
  useEffect(() => {
    if (!pullJob || pullJob.status === 'done' || pullJob.status === 'failed') return
    const h = setTimeout(async () => {
      const j = await api.get<Job>(`/api/jobs/${pullJob.id}`)
      setPullJob(j)
      if (j.status === 'done' || j.status === 'failed') void refresh()
    }, 1200)
    return () => clearTimeout(h)
  }, [pullJob, refresh])

  useEffect(() => {
    if (!reembedJob || reembedJob.status === 'done' || reembedJob.status === 'failed') return
    const h = setTimeout(async () => {
      setReembedJob(await api.get<Job>(`/api/jobs/${reembedJob.id}`))
    }, 1500)
    return () => clearTimeout(h)
  }, [reembedJob])

  async function saveTask(u: TaskRoutingUpdate) {
    setError(null)
    try {
      const res = await api.put<TaskRoutingUpdateResponse>('/api/models/tasks', u)
      setOv(res)
      if (res.needs_reembed) setNeedsReembed(true)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update task')
    }
  }

  async function saveSecret(body: Record<string, unknown>) {
    setError(null)
    try {
      await api.put('/api/models/secrets', body)
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save')
    }
  }

  async function runTest(task: AiTask) {
    setTests((t) => ({ ...t, [task]: 'running' }))
    try {
      const r = await api.post<TestTaskResult>('/api/models/test', { task })
      setTests((t) => ({ ...t, [task]: r }))
    } catch (err) {
      setTests((t) => ({ ...t, [task]: { ok: false, detail: err instanceof Error ? err.message : 'error', latency_ms: null } }))
    }
  }

  async function pull(model: string) {
    if (!model.trim()) return
    setError(null)
    try {
      const { job_id } = await api.post<JobStartResponse>('/api/models/pull', { model: model.trim() })
      setPullJob(await api.get<Job>(`/api/jobs/${job_id}`))
      setPullName('')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Pull failed')
    }
  }

  async function remove(name: string) {
    if (!confirm(`Delete Ollama model ${name}?`)) return
    await api.delete(`/api/models/${encodeURIComponent(name)}`)
    void refresh()
  }

  async function reembed() {
    const { job_id } = await api.post<JobStartResponse>('/api/models/reembed', {})
    setNeedsReembed(false)
    setReembedJob(await api.get<Job>(`/api/jobs/${job_id}`))
  }

  const pulledNames = new Set((ov?.ollama_models ?? []).map((m) => m.name.split(':')[0]))
  const knownProviders = ov?.providers.map((p) => p.name) ?? ['ollama']

  return (
    <section className="flex flex-col gap-4">
      <h1 className="text-lg font-semibold text-slate-100">AI models & providers</h1>
      {error && <p className="card border-red-900/60 bg-red-950/40 px-4 py-2 text-sm text-red-300">{error}</p>}
      {!ov && <p className="text-sm text-slate-500">Loading…</p>}

      {needsReembed && (
        <div className="card flex flex-wrap items-center gap-3 border-amber-900/60 bg-amber-950/30 px-4 py-3 text-sm text-amber-200">
          <span>The embedding model changed — search results are stale until the library is re-embedded.</span>
          <button type="button" className="btn-secondary" onClick={() => void reembed()}>
            Re-embed now
          </button>
        </div>
      )}
      {reembedJob && (
        <p className="card px-4 py-2 text-sm text-slate-300">
          Re-embed {reembedJob.status}
          {reembedJob.progress ? ` — ${reembedJob.progress.done ?? 0}/${reembedJob.progress.total ?? '?'}` : ''}
        </p>
      )}

      {ov && (
        <>
          {/* --- per-task routing --- */}
          <div className="flex flex-col gap-2">
            <span className="label">Task routing</span>
            {AI_TASKS.map((task) => (
              <TaskRow
                key={task}
                task={task}
                routing={ov.tasks[task]}
                knownProviders={knownProviders}
                onSave={saveTask}
                onTest={() => void runTest(task)}
                test={tests[task]}
              />
            ))}
          </div>

          {/* --- provider keys --- */}
          <div className="flex flex-col gap-2">
            <span className="label">Provider API keys</span>
            {ov.providers
              .filter((p) => p.kind !== 'ollama')
              .map((p) => (
                <KeyRow key={p.name} name={p.name} set={p.api_key_set} onSave={(k) => void saveSecret({ provider: p.name, api_key: k })} />
              ))}
            {ov.providers.filter((p) => p.kind !== 'ollama').length === 0 && (
              <p className="text-xs text-slate-500">
                No API providers configured yet — point a task at one above (e.g. provider <code>anthropic</code>, kind{' '}
                <code>anthropic</code>) and its key field will appear here.
              </p>
            )}
          </div>

          {/* --- ollama models --- */}
          <div className="flex flex-col gap-2">
            <span className="label">
              Ollama models {ov.ollama_reachable ? '' : '(server unreachable)'}
            </span>
            {ov.ollama_models.map((m) => (
              <div key={m.name} className="flex items-center justify-between gap-2 text-sm text-slate-300">
                <span>
                  {m.name} <span className="text-xs text-slate-500">{humanBytes(m.size)}</span>
                </span>
                <button type="button" className="btn-ghost text-xs" onClick={() => void remove(m.name)}>
                  remove
                </button>
              </div>
            ))}
            <div className="flex gap-2">
              <input
                className="input"
                placeholder="pull a model, e.g. qwen2.5:3b"
                value={pullName}
                onChange={(e) => setPullName(e.target.value)}
              />
              <button type="button" className="btn-secondary" onClick={() => void pull(pullName)}>
                Pull
              </button>
            </div>
            {pullJob && pullJob.status !== 'done' && pullJob.status !== 'failed' && (
              <p className="text-xs text-slate-400">
                Pulling… {String((pullJob.progress?.status as string) ?? '')}{' '}
                {pullJob.progress?.completed && pullJob.progress?.total
                  ? `${Math.round((Number(pullJob.progress.completed) / Number(pullJob.progress.total)) * 100)}%`
                  : ''}
              </p>
            )}
            {pullJob?.status === 'failed' && <p className="text-xs text-red-300">Pull failed: {pullJob.error_message}</p>}

            <div className="flex flex-col gap-1 pt-1">
              <span className="text-xs text-slate-500">Suggested</span>
              {suggested.map((s) => (
                <div key={`${s.provider_kind}-${s.model}`} className="flex items-center justify-between gap-2 text-xs text-slate-400">
                  <span>
                    <code className="text-slate-300">{s.model}</code> · {s.size} · {s.tasks.join('/')} — {s.note}
                  </span>
                  {s.provider_kind === 'ollama' && !pulledNames.has(s.model.split(':')[0]) && (
                    <button type="button" className="btn-ghost text-xs" onClick={() => void pull(s.model)}>
                      pull
                    </button>
                  )}
                </div>
              ))}
            </div>
          </div>
        </>
      )}
    </section>
  )
}

function TaskRow({
  task,
  routing,
  knownProviders,
  onSave,
  onTest,
  test,
}: {
  task: AiTask
  routing: { provider: string; model: string; source: string }
  knownProviders: string[]
  onSave: (u: TaskRoutingUpdate) => void
  onTest: () => void
  test: TestTaskResult | 'running' | undefined
}) {
  const [provider, setProvider] = useState(routing.provider)
  const [model, setModel] = useState(routing.model)
  const [kind, setKind] = useState<ProviderKind>('ollama')
  useEffect(() => {
    setProvider(routing.provider)
    setModel(routing.model)
  }, [routing.provider, routing.model])

  const isNewProvider = provider.trim() !== '' && !knownProviders.includes(provider.trim())
  const dirty = provider !== routing.provider || model !== routing.model

  return (
    <div className="card flex flex-wrap items-center gap-2 px-3 py-2 text-sm">
      <span className="w-20 font-medium text-slate-200">{task}</span>
      <input
        className="input w-32"
        value={provider}
        onChange={(e) => setProvider(e.target.value)}
        list={`providers-${task}`}
        placeholder="provider"
      />
      <datalist id={`providers-${task}`}>
        {knownProviders.map((p) => (
          <option key={p} value={p} />
        ))}
      </datalist>
      {isNewProvider && (
        <select className="input w-28" value={kind} onChange={(e) => setKind(e.target.value as ProviderKind)}>
          {PROVIDER_KINDS.map((k) => (
            <option key={k} value={k}>
              {k}
            </option>
          ))}
        </select>
      )}
      <input className="input flex-1" value={model} onChange={(e) => setModel(e.target.value)} placeholder="model" />
      <button
        type="button"
        className="btn-secondary"
        disabled={!dirty}
        onClick={() =>
          onSave({
            task,
            provider: provider.trim(),
            model: model.trim(),
            provider_kind: isNewProvider ? kind : null,
          })
        }
      >
        Save
      </button>
      <button type="button" className="btn-ghost" onClick={onTest}>
        Test
      </button>
      {routing.source === 'default' && <span className="text-xs text-slate-500">(default)</span>}
      {test === 'running' && <span className="text-xs text-slate-400">testing…</span>}
      {test && test !== 'running' && (
        <span className={`text-xs ${test.ok ? 'text-emerald-300' : 'text-red-300'}`}>
          {test.detail}
          {test.latency_ms != null ? ` (${test.latency_ms} ms)` : ''}
        </span>
      )}
    </div>
  )
}

function KeyRow({ name, set, onSave }: { name: string; set: boolean; onSave: (key: string | null) => void }) {
  const [val, setVal] = useState('')
  return (
    <div className="flex items-center gap-2 text-sm">
      <span className="w-24 text-slate-300">{name}</span>
      <input
        className="input flex-1"
        type="password"
        placeholder={set ? '•••••• (set — enter a new key to replace)' : 'paste API key'}
        value={val}
        onChange={(e) => setVal(e.target.value)}
      />
      <button
        type="button"
        className="btn-secondary"
        onClick={() => {
          onSave(val || null)
          setVal('')
        }}
      >
        {val ? 'Save' : set ? 'Clear' : 'Save'}
      </button>
    </div>
  )
}
