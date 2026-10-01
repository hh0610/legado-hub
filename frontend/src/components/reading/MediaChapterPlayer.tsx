import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { ChevronLeft, ChevronRight, Gauge, Loader2, RefreshCw } from "lucide-react"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"

const SPEEDS = [0.75, 1, 1.25, 1.5, 2, 3]
const HLS_MIME_RE = /mpegurl/i
const HLS_PATH_RE = /\.m3u8(\?|#|$)/i
const POSITION_SAVE_INTERVAL_MS = 3000

interface PlayerChapter {
  title: string
  readChapterId?: string
}

interface MediaChapterPlayerProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  chapter: PlayerChapter | null
  bookTitle: string
  bookAuthor: string
  mediaKind: "audio" | "video"
  mediaUrl?: string | null
  mediaType?: string | null
  durationSeconds?: number | null
  loading: boolean
  error: unknown
  onRetry: () => void
  hasPrev: boolean
  hasNext: boolean
  onPrevChapter: () => void
  onNextChapter: () => void
  bookId: string
}

function isHlsSource(url: string, mediaType?: string | null): boolean {
  if (mediaType && HLS_MIME_RE.test(mediaType)) return true
  return HLS_PATH_RE.test(url)
}

function supportsNativeHls(kind: "audio" | "video"): boolean {
  try {
    const probe = document.createElement(kind === "video" ? "video" : "audio")
    return !!probe.canPlayType("application/vnd.apple.mpegurl")
  } catch {
    return false
  }
}

function formatDuration(totalSeconds: number): string {
  if (!Number.isFinite(totalSeconds) || totalSeconds <= 0) return ""
  const seconds = Math.floor(totalSeconds % 60)
  const minutes = Math.floor((totalSeconds / 60) % 60)
  const hours = Math.floor(totalSeconds / 3600)
  const mm = hours > 0 ? String(minutes).padStart(2, "0") : String(minutes)
  const ss = String(seconds).padStart(2, "0")
  return hours > 0 ? `${hours}:${mm}:${ss}` : `${mm}:${ss}`
}

function errorMessage(error: unknown): string {
  if (!error) return ""
  if (typeof error === "string") return error
  if (typeof error === "object" && "message" in error) return String((error as { message: unknown }).message)
  return ""
}

