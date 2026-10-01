import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useNavigate } from "react-router"
import { SearchIcon, Loader2, ArrowRight, Activity } from "lucide-react"
import { api, apiErrorMessage } from "@/lib/api"
import { useAuth } from "@/lib/auth"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Progress } from "@/components/ui/progress"
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"

interface SearchCard {
  candidateId: string
  name: string
  author?: string
  coverUrl?: string
  intro?: string
  wordCount?: string
  chapterCount?: number
  completed?: boolean
  status?: string
  contentType?: string
  alreadyIngested?: boolean
  alreadySubscribed?: boolean
  subscriptionStatus?: string
  aggregateBookId?: string
  sourceCount?: number
  sourceSummaryText?: string
}

interface SearchJobData {
  status?: string
  liveSearchPending?: boolean
  error?: string
  message?: string
  cards?: SearchCard[]
  events?: Array<{ type: string; sourceId?: string; count?: number; error?: string; message?: string; ts: number }>
}

interface SubscriptionDiscoveryPageProps {
  mode?: "user" | "admin"
}

const terminalSearchStatuses = new Set(["completed", "partial", "timed_out", "failed", "cancelled", "interrupted", "unknown"])
const failedSearchStatuses = new Set(["failed", "timed_out", "interrupted", "unknown"])

