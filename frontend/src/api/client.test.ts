import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  ApiError,
  api,
  getAuthToken,
  isNotFound,
  isNotImplemented,
  mediaUrl,
  setAuthToken,
  setUnauthorizedHandler,
  streamChatMessage,
  uploadFile,
} from './client'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('api request headers', () => {
  beforeEach(() => {
    setAuthToken(null)
  })

  it('sends the X-GramVault-Client header and no Authorization when no token is stored', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)

    await api.get('/api/library/items')

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    const headers = init.headers as Record<string, string>
    expect(headers['X-GramVault-Client']).toBe('1')
    expect(headers.Authorization).toBeUndefined()
  })

  it('sends a Bearer Authorization header once a token is stored', async () => {
    setAuthToken('secret-token')
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)

    await api.get('/api/library/items')

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    const headers = init.headers as Record<string, string>
    expect(headers.Authorization).toBe('Bearer secret-token')
  })

  it('round-trips the token through localStorage via getAuthToken/setAuthToken', () => {
    expect(getAuthToken()).toBeNull()
    setAuthToken('abc')
    expect(getAuthToken()).toBe('abc')
    setAuthToken(null)
    expect(getAuthToken()).toBeNull()
  })
})

describe('parseResponse / ApiError', () => {
  afterEach(() => {
    setUnauthorizedHandler(null)
  })

  it('throws ApiError with the detail field on a non-2xx JSON response', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ detail: 'not found' }, 404)))

    const err = await api.get('/api/library/items/999').catch((e: unknown) => e)
    expect(err).toBeInstanceOf(ApiError)
    expect((err as ApiError).status).toBe(404)
    expect((err as ApiError).detail).toBe('not found')
    expect(isNotFound(err)).toBe(true)
    expect(isNotImplemented(err)).toBe(false)
  })

  it('fires the unauthorized handler exactly once on a 401', async () => {
    const onUnauthorized = vi.fn()
    setUnauthorizedHandler(onUnauthorized)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ detail: 'nope' }, 401)))

    await api.get('/api/jobs').catch(() => undefined)

    expect(onUnauthorized).toHaveBeenCalledTimes(1)
  })

  it('treats a 204 as an empty success rather than a JSON parse attempt', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(null, { status: 204 })))
    await expect(api.delete('/api/library/items/1')).resolves.toBeUndefined()
  })
})

describe('mediaUrl', () => {
  it('percent-encodes each path segment without escaping the slashes', () => {
    // The leading `/media` is the mount prefix; `media/ab/...` is the
    // library-relative path, so the doubled `media/` here is expected.
    expect(mediaUrl('media/ab/ab34 space.jpg')).toBe('/media/media/ab/ab34%20space.jpg')
  })
})

describe('uploadFile (XHR path)', () => {
  class FakeXHR {
    static instances: FakeXHR[] = []
    method = ''
    url = ''
    status = 200
    responseText = '{}'
    upload = { onprogress: null as ((evt: ProgressEvent) => void) | null }
    onload: (() => void) | null = null
    onerror: (() => void) | null = null
    headers: Record<string, string> = {}
    sentBody: unknown = null

    constructor() {
      FakeXHR.instances.push(this)
    }
    open(method: string, url: string) {
      this.method = method
      this.url = url
    }
    setRequestHeader(name: string, value: string) {
      this.headers[name] = value
    }
    send(body: unknown) {
      this.sentBody = body
      queueMicrotask(() => this.onload?.())
    }
  }

  beforeEach(() => {
    FakeXHR.instances = []
    setAuthToken(null)
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    vi.stubGlobal('XMLHttpRequest', FakeXHR as any)
  })

  it('sends the client header and bearer token, and posts the file as FormData', async () => {
    setAuthToken('up-token')
    const file = new File(['hello'], 'clip.mp4', { type: 'video/mp4' })

    await uploadFile('/api/import/upload', file)

    const xhr = FakeXHR.instances[0]
    expect(xhr.method).toBe('POST')
    expect(xhr.headers['X-GramVault-Client']).toBe('1')
    expect(xhr.headers.Authorization).toBe('Bearer up-token')
    expect(xhr.sentBody).toBeInstanceOf(FormData)
    expect((xhr.sentBody as FormData).get('file')).toBe(file)
  })

  it('omits Authorization when no token is stored', async () => {
    const file = new File(['hello'], 'clip.mp4')
    await uploadFile('/api/import/upload', file)
    expect(FakeXHR.instances[0].headers.Authorization).toBeUndefined()
  })
})

