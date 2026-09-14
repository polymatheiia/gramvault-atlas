import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { renderMessageWithCitations } from './citations'
import type { ChatCitation } from '../types'

function renderText(text: string, citations: ChatCitation[] = []) {
  return render(
    <MemoryRouter>
      <div>{renderMessageWithCitations(text, citations)}</div>
    </MemoryRouter>,
  )
}

describe('renderMessageWithCitations', () => {
  it('renders plain text with no citation markers unchanged', () => {
    renderText('no citations here')
    expect(screen.getByText('no citations here')).toBeInTheDocument()
  })

  it('replaces a citation marker with a linked chip, dropping the raw marker text', () => {
    renderText('You saved a pasta recipe [[item:42]] last spring.')

    const chip = screen.getByRole('link', { name: 'Item #42' })
    expect(chip).toHaveAttribute('href', '/items/42')
    expect(screen.queryByText(/\[\[item:42\]\]/)).not.toBeInTheDocument()
    expect(screen.getByText(/You saved a pasta recipe/)).toBeInTheDocument()
    expect(screen.getByText(/last spring\./)).toBeInTheDocument()
  })

  it('renders back-to-back citations as separate chips', () => {
    renderText('See [[item:1]][[item:2]] for both.')
    expect(screen.getByRole('link', { name: 'Item #1' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Item #2' })).toBeInTheDocument()
  })

  it('passes a matching citation snippet through as the chip title/tooltip', () => {
    renderText('[[item:5]]', [{ id: 1, message_id: 1, item_id: 5, media_file_id: null, snippet: 'a snippet' }])
    expect(screen.getByRole('link', { name: 'Item #5' })).toHaveAttribute('title', 'a snippet')
  })

  it('a marker with no matching citation entry still renders a chip, with no title', () => {
    renderText('[[item:99]]', [])
    const chip = screen.getByRole('link', { name: 'Item #99' })
    expect(chip).not.toHaveAttribute('title')
  })
})
