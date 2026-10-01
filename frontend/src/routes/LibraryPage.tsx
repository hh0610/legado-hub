import { useMemo, useState } from "react"
import { Link, useNavigate } from "react-router"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { Search, MoreVertical, Play, Pause, Archive, RefreshCw, Trash2, Loader2, ShieldCheck, Wrench } from "lucide-react"
import { api, apiErrorMessage } from "@/lib/api"
import { useAuth } from "@/lib/auth"
import { executeLibraryBookAction } from "@/lib/library-actions"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Progress } from "@/components/ui/progress"
import { Badge } from "@/components/ui/badge"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Alert, AlertDescription } from "@/components/ui/alert"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"

interface LibraryBook {
  aggregateBookId: string
  displayName: string
  displayAuthor: string
  coverUrl?: string
  totalChapters: number
  processedChapters: number
  visibleProcessedChapters: number
  failedChapters?: number
  status: string
  bookStatus: string
  lastChapterTitle?: string
  intro?: string
  wordCount?: string
  primarySourceName?: string
  contentType?: string
  bookState?: {
    chapterCount?: number
    readableChapterCount?: number
    previewChapterCount?: number
    failedChapterCount?: number
  }
  subscription?: {
    status: "active" | "paused" | "archived"
    startChapterIndex: number
    autoArchiveOnComplete: boolean
  }
  personalProgress?: {
    fullCount: number
    previewCount: number
    failedCount: number
    pendingCount: number
    coverageRatio: number
  }
}

interface LibraryIntegrityReport {
  summary: {
    totalBooks: number
    healthyBooks: number
    repairableBooks: number
    brokenBooks: number
    issueCount: number
  }
  books: Array<{
    bookId: string
    name: string
    author: string
    status: "healthy" | "repairable" | "broken"
    repairable: boolean
    issues: Array<{ code: string; message: string; count: number }>
  }>
}

function processStatusLabel(status: string) {
  switch (status) {
    case "completed":
      return <Badge className="rounded-full border border-emerald-200 bg-emerald-50 text-emerald-600 hover:bg-emerald-50">已完成</Badge>
    case "active":
      return <Badge className="rounded-full border border-slate-200 bg-slate-100 text-slate-700 hover:bg-slate-100">处理中</Badge>
    case "error":
      return <Badge className="rounded-full border border-rose-200 bg-rose-50 text-rose-600 hover:bg-rose-50">异常</Badge>
    case "archived":
      return <Badge className="rounded-full border border-slate-200 bg-slate-100 text-slate-500 hover:bg-slate-100">已归档</Badge>
    case "paused":
      return <Badge className="rounded-full border border-slate-200 bg-slate-100 text-slate-500 hover:bg-slate-100">已暂停</Badge>
    default:
      return <Badge className="rounded-full border border-slate-200 bg-slate-100 text-slate-500 hover:bg-slate-100">未知</Badge>
  }
}

function isArchivedBook(book: LibraryBook, isAdmin: boolean) {
  return (isAdmin ? book.status : book.subscription?.status) === "archived"
}

