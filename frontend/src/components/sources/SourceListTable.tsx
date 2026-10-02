import type { ReactNode } from "react"
import { AlertCircle, Settings2 } from "lucide-react"
import { Card, CardContent } from "@/components/ui/card"
import { Checkbox } from "@/components/ui/checkbox"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"

const CAPABILITY_LABELS: Record<string, string> = { search: "搜索", detail: "详情", toc: "目录", chapter: "正文", content: "正文", chapter_reviews: "章评", explore: "发现", auth: "登录" }
const CAPABILITY_COLORS: Record<string, string> = {
  search: "bg-blue-50 text-blue-600",
  detail: "bg-emerald-50 text-emerald-600",
  toc: "bg-purple-50 text-purple-600",
  chapter: "bg-amber-50 text-amber-600",
  content: "bg-amber-50 text-amber-600",
  chapter_reviews: "bg-slate-100 text-slate-600",
  explore: "bg-cyan-50 text-cyan-600",
  auth: "bg-rose-50 text-rose-600",
}
const MEDIA_KIND_LABELS: Record<string, { label: string; className: string }> = {
  audio: { label: "有声", className: "bg-violet-50 text-violet-600" },
  video: { label: "视频", className: "bg-sky-50 text-sky-600" },
}

function sourceMediaKind(source: any): string {
  const kind = String(source?.content?.kind || "").toLowerCase()
  return kind === "audio" || kind === "video" ? kind : ""
}

function sourceLatency(source: any) {
  const latency = Number(source.health?.pingLatencyMs ?? source.latency ?? 0)
  return Number.isFinite(latency) ? latency : 0
}

function latencyClass(latency: number) {
  if (latency === 0) return "text-slate-400"
  if (latency < 100) return "text-emerald-500"
  if (latency < 300) return "text-amber-500"
  return "text-rose-500"
}

interface SourceListTableProps {
  items: any[]
  title: string
  description: string
  emptyMessage: string
  headerTabs?: ReactNode
  toolbar?: ReactNode
  loading?: boolean
  selectable?: boolean
  selectedIds?: Set<string>
  selectionDisabled?: boolean
  onToggleSelectAll?: () => void
  onToggleSelected?: (pluginId: string) => void
  extraHeaders?: ReactNode
  extraColumnCount?: number
  renderExtraCells?: (item: any) => ReactNode
  onOpenSettings?: (item: any) => void
  tableClassName?: string
  testId?: string
}

