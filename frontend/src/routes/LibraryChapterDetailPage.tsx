import { useMemo, useState } from "react"
import { useNavigate, useParams } from "react-router"
import { useQuery } from "@tanstack/react-query"
import {
  ChevronLeft,
  Loader2,
  ScrollText,
  Waypoints,
} from "lucide-react"
import { api, apiErrorMessage } from "@/lib/api"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { alignmentReasonLabel } from "@/lib/utils"

interface TraceSummary {
  stage?: string
  currentStep?: string
  nextStep?: string
  chapterStatus?: string
  selectedSource?: string
  selectedContentSource?: string
  fallbackSourceId?: string
  alignmentPassed?: boolean | null
  alignmentReason?: string
  titleSimilarity?: number | null
  previewSimilarity?: number | null
  processedAt?: string
  traceHash?: string
  stage3Verdict?: string
  stage3Reason?: string
  currentChapterIndex?: number | null
  currentChapterTitle?: string
  nextChapterIndex?: number | null
  nextChapterTitle?: string
}

interface ChapterProgressDetail {
  found?: boolean
  bookId: string
  chapterId: string
  chapterIndex: number
  title: string
  status: string
  previewOnly: boolean
  contentLength: number
  sourceWordCount: number
  traceSummary?: TraceSummary
}

function statusBadge(status?: string) {
  const map: Record<
    string,
    { label: string; variant: "success" | "warning" | "info" | "destructive" | "outline" }
  > = {
    processed: { label: "已处理", variant: "success" },
    completed: { label: "已完成", variant: "success" },
    fallback: { label: "回退", variant: "warning" },
    error: { label: "失败", variant: "destructive" },
    pending: { label: "待处理", variant: "outline" },
    active: { label: "处理中", variant: "info" },
  }
  const entry = map[status || ""] || { label: status || "未知", variant: "outline" as const }
  return <Badge variant={entry.variant}>{entry.label}</Badge>
}

function formatDate(value?: string | null) {
  if (!value) return "-"
  try {
    return new Date(value).toLocaleString("zh-CN")
  } catch {
    return value
  }
}

function formatDecimal(value?: number | null) {
  return value == null ? "-" : value.toFixed(3)
}

function processingVerdictText(verdict?: string, reason?: string) {
  if (verdict === "trusted_current") return "当前正文可信"
  if (verdict === "trusted_candidate") return "候选正文可信"
  if (verdict === "waiting_for_candidates") return "等待候选更新"
  if (verdict === "suspect") return "正文存疑"
  if (reason === "content_candidate_untrusted") return "等待候选更新"
  if (reason === "suspect_content") return "正文存疑"
  if (reason === "mixed_book_content") return "疑似串书正文"
  if (reason === "repeated_body") return "疑似重复正文"
  if (reason === "wrong_chapter_order") return "疑似章节错位"
  return verdict || reason || "-"
}

function deriveStageState(progress?: ChapterProgressDetail) {
  const trace = progress?.traceSummary || {}
  const status = progress?.status || trace.chapterStatus || ""
  const hasSelection = Boolean(trace.selectedSource || trace.selectedContentSource)
  const hasProcessedResult = Boolean(trace.processedAt)
  const isDone = status === "processed" || status === "fallback" || status === "completed"
  const isError = status === "error"

  const currentStage = isDone ? "Stage 3" : hasProcessedResult ? "Stage 3" : hasSelection ? "Stage 2" : "Stage 1"
  const failureReason =
    processingVerdictText(trace.stage3Verdict, trace.stage3Reason) !== "-"
      ? processingVerdictText(trace.stage3Verdict, trace.stage3Reason)
      : (
    trace.alignmentReason ? alignmentReasonLabel(trace.alignmentReason) :
    (isError ? "处理失败" : status === "fallback" ? "已回退到备用结果" : "")
        )

  return {
    currentStage,
    failureReason,
    currentStep: trace.currentStep || processingVerdictText(trace.stage3Verdict, trace.stage3Reason) || statusBadgeText(status),
    nextStep: trace.nextStep || (isDone ? "等待下一次调度" : "继续处理下一章"),
    currentChapterIndex: trace.currentChapterIndex ?? progress?.chapterIndex,
    currentChapterTitle: trace.currentChapterTitle || progress?.title || "",
    nextChapterIndex: trace.nextChapterIndex ?? ((progress?.chapterIndex || 0) + 1),
    nextChapterTitle: trace.nextChapterTitle || "",
    nodes: [
      {
        id: "stage1",
        label: "Stage 1",
        state: hasSelection || hasProcessedResult || isDone ? "done" : "current",
        detail: trace.selectedSource || "选源中",
      },
      {
        id: "stage2",
        label: "Stage 2",
        state: hasProcessedResult || isDone ? "done" : hasSelection ? "current" : "waiting",
        detail: trace.selectedContentSource || "处理中",
      },
      {
        id: "stage3",
        label: "Stage 3",
        state: isDone ? "done" : hasProcessedResult ? "current" : "waiting",
        detail: processingVerdictText(trace.stage3Verdict, trace.stage3Reason) || statusBadgeText(status),
      },
    ] as const,
  }
}

