import { useEffect, useState } from 'react'
import { fetchCaptionsObjectUrl } from '../api/client'

/** A `<track>` with a media file's Whisper captions, loaded through the
 * authenticated API (see `fetchCaptionsObjectUrl`). Renders nothing until
 * the captions arrive, or at all when the file has none. */
export function CaptionTrack({ mediaFileId }: { mediaFileId: number }) {
  const [src, setSrc] = useState<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    let objectUrl: string | null = null
    fetchCaptionsObjectUrl(mediaFileId, controller.signal)
      .then((url) => {
        if (controller.signal.aborted) {
          if (url) URL.revokeObjectURL(url)
          return
        }
        objectUrl = url
        setSrc(url)
      })
      .catch(() => undefined)
    return () => {
      controller.abort()
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [mediaFileId])

  if (!src) return null
  return <track kind="captions" srcLang="auto" label="Transcript" src={src} default />
}
