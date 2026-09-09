import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { api, mediaUrl } from '../api/client'
import { filterSearch, useSiblingIds } from '../lib/gallery'
import type { Item, ItemListResponse, MediaFile } from '../types'

const WINDOW_BEHIND = 1
const WINDOW_AHEAD = 3

function FeedMedia({ item, active }: { item: Item; active: boolean }) {
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const [muted, setMuted] = useState(true)
  const [paused, setPaused] = useState(false)

  const first = item.media_files[0] as MediaFile | undefined

  useEffect(() => {
    const v = videoRef.current
    if (!v) return
    if (active && !paused) {
      v.play().catch(() => undefined)
    } else {
      v.pause()
      if (!active) {
        v.currentTime = 0
        if (paused) setPaused(false)
      }
    }
  }, [active, paused])

  if (!first) {
    return <div className="flex h-full w-full items-center justify-center text-slate-600">No media</div>
  }

  if (first.media_type === 'video') {
    return (
      <div className="relative flex h-full w-full items-center justify-center">
        <video
          ref={videoRef}
          src={mediaUrl(first.file_path)}
          className="max-h-full max-w-full"
          loop
          muted={muted}
          playsInline
          preload={active ? 'auto' : 'metadata'}
          onClick={() => setPaused((p) => !p)}
        />
        {paused && (
          <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
            <span className="rounded-full bg-black/50 px-5 py-3 text-2xl text-white">▶</span>
          </div>
        )}
        <button
          type="button"
          className="absolute right-3 top-3 rounded-full bg-black/50 px-3 py-1.5 text-xs text-white"
          onClick={() => setMuted((m) => !m)}
        >
          {muted ? '🔇 Unmute' : '🔊 Mute'}
        </button>
      </div>
    )
  }

  if (item.media_files.length > 1) {
    return (
      <div className="flex h-full w-full snap-x snap-mandatory items-center overflow-x-auto">
        {item.media_files.map((f) => (
          <div
            key={f.id ?? f.file_path}
            className="flex h-full w-full shrink-0 snap-center items-center justify-center"
          >
            {f.media_type === 'video' ? (
              <video src={mediaUrl(f.file_path)} className="max-h-full max-w-full" controls playsInline />
            ) : (
              <img src={mediaUrl(f.file_path)} alt="" className="max-h-full max-w-full object-contain" />
            )}
          </div>
        ))}
      </div>
    )
  }

  return (
    <img
      src={mediaUrl(first.file_path)}
      alt={item.caption ?? ''}
      className="max-h-full max-w-full object-contain"
    />
  )
}

function FeedSlide({ item, active, ctx }: { item: Item | undefined; active: boolean; ctx: string }) {
  const [expanded, setExpanded] = useState(false)

  return (
    <div className="relative flex h-full w-full snap-start snap-always items-center justify-center">
      {item ? (
        <>
          <FeedMedia item={item} active={active} />
          <div className="pointer-events-none absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/80 to-transparent p-4 pt-16">
            <div className="pointer-events-auto mx-auto flex max-w-2xl flex-col gap-2">
              <div className="flex flex-wrap items-center gap-2 text-sm text-white">
                <span className="font-medium">@{item.author?.username ?? 'unknown'}</span>
                {item.category && <span className="badge bg-white/15 text-white">{item.category}</span>}
              </div>
              {item.caption && (
                <p
                  className={`text-sm text-slate-200 ${expanded ? '' : 'line-clamp-2'}`}
                  onClick={() => setExpanded((e) => !e)}
                >
                  {item.caption}
                </p>
              )}
              <div className="flex gap-3 text-xs">
                <Link to={`/items/${item.id}${ctx}`} className="text-accent no-underline hover:underline">
                  Details
                </Link>
                {item.permalink && (
                  <a
                    href={item.permalink}
                    target="_blank"
                    rel="noreferrer"
                    className="text-accent no-underline hover:underline"
                  >
                    Instagram
                  </a>
                )}
              </div>
            </div>
          </div>
        </>
      ) : (
        <div className="text-sm text-slate-600">Loading…</div>
      )}
    </div>
  )
}

export function Feed() {
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const { ids, loading } = useSiblingIds(searchParams)
  const ctx = filterSearch(searchParams)

  const [itemsById, setItemsById] = useState<Map<number, Item>>(new Map())
  const [activeIdx, setActiveIdx] = useState(0)
  const requested = useRef<Set<number>>(new Set())
  const scrollRef = useRef<HTMLDivElement | null>(null)

  // Load item payloads for a window around the active slide.
  useEffect(() => {
    if (ids.length === 0) return
    const from = Math.max(0, activeIdx - WINDOW_BEHIND)
    const to = Math.min(ids.length, activeIdx + WINDOW_AHEAD + 1)
    const missing = ids.slice(from, to).filter((id) => !requested.current.has(id))
    if (missing.length === 0) return
    missing.forEach((id) => requested.current.add(id))
    api
      .get<ItemListResponse>('/api/library/items', { ids: missing.join(',') })
      .then((res) => {
        setItemsById((prev) => {
          const next = new Map(prev)
          for (const it of res.items) if (it.id != null) next.set(it.id, it)
          return next
        })
      })
      .catch(() => missing.forEach((id) => requested.current.delete(id)))
  }, [ids, activeIdx])

  // Active slide = whichever is snapped into view.
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    let frame = 0
    const onScroll = () => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(() => {
        const idx = Math.round(el.scrollTop / Math.max(1, el.clientHeight))
        setActiveIdx((cur) => (cur === idx ? cur : idx))
      })
    }
    el.addEventListener('scroll', onScroll, { passive: true })
    return () => {
      el.removeEventListener('scroll', onScroll)
      cancelAnimationFrame(frame)
    }
  }, [ids.length])

  // Keyboard: ↑/↓/space move a slide, Esc closes.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        navigate(`/${ctx}`)
        return
      }
      const dir = e.key === 'ArrowDown' || e.key === ' ' ? 1 : e.key === 'ArrowUp' ? -1 : 0
      if (dir === 0 || !scrollRef.current) return
      e.preventDefault()
      const el = scrollRef.current
      el.scrollTo({ top: (activeIdx + dir) * el.clientHeight, behavior: 'smooth' })
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [activeIdx, ctx, navigate])

  return (
    <div className="fixed inset-0 z-30 flex flex-col bg-black">
      <div className="flex items-center justify-between gap-3 px-4 py-2 text-sm text-white">
        <Link
          to={`/${ctx}`}
          className="rounded-lg bg-white/10 px-3 py-1.5 no-underline hover:bg-white/20"
        >
          ✕ Close
        </Link>
        <span className="text-slate-400">
          {loading
            ? 'Loading…'
            : ids.length > 0
              ? `${Math.min(activeIdx + 1, ids.length)} / ${ids.length}`
              : 'Nothing to show'}
        </span>
      </div>
      <div ref={scrollRef} className="flex-1 snap-y snap-mandatory overflow-y-auto overscroll-contain">
        {ids.map((id, idx) => (
          <FeedSlide
            key={id}
            item={itemsById.get(id)}
            active={idx === activeIdx}
            ctx={ctx}
          />
        ))}
        {!loading && ids.length === 0 && (
          <div className="flex h-full items-center justify-center text-sm text-slate-500">
            No items match this filter.
          </div>
        )}
      </div>
    </div>
  )
}
