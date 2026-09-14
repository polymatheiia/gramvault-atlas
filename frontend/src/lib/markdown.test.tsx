import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { ChatMarkdown } from './markdown'

function renderMarkdown(content: string) {
  return render(
    <MemoryRouter>
      <ChatMarkdown content={content} />
    </MemoryRouter>,
  )
}

describe('ChatMarkdown', () => {
  it('renders a citation marker as a linked chip, not raw text', () => {
    renderMarkdown('see [[item:5]] for details')

    const link = screen.getByRole('link', { name: 'Item #5' })
    expect(link).toHaveAttribute('href', '/items/5')
    expect(screen.queryByText(/\[\[item:5\]\]/)).not.toBeInTheDocument()
  })

  it('renders two citation markers in one paragraph without dropping the tail text', () => {
    renderMarkdown('[[item:1]] and [[item:2]] — the rest of the sentence')

    expect(screen.getByRole('link', { name: 'Item #1' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Item #2' })).toBeInTheDocument()
    expect(screen.getByText(/the rest of the sentence/)).toBeInTheDocument()
  })

  it('renders basic GFM formatting (bold, lists)', () => {
    renderMarkdown('**bold** text\n\n- one\n- two')

    expect(screen.getByText('bold').tagName).toBe('STRONG')
    expect(screen.getByText('one').tagName).toBe('LI')
    expect(screen.getByText('two').tagName).toBe('LI')
  })

  it('strips an <img> tag instead of rendering it', () => {
    const { container } = renderMarkdown('before <img src="x" onerror="alert(1)"> after')
    expect(container.querySelector('img')).toBeNull()
  })

  it('does not render a javascript: link', () => {
    const { container } = renderMarkdown('[click me](javascript:alert(1))')
    const link = container.querySelector('a')
    // rehype-sanitize drops the disallowed href, leaving the link text but
    // no anchor with a javascript: URL for the browser to ever navigate to.
    expect(link?.getAttribute('href') ?? '').not.toContain('javascript:')
  })

  it('keeps an http(s) link and opens it in a new tab', () => {
    const { container } = renderMarkdown('[a site](https://example.com)')
    const link = container.querySelector('a')
    expect(link).toHaveAttribute('href', 'https://example.com')
    expect(link).toHaveAttribute('target', '_blank')
  })
})
