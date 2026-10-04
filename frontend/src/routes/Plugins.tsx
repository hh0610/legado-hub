import { useState } from "react"
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query"
import { useSearchParams } from "react-router"
import { RotateCw, Power, SlidersHorizontal, KeyRound } from "lucide-react"
import { api, apiErrorMessage } from "@/lib/api"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { SourceListTable } from "@/components/sources/SourceListTable"
import { PluginSettingsDialog } from "@/components/sources/PluginSettingsDialog"
import { OfficialSourcesPage } from "./OfficialSourcesPage"

export function Plugins() {
  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())
  const [settingsTarget, setSettingsTarget] = useState<any | null>(null)
  const activeTab: "thirdparty" | "official" = searchParams.get("tab") === "official" ? "official" : "thirdparty"

  const { data, isLoading, error: pluginsError, refetch: refetchPlugins } = useQuery({ queryKey: ["plugins"], queryFn: api.plugins })
  const allPlugins = data?.items || []
  const plugins = activeTab === "thirdparty" ? allPlugins.filter((p: any) => !p.official) : allPlugins.filter((p: any) => p.official)
  const pingAllMutation = useMutation({
    mutationFn: () => api.pingAllPlugins(plugins.map((p: any) => p.pluginId)),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["plugins"] }),
  })
  const batchEnableMutation = useMutation({
    mutationFn: ({ ids, enabled }: { ids: string[]; enabled: boolean }) => api.batchEnablePlugins(ids, enabled),
    onSuccess: () => { setSelectedIds(new Set()); queryClient.invalidateQueries({ queryKey: ["plugins"] }) },
  })
  const enabledCount = plugins.filter((p: any) => p.enabled).length
  const browserSourceCount = plugins.filter((p: any) => (p.accessType || p.sourceType) === "Browser").length
  const pingReachable = plugins.filter((p: any) => p.health?.pingStatus === "reachable").length
  const pingChecked = plugins.filter((p: any) => ["reachable", "unreachable"].includes(p.health?.pingStatus)).length
  const pingRate = pingChecked > 0 ? ((pingReachable / pingChecked) * 100).toFixed(1) : "0"

  const toggleSelectAll = () => {
    setSelectedIds((prev) => prev.size === plugins.length ? new Set() : new Set(plugins.map((p: any) => p.pluginId)))
  }

  const handleRowCheck = (pluginId: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev)
      if (next.has(pluginId)) next.delete(pluginId)
      else next.add(pluginId)
      return next
    })
  }

  const selectTab = (tab: "thirdparty" | "official") => {
    setSelectedIds(new Set())
    setSearchParams(tab === "official" ? { tab: "official" } : {}, { replace: true })
  }

  const sourceTabs = (
    <Tabs value={activeTab} onValueChange={(value) => selectTab(value as "thirdparty" | "official")} className="gap-0">
      <TabsList aria-label="书源类型" className="h-11 w-fit justify-start rounded-none bg-transparent p-0">
        <TabsTrigger value="thirdparty" className="h-11 gap-2 rounded-none border-b-2 border-transparent px-4 py-0 data-[state=active]:border-slate-900 data-[state=active]:bg-transparent data-[state=active]:shadow-none">
          <SlidersHorizontal className="h-4 w-4" />第三方书源
        </TabsTrigger>
        <TabsTrigger value="official" className="h-11 gap-2 rounded-none border-b-2 border-transparent px-4 py-0 data-[state=active]:border-slate-900 data-[state=active]:bg-transparent data-[state=active]:shadow-none">
          <KeyRound className="h-4 w-4" />官方源
        </TabsTrigger>
      </TabsList>
    </Tabs>
  )

  const batchActions = selectedIds.size > 0 ? (
    <>
      <span className="text-xs font-medium text-slate-500">已选 {selectedIds.size} 项</span>
      <Button variant="outline" size="sm" className="h-8 bg-white text-xs" disabled={batchEnableMutation.isPending} onClick={() => batchEnableMutation.mutate({ ids: Array.from(selectedIds), enabled: true })}>
        <Power className="mr-1 h-3.5 w-3.5 text-emerald-500" />批量启用
      </Button>
      <Button variant="outline" size="sm" className="h-8 bg-white text-xs" disabled={batchEnableMutation.isPending} onClick={() => batchEnableMutation.mutate({ ids: Array.from(selectedIds), enabled: false })}>
        <Power className="mr-1 h-3.5 w-3.5 text-slate-400" />批量禁用
      </Button>
    </>
  ) : null

  return (
    <div className="space-y-6">
      <div>
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-slate-900">书源管理</h1>
          <p className="mt-1 text-sm text-slate-500">管理第三方书源、官方账号认证与站点连通性。</p>
        </div>
      </div>

      <div data-testid="source-stats" className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Card className="shadow-none">
          <CardContent className="p-4">
            <p className="text-xs font-medium text-slate-500">当前书源数量</p>
            <div className="mt-2 flex items-baseline gap-2"><span className="text-2xl font-semibold tabular-nums text-slate-900">{plugins.length}</span><span className="text-xs text-slate-400">规则源</span></div>
          </CardContent>
        </Card>
        <Card className="shadow-none">
          <CardContent className="p-4">
            <p className="text-xs font-medium text-slate-500">已启用</p>
            <div className="mt-2 flex items-baseline gap-2"><span className="text-2xl font-semibold tabular-nums text-emerald-700">{enabledCount}</span><span className="text-xs text-slate-400">/{plugins.length} 运行中</span></div>
          </CardContent>
        </Card>
        <Card className="shadow-none">
          <CardContent className="p-4">
            <p className="text-xs font-medium text-slate-500">Browser 模式</p>
            <div className="mt-2 flex items-baseline gap-2"><span className="text-2xl font-semibold tabular-nums text-blue-700">{browserSourceCount}</span><span className="text-xs text-slate-400">/{plugins.length} 个书源</span></div>
          </CardContent>
        </Card>
        <Card className="shadow-none">
          <CardContent className="p-4">
            <p className="text-xs font-medium text-slate-500">Ping 连通率</p>
            <div className="mt-2 flex items-baseline gap-2"><span className="text-2xl font-semibold tabular-nums text-amber-700">{pingRate}%</span><span className="text-xs text-slate-400">{pingChecked}/{plugins.length} 已测</span></div>
          </CardContent>
        </Card>
      </div>

      {activeTab === "official" ? (
        <div key="official" className="space-y-6 motion-safe:animate-in motion-safe:fade-in-0 motion-safe:slide-in-from-bottom-1 motion-safe:duration-200">
          {(pluginsError || batchEnableMutation.error) && (
            <Alert variant="destructive">
              <AlertDescription className="flex flex-wrap items-center justify-between gap-3">
                <span>{apiErrorMessage(batchEnableMutation.error || pluginsError, "书源状态更新失败，请稍后重试。")}</span>
                {pluginsError && <Button type="button" size="sm" variant="outline" onClick={() => { void refetchPlugins() }}>重试</Button>}
              </AlertDescription>
            </Alert>
          )}
          <OfficialSourcesPage
            basePlugins={plugins}
            headerTabs={sourceTabs}
            batchToolbar={batchActions}
            selectedIds={selectedIds}
            selectionDisabled={batchEnableMutation.isPending}
            onToggleSelectAll={toggleSelectAll}
            onToggleSelected={handleRowCheck}
            onOpenSettings={(source) => setSettingsTarget(source)}
          />
        </div>
      ) : (
      <div key="thirdparty" className="space-y-6 motion-safe:animate-in motion-safe:fade-in-0 motion-safe:slide-in-from-bottom-1 motion-safe:duration-200">
      {(pluginsError || pingAllMutation.error || batchEnableMutation.error) && (
        <Alert variant="destructive">
          <AlertDescription className="flex flex-wrap items-center justify-between gap-3">
            <span>{apiErrorMessage(pingAllMutation.error || batchEnableMutation.error || pluginsError, "书源操作失败，请稍后重试。")}</span>
            {pluginsError && <Button type="button" size="sm" variant="outline" onClick={() => { void refetchPlugins() }}>重试</Button>}
          </AlertDescription>
        </Alert>
      )}

      <SourceListTable
        items={plugins}
        title="第三方书源列表"
        description="管理规则插件的启用状态与站点连通性。"
        emptyMessage="暂无第三方书源。"
        headerTabs={sourceTabs}
        loading={isLoading}
        selectable
        selectedIds={selectedIds}
        selectionDisabled={batchEnableMutation.isPending}
        onToggleSelectAll={toggleSelectAll}
        onToggleSelected={handleRowCheck}
        onOpenSettings={(source) => setSettingsTarget(source)}
        testId="thirdparty-sources-table-boundary"
        toolbar={(
          <div className="flex flex-wrap items-center justify-end gap-2">
            {batchActions}
            <Button variant="outline" disabled={pingAllMutation.isPending || plugins.length === 0} onClick={() => pingAllMutation.mutate()}>
              <RotateCw className={`mr-2 h-4 w-4 ${pingAllMutation.isPending ? "animate-spin" : ""}`} />Ping 全部
            </Button>
          </div>
        )}
      />
      </div>
      )}
      <PluginSettingsDialog
        pluginId={settingsTarget?.pluginId || null}
        pluginName={settingsTarget?.name || ""}
        open={!!settingsTarget}
        onOpenChange={(open) => { if (!open) setSettingsTarget(null) }}
      />
    </div>
  )
}