describe('streamChatMessage', () => {
  function sseBody(text: string): Response {
    const encoder = new TextEncoder()
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode(text))
        controller.close()
      },
    })
    return new Response(stream, { status: 200 })
  }

  it('sends the client header and bearer token on the streaming POST', async () => {
    setAuthToken('stream-token')
    const fetchMock = vi.fn().mockResolvedValue(sseBody('event: done\r\ndata: {"message_id":1,"content":"hi","citations":[]}\r\n\r\n'))
    vi.stubGlobal('fetch', fetchMock)

    await streamChatMessage(1, 'hello', {})

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit]
    const headers = init.headers as Record<string, string>
    expect(headers['X-GramVault-Client']).toBe('1')
    expect(headers.Authorization).toBe('Bearer stream-token')
  })

  it('parses \\r\\n-framed token/done events into their handlers', async () => {
    setAuthToken(null)
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        sseBody(
          'event: token\r\ndata: {"content":"Hi "}\r\n\r\n' +
            'event: token\r\ndata: {"content":"there"}\r\n\r\n' +
            'event: done\r\ndata: {"message_id":7,"content":"Hi there","citations":[]}\r\n\r\n',
        ),
      ),
    )

    const tokens: string[] = []
    let done: unknown = null
    await streamChatMessage(1, 'hello', {
      onToken: (t) => tokens.push(t),
      onDone: (payload) => {
        done = payload
      },
    })

    expect(tokens).toEqual(['Hi ', 'there'])
    expect(done).toEqual({ message_id: 7, content: 'Hi there', citations: [] })
  })

  it('parses \\n-framed events too (a non-sse-starlette server)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(sseBody('event: token\ndata: {"content":"hi"}\n\n')),
    )
    const tokens: string[] = []
    await streamChatMessage(1, 'hello', { onToken: (t) => tokens.push(t) })
    expect(tokens).toEqual(['hi'])
  })

  it('calls onError when the stream ends without a done or error event', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(sseBody('event: token\r\ndata: {"content":"partial"}\r\n\r\n')),
    )
    const tokens: string[] = []
    let error: string | undefined
    await streamChatMessage(1, 'hello', { onToken: (t) => tokens.push(t), onError: (d) => (error = d) })
    expect(tokens).toEqual(['partial'])
    expect(error).toMatch(/closed before the reply finished/)
  })

  it('does not call onError after a done event', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(sseBody('event: done\r\ndata: {"message_id":1,"content":"hi","citations":[]}\r\n\r\n')),
    )
    const onError = vi.fn()
    await streamChatMessage(1, 'hello', { onError })
    expect(onError).not.toHaveBeenCalled()
  })

  it('calls onError with the response detail on a non-ok response', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: 'no chat model' }), { status: 503 })),
    )
    let error: string | undefined
    await streamChatMessage(1, 'hello', { onError: (d) => (error = d) })
    expect(error).toBe('no chat model')
  })

  it('a malformed frame does not wedge the stream — later valid frames still arrive', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        sseBody('event: token\r\ndata: not json\r\n\r\nevent: token\r\ndata: {"content":"ok"}\r\n\r\n'),
      ),
    )
    const tokens: string[] = []
    await streamChatMessage(1, 'hello', { onToken: (t) => tokens.push(t) })
    expect(tokens).toEqual(['ok'])
  })

  it('parses a sources event before the first token', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        sseBody(
          'event: sources\r\ndata: {"results":[{"item_id":3,"media_file_id":null,"snippet":"s","score":0.9}]}\r\n\r\n' +
            'event: token\r\ndata: {"content":"hi"}\r\n\r\n',
        ),
      ),
    )
    let sources: unknown = null
    await streamChatMessage(1, 'hello', { onSources: (s) => (sources = s) })
    expect(sources).toEqual({ results: [{ item_id: 3, media_file_id: null, snippet: 's', score: 0.9 }] })
  })

  it('calls onStopped instead of onError when an already-aborted signal breaks the read loop', async () => {
    const controller = new AbortController()
    const stream = new ReadableStream<Uint8Array>({
      pull(streamController) {
        controller.abort()
        streamController.error(new DOMException('The user aborted a request.', 'AbortError'))
      },
    })
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(stream, { status: 200 })))

    let stopped = false
    let error: string | undefined
    await streamChatMessage(1, 'hello', { onStopped: () => (stopped = true), onError: (d) => (error = d) }, controller.signal)

    expect(stopped).toBe(true)
    expect(error).toBeUndefined()
  })
})