export function SubscriptionDiscoveryPage({ mode = "user" }: SubscriptionDiscoveryPageProps) {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { user, entrypoint } = useAuth()
  const isAdmin = entrypoint !== "public" && (user?.role === "admin" || mode === "admin")

  const [query, setQuery] = useState("")
  const [jobId, setJobId] = useState<string | null>(null)
  const [showLogs, setShowLogs] = useState(false)
  const [selectedCard, setSelectedCard] = useState<SearchCard | null>(null)
  const [startChapterIndex, setStartChapterIndex] = useState("1")

  const openSubscriptionDialog = (card: SearchCard) => {
    setStartChapterIndex("1")
    setSelectedCard(card)
  }

  const searchMutation = useMutation({
    mutationFn: (keyword: string) => api.subscribe.search({ keyword, page: 1 }),
    onSuccess: (data) => setJobId(data.jobId),
  })

  const { data: jobData, error: jobError, refetch: refetchJob } = useQuery<SearchJobData | null>({
    queryKey: ["subscribe", "search", jobId],
    queryFn: () => (jobId ? api.subscribe.searchJob(jobId) : Promise.resolve(null)),
    enabled: !!jobId,
    refetchInterval: (q) => {
      const d = q.state.data as SearchJobData | null
      if (q.state.error || !d || d.liveSearchPending === false || terminalSearchStatuses.has(d.status || "")) return false
      return 500
    },
  })

  const subscribeMutation = useMutation({
    mutationFn: async (card: SearchCard) => {
      const data = await api.subscribe.subscribeCard(jobId!, card.candidateId, {
        startChapterIndex: Math.max(1, Number(startChapterIndex) || 1),
      })
      const bookId = data.aggregateBookId || data.book?.aggregateBookId
      if (!bookId) throw new Error("订阅响应缺少书籍 ID，请刷新后确认订阅状态。")
      return { ...data, aggregateBookId: bookId }
    },
    onSuccess: (data) => {
      setSelectedCard(null)
      void queryClient.invalidateQueries({ queryKey: ["library"] })
      navigate(`/console/library/${data.aggregateBookId}`)
    },
  })

  const handleSearch = () => {
    if (!query.trim() || searchMutation.isPending || isSearching) return
    setJobId(null)
    setSelectedCard(null)
    searchMutation.mutate(query.trim())
  }

  const handleRetry = () => {
    if (jobError && jobId) {
      void refetchJob()
      return
    }
    handleSearch()
  }

  const isSearching = searchMutation.isPending || (!jobError && !!jobData && jobData.liveSearchPending !== false && !terminalSearchStatuses.has(jobData.status || ""))
  const cards: SearchCard[] = jobData?.cards || []
  const events = jobData?.events || []

  return (
    <div className="space-y-8 max-w-6xl mx-auto">
      <div className="text-center pt-8 pb-4">
        <h1 className="text-3xl md:text-4xl font-bold tracking-tight text-slate-900 mb-4">发现新书</h1>
        <p className="text-slate-500 max-w-2xl mx-auto mb-8">
          输入小说、作者或关键字，查找可订阅候选并建立个人书库关系。
        </p>

        <div className="max-w-2xl mx-auto">
          <div className="w-full relative group shadow-sm hover:shadow transition-shadow duration-300 rounded-full bg-white border border-slate-200 focus-within:border-blue-400 focus-within:ring-2 focus-within:ring-blue-400/10">
            <div className="flex items-center w-full px-1 py-1">
              <div className="pl-4 pr-2 text-slate-400 group-focus-within:text-blue-500 transition-colors">
                <SearchIcon className="h-4 w-4" />
              </div>
              <input
                type="text"
                aria-label="搜索小说或作者"
                name="subscription-search"
                autoComplete="off"
                placeholder="搜索你想看的小说或作者…"
                className="flex-1 bg-transparent border-0 py-2 md:py-2.5 text-sm focus:outline-none focus:ring-0 text-slate-800 placeholder:text-slate-400"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault()
                    handleSearch()
                  }
                }}
              />
              <div className="pr-1 flex items-center gap-2 text-slate-400">
                <button
                  type="button"
                  aria-label="开始搜索"
                  aria-busy={isSearching}
                  onClick={handleSearch}
                  disabled={!query || isSearching}
                  className="bg-slate-100 hover:bg-slate-200 disabled:opacity-50 disabled:hover:bg-slate-100 text-slate-700 w-8 h-8 md:w-9 md:h-9 rounded-full flex items-center justify-center transition-[background-color,opacity,transform] duration-200 active:scale-95"
                >
                  {isSearching ? <Loader2 className="h-3 w-3 animate-spin" /> : <ArrowRight className="h-3 w-3" />}
                </button>
              </div>
            </div>
          </div>
        </div>
      </div>

      {(searchMutation.error || jobError || subscribeMutation.error || failedSearchStatuses.has(jobData?.status || "")) && (
        <Alert variant="destructive">
          <AlertDescription className="flex flex-wrap items-center justify-between gap-3">
            <span>
              {searchMutation.error
                ? apiErrorMessage(searchMutation.error, "搜索发起失败，请稍后重试。")
                : jobError
                  ? apiErrorMessage(jobError, "搜索状态加载失败，请稍后重试。")
                  : subscribeMutation.error
                    ? apiErrorMessage(subscribeMutation.error, "订阅失败，请稍后重试。")
                    : jobData?.message || jobData?.error || (jobData?.status === "unknown" ? "搜索任务已过期，请重新搜索。" : jobData?.status === "timed_out" ? "搜索超时，请查看已返回的结果或重新搜索。" : "操作失败，请稍后重试。")}
            </span>
            {(jobError || searchMutation.error || failedSearchStatuses.has(jobData?.status || "")) && (
              <Button type="button" size="sm" variant="outline" onClick={handleRetry} disabled={!query.trim() || searchMutation.isPending}>
                重试
              </Button>
            )}
          </AlertDescription>
        </Alert>
      )}

      {isSearching && (
        <div className="flex flex-col items-center justify-center py-10">
          <div className="w-full max-w-md space-y-4">
            <div className="flex justify-between text-sm text-slate-500">
              <span className="flex items-center gap-2"><Loader2 className="h-4 w-4 animate-spin" /> 正在搜索…</span>
            </div>
            <Progress value={undefined} className="h-1 w-full bg-slate-100 [&>div]:bg-blue-500 animate-pulse" />
          </div>
        </div>
      )}

      {!isSearching && cards.length > 0 && (
        <div className="space-y-4">
          <div className="flex flex-col sm:flex-row sm:items-center justify-between text-sm text-slate-500 gap-2">
            <span>找到 {cards.length} 本相关书籍</span>
            {isAdmin && events.length > 0 && (
              <Button variant="ghost" size="sm" onClick={() => setShowLogs(!showLogs)} className="h-8">
                <Activity className="h-4 w-4 mr-2" />
                {showLogs ? "隐藏事件日志" : "查看事件日志"}
              </Button>
            )}
          </div>

          {isAdmin && showLogs && events.length > 0 && (
            <div className="h-32 space-y-1 overflow-y-auto rounded-lg border border-slate-200 bg-slate-50 p-3 font-mono text-xs text-slate-600">
              {events.map((e, i) => {
                const time = new Date((e.ts || 0) * 1000).toLocaleTimeString()
                return (
                  <div key={i} className="border-b border-slate-200/70 py-1 last:border-0">
                    <span className="text-slate-400">[{time}]</span> <span className="font-medium text-slate-700">{e.sourceId || "system"}</span>: {e.type === "source_complete" ? `完成 (${e.count ?? 0} 条)` : e.type === "source_error" ? `错误: ${e.error || ""}` : e.type === "source_timeout" ? "超时" : e.type === "source_empty" ? "无结果" : e.message || e.type}
                  </div>
                )
              })}
            </div>
          )}

          <div className="grid gap-6 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
            {cards.map((card) => (
              <Card
                key={card.candidateId}
                className="overflow-hidden flex flex-col hover:shadow-lg transition-shadow duration-200 cursor-pointer relative group"
                role="button"
                tabIndex={0}
                onClick={() => {
                  if (card.alreadySubscribed && card.aggregateBookId) {
                    navigate(`/console/library/${card.aggregateBookId}`)
                  } else {
                    openSubscriptionDialog(card)
                  }
                }}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault()
                    if (card.alreadySubscribed && card.aggregateBookId) {
                      navigate(`/console/library/${card.aggregateBookId}`)
                    } else {
                      openSubscriptionDialog(card)
                    }
                  }
                }}
              >
                <div className="flex p-5 gap-4">
                  <div className="relative w-20 h-28 bg-slate-200 rounded-md overflow-hidden flex-shrink-0 shadow-sm">
                    {card.coverUrl ? (
                      <img src={card.coverUrl} alt={card.name} width={80} height={112} loading="lazy" className="w-full h-full object-cover" />
                    ) : (
                      <div className="flex h-full w-full items-center justify-center text-slate-400">
                        <SearchIcon className="h-6 w-6" />
                      </div>
                    )}
                    {card.sourceSummaryText && (
                      <div className="absolute bottom-0 left-0 right-0 bg-black/60 backdrop-blur-sm py-1 px-1.5">
                        <p className="text-[9px] text-white/90 text-center truncate">{card.sourceSummaryText}</p>
                      </div>
                    )}
                  </div>
                  <div className="flex flex-col flex-1 min-w-0">
                    <h3 className="font-semibold text-slate-900 truncate text-base">{card.name}</h3>
                    <p className="text-sm text-slate-500 truncate mt-0.5">{card.author || "未知作者"}</p>
                    <div className="mt-2.5 text-xs text-slate-500 flex flex-wrap items-center gap-x-1.5 gap-y-1">
                      {card.contentType === "audio" ? (
                        <Badge variant="secondary" className="bg-violet-50 text-violet-600 border-transparent hover:bg-violet-50 font-normal">有声书</Badge>
                      ) : card.contentType === "video" ? (
                        <Badge variant="secondary" className="bg-sky-50 text-sky-600 border-transparent hover:bg-sky-50 font-normal">视频</Badge>
                      ) : null}
                      <span className={card.completed ? "text-slate-600" : "text-emerald-600"}>{card.completed ? "已完结" : "连载中"}</span>
                      <span className="text-slate-300">·</span>
                      <span>{card.wordCount || ""}</span>
                    </div>
                    {card.chapterCount != null && card.chapterCount > 0 && (
                      <div className="mt-auto pt-2 text-xs text-slate-400">{card.chapterCount} 章</div>
                    )}
                  </div>
                </div>
                {card.intro && (
                  <div className="px-5 py-4 bg-slate-50/50 border-t border-slate-100 flex-1 flex flex-col">
                    <p className="text-xs text-slate-600 line-clamp-3 leading-relaxed flex-1">{card.intro}</p>
                  </div>
                )}
                <div className="p-4 border-t border-slate-100 mt-auto flex items-center justify-between">
                  {card.alreadySubscribed ? (
                    <Badge variant="secondary" className="bg-slate-100 text-slate-600 border-transparent hover:bg-slate-100 font-normal">
                      {card.subscriptionStatus === "paused" ? "已暂停" : card.subscriptionStatus === "archived" ? "已归档" : "已订阅"}
                    </Badge>
                  ) : card.alreadyIngested ? (
                    <Badge variant="secondary" className="bg-blue-50 text-blue-600 border-transparent hover:bg-blue-50 font-normal">已入库</Badge>
                  ) : (
                    <Badge variant="success" className="font-normal">可订阅</Badge>
                  )}
                  <span className="flex items-center gap-1.5 text-xs text-blue-600">
                    {!card.alreadySubscribed && (card.sourceCount ?? 0) > 1 && (
                      <span className="text-slate-400">{card.sourceCount} 个来源</span>
                    )}
                    <span>{card.alreadySubscribed ? "查看详情" : "订阅"}</span>
                    <ArrowRight className="h-3 w-3 transition-transform group-hover:translate-x-0.5" />
                  </span>
                </div>
              </Card>
            ))}
          </div>
        </div>
      )}

      {!isSearching && jobData?.status === "completed" && cards.length === 0 && (
        <div className="py-12 text-center text-sm text-slate-500">未找到匹配的书籍。</div>
      )}

      <Dialog open={!!selectedCard} onOpenChange={(open) => { if (!open) setSelectedCard(null) }}>
        <DialogContent className="max-w-md p-0 overflow-hidden sm:rounded-xl gap-0">
          <DialogHeader className="p-6 pb-0">
            <DialogTitle className="text-2xl">{selectedCard?.name}</DialogTitle>
            <p className="text-base text-slate-500 mt-1">{selectedCard?.author || "未知作者"}</p>
          </DialogHeader>
          {selectedCard?.intro && (
            <div className="px-6 py-5">
              <p className="text-sm text-slate-600 leading-relaxed">{selectedCard.intro}</p>
            </div>
          )}
          <div className="space-y-5 border-t border-slate-100 px-6 py-5">
            <div className="space-y-2">
              <Label htmlFor="start-chapter-index">从第几章开始订阅</Label>
              <Input
                id="start-chapter-index"
                type="number"
                min={1}
                step={1}
                value={startChapterIndex}
                onChange={(event) => setStartChapterIndex(event.target.value)}
              />
            </div>
          </div>
          <div className="px-6 py-4 border-t border-slate-100 mt-auto flex justify-end gap-3 bg-white">
            <Button variant="outline" onClick={() => setSelectedCard(null)}>取消</Button>
            <Button onClick={() => selectedCard && subscribeMutation.mutate(selectedCard)} disabled={subscribeMutation.isPending}>
              {subscribeMutation.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              确认订阅
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  )
}