function statusBadgeText(status?: string) {
  if (status === "processed") return "已处理"
  if (status === "fallback") return "回退"
  if (status === "error") return "失败"
  if (status === "pending") return "待处理"
  return status || "等待中"
}

function stageNodeClass(state: string) {
  if (state === "done") return "border-primary/20 bg-primary/10 text-primary"
  if (state === "current") return "border-pink/20 bg-pink/10 text-pink"
  return "border-border bg-muted/40 text-muted-foreground"
}

function MetaItem({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="rounded-lg border p-3">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="mt-0.5 text-sm font-medium">{value}</div>
    </div>
  )
}

export function LibraryChapterDetailPage() {
  const { bookId, chapterId } = useParams<{ bookId: string; chapterId: string }>()
  const navigate = useNavigate()
  const [progressOpen, setProgressOpen] = useState(false)

  const bookQuery = useQuery({
    queryKey: ["library", "book", bookId, "summary"],
    queryFn: () => api.libraryBookSummary(bookId!),
    enabled: !!bookId,
    refetchInterval: 5000,
    refetchOnWindowFocus: "always",
  })

  const progressQuery = useQuery({
    queryKey: ["library", "book", bookId, "chapter", chapterId, "progress"],
    queryFn: () => api.libraryBookChapterProgress(bookId!, chapterId!),
    enabled: !!bookId && !!chapterId,
    refetchInterval: 5000,
    refetchOnWindowFocus: "always",
  })

  const chapter = progressQuery.data as ChapterProgressDetail | undefined
  const trace = chapter?.traceSummary || {}
  const stageState = useMemo(() => deriveStageState(chapter), [chapter])

  if (progressQuery.isLoading) {
    return (
      <div className="flex min-h-[300px] items-center justify-center">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />
      </div>
    )
  }

  if (progressQuery.error) {
    return (
      <Alert variant="destructive">
        <AlertDescription className="flex flex-wrap items-center justify-between gap-3">
          <span>章节状态加载失败：{apiErrorMessage(progressQuery.error, "请稍后重试。")}</span>
          <Button type="button" size="sm" variant="outline" onClick={() => { void progressQuery.refetch() }}>重试</Button>
        </AlertDescription>
      </Alert>
    )
  }

  if (!chapter || chapter.found === false) {
    return (
      <div className="flex flex-col items-center justify-center rounded-lg border py-16 text-center">
        <p className="text-base font-semibold">这章好像走丢了</p>
        <p className="text-sm text-muted-foreground">回书籍详情页再找找吧～</p>
        <Button
          variant="outline"
          size="sm"
          className="mt-4"
          onClick={() => navigate(`/console/library/${bookId}`)}
        >
          <ChevronLeft className="mr-1 h-4 w-4" />
          返回书籍详情
        </Button>
      </div>
    )
  }

  return (
    <div className="space-y-5">
      <button
        type="button"
        onClick={() => navigate(`/console/library/${bookId}`)}
        className="inline-flex items-center gap-1.5 rounded-xl bg-card/80 px-3 py-1.5 text-sm text-muted-foreground shadow-sm backdrop-blur hover:text-foreground hover:bg-card transition-colors"
      >
        <ChevronLeft className="h-4 w-4" />
        返回书籍详情
      </button>

      <div className="flex items-center gap-3">
        <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-primary/10">
          <ScrollText className="h-5 w-5 text-primary" />
        </div>
        <div>
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="text-2xl font-bold tracking-tight text-foreground">{chapter.title}</h1>
            {statusBadge(chapter.status)}
          </div>
      <p className="text-sm text-muted-foreground">
            {bookQuery.data?.displayName || bookId} / 第 {chapter.chapterIndex} 章
          </p>
          {bookQuery.error && <p className="mt-1 text-xs text-amber-600">书籍摘要暂时不可用</p>}
        </div>
      </div>

      <div className="grid gap-4 lg:grid-cols-[1.2fr_1fr]">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">章节概览</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="grid gap-3 text-sm md:grid-cols-2">
              <MetaItem label="当前阶段" value={stageState.currentStage} />
              <MetaItem label="当前步骤" value={stageState.currentStep || "-"} />
              <MetaItem label="下一步骤" value={stageState.nextStep || "-"} />
              <MetaItem label="失败原因" value={stageState.failureReason || "-"} />
              <MetaItem
                label="当前章节"
                value={`第 ${stageState.currentChapterIndex || chapter.chapterIndex} 章 ${stageState.currentChapterTitle || chapter.title}`}
              />
              <MetaItem
                label="下一章节"
                value={
                  stageState.nextChapterIndex
                    ? `第 ${stageState.nextChapterIndex} 章 ${stageState.nextChapterTitle || ""}`
                    : "-"
                }
              />
              <MetaItem label="内容长度" value={chapter.contentLength || 0} />
              <MetaItem label="来源字数" value={chapter.sourceWordCount || 0} />
              <MetaItem label="预览模式" value={chapter.previewOnly ? "是" : "否"} />
              <MetaItem label="处理时间" value={formatDate(trace.processedAt)} />
            </div>
            <div className="mt-4">
              <Button variant="outline" size="sm" onClick={() => setProgressOpen(true)}>
                <Waypoints className="mr-1.5 h-4 w-4" />
                查看进度
              </Button>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">Trace 摘要</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <MetaItem label="选中来源" value={trace.selectedSource || "-"} />
            <MetaItem label="内容来源" value={trace.selectedContentSource || "-"} />
            <MetaItem label="回退来源" value={trace.fallbackSourceId || "-"} />
            <MetaItem label="处理结论" value={processingVerdictText(trace.stage3Verdict, trace.stage3Reason)} />
            <MetaItem label="处理说明" value={trace.stage3Reason || "-"} />
            <MetaItem label="Trace Hash" value={trace.traceHash || "-"} />
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-sm">对齐与失败信息</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid gap-3 text-sm md:grid-cols-2">
            <MetaItem
              label="对齐结果"
              value={trace.alignmentPassed == null ? "-" : trace.alignmentPassed ? "通过" : "未通过"}
            />
            <MetaItem label="对齐原因" value={trace.alignmentReason ? alignmentReasonLabel(trace.alignmentReason) : "-"} />
            <MetaItem label="标题相似度" value={formatDecimal(trace.titleSimilarity)} />
            <MetaItem label="预览相似度" value={formatDecimal(trace.previewSimilarity)} />
          </div>
        </CardContent>
      </Card>

      <Dialog open={progressOpen} onOpenChange={setProgressOpen}>
        <DialogContent className="max-w-3xl">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <ScrollText className="h-4 w-4" />
              章节进度
            </DialogTitle>
          </DialogHeader>
          <div className="grid gap-4 lg:grid-cols-[1.1fr_1fr]">
            <div className="space-y-3">
              {stageState.nodes.map((node, index) => (
                <div key={node.id} className="flex gap-3">
                  <div className="flex flex-col items-center">
                    <div
                      className={`flex h-10 w-10 items-center justify-center rounded-full border text-xs font-semibold ${stageNodeClass(node.state)}`}
                    >
                      {index + 1}
                    </div>
                    {index < stageState.nodes.length - 1 && (
                      <div className="mt-1 h-8 w-px bg-border" />
                    )}
                  </div>
                  <div className="flex-1 rounded-lg border p-3">
                    <div className="flex items-center justify-between gap-2">
                      <div className="font-medium">{node.label}</div>
                      <Badge variant={node.state === "done" ? "success" : node.state === "current" ? "info" : "outline"}>
                        {node.state === "done" ? "完成" : node.state === "current" ? "当前" : "等待"}
                      </Badge>
                    </div>
                    <div className="mt-1 text-sm text-muted-foreground">{node.detail || "-"}</div>
                  </div>
                </div>
              ))}
            </div>

            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm flex items-center gap-2">
                  <ScrollText className="h-4 w-4" />
                  日志面板
                </CardTitle>
              </CardHeader>
              <CardContent>
                <ScrollArea className="h-[320px] pr-4">
                  <div className="space-y-3 text-sm">
                    <MetaItem label="阶段" value={stageState.currentStage} />
                    <MetaItem label="当前步骤" value={stageState.currentStep || "-"} />
                    <MetaItem label="下一步骤" value={stageState.nextStep || "-"} />
                    <MetaItem label="失败原因" value={stageState.failureReason || "-"} />
                    <MetaItem
                      label="当前章节"
                      value={`第 ${stageState.currentChapterIndex || chapter.chapterIndex} 章`}
                    />
                    <MetaItem label="选中来源" value={trace.selectedSource || "-"} />
                    <MetaItem label="内容来源" value={trace.selectedContentSource || "-"} />
                    <MetaItem label="处理结论" value={processingVerdictText(trace.stage3Verdict, trace.stage3Reason)} />
                    <MetaItem label="处理说明" value={trace.stage3Reason || "-"} />
                    <MetaItem label="处理时间" value={formatDate(trace.processedAt)} />
                    <MetaItem label="Trace Hash" value={trace.traceHash || "-"} />
                  </div>
                </ScrollArea>
              </CardContent>
            </Card>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  )
}
