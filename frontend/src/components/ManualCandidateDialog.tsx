import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Loader2, AlertTriangle, CheckCircle2, FileText } from "lucide-react"
import { api, apiErrorMessage } from "@/lib/api"
import { alignmentReasonLabel } from "@/lib/utils"
import { Button } from "@/components/ui/button"
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription,
} from "@/components/ui/dialog"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { ScrollArea } from "@/components/ui/scroll-area"

interface ManualChapterRef {
  chapterId: string
  chapterIndex: number
  title: string
}

interface ManualWordCountInfo {
  passed: boolean
  actual: number
  expected: number
  ratio?: number | null
}

interface ManualAlignmentInfo {
  passed: boolean
  titleSimilarity: number
  previewSimilarity: number
  headPreviewSimilarity: number
  reason: string
}

interface ManualCandidateItem {
  sourceId: string
  sourceName: string
  sourceChapterId: string
  candidateTitle: string
  officialWordCount: number
  contentLength: number
  classification: string
  wordCount: ManualWordCountInfo
  alignment: ManualAlignmentInfo
  autoAcceptable: boolean
  head: string
  tail: string
  error: string
}

interface ManualSourceError {
  sourceId: string
  sourceName: string
  error: string
}

interface ManualScanResult {
  targetTitle: string
  chapterIndex: number
  officialPreviewLength: number
  officialWordCount: number
  items: ManualCandidateItem[]
  sourceErrors: ManualSourceError[]
}

function classificationLabel(cls: string) {
  const m: Record<string, string> = {
    full: "完整正文", preview: "预览内容", empty: "空内容", unknown: "判定未知",
  }
  return m[cls] || cls || "判定未知"
}

function classificationClass(cls: string) {
  if (cls === "full") return "bg-emerald-100 text-emerald-700"
  if (cls === "preview") return "bg-orange-100 text-orange-700"
  return "bg-rose-100 text-rose-700"
}

function CandidateRejections({ item }: { item: ManualCandidateItem }) {
  const reasons: string[] = []
  if (item.error) {
    reasons.push(`拉取失败：${item.error}`)
  } else {
    if (item.classification !== "full") {
      reasons.push(`自动分类为「${classificationLabel(item.classification)}」，未达完整正文标准`)
    }
    if (item.officialWordCount > 0 && !item.wordCount.passed) {
      const pct = item.wordCount.ratio != null ? `（${Math.round(item.wordCount.ratio * 100)}%）` : ""
      reasons.push(
        `字数未过门：净化后 ${item.wordCount.actual} 字 / 官方 ${item.officialWordCount} 字${pct}`,
      )
    }
    if (!item.alignment.passed) {
      reasons.push(`与官方预览对齐未过：${alignmentReasonLabel(item.alignment.reason)}`)
    }
  }
  if (reasons.length === 0) return null
  return (
    <Alert variant="destructive" className="mt-2 py-2 [&>svg]:top-2" data-testid="manual-candidate-reject">
      <AlertTriangle className="h-4 w-4" />
      <AlertDescription className="text-xs space-y-0.5 !translate-y-0">
        {reasons.map((r) => <div key={r}>· {r}</div>)}
      </AlertDescription>
    </Alert>
  )
}

function SimBadge({ label, value }: { label: string; value: number }) {
  const pct = Math.round((value || 0) * 100)
  const tone = pct >= 60 ? "text-emerald-600" : pct >= 30 ? "text-amber-600" : "text-slate-400"
  return (
    <span className={`text-xs ${tone}`}>{label} {pct}%</span>
  )
}

