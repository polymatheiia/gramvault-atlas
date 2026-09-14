import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { api, type ChatSourcesPayload, streamChatMessage } from '../api/client'
import { ChatMarkdown } from '../lib/markdown'
import type { ChatMessage, ChatSession } from '../types'

type SourceResult = ChatSourcesPayload['results'][number]

interface DraftMessage {
  role: 'user' | 'assistant'
  content: string
  citations: ChatMessage['citations']
  streaming?: boolean
  /** Populated from the `sources` SSE frame, which arrives before any
   * token — what retrieval actually found, not filtered down to what the
   * model chose to cite (UX-5). `undefined` for a historical message
   * loaded from GET .../messages, which doesn't carry this. */
  sources?: SourceResult[]
}

export function Chat() {
  const [sessions, setSessions] = useState<ChatSession[]>([])
  const [activeId, setActiveId] = useState<number | null>(null)
  const [messages, setMessages] = useState<DraftMessage[]>([])
  const [input, setInput] = useState('')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [expandedSources, setExpandedSources] = useState<number | null>(null)
  const [renamingId, setRenamingId] = useState<number | null>(null)
  const [renameValue, setRenameValue] = useState('')
  const bottomRef = useRef<HTMLDivElement>(null)
  const hasAutoSelected = useRef(false)
  const abortRef = useRef<AbortController | null>(null)
  // Set when handleSend creates a session inline: the setActiveId below
  // would otherwise trigger the messages-fetch effect mid-stream, and the
  // server's (still incomplete) list would clobber the optimistic user
  // bubble + streaming draft.
  const skipNextMessagesFetch = useRef(false)

  function loadSessions() {
    api
      .get<ChatSession[]>('/api/chat/sessions')
      .then((list) => {
        setSessions(list)
        if (!hasAutoSelected.current && list.length > 0 && list[0].id !== null) {
          hasAutoSelected.current = true
          setActiveId(list[0].id)
        }
      })
      .catch(() => setSessions([]))
  }

  useEffect(loadSessions, [])

  useEffect(() => {
    if (activeId === null) {
      setMessages([])
      return
    }
    if (skipNextMessagesFetch.current) {
      skipNextMessagesFetch.current = false
      return
    }
    api
      .get<ChatMessage[]>(`/api/chat/sessions/${activeId}/messages`)
      .then((list) =>
        setMessages(list.map((m) => ({ role: m.role === 'user' ? 'user' : 'assistant', content: m.content, citations: m.citations }))),
      )
      .catch(() => setMessages([]))
  }, [activeId])

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  // Abort an in-flight stream if the user navigates away mid-response,
  // rather than leaving a detached fetch running against a page that's
  // no longer showing its output.
  useEffect(() => () => abortRef.current?.abort(), [])

  async function newSession() {
    const session = await api.post<ChatSession>('/api/chat/sessions', { title: null })
    setSessions((prev) => [session, ...prev])
    setActiveId(session.id)
  }

  function startRename(session: ChatSession) {
    if (session.id == null) return
    setRenamingId(session.id)
    setRenameValue(session.title ?? '')
  }

  async function commitRename() {
    if (renamingId == null) return
    const title = renameValue.trim() || null
    setRenamingId(null)
    try {
      const updated = await api.patch<ChatSession>(`/api/chat/sessions/${renamingId}`, { title })
      setSessions((prev) => prev.map((s) => (s.id === renamingId ? updated : s)))
    } catch {
      loadSessions()
    }
  }

  async function deleteSession(session: ChatSession) {
    if (session.id == null) return
    if (!confirm(`Delete "${session.title || `Session ${session.id}`}"? This can't be undone.`)) return
    await api.delete(`/api/chat/sessions/${session.id}`)
    setSessions((prev) => prev.filter((s) => s.id !== session.id))
    if (activeId === session.id) {
      hasAutoSelected.current = false
      setActiveId(null)
    }
  }

  function stopStreaming() {
    abortRef.current?.abort()
  }

  async function handleSend(e: FormEvent) {
    e.preventDefault()
    const content = input.trim()
    if (!content || sending) return

    let sessionId = activeId
    if (sessionId === null) {
      const session = await api.post<ChatSession>('/api/chat/sessions', { title: null })
      setSessions((prev) => [session, ...prev])
      sessionId = session.id
      skipNextMessagesFetch.current = true
      setActiveId(sessionId)
    }
    if (sessionId === null) return

    setInput('')
    setError(null)
    setSending(true)
    setMessages((prev) => [
      ...prev,
      { role: 'user', content, citations: [] },
      { role: 'assistant', content: '', citations: [], streaming: true },
    ])

    function finalizeStreamingMessage(update: Partial<DraftMessage>) {
      setMessages((prev) => {
        const next = [...prev]
        const last = next[next.length - 1]
        if (last?.streaming) next[next.length - 1] = { ...last, streaming: false, ...update }
        return next
      })
    }

    const controller = new AbortController()
    abortRef.current = controller

    await streamChatMessage(
      sessionId,
      content,
      {
        onSources: (payload) => {
          setMessages((prev) => {
            const next = [...prev]
            const last = next[next.length - 1]
            if (last?.streaming) next[next.length - 1] = { ...last, sources: payload.results }
            return next
          })
        },
        onToken: (chunk) => {
          setMessages((prev) => {
            const next = [...prev]
            const last = next[next.length - 1]
            if (last?.streaming) next[next.length - 1] = { ...last, content: last.content + chunk }
            return next
          })
        },
        onDone: (payload) => {
          finalizeStreamingMessage({ content: payload.content, citations: payload.citations })
          setSending(false)
          abortRef.current = null
          loadSessions()
        },
        onStopped: () => {
          finalizeStreamingMessage({})
          setSending(false)
          abortRef.current = null
        },
        onError: (detail) => {
          setError(detail)
          setMessages((prev) => prev.filter((m) => !m.streaming))
          setSending(false)
          abortRef.current = null
        },
      },
      controller.signal,
    )
  }

  return (
    <div className="mx-auto flex h-[calc(100vh-57px)] max-w-6xl gap-4 px-4 py-4">
      <aside className="card flex w-56 shrink-0 flex-col gap-1 overflow-y-auto p-2">
        <button type="button" className="btn-secondary mb-2 w-full" onClick={() => void newSession()}>
          + New chat
        </button>
        {sessions.map((s) => (
          <div key={s.id} className="group flex items-center gap-1">
            {renamingId === s.id ? (
              <form
                className="flex-1"
                onSubmit={(e) => {
                  e.preventDefault()
                  void commitRename()
                }}
              >
                <input
                  className="input w-full py-1 text-sm"
                  autoFocus
                  value={renameValue}
                  onChange={(e) => setRenameValue(e.target.value)}
                  onBlur={() => void commitRename()}
                  onKeyDown={(e) => {
                    if (e.key === 'Escape') setRenamingId(null)
                  }}
                />
              </form>
            ) : (
              <button
                type="button"
                onClick={() => setActiveId(s.id)}
                className={`flex-1 truncate rounded-lg px-2.5 py-2 text-left text-sm ${
                  s.id === activeId ? 'bg-surface-overlay text-slate-100' : 'text-slate-400 hover:bg-surface-raised'
                }`}
              >
                {s.title || `Session ${s.id}`}
              </button>
            )}
            <button
              type="button"
              aria-label="Rename chat"
              title="Rename"
              className="shrink-0 px-1 text-xs text-slate-500 opacity-0 hover:text-slate-200 group-hover:opacity-100"
              onClick={() => startRename(s)}
            >
              ✎
            </button>
            <button
              type="button"
              aria-label="Delete chat"
              title="Delete"
              className="shrink-0 px-1 text-xs text-slate-500 opacity-0 hover:text-red-300 group-hover:opacity-100"
              onClick={() => void deleteSession(s)}
            >
              ✕
            </button>
          </div>
        ))}
        {sessions.length === 0 && <p className="px-2 py-2 text-xs text-slate-500">No sessions yet.</p>}
      </aside>

      <section className="flex flex-1 flex-col gap-3">
        <div className="card flex flex-1 flex-col gap-3 overflow-y-auto p-4">
          {messages.length === 0 && <p className="text-sm text-slate-500">Ask something about your saved library.</p>}
          {messages.map((m, idx) => (
            <div key={idx} className={`flex flex-col gap-1 ${m.role === 'user' ? 'items-end' : 'items-start'}`}>
              <div
                className={`max-w-[75%] rounded-xl px-3.5 py-2.5 text-sm ${
                  m.role === 'user' ? 'bg-accent whitespace-pre-wrap text-slate-950' : 'bg-surface-overlay text-slate-100'
                }`}
              >
                {m.content ? (
                  m.role === 'assistant' ? (
                    <ChatMarkdown content={m.content} citations={m.citations} />
                  ) : (
                    m.content
                  )
                ) : (
                  m.streaming && <span className="text-slate-400">…</span>
                )}
              </div>
              {m.role === 'assistant' && m.sources && m.sources.length > 0 && (
                <div className="max-w-[75%] text-xs text-slate-500">
                  <button
                    type="button"
                    className="text-slate-500 hover:text-slate-300"
                    onClick={() => setExpandedSources((cur) => (cur === idx ? null : idx))}
                  >
                    {expandedSources === idx ? '▾' : '▸'} Sources ({m.sources.length})
                  </button>
                  {expandedSources === idx && (
                    <ul className="mt-1 flex flex-col gap-1 border-l border-surface-border pl-2">
                      {m.sources.map((s, i) => (
                        <li key={i}>
                          <Link to={`/items/${s.item_id}`} className="text-accent no-underline hover:underline">
                            Item #{s.item_id}
                          </Link>
                          {s.snippet && <span className="ml-1 text-slate-500">— {s.snippet}</span>}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              )}
            </div>
          ))}
          <div ref={bottomRef} />
        </div>

        {error && <p className="card border-red-900/60 bg-red-950/40 px-4 py-2 text-sm text-red-300">{error}</p>}

        <form onSubmit={(e) => void handleSend(e)} className="flex gap-2">
          <input
            className="input"
            placeholder="Ask about your library…"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            disabled={sending}
          />
          {sending ? (
            <button type="button" className="btn-secondary" onClick={stopStreaming}>
              Stop
            </button>
          ) : (
            <button type="submit" className="btn-primary" disabled={!input.trim()}>
              Send
            </button>
          )}
        </form>
      </section>
    </div>
  )
}