export function SourceListTable({
  items,
  title,
  description,
  emptyMessage,
  headerTabs,
  toolbar,
  loading = false,
  selectable = false,
  selectedIds = new Set<string>(),
  selectionDisabled = false,
  onToggleSelectAll,
  onToggleSelected,
  extraHeaders,
  extraColumnCount = 0,
  renderExtraCells,
  onOpenSettings,
  tableClassName = "min-w-[900px]",
  testId,
}: SourceListTableProps) {
  const allSelected = items.length > 0 && selectedIds.size === items.length
  const columnCount = 3 + Number(selectable) + extraColumnCount

  return (
    <section>
      <Card className="overflow-hidden border border-slate-200 shadow-sm">
        <CardContent className="p-0" data-testid={testId}>
          {headerTabs && <div className="border-b border-slate-200 bg-slate-50/60 px-4">{headerTabs}</div>}
          <div className="flex flex-col gap-3 border-b border-slate-200 p-3 sm:flex-row sm:items-end sm:justify-between">
            <div>
              <h2 className="text-sm font-semibold text-slate-900">{title}</h2>
              <p className="mt-1 text-sm text-slate-500">{description}</p>
            </div>
            {toolbar}
          </div>
          {loading ? (
            <div role="status" className="py-10 text-center text-sm text-slate-500">加载中...</div>
          ) : (
            <Table
              className={`${tableClassName} table-fixed`}
              containerClassName="max-h-[clamp(12rem,calc(100dvh-36rem),28rem)] overscroll-contain md:max-h-[clamp(14rem,calc(100dvh-24rem),34rem)]"
            >
              <TableHeader className="sticky top-0 z-10 border-b border-slate-200 bg-slate-50 [&_th]:h-9 [&_th]:py-2">
                <TableRow>
                  {selectable && (
                    <TableHead className="w-12 text-center">
                      <Checkbox
                        aria-label="全选当前书源"
                        checked={allSelected}
                        disabled={selectionDisabled || items.length === 0}
                        onCheckedChange={onToggleSelectAll}
                      />
                    </TableHead>
                  )}
                  <TableHead className="py-3 font-semibold text-slate-700">插件名称</TableHead>
                  <TableHead className="w-52 text-center font-semibold text-slate-700">解析能力</TableHead>
                  <TableHead className="w-24 text-center font-semibold text-slate-700">Ping</TableHead>
                  {extraHeaders}
                </TableRow>
              </TableHeader>
              <TableBody className="divide-y divide-slate-100">
                {items.map((source: any) => {
                  const pingStatus = source.health?.pingStatus || "unknown"
                  const latency = sourceLatency(source)
                  return (
                    <TableRow key={source.pluginId} className={`[&>td]:py-2.5 transition-colors duration-150 hover:bg-slate-50/50 ${!source.enabled ? "bg-slate-50/40 opacity-60 grayscale" : ""}`}>
                      {selectable && (
                        <TableCell className="py-4 text-center">
                          <Checkbox
                            aria-label={`选择 ${source.name}`}
                            checked={selectedIds.has(source.pluginId)}
                            disabled={selectionDisabled}
                            onCheckedChange={() => onToggleSelected?.(source.pluginId)}
                          />
                        </TableCell>
                      )}
                      <TableCell className="py-4">
                        <div className="min-w-0">
                          <div className="flex items-center gap-1.5">
                            <span className="text-sm font-semibold text-slate-900">{source.name}</span>
                            {(source.accessType || source.sourceType) === "Browser" && <span className="rounded bg-blue-50 px-1.5 py-0.5 text-[9px] font-medium text-blue-700">Browser</span>}
                            {sourceMediaKind(source) && (
                              <span className={`rounded px-1.5 py-0.5 text-[9px] font-medium ${MEDIA_KIND_LABELS[sourceMediaKind(source)].className}`}>
                                {MEDIA_KIND_LABELS[sourceMediaKind(source)].label}
                              </span>
                            )}
                            {onOpenSettings && (
                              <button
                                type="button"
                                aria-label={`打开 ${source.name} 源设置`}
                                title="源设置"
                                className="ml-0.5 rounded p-0.5 text-slate-400 transition hover:bg-slate-100 hover:text-slate-700"
                                onClick={() => onOpenSettings(source)}
                              >
                                <Settings2 className="h-3.5 w-3.5" />
                              </button>
                            )}
                          </div>
                          <div className="mt-1 space-y-0.5 text-[10px] text-slate-400">
                            <div>作者: {source.author || source.contributor || source.domain || source.baseUrls?.[0] || "-"}</div>
                            {source.version && <div className="font-mono">版本: v{source.version}</div>}
                          </div>
                        </div>
                      </TableCell>
                      <TableCell className="text-center">
                        <div className="mx-auto flex w-44 flex-wrap justify-center gap-0.5">
                          {source.capabilities?.map((capability: string) => (
                            <span key={capability} className={`mr-1 rounded px-1.5 py-0.5 text-[9px] font-medium ${CAPABILITY_COLORS[capability] || "bg-slate-50 text-slate-500"}`}>{CAPABILITY_LABELS[capability] || capability}</span>
                          ))}
                        </div>
                      </TableCell>
                      <TableCell className="text-center">
                        {pingStatus === "unreachable" ? (
                          <span className="text-xs font-medium text-rose-600">不可达</span>
                        ) : latency > 0 ? (
                          <span className={`font-mono text-xs font-semibold ${latencyClass(latency)}`}>{latency}ms</span>
                        ) : (
                          <span className="text-xs text-slate-400">--</span>
                        )}
                      </TableCell>
                      {renderExtraCells?.(source)}
                    </TableRow>
                  )
                })}
                {items.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={columnCount} className="py-12 text-center text-sm text-slate-400">
                      <AlertCircle className="mx-auto mb-2 h-8 w-8 text-slate-300" />{emptyMessage}
                    </TableCell>
                  </TableRow>
                )}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </section>
  )
}
