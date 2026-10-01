/**
 * Typed fetch wrapper for the GramVault backend API.
 *
 * Base URL resolution:
 *  - In production, `gramvault.main` serves the built frontend and the API
 *    from the same origin, so a relative `/api/...` path works with no
 *    configuration.
 *  - In dev, Vite's dev server proxies `/api` to the FastAPI dev server
 *    (see `vite.config.ts`), so relative paths work there too.
 *  - `VITE_API_BASE_URL` can override this (e.g. to point at a backend
 *    running on a non-default host/port without touching the proxy).
 */

import type { ChatCitation } from '../types'

const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')

// --- optional bearer token (matches gramvault.api.auth) ---
const TOKEN_KEY = 'gv_token'

export function getAuthToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY)
  } catch {
    return null
  }
}

export function setAuthToken(token: string | null): void {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token)
    else localStorage.removeItem(TOKEN_KEY)
  } catch {
    /* private mode / storage disabled — token just won't persist */
  }
}

let onUnauthorized: (() => void) | null = null

/** Register a callback fired whenever the API answers 401 (the server has
 * `auth.token` set and ours is missing/wrong). */
export function setUnauthorizedHandler(fn: (() => void) | null): void {
  onUnauthorized = fn
}

// Forces a real CORS preflight on cross-origin requests (a custom header
// isn't "simple"), which the backend's CORS policy then denies for any
// origin not on its allowlist — see gramvault.api.csrf.CrossSiteGuard.
const CLIENT_HEADER_NAME = 'X-GramVault-Client'

function authHeaders(): Record<string, string> {
  const token = getAuthToken()
  return {
    [CLIENT_HEADER_NAME]: '1',
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  }
}

export class ApiError extends Error {
  status: number
  detail: unknown

