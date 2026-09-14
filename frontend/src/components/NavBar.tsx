import { useState } from 'react'
import { NavLink } from 'react-router-dom'

const LINKS = [
  { to: '/', label: 'Gallery', end: true },
  { to: '/chat', label: 'Chat', end: false },
  { to: '/import', label: 'Import', end: false },
  { to: '/pull', label: 'Pull', end: false },
  { to: '/enrich', label: 'Enrich', end: false },
  { to: '/categorize', label: 'Categorize', end: false },
  { to: '/digest', label: 'Digest', end: false },
  { to: '/settings', label: 'Settings', end: false },
]

const LINK_CLASS = ({ isActive }: { isActive: boolean }) =>
  `rounded-lg px-3 py-1.5 text-sm font-medium no-underline transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent ${
    isActive
      ? 'bg-surface-overlay text-slate-100'
      : 'text-slate-400 hover:bg-surface-raised hover:text-slate-200'
  }`

export function NavBar() {
  const [open, setOpen] = useState(false)

  return (
    <header className="sticky top-0 z-20 border-b border-surface-border bg-surface/95 backdrop-blur">
      <div className="mx-auto flex max-w-6xl items-center justify-between gap-6 px-4 py-3">
        <NavLink to="/" className="flex items-center gap-2 text-slate-100 no-underline" onClick={() => setOpen(false)}>
          <span className="text-lg font-semibold tracking-tight">GramVault Atlas</span>
        </NavLink>
        <nav className="hidden items-center gap-1 md:flex">
          {LINKS.map((link) => (
            <NavLink key={link.to} to={link.to} end={link.end} className={LINK_CLASS}>
              {link.label}
            </NavLink>
          ))}
        </nav>
        <button
          type="button"
          className="btn-ghost md:hidden"
          aria-label={open ? 'Close navigation menu' : 'Open navigation menu'}
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
        >
          {open ? '✕' : '☰'}
        </button>
      </div>
      {open && (
        <nav className="flex flex-col gap-1 border-t border-surface-border px-4 py-2 md:hidden">
          {LINKS.map((link) => (
            <NavLink key={link.to} to={link.to} end={link.end} className={LINK_CLASS} onClick={() => setOpen(false)}>
              {link.label}
            </NavLink>
          ))}
        </nav>
      )}
    </header>
  )
}