export function LibraryPage() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { user, entrypoint } = useAuth()
  const isAdmin = entrypoint !== "public" && user?.role === "admin"

  const [searchQuery, setSearchQuery] = useState("")
  const [libraryView, setLibraryView] = useState<"active" | "completed">("active")
  const [deleteBookId, setDeleteBookId] = useState<string | null>(null)
  const [integrityOpen, setIntegrityOpen] = useState(false)
  const [actionNotice, setActionNotice] = useState<{ pending: boolean; text: string } | null>(null)

  const { data, isLoading, error: libraryError, refetch: refetchLibrary } = useQuery({
    queryKey: ["library", isAdmin ? "admin" : "mine"],
    queryFn: () => isAdmin ? api.libraryBooks() : api.subscribe.myLibrary(),
    refetchInterval: 5000,
    refetchOnWindowFocus: "always",
  })

  const actionMutation = useMutation({
    mutationFn: ({ bookId, action }: { bookId: string; action: string }) => executeLibraryBookAction(bookId, action, isAdmin),
    onMutate: ({ action }) => setActionNotice({ pending: true, text: action === "rebuild" ? "正在重新处理书籍…" : "正在更新订阅状态…" }),
    onSuccess: async (_result, { action }) => {
      await queryClient.invalidateQueries({ queryKey: ["library"] })
      setActionNotice({ pending: false, text: action === "rebuild" ? "书籍已重新进入处理队列。" : "订阅状态已更新。" })
    },
    onError: () => setActionNotice(null),
  })

  const deleteMutation = useMutation({
    mutationFn: (bookId: string) => isAdmin ? api.deleteLibraryBook(bookId) : api.subscribe.removeSubscription(bookId),
    onMutate: () => setActionNotice({ pending: true, text: isAdmin ? "正在删除共享书籍…" : "正在移除订阅…" }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["library"] })
      setDeleteBookId(null)
      setActionNotice({ pending: false, text: isAdmin ? "共享书籍已删除。" : "订阅已移除。" })
    },
    onError: () => setActionNotice(null),
  })

  const integrityQuery = useQuery<LibraryIntegrityReport>({
    queryKey: ["library-integrity"],
    queryFn: () => api.libraryIntegrity(),
    enabled: isAdmin && integrityOpen,
  })

  const integrityRepairMutation = useMutation({
    mutationFn: () => api.repairLibraryIntegrity(),
    onSuccess: async () => {
      await Promise.all([
        integrityQuery.refetch(),
        queryClient.invalidateQueries({ queryKey: ["library"] }),
      ])
      setActionNotice({ pending: false, text: "完整性修复已执行，未恢复项已重新排队。" })
    },
  })

  const books: LibraryBook[] = useMemo(() => {
    const items = (data as { items?: LibraryBook[] } | undefined)?.items || []
    const scoped = items.filter((book) => libraryView === "completed"
      ? isArchivedBook(book, isAdmin)
      : !isArchivedBook(book, isAdmin))
    if (!searchQuery) return scoped
    return scoped.filter(
      (b) =>
        b.displayName.toLowerCase().includes(searchQuery.toLowerCase()) ||
        (b.displayAuthor || "").toLowerCase().includes(searchQuery.toLowerCase())
    )
  }, [data, isAdmin, libraryView, searchQuery])
  const allBooks = (data as { items?: LibraryBook[] } | undefined)?.items || []
  const completedCount = allBooks.filter((book) => isArchivedBook(book, isAdmin)).length
  const activeCount = allBooks.length - completedCount

  const bookToDelete = books.find((b) => b.aggregateBookId === deleteBookId)
  const mutationError = actionMutation.error || deleteMutation.error
  const libraryBusy = actionMutation.isPending || deleteMutation.isPending

  return (
    <div className="space-y-6">
      <div className="relative flex flex-col justify-between gap-4 sm:flex-row sm:items-center">
        <div className="pr-12 sm:pr-0">
          <h1 className="text-2xl font-bold tracking-tight text-slate-900">{isAdmin ? "全局书库" : "我的书库"}</h1>
          <p className="mt-1 text-sm text-slate-500">{isAdmin ? `共 ${books.length} 本书籍` : "个人订阅、章节覆盖与处理状态"}</p>
        </div>
        {isAdmin && (
          <Button
            type="button"
            variant="outline"
            className="absolute right-0 top-0 h-9 w-9 p-0 sm:static sm:w-auto sm:gap-2 sm:px-4"
            aria-label="检查书库完整性"
            title="检查书库完整性"
            onClick={() => setIntegrityOpen(true)}
          >
            <ShieldCheck className="h-4 w-4" />
            <span className="hidden sm:inline">完整性检查</span>
          </Button>
        )}
      </div>

      <Tabs value={libraryView} onValueChange={(value) => setLibraryView(value as "active" | "completed")}>
        <div className="flex justify-center">
          <TabsList aria-label="书库分类" className="h-auto w-auto gap-1 rounded-xl bg-slate-100 p-1.5 text-slate-500">
            <TabsTrigger value="active" className="gap-2 px-4">进行中 <span className="text-xs text-slate-400">{activeCount}</span></TabsTrigger>
            <TabsTrigger value="completed" className="gap-2 px-4">已完结 <span className="text-xs text-slate-400">{completedCount}</span></TabsTrigger>
          </TabsList>
        </div>
      </Tabs>

      <div className="flex flex-col sm:flex-row gap-4 items-center">
        <div className="w-full relative group shadow-sm hover:shadow transition-shadow duration-300 rounded-full bg-white border border-slate-200 focus-within:border-blue-400 focus-within:ring-2 focus-within:ring-blue-400/10 flex-1">
          <div className="flex items-center w-full px-1 py-1">
            <div className="pl-4 pr-2 text-slate-400 group-focus-within:text-blue-500 transition-colors">
              <Search className="h-4 w-4" />
            </div>
            <input
              type="text"
              name="library-search"
              autoComplete="off"
              placeholder="在书库中搜索…"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="flex-1 bg-transparent border-0 py-2 md:py-2.5 text-sm focus:outline-none focus:ring-0 text-slate-800 placeholder:text-slate-400"
            />
          </div>
        </div>
      </div>

      {(libraryError || mutationError) && (
        <Alert variant="destructive">
          <AlertDescription className="flex flex-wrap items-center justify-between gap-3">
            <span>{apiErrorMessage(mutationError || libraryError, "书库操作失败，请稍后重试。")}</span>
            {libraryError && (
              <Button type="button" size="sm" variant="outline" onClick={() => { void refetchLibrary() }}>
                重试
              </Button>
            )}
          </AlertDescription>
        </Alert>
      )}

      {actionNotice && (
        <Alert aria-live="polite" className={actionNotice.pending ? "border-blue-200 bg-blue-50 text-blue-800" : "border-emerald-200 bg-emerald-50 text-emerald-800"}>
          <AlertDescription className="flex items-center gap-2">
            {actionNotice.pending && <Loader2 className="h-4 w-4 animate-spin" />}
            <span>{actionNotice.text}</span>
          </AlertDescription>
        </Alert>
      )}

      {isLoading ? (
        <div className="flex items-center justify-center py-20">
          <Loader2 className="h-6 w-6 animate-spin text-slate-400" />
        </div>
      ) : books.length > 0 ? (
        <div className="grid gap-6 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
          {books.map((book) => {
            const personal = book.personalProgress
            const displayStatus = isAdmin ? book.status : book.subscription?.status || "unknown"
            const hasBookState = Number(book.bookState?.chapterCount || 0) > 0
            const fullCount = isAdmin
              ? Number(hasBookState ? book.bookState?.readableChapterCount : book.visibleProcessedChapters) || 0
              : Number(personal?.fullCount || 0)
            const previewCount = isAdmin
              ? Number(hasBookState ? book.bookState?.previewChapterCount : 0) || 0
              : Number(personal?.previewCount || 0)
            const failedCount = isAdmin
              ? Number(hasBookState ? book.bookState?.failedChapterCount : book.failedChapters) || 0
              : Number(personal?.failedCount || 0)
            const personalTotal = Number(personal?.fullCount || 0)
              + Number(personal?.previewCount || 0)
              + Number(personal?.failedCount || 0)
              + Number(personal?.pendingCount || 0)
            const scopeTotal = isAdmin
              ? Math.max(Number(book.totalChapters || 0), Number(book.bookState?.chapterCount || 0))
              : personalTotal
            const progress = scopeTotal > 0
              ? Math.round((Math.max(0, Math.min(fullCount, scopeTotal)) / scopeTotal) * 100)
              : 0
            const hasError = typeof failedCount === "number" && failedCount > 0
            const trackingCurrentChapters = book.bookStatus !== "completed"
              && scopeTotal > 0
              && fullCount >= scopeTotal
              && previewCount === 0
              && failedCount === 0
            return (
              <Card key={book.aggregateBookId} className="overflow-hidden flex flex-col hover:shadow-lg transition-shadow duration-200 relative group">
                <Link
                  className="absolute inset-0 z-10 rounded-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-400 focus-visible:ring-offset-2"
                  aria-label={`打开《${book.displayName}》详情`}
                  to={`/console/library/${book.aggregateBookId}`}
                />
                <div className="flex p-5 gap-4">
                  <div className="relative w-20 h-28 bg-slate-200 rounded-md overflow-hidden flex-shrink-0 shadow-sm">
                    {book.coverUrl ? (
                      <img src={book.coverUrl} alt={book.displayName} width={80} height={112} loading="lazy" className="w-full h-full object-cover" />
                    ) : (
                      <div className="flex h-full w-full items-center justify-center text-slate-400">
                        <Search className="h-6 w-6" />
                      </div>
                    )}
                    {isAdmin && book.primarySourceName && (
                      <div className="absolute bottom-0 left-0 right-0 bg-black/60 backdrop-blur-sm py-1 px-1.5">
                        <p className="text-[9px] text-white/90 text-center truncate">{book.primarySourceName}</p>
                      </div>
                    )}
                  </div>

                  <div className="flex flex-col flex-1 min-w-0">
                    <div className="flex justify-between items-start">
                      <h3 className="font-semibold text-slate-900 truncate text-base">{book.displayName}</h3>

                      <div className="relative z-20">
                        <DropdownMenu>
                          <DropdownMenuTrigger asChild>
                            <Button
                              variant="ghost"
                              size="icon"
                              className="h-6 w-6 -mr-2 -mt-1"
                              aria-label={`打开《${book.displayName}》操作菜单`}
                            >
                              <MoreVertical className="h-4 w-4" />
                            </Button>
                          </DropdownMenuTrigger>
                          <DropdownMenuContent align="end" className="w-36">
                            {isAdmin && (
                              <DropdownMenuItem disabled={libraryBusy} onSelect={() => actionMutation.mutate({ bookId: book.aggregateBookId, action: "rebuild" })}>
                                <RefreshCw className="h-3 w-3 mr-2" /> 重新处理
                              </DropdownMenuItem>
                            )}
                            {displayStatus !== "archived" && (displayStatus === "paused" ? (
                              <DropdownMenuItem disabled={libraryBusy} onSelect={() => actionMutation.mutate({ bookId: book.aggregateBookId, action: "resume" })}>
                                <Play className="h-3 w-3 mr-2" /> 继续
                              </DropdownMenuItem>
                            ) : (
                              <DropdownMenuItem disabled={libraryBusy} onSelect={() => actionMutation.mutate({ bookId: book.aggregateBookId, action: "pause" })}>
                                <Pause className="h-3 w-3 mr-2" /> 暂停
                              </DropdownMenuItem>
                            ))}
                            {displayStatus !== "archived" && (
                              <DropdownMenuItem disabled={libraryBusy} onSelect={() => actionMutation.mutate({ bookId: book.aggregateBookId, action: "archive" })}>
                                <Archive className="h-3 w-3 mr-2" /> 归档
                              </DropdownMenuItem>
                            )}
                            <DropdownMenuSeparator />
                            <DropdownMenuItem disabled={libraryBusy} className="text-rose-600 focus:text-rose-600" onSelect={() => setDeleteBookId(book.aggregateBookId)}>
                              <Trash2 className="h-3 w-3 mr-2" /> {isAdmin ? "删除" : "移除订阅"}
                            </DropdownMenuItem>
                          </DropdownMenuContent>
                        </DropdownMenu>
                      </div>
                    </div>

                    <p className="text-sm text-slate-500 truncate mt-0.5">{book.displayAuthor || "未知作者"}</p>

                    <div className="mt-2.5 text-xs text-slate-500 flex flex-wrap items-center gap-x-1.5 gap-y-1">
                      {book.contentType === "audio" ? (
                        <Badge className="rounded-full border border-violet-200 bg-violet-50 text-violet-600 hover:bg-violet-50">有声书</Badge>
                      ) : book.contentType === "video" ? (
                        <Badge className="rounded-full border border-sky-200 bg-sky-50 text-sky-600 hover:bg-sky-50">视频</Badge>
                      ) : null}
                      <span className={book.bookStatus === "ongoing" ? "text-emerald-600" : "text-slate-600"}>
                        {book.bookStatus === "completed" ? "已完结" : "连载中"}
                      </span>
                      {book.wordCount && (
                        <>
                          <span className="text-slate-300">•</span>
                          <span>{book.wordCount}</span>
                        </>
                      )}
                    </div>

                    <div className="mt-auto pt-2 text-xs text-slate-400">
                      {book.lastChapterTitle ? `最新至 ${book.lastChapterTitle}` : ""}
                    </div>
                  </div>
                </div>

                {book.intro && (
                  <div className="px-5 py-4 bg-slate-50/50 border-t border-slate-100 flex-1 flex flex-col">
                    <p className="text-xs text-slate-600 line-clamp-3 leading-relaxed flex-1">{book.intro}</p>
                  </div>
                )}

                <div className="p-4 border-t border-slate-100 mt-auto bg-white flex flex-col justify-end">
                  <div className="flex justify-between items-center mb-2 text-xs text-slate-500">
                    <div className="flex items-center gap-2">
                      {trackingCurrentChapters ? (
                        <Badge className="rounded-full border border-emerald-200 bg-emerald-50 text-emerald-700 hover:bg-emerald-50">
                          <span className="mr-1.5 h-1.5 w-1.5 rounded-full bg-emerald-500 motion-safe:animate-pulse" />
                          已同步 · 追更中
                        </Badge>
                      ) : processStatusLabel(displayStatus)}
                    </div>
                    <span>{progress}%</span>
                  </div>
                  <Progress
                    value={progress}
                    className="h-1.5"
                    indicatorClassName={
                      displayStatus === "error" ? "bg-rose-500" :
                      displayStatus === "completed" ? "bg-emerald-500" :
                      displayStatus === "archived" ? "bg-slate-400" :
                      "bg-slate-500"
                    }
                  />
                  <div className="mt-2.5 text-xs text-slate-500 flex items-center gap-2">
                    <span>{`全文 ${fullCount} · 预览 ${previewCount}`}</span>
                    <span className="text-slate-300">·</span>
                    <span className={hasError ? "text-rose-500" : ""}>
                      {typeof failedCount === "number" ? `失败 ${failedCount}` : "失败数未知"}
                    </span>
                    {!isAdmin && <><span className="text-slate-300">·</span><span>待处理 {personal?.pendingCount ?? 0}</span></>}
                  </div>
                </div>
              </Card>
            )
          })}
        </div>
      ) : (
        <div className="flex flex-col items-center justify-center py-20 text-center">
          <div className="w-16 h-16 bg-slate-50 rounded-full flex items-center justify-center mb-4 text-slate-400">
            <Search className="h-8 w-8" />
          </div>
          {searchQuery ? (
            <p className="text-slate-500">当前范围没有匹配书籍</p>
          ) : (
            <>
              <p className="text-slate-500 mb-4">{libraryView === "completed" ? "暂无已完结书籍" : isAdmin ? "暂无进行中的书籍" : "你还没有进行中的订阅"}</p>
              <Button onClick={() => navigate("/console/subscription")}>去发现新书</Button>
            </>
          )}
        </div>
      )}

      <Dialog open={!!deleteBookId} onOpenChange={(open) => { if (!open) setDeleteBookId(null) }}>
        <DialogContent onClick={(e) => e.stopPropagation()}>
          <DialogHeader>
            <DialogTitle>{isAdmin ? "删除书籍" : "移除订阅"}</DialogTitle>
            <DialogDescription>
              {isAdmin
                ? `确定要将《${bookToDelete?.displayName || ""}》从书库中删除吗？此操作不可逆，将同时移除所有用户对此书的订阅及相关章节数据。`
                : `确定要移除《${bookToDelete?.displayName || ""}》的订阅吗？共享书籍和其他用户的订阅不会受影响。`}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteBookId(null)}>取消</Button>
            <Button variant="destructive" onClick={() => bookToDelete && deleteMutation.mutate(bookToDelete.aggregateBookId)} disabled={libraryBusy}>
              {deleteMutation.isPending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              {isAdmin ? "确认删除" : "确认移除"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={integrityOpen} onOpenChange={setIntegrityOpen}>
        <DialogContent className="sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <ShieldCheck className="h-5 w-5 text-slate-600" />
              书库完整性
            </DialogTitle>
            <DialogDescription>检查数据库、章节文件与来源快照是否一致。</DialogDescription>
          </DialogHeader>

          {integrityQuery.isLoading ? (
            <div className="flex h-44 items-center justify-center">
              <Loader2 className="h-6 w-6 animate-spin text-slate-400" />
            </div>
          ) : integrityQuery.error ? (
            <Alert variant="destructive">
              <AlertDescription>{apiErrorMessage(integrityQuery.error, "完整性检查失败。")}</AlertDescription>
            </Alert>
          ) : integrityQuery.data ? (
            <>
              <div className="grid grid-cols-3 divide-x border-y border-slate-200 py-3 text-center">
                <div>
                  <p className="text-xl font-semibold text-emerald-600">{integrityQuery.data.summary.healthyBooks}</p>
                  <p className="text-xs text-slate-500">健康</p>
                </div>
                <div>
                  <p className="text-xl font-semibold text-amber-600">{integrityQuery.data.summary.repairableBooks}</p>
                  <p className="text-xs text-slate-500">可修复</p>
                </div>
                <div>
                  <p className="text-xl font-semibold text-rose-600">{integrityQuery.data.summary.brokenBooks}</p>
                  <p className="text-xs text-slate-500">严重异常</p>
                </div>
              </div>

              <div className="max-h-80 space-y-3 overflow-y-auto pr-1">
                {integrityQuery.data.books.every((book) => book.status === "healthy") ? (
                  <p className="py-10 text-center text-sm text-slate-500">未发现完整性问题</p>
                ) : integrityQuery.data.books.filter((book) => book.status !== "healthy").map((book) => (
                  <div key={book.bookId} className="border-b border-slate-100 pb-3 last:border-0">
                    <div className="flex items-center justify-between gap-3">
                      <div className="min-w-0">
                        <p className="truncate text-sm font-medium text-slate-900">{book.name}</p>
                        <p className="truncate text-xs text-slate-500">{book.author || "未知作者"}</p>
                      </div>
                      <Badge className={book.status === "broken"
                        ? "border border-rose-200 bg-rose-50 text-rose-700 hover:bg-rose-50"
                        : "border border-amber-200 bg-amber-50 text-amber-700 hover:bg-amber-50"}
                      >
                        {book.status === "broken" ? "需人工处理" : "可修复"}
                      </Badge>
                    </div>
                    <ul className="mt-2 space-y-1 text-xs text-slate-600">
                      {book.issues.map((issue) => (
                        <li key={issue.code}>{issue.message}{issue.count > 1 ? `（${issue.count}）` : ""}</li>
                      ))}
                    </ul>
                  </div>
                ))}
              </div>
            </>
          ) : null}

          {(integrityRepairMutation.error) && (
            <Alert variant="destructive">
              <AlertDescription>{apiErrorMessage(integrityRepairMutation.error, "完整性修复失败。")}</AlertDescription>
            </Alert>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setIntegrityOpen(false)}>关闭</Button>
            <Button
              onClick={() => integrityRepairMutation.mutate()}
              disabled={!integrityQuery.data?.summary.repairableBooks || integrityRepairMutation.isPending}
            >
              {integrityRepairMutation.isPending ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Wrench className="mr-2 h-4 w-4" />}
              修复可恢复项
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
