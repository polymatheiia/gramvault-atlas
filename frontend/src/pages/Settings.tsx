import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import type {
  AiTask,
  ExportJobStatus,
  ExportRequest,
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

const AI_TASKS: AiTask[] = ['chat', 'vision', 'embedding', 'categorize', 'digest']
const PROVIDER_KINDS: ProviderKind[] = ['ollama', 'openai', 'anthropic']

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
  const [vaultPath, setVaultPath] = useState('')
  const [subfolder, setSubfolder] = useState('')
  const [validation, setValidation] = useState<VaultPathCheckResponse | null>(null)
  const [validating, setValidating] = useState(false)

  const [job, setJob] = useState<ExportJobStatus | null>(null)
  const [exporting, setExporting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)

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
    <div className="mx-auto flex max-w-2xl flex-col gap-8 px-4 py-6">
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

      <ModelSettings />
    </div>
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
  const [tokenInput, setTokenInput] = useState('')

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
      <h2 className="text-lg font-semibold text-slate-100">AI models & providers</h2>
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

          {/* --- api auth token --- */}
          <div className="flex flex-col gap-2">
            <span className="label">API auth token</span>
            <p className="text-xs text-slate-500">
              When set, every request to this server needs <code>Authorization: Bearer &lt;token&gt;</code>. Only bind to
              <code>0.0.0.0</code> with a token set.{' '}
              {ov.auth_token_set ? <span className="text-emerald-300">Currently set.</span> : 'Currently off.'}
            </p>
            <div className="flex gap-2">
              <input
                className="input"
                type="password"
                placeholder={ov.auth_token_set ? '•••••• (enter a new token to change)' : 'set a long random token'}
                value={tokenInput}
                onChange={(e) => setTokenInput(e.target.value)}
              />
              <button
                type="button"
                className="btn-secondary"
                onClick={() => {
                  void saveSecret({ set_auth_token: true, auth_token: tokenInput || null })
                  setTokenInput('')
                }}
              >
                {tokenInput ? 'Save' : ov.auth_token_set ? 'Clear' : 'Save'}
              </button>
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