export function ManualCandidateDialog({
  bookId,
  chapter,
  open,
  onOpenChange,
  onApplied,
}: {
  bookId: string
  chapter: ManualChapterRef | null
  open: boolean
  onOpenChange: (open: boolean) => void
  onApplied: () => void
}) {
  const queryClient = useQueryClient()
  const [applyingId, setApplyingId] = useState<string>("")
  const [confirmingId, setConfirmingId] = useState<string>("")

  const scanQuery = useQuery({
    queryKey: ["library", "book", bookId, "manual-candidates", chapter?.chapterId],
    enabled: open && Boolean(chapter),
    staleTime: 0,
    queryFn: async (): Promise<ManualScanResult> => {
      const resp = await api.scanManualChapterCandidates(bookId, chapter!.chapterId)
      return resp.scan as ManualScanResult
    },
  })

  const applyMutation = useMutation({
    mutationFn: (item: ManualCandidateItem) =>
      api.applyManualChapterCandidate(bookId, chapter!.chapterId, {
        sourceId: item.sourceId,
        sourceChapterId: item.sourceChapterId,
      }),
    onSuccess: (resp) => {
      if (resp && resp.ok === false) {
        throw new Error(resp.error || "采纳失败")
      }
      void queryClient.invalidateQueries({ queryKey: ["library", "book", bookId] })
      onApplied()
      onOpenChange(false)
    },
    onSettled: () => {
      setApplyingId("")
      setConfirmingId("")
    },
  })

  const handleApply = (item: ManualCandidateItem) => {
    if (applyingId) return
    if (confirmingId !== item.sourceChapterId) {
      setConfirmingId(item.sourceChapterId)
      return
    }
    setApplyingId(item.sourceChapterId)
    applyMutation.mutate(item)
  }

  const scan = scanQuery.data
  const busy = applyMutation.isPending

  return (
    <Dialog open={open} onOpenChange={(v) => { if (!busy) onOpenChange(v) }}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>手动选源补全</DialogTitle>
          <DialogDescription>
            {chapter ? `第 ${chapter.chapterIndex} 章《${chapter.title}》` : ""}
            ：逐源检查匹配章节，被自动门拒绝的内容也会列出，请肉眼确认开头/结尾后再采纳。
          </DialogDescription>
        </DialogHeader>

        {scanQuery.isLoading ? (
          <div className="py-12 flex flex-col items-center gap-3 text-slate-500 text-sm" data-testid="manual-scan-loading">
            <Loader2 className="h-6 w-6 animate-spin" />
            正在扫描各候选源（可能需要一分钟左右）…
          </div>
        ) : scanQuery.isError ? (
          <Alert variant="destructive">
            <AlertTriangle className="h-4 w-4" />
            <AlertDescription>{apiErrorMessage(scanQuery.error, "扫描失败，请稍后重试。")}</AlertDescription>
          </Alert>
        ) : (
          <ScrollArea className="max-h-[60vh] pr-3">
            <div className="text-xs text-slate-500 mb-3">
              官方预览 {scan?.officialPreviewLength ?? 0} 字 · 官方全文字数 {scan?.officialWordCount ?? 0}
            </div>
            {applyMutation.isError && (
              <Alert variant="destructive" className="mb-3">
                <AlertTriangle className="h-4 w-4" />
                <AlertDescription>{apiErrorMessage(applyMutation.error, "采纳失败，请稍后重试。")}</AlertDescription>
              </Alert>
            )}
            {scan?.sourceErrors?.length ? (
              <Alert className="mb-3 py-2 [&>svg]:top-2">
                <AlertTriangle className="h-4 w-4 text-amber-500" />
                <AlertDescription className="text-xs space-y-0.5 !translate-y-0">
                  {scan.sourceErrors.map((e) => (
                    <div key={e.sourceId}>· {e.sourceName}：{e.error}</div>
                  ))}
                </AlertDescription>
              </Alert>
            ) : null}
            {!scan?.items.length ? (
              <div className="py-10 text-center text-sm text-slate-400">没有找到任何候选章节。</div>
            ) : (
              <div className="space-y-3">
                {scan.items.map((item) => {
                  const applying = applyingId === item.sourceChapterId
                  const confirming = confirmingId === item.sourceChapterId
                  const disabled = busy || Boolean(item.error) || item.contentLength === 0
                  return (
                    <div
                      key={`${item.sourceId}:${item.sourceChapterId}`}
                      className="border border-slate-200 rounded-lg p-3"
                      data-testid="manual-candidate-item"
                    >
                      <div className="flex items-start justify-between gap-3">
                        <div className="min-w-0">
                          <div className="text-sm font-medium text-slate-800 truncate">
                            <FileText className="inline h-3.5 w-3.5 mr-1 text-slate-400" />
                            {item.sourceName}
                          </div>
                          <div className="text-xs text-slate-500 mt-0.5 truncate">
                            候选章节：{item.candidateTitle}
                          </div>
                        </div>
                        <Button
                          size="sm"
                          variant={confirming ? "destructive" : "outline"}
                          className="h-8 shrink-0"
                          disabled={disabled}
                          onClick={() => handleApply(item)}
                          data-testid="manual-candidate-apply"
                        >
                          {applying ? <Loader2 className="h-3.5 w-3.5 animate-spin mr-1" /> : null}
                          {applying ? "采纳中…" : confirming ? "再次点击确认" : "采用"}
                        </Button>
                      </div>
                      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 mt-2">
                        <span className={`text-xs px-1.5 py-0.5 rounded ${classificationClass(item.classification)}`}>
                          {classificationLabel(item.classification)}
                        </span>
                        <span className={`text-xs ${item.wordCount.passed ? "text-emerald-600" : "text-rose-600"}`}>
                          字数 {item.contentLength}/{item.officialWordCount || "-"}
                        </span>
                        {scan.officialPreviewLength > 0 && (
                          <>
                            <SimBadge label="标题" value={item.alignment.titleSimilarity} />
                            <SimBadge label="预览" value={item.alignment.previewSimilarity} />
                            <SimBadge label="开头" value={item.alignment.headPreviewSimilarity} />
                          </>
                        )}
                        {item.autoAcceptable && (
                          <span className="text-xs text-emerald-600 inline-flex items-center">
                            <CheckCircle2 className="h-3.5 w-3.5 mr-0.5" />自动门可通过
                          </span>
                        )}
                      </div>
                      <CandidateRejections item={item} />
                      {item.head && (
                        <div className="mt-2 text-xs text-slate-600 space-y-1">
                          <p className="whitespace-pre-wrap leading-relaxed bg-slate-50 rounded p-2">开头：{item.head}</p>
                          {item.tail && (
                            <p className="whitespace-pre-wrap leading-relaxed bg-slate-50 rounded p-2">结尾：{item.tail}</p>
                          )}
                        </div>
                      )}
                    </div>
                  )
                })}
              </div>
            )}
          </ScrollArea>
        )}
      </DialogContent>
    </Dialog>
  )
}
