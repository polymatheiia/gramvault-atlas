import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AuthGate } from './AuthGate'
import { getAuthToken, setAuthToken } from '../api/client'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status })
}

describe('AuthGate', () => {
  beforeEach(() => {
    setAuthToken(null)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders children immediately while the probe request succeeds', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ jobs: [] })))

    render(
      <AuthGate>
        <div>protected content</div>
      </AuthGate>,
    )

    expect(screen.getByText('protected content')).toBeInTheDocument()
    // Give the fire-and-forget probe a tick to resolve so it doesn't leak
    // into the next test as an unhandled state update.
    await waitFor(() => expect(fetch).toHaveBeenCalled())
  })

  it('shows the token prompt once the startup probe 401s', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ detail: 'unauthorized' }, 401)))

    render(
      <AuthGate>
        <div>protected content</div>
      </AuthGate>,
    )

    expect(await screen.findByText('API token required')).toBeInTheDocument()
    expect(screen.queryByText('protected content')).not.toBeInTheDocument()
    // No token was ever stored, so this isn't framed as a rejection.
    expect(screen.queryByText('The stored token was rejected.')).not.toBeInTheDocument()
  })

  it('frames a 401 with an existing stored token as a rejection', async () => {
    setAuthToken('a-bad-token')
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ detail: 'unauthorized' }, 401)))

    render(
      <AuthGate>
        <div>protected content</div>
      </AuthGate>,
    )

    expect(await screen.findByText('The stored token was rejected.')).toBeInTheDocument()
  })

  it('submitting the form stores the token and reloads the page', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ detail: 'unauthorized' }, 401)))
    const reload = vi.fn()
    vi.stubGlobal('location', { ...window.location, reload })

    render(
      <AuthGate>
        <div>protected content</div>
      </AuthGate>,
    )

    const input = await screen.findByPlaceholderText('Bearer token')
    await userEvent.type(input, 'my-new-token')
    await userEvent.click(screen.getByRole('button', { name: 'Unlock' }))

    expect(getAuthToken()).toBe('my-new-token')
    expect(reload).toHaveBeenCalled()
  })
})
