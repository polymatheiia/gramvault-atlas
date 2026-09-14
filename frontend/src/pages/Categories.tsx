import { useEffect, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { ApiError, api } from '../api/client'
import type { CategoryCreateRequest, CategoryListResponse, CategoryUpdateRequest, CategoryWithCount } from '../types'

const DEFAULT_COLOR = '#64748b'

function CategoryRow({
  category,
  isFirst,
  isLast,
  allCategories,
  onMove,
  onSave,
  onDelete,
}: {
  category: CategoryWithCount
  isFirst: boolean
  isLast: boolean
  allCategories: CategoryWithCount[]
  onMove: (category: CategoryWithCount, direction: 'up' | 'down') => void
  onSave: (id: number, body: CategoryUpdateRequest) => Promise<void>
  onDelete: (category: CategoryWithCount, moveTo: number | null) => Promise<void>
}) {
  const [editing, setEditing] = useState(false)
  const [name, setName] = useState(category.name)
  const [description, setDescription] = useState(category.description ?? '')
  const [color, setColor] = useState(category.color ?? DEFAULT_COLOR)
  const [saving, setSaving] = useState(false)
  const [confirmingDelete, setConfirmingDelete] = useState(false)
  const [moveTo, setMoveTo] = useState<string>('')
  const [error, setError] = useState<string | null>(null)

  const nameChanged = name.trim() !== category.name

  async function save() {
    const trimmed = name.trim()
    if (!trimmed) {
      setError('Name must not be empty')
      return
    }
    setSaving(true)
    setError(null)
    try {
      await onSave(category.id as number, {
        name: trimmed !== category.name ? trimmed : undefined,
        description: description !== (category.description ?? '') ? description || null : undefined,
        color: color !== (category.color ?? DEFAULT_COLOR) ? color : undefined,
      })
      setEditing(false)
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail) : err instanceof Error ? err.message : 'Failed to save')
    } finally {
      setSaving(false)
    }
  }

  async function confirmDelete() {
    setSaving(true)
    setError(null)
    try {
      await onDelete(category, moveTo ? Number(moveTo) : null)
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail) : err instanceof Error ? err.message : 'Failed to delete')
      setSaving(false)
    }
  }

  const otherCategories = allCategories.filter((c) => c.id !== category.id)

  return (
    <div className="card flex flex-col gap-2 px-4 py-3">
      <div className="flex items-center gap-3">
        <span
          className="h-4 w-4 shrink-0 rounded-full border border-surface-border"
          style={{ backgroundColor: category.color ?? DEFAULT_COLOR }}
          aria-hidden
        />
        <div className="flex flex-1 items-center gap-2">
          {editing ? (
            <input className="input w-auto flex-1" value={name} onChange={(e) => setName(e.target.value)} />
          ) : (
            <span className="text-sm text-slate-100">{category.name}</span>
          )}
          <span className="badge bg-surface-overlay text-slate-400">{category.count}</span>
        </div>
        <div className="flex items-center gap-1">
          <button
            type="button"
            aria-label="Move up"
            title="Move up"
            className="btn-secondary px-2 py-1 text-xs disabled:opacity-30"
            disabled={isFirst}
            onClick={() => onMove(category, 'up')}
          >
            ↑
          </button>
          <button
            type="button"
            aria-label="Move down"
            title="Move down"
            className="btn-secondary px-2 py-1 text-xs disabled:opacity-30"
            disabled={isLast}
            onClick={() => onMove(category, 'down')}
          >
            ↓
          </button>
          {editing ? (
            <>
              <button type="button" className="btn-primary px-2 py-1 text-xs" disabled={saving} onClick={() => void save()}>
                Save
              </button>
              <button
                type="button"
                className="btn-secondary px-2 py-1 text-xs"
                onClick={() => {
                  setEditing(false)
                  setName(category.name)
                  setDescription(category.description ?? '')
                  setColor(category.color ?? DEFAULT_COLOR)
                  setError(null)
                }}
              >
                Cancel
              </button>
            </>
          ) : (
            <>
              <input
                type="color"
                aria-label="Category color"
                className="h-7 w-7 cursor-pointer rounded border border-surface-border bg-transparent"
                value={category.color ?? DEFAULT_COLOR}
                onChange={(e) => void onSave(category.id as number, { color: e.target.value })}
              />
              <button type="button" className="btn-secondary px-2 py-1 text-xs" onClick={() => setEditing(true)}>
                Edit
              </button>
              <button
                type="button"
                className="btn-secondary px-2 py-1 text-xs text-red-300 hover:text-red-200"
                onClick={() => setConfirmingDelete(true)}
              >
                Delete
              </button>
            </>
          )}
        </div>
      </div>

      {editing && (
        <div className="flex flex-col gap-1 pl-7">
          <label className="flex items-center gap-2 text-xs text-slate-500">
            Color
            <input type="color" value={color} onChange={(e) => setColor(e.target.value)} className="h-6 w-6 cursor-pointer rounded" />
          </label>
          <textarea
            className="input text-xs"
            rows={2}
            placeholder="Description — this is the rubric the LLM pass uses to decide this category"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
          {nameChanged && (
            <p className="text-xs text-amber-400">
              Renaming disables keyword-based auto-categorization for this category until its keyword
              list is retuned for the new name — the LLM pass and manual assignment are unaffected.
            </p>
          )}
        </div>
      )}

      {confirmingDelete && (
        <div className="flex flex-wrap items-center gap-2 border-t border-surface-border pt-2 pl-7 text-xs text-slate-400">
          {category.count > 0 ? (
            <>
              <span>{category.count} item(s) use this category — move them to:</span>
              <select className="input w-auto py-1 text-xs" value={moveTo} onChange={(e) => setMoveTo(e.target.value)}>
                <option value="">— uncategorized —</option>
                {otherCategories.map((c) => (
                  <option key={c.id ?? c.name} value={c.id ?? ''}>
                    {c.name}
                  </option>
                ))}
              </select>
            </>
          ) : (
            <span>Delete "{category.name}"?</span>
          )}
          <button type="button" className="btn-primary px-2 py-1 text-xs" disabled={saving} onClick={() => void confirmDelete()}>
            Confirm delete
          </button>
          <button type="button" className="btn-secondary px-2 py-1 text-xs" onClick={() => setConfirmingDelete(false)}>
            Cancel
          </button>
        </div>
      )}

      {error && <p className="pl-7 text-xs text-red-300">{error}</p>}
    </div>
  )
}

