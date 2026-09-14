import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'

export interface Toast {
  id: number
  tone: 'success' | 'error' | 'info'
  message: string
  href?: string
}

interface ToastContextValue {
  toasts: Toast[]
  push: (toast: Omit<Toast, 'id'>) => void
  dismiss: (id: number) => void
}

const ToastContext = createContext<ToastContextValue | null>(null)

const AUTO_DISMISS_MS = 8000

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])
  const nextId = useRef(0)

  const dismiss = useCallback((id: number) => {
    setToasts((list) => list.filter((t) => t.id !== id))
  }, [])

  const push = useCallback(
    (toast: Omit<Toast, 'id'>) => {
      const id = nextId.current++
      setToasts((list) => [...list, { ...toast, id }])
      setTimeout(() => dismiss(id), AUTO_DISMISS_MS)
    },
    [dismiss],
  )

  return (
    <ToastContext.Provider value={{ toasts, push, dismiss }}>
      {children}
      <ToastViewport />
    </ToastContext.Provider>
  )
}

export function useToast(): ToastContextValue {
  const ctx = useContext(ToastContext)
  if (!ctx) throw new Error('useToast must be used within a ToastProvider')
  return ctx
}

const TONE_CLASSES: Record<Toast['tone'], string> = {
  success: 'border-emerald-500/40 bg-emerald-950/90',
  error: 'border-red-500/40 bg-red-950/90',
  info: 'border-surface-border bg-surface-overlay',
}

function ToastViewport() {
  const { toasts, dismiss } = useToast()
  if (toasts.length === 0) return null

  return (
    <div className="fixed bottom-4 right-4 z-50 flex w-full max-w-sm flex-col gap-2 px-4 sm:px-0">
      {toasts.map((toast) => (
        <div
          key={toast.id}
          role="status"
          className={`flex items-start justify-between gap-3 rounded-lg border px-4 py-3 text-sm text-slate-100 shadow-lg ${TONE_CLASSES[toast.tone]}`}
        >
          <div className="flex-1">
            {toast.href ? (
              <Link to={toast.href} className="no-underline hover:underline" onClick={() => dismiss(toast.id)}>
                {toast.message}
              </Link>
            ) : (
              toast.message
            )}
          </div>
          <button
            type="button"
            aria-label="Dismiss notification"
            className="text-slate-400 hover:text-slate-200"
            onClick={() => dismiss(toast.id)}
          >
            ✕
          </button>
        </div>
      ))}
    </div>
  )
}