export function MediaChapterPlayer({
  open,
  onOpenChange,
  chapter,
  bookTitle,
  bookAuthor,
  mediaKind,
  mediaUrl,
  mediaType,
  durationSeconds,
  loading,
  error,
  onRetry,
  hasPrev,
  hasNext,
  onPrevChapter,
  onNextChapter,
  bookId,
}: MediaChapterPlayerProps) {
  const elementRef = useRef<HTMLMediaElement | null>(null)
  const hlsRef = useRef<{ destroy: () => void } | null>(null)
  const [rateIndex, setRateIndex] = useState(1)
  const lastSavedRef = useRef(0)
  const useHls = useMemo(
    () => !!mediaUrl && isHlsSource(mediaUrl, mediaType),
    [mediaUrl, mediaType],
  )
  const needsHlsJs = useHls && !supportsNativeHls(mediaKind)
  const mediaError = errorMessage(error)
  const positionStorageKey = `legadohub:media-pos:${bookId}:${chapter?.readChapterId || "unknown"}`

  const savePosition = useCallback((force: boolean) => {
    const element = elementRef.current
    if (!element || !chapter?.readChapterId) return
    const now = Date.now()
    if (!force && now - lastSavedRef.current < POSITION_SAVE_INTERVAL_MS) return
    lastSavedRef.current = now
    try {
      if (element.currentTime > 1) {
        localStorage.setItem(positionStorageKey, String(element.currentTime))
      }
    } catch {
      // localStorage unavailable — playback position is best-effort.
    }
  }, [chapter?.readChapterId, positionStorageKey])

  const restorePosition = useCallback(() => {
    const element = elementRef.current
    if (!element) return
    try {
      const saved = Number(localStorage.getItem(positionStorageKey) || 0)
      if (saved > 1 && Number.isFinite(element.duration) && saved < element.duration - 5) {
        element.currentTime = saved
      }
    } catch {
      // ignore
    }
  }, [positionStorageKey])

  // Attach HLS sources through hls.js when the browser has no native support.
  useEffect(() => {
    if (!open || !needsHlsJs || !mediaUrl) return
    let disposed = false
    void (async () => {
      const mod = await import("hls.js")
      if (disposed) return
      const Hls = mod.default
      if (!Hls.isSupported()) return
      const hls = new Hls({ enableWorker: true })
      hls.loadSource(mediaUrl)
      if (elementRef.current) hls.attachMedia(elementRef.current)
      hlsRef.current = hls
    })()
    return () => {
      disposed = true
      hlsRef.current?.destroy()
      hlsRef.current = null
    }
  }, [open, needsHlsJs, mediaUrl])

  useEffect(() => {
    if (!open) {
      savePosition(true)
      elementRef.current?.pause()
      lastSavedRef.current = 0
    }
  }, [open, savePosition])

  useEffect(() => {
    const handler = () => savePosition(true)
    window.addEventListener("beforeunload", handler)
    return () => window.removeEventListener("beforeunload", handler)
  }, [savePosition])

  const cycleRate = () => {
    const next = (rateIndex + 1) % SPEEDS.length
    setRateIndex(next)
    if (elementRef.current) elementRef.current.playbackRate = SPEEDS[next]
  }

  const mediaProps = {
    autoPlay: true,
    controls: true,
    playsInline: true,
    onLoadedMetadata: (e: React.SyntheticEvent<HTMLMediaElement>) => {
      e.currentTarget.playbackRate = SPEEDS[rateIndex]
      restorePosition()
    },
    onTimeUpdate: () => savePosition(false),
    onPause: () => savePosition(true),
    onEnded: () => {
      savePosition(true)
      if (hasNext) onNextChapter()
    },
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle className="flex items-center justify-between gap-3 pr-8">
            <span className="truncate">{chapter?.title || "播放"}</span>
            <span className="shrink-0 text-xs font-normal text-slate-400">
              {mediaKind === "video" ? "视频" : "有声书"}
            </span>
          </DialogTitle>
          <DialogDescription className="truncate">
            {bookTitle}
            {bookAuthor ? ` · ${bookAuthor}` : ""}
          </DialogDescription>
        </DialogHeader>

        {loading ? (
          <div className="flex min-h-[160px] items-center justify-center">
            <Loader2 className="h-6 w-6 animate-spin text-slate-400" />
          </div>
        ) : mediaError || !mediaUrl ? (
          <Alert variant="destructive">
            <AlertDescription className="flex flex-wrap items-center justify-between gap-3">
              <span>媒体加载失败：{mediaError || "章节尚未就绪或缺少播放地址"}</span>
              <Button type="button" size="sm" variant="outline" onClick={onRetry}>
                <RefreshCw className="h-4 w-4 mr-1" /> 重试
              </Button>
            </AlertDescription>
          </Alert>
        ) : mediaKind === "video" ? (
          <video
            ref={elementRef as React.RefObject<HTMLVideoElement>}
            src={needsHlsJs ? undefined : mediaUrl || undefined}
            className="max-h-[70vh] w-full rounded-lg bg-black"
            {...mediaProps}
          />
        ) : (
          <div className="flex min-h-[120px] flex-col justify-center">
            <audio
              ref={elementRef as React.RefObject<HTMLAudioElement>}
              src={needsHlsJs ? undefined : mediaUrl || undefined}
              className="w-full"
              {...mediaProps}
            />
          </div>
        )}

        <div className="flex flex-wrap items-center justify-between gap-3 border-t pt-3">
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" disabled={!hasPrev} onClick={onPrevChapter}>
              <ChevronLeft className="h-4 w-4 mr-1" /> 上一集
            </Button>
            <Button variant="outline" size="sm" disabled={!hasNext} onClick={onNextChapter}>
              下一集 <ChevronRight className="h-4 w-4 ml-1" />
            </Button>
          </div>
          <div className="flex items-center gap-2 text-xs text-slate-400">
            {durationSeconds ? <span>约 {formatDuration(durationSeconds)}</span> : null}
            <Button variant="ghost" size="sm" onClick={cycleRate} title="播放速度">
              <Gauge className="h-4 w-4 mr-1" /> {SPEEDS[rateIndex]}x
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}