  constructor(status: number, detail: unknown, message?: string) {
    super(message ?? `API error ${status}`)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

/** True while a stub route (see backend/gramvault/api/routes_*.py) still
 * raises HTTP 501 "Not implemented". Callers use this to render a
 * friendly "not built yet" state instead of a generic error. */
export function isNotImplemented(err: unknown): boolean {
  return err instanceof ApiError && err.status === 501
}

export function isNotFound(err: unknown): boolean {
  return err instanceof ApiError && err.status === 404
}

function buildUrl(path: string, params?: Record<string, unknown>): string {
  const url = new URL(`${API_BASE_URL}${path}`, window.location.origin)
  if (params) {
    for (const [key, value] of Object.entries(params)) {
      if (value === undefined || value === null || value === '') continue
      url.searchParams.set(key, String(value))
    }
  }
  return url.pathname + url.search
}

/** Parse a response body as JSON, falling back to the raw text — an error
 * from something other than the API (a reverse proxy's HTML 413/502 page,
 * Starlette's plain-text "Invalid host header") isn't JSON. */
function parseBody(text: string): unknown {
  if (!text) return undefined
  try {
    return JSON.parse(text)
  } catch {
    return text
  }
}

function apiErrorFrom(status: number, body: unknown): ApiError {
  if (body && typeof body === 'object' && 'detail' in body) {
    const detail = (body as { detail: unknown }).detail
    return new ApiError(status, detail, typeof detail === 'string' ? detail : undefined)
  }
  // Non-JSON: show short plain text as-is, never a whole HTML page.
  const text = typeof body === 'string' ? body.trim() : ''
  const short = text && text.length <= 200 && !text.startsWith('<') ? `: ${text}` : ''
  return new ApiError(status, body, `Request failed (${status})${short}`)
}

async function parseResponse<T>(res: Response): Promise<T> {
  if (res.status === 401) onUnauthorized?.()
  if (res.status === 204) return undefined as T
  const body = parseBody(await res.text())
  if (!res.ok) throw apiErrorFrom(res.status, body)
  return body as T
}

interface RequestOptions {
  method?: string
  params?: Record<string, unknown>
  body?: unknown
  signal?: AbortSignal
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', params, body, signal } = options
  const res = await fetch(buildUrl(path, params), {
    method,
    headers: {
      ...authHeaders(),
      ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
    },
    body: body !== undefined ? JSON.stringify(body) : undefined,
    signal,
  })
  return parseResponse<T>(res)
}

export const api = {
  get: <T>(path: string, params?: Record<string, unknown>, signal?: AbortSignal) =>
    request<T>(path, { method: 'GET', params, signal }),
  post: <T>(path: string, body?: unknown, params?: Record<string, unknown>) =>
    request<T>(path, { method: 'POST', body, params }),
  put: <T>(path: string, body?: unknown) => request<T>(path, { method: 'PUT', body }),
  patch: <T>(path: string, body?: unknown) => request<T>(path, { method: 'PATCH', body }),
  delete: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
}

/** Upload a file via multipart/form-data (fetch handles the boundary when
 * we pass a FormData body directly, so this bypasses the JSON `request`
 * helper above). */
export async function uploadFile<T>(
  path: string,
  file: File,
  fieldName = 'file',
  onProgress?: (pct: number) => void,
): Promise<T> {
  // Use XHR instead of fetch so we can report upload progress.
  return new Promise<T>((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', buildUrl(path))
    xhr.setRequestHeader(CLIENT_HEADER_NAME, '1')
    const token = getAuthToken()
    if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`)
    xhr.upload.onprogress = (evt) => {
      if (onProgress && evt.lengthComputable) {
        onProgress(Math.round((evt.loaded / evt.total) * 100))
      }
    }
    xhr.onload = () => {
      // A throw in here (it used to be an unguarded JSON.parse of e.g. a
      // proxy's HTML 413 page) would leave this promise pending forever.
      const body = parseBody(xhr.responseText)
      if (xhr.status === 401) onUnauthorized?.()
      if (xhr.status >= 200 && xhr.status < 300) resolve(body as T)
      else reject(apiErrorFrom(xhr.status, body))
    }
    xhr.onerror = () => reject(new ApiError(0, null, 'Network error'))
    const form = new FormData()
    form.append(fieldName, file)
    xhr.send(form)
  })
}

/**
 * Resolve a `MediaFile.file_path` (POSIX path relative to the configured
 * library dir, e.g. "media/ab/ab34...ef.jpg" — see
 * `backend/gramvault/ingestion/organizer.py`) into a URL an `<img>`/`<video>`
 * tag can load. Served by the `/media` mount in `backend/gramvault/main.py`.
 */
export function mediaUrl(filePath: string): string {
  const encoded = filePath.split('/').map(encodeURIComponent).join('/')
  return `${API_BASE_URL}/media/${encoded}`
}

/** WebVTT captions for a media file, built from Whisper's per-segment
 * timestamps (`GET /api/library/media/{id}/captions.vtt`), as a `blob:` URL
 * for a `<track>` — or null when there are none (the endpoint 404s with no
 * transcript yet). Fetched here rather than pointing `<track src>` at the
 * API, because a track load can't carry the bearer token: with auth on
 * (the default) it was always rejected with 401. Callers own the URL and
 * must `URL.revokeObjectURL` it. */
export async function fetchCaptionsObjectUrl(
  mediaFileId: number,
  signal?: AbortSignal,
): Promise<string | null> {
  const res = await fetch(buildUrl(`/api/library/media/${mediaFileId}/captions.vtt`), {
    headers: authHeaders(),
    signal,
  })
  if (res.status === 401) onUnauthorized?.()
  if (!res.ok) return null
  const vtt = await res.text()
  return URL.createObjectURL(new Blob([vtt], { type: 'text/vtt' }))
}

export interface ChatStreamDonePayload {
  message_id: number
  content: string
  citations: ChatCitation[]
}

export interface ChatSourcesPayload {
  results: { item_id: number; media_file_id: number | null; snippet: string | null; score: number }[]
}

export interface ChatStreamHandlers {
  onSources?: (payload: ChatSourcesPayload) => void
  onToken?: (content: string) => void
  onDone?: (payload: ChatStreamDonePayload) => void
  onError?: (detail: string) => void
  /** The caller aborted `signal` (the Stop button) — not an error. Any
   * tokens already delivered via `onToken` are what the user sees; the
   * server may still persist that partial reply (see stream_message's
   * disconnect-durability guarantee), but no further `onDone` arrives. */
  onStopped?: () => void
}

/**
 * POST a new chat message and consume the Server-Sent Events response from
 * `POST /api/chat/sessions/:id/messages` (see
 * `backend/gramvault/chat/service.py::stream_message` for the exact wire
 * contract: `event: token|done|error` frames with JSON `data`).
 *
 * The browser's native `EventSource` only supports GET requests, so this
 * sends the POST via `fetch` and parses the `text/event-stream` framing
 * from the response body manually.
 */
export async function streamChatMessage(
  sessionId: number,
  content: string,
  handlers: ChatStreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  let res: Response
  try {
    res = await fetch(buildUrl(`/api/chat/sessions/${sessionId}/messages`), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ content }),
      signal,
    })
  } catch {
    if (signal?.aborted) handlers.onStopped?.()
    else handlers.onError?.('Network error while sending message')
    return
  }

  if (!res.ok || !res.body) {
    const text = await res.text().catch(() => '')
    let detail: unknown = text
    try {
      const parsed = text ? JSON.parse(text) : undefined
      detail = parsed && typeof parsed === 'object' && 'detail' in parsed ? parsed.detail : text
    } catch {
      // not JSON, fall back to raw text
    }
    handlers.onError?.(typeof detail === 'string' && detail ? detail : `Request failed (${res.status})`)
    return
  }

  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  // Whether `done` or `error` arrived. A stream that just ends without one
  // (the server hit an error it didn't turn into an event, a proxy cut the
  // connection) must still end the "Sending…" state in the caller.
  let terminal = false

  const handleFrame = (rawFrame: string) => {
    let eventName = 'message'
    const dataLines: string[] = []
    // SSE lines may end in \r\n (sse-starlette's default) or \n.
    for (const line of rawFrame.split(/\r?\n/)) {
      if (!line || line.startsWith(':')) continue // blank/comment (keep-alive)
      if (line.startsWith('event:')) eventName = line.slice('event:'.length).trim()
      else if (line.startsWith('data:')) dataLines.push(line.slice('data:'.length).trim())
    }
    if (dataLines.length === 0) return
    const data = dataLines.join('\n')
    try {
      if (eventName === 'sources') {
        handlers.onSources?.(JSON.parse(data) as ChatSourcesPayload)
      } else if (eventName === 'token') {
        const parsed = JSON.parse(data) as { content: string }
        handlers.onToken?.(parsed.content)
      } else if (eventName === 'done') {
        const parsed = JSON.parse(data) as ChatStreamDonePayload
        terminal = true
        handlers.onDone?.(parsed)
      } else if (eventName === 'error') {
        const parsed = JSON.parse(data) as { detail: string }
        terminal = true
        handlers.onError?.(parsed.detail)
      }
    } catch {
      // A malformed frame must not wedge the stream (and with it the
      // "Sending…" UI state) — skip it and keep consuming.
    }
  }

  // Frames are separated by a blank line: \r\n\r\n from sse-starlette
  // (its default line separator is \r\n), or \n\n from other servers.
  // NB: "\r\n\r\n" does NOT contain "\n\n", so both must be searched —
  // matching only \n\n silently drops every frame of a \r\n stream.
  const drainFrames = () => {
    for (;;) {
      const crlf = buffer.indexOf('\r\n\r\n')
      const lf = buffer.indexOf('\n\n')
      let index: number
      let sepLength: number
      if (crlf !== -1 && (lf === -1 || crlf < lf)) {
        index = crlf
        sepLength = 4
      } else if (lf !== -1) {
        index = lf
        sepLength = 2
      } else {
        return
      }
      handleFrame(buffer.slice(0, index))
      buffer = buffer.slice(index + sepLength)
    }
  }

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      drainFrames()
    }
    if (buffer.trim()) handleFrame(buffer)
    if (!terminal) handlers.onError?.('The connection closed before the reply finished.')
  } catch {
    // Aborting `signal` mid-stream (Stop) tears down the fetch body reader
    // the same way a network drop would — tell them apart by the signal,
    // not by rethrowing an AbortError past callers that don't expect one.
    if (signal?.aborted) handlers.onStopped?.()
    else handlers.onError?.('Connection to the server was lost')
  }
}

export { API_BASE_URL }