export function Categories() {
  const [data, setData] = useState<CategoryListResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [newName, setNewName] = useState('')
  const [creating, setCreating] = useState(false)

  function refresh() {
    api
      .get<CategoryListResponse>('/api/library/categories')
      .then(setData)
      .catch((err) => setError(err instanceof Error ? err.message : 'Failed to load categories'))
  }

  useEffect(refresh, [])

  const categories = data?.categories ?? []

  async function createCategory(e: FormEvent) {
    e.preventDefault()
    const name = newName.trim()
    if (!name) return
    setCreating(true)
    setError(null)
    try {
      const body: CategoryCreateRequest = { name }
      await api.post('/api/library/categories', body)
      setNewName('')
      refresh()
    } catch (err) {
      setError(err instanceof ApiError ? String(err.detail) : err instanceof Error ? err.message : 'Failed to create category')
    } finally {
      setCreating(false)
    }
  }

  async function saveCategory(id: number, body: CategoryUpdateRequest) {
    await api.patch(`/api/library/categories/${id}`, body)
    refresh()
  }

  async function moveCategory(category: CategoryWithCount, direction: 'up' | 'down') {
    const sorted = [...categories].sort((a, b) => a.sort_order - b.sort_order)
    const idx = sorted.findIndex((c) => c.id === category.id)
    const swapIdx = direction === 'up' ? idx - 1 : idx + 1
    const neighbor = sorted[swapIdx]
    if (!neighbor || category.id == null || neighbor.id == null) return
    await Promise.all([
      api.patch(`/api/library/categories/${category.id}`, { sort_order: neighbor.sort_order }),
      api.patch(`/api/library/categories/${neighbor.id}`, { sort_order: category.sort_order }),
    ])
    refresh()
  }

  async function deleteCategory(category: CategoryWithCount, moveTo: number | null) {
    if (category.id == null) return
    await api.delete(`/api/library/categories/${category.id}${moveTo != null ? `?move_to=${moveTo}` : ''}`)
    refresh()
  }

  const sorted = [...categories].sort((a, b) => a.sort_order - b.sort_order)

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-6 px-4 py-6">
      <section className="flex flex-col gap-1">
        <h1 className="text-lg font-semibold text-slate-100">Categories</h1>
        <p className="text-sm text-slate-400">
          The taxonomy the classifier sorts items into.{' '}
          {data && `${data.total} items, ${data.uncategorized_count} uncategorized.`} Each
          description is the rubric the LLM pass uses — write it as an instruction, not a label.
        </p>
      </section>

      {error && <p className="card border-red-900/60 bg-red-950/40 px-4 py-2 text-sm text-red-300">{error}</p>}

      <form onSubmit={(e) => void createCategory(e)} className="flex gap-2">
        <input
          className="input"
          placeholder="New category name…"
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          disabled={creating}
        />
        <button type="submit" className="btn-primary" disabled={creating || !newName.trim()}>
          Add
        </button>
      </form>

      <section className="flex flex-col gap-2">
        {sorted.map((c, idx) => (
          <CategoryRow
            key={c.id ?? c.name}
            category={c}
            isFirst={idx === 0}
            isLast={idx === sorted.length - 1}
            allCategories={sorted}
            onMove={(cat, dir) => void moveCategory(cat, dir)}
            onSave={saveCategory}
            onDelete={deleteCategory}
          />
        ))}
        {sorted.length === 0 && data && <p className="text-sm text-slate-500">No categories yet.</p>}
      </section>

      <p className="text-xs text-slate-500">
        Review queue and bulk triage live on the <Link to="/categorize" className="text-accent no-underline hover:underline">Categorize</Link> page.
      </p>
    </div>
  )
}
