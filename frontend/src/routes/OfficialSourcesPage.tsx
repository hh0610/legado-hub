import { type ReactNode, useCallback, useEffect, useRef, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { AlertCircle, LogIn, LogOut, RefreshCw, XCircle, Monitor } from "lucide-react"
import { api, apiErrorMessage } from "@/lib/api"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { TableCell, TableHead } from "@/components/ui/table"
import { OfficialSourceLoginDialog } from "@/components/auth/OfficialSourceLoginDialog"
import { SourceListTable } from "@/components/sources/SourceListTable"

const TERMINAL_LOGIN_STATUSES = new Set(["success", "pending", "failed", "timeout", "cancelled", "none"])
export const BROWSER_LOGIN_TIMEOUT_MS = 5 * 60 * 1000
const BROWSER_LOGIN_POLL_INTERVAL_MS = 3000

interface OfficialSourcesPageProps {
  basePlugins?: any[]
  headerTabs?: ReactNode
  batchToolbar?: ReactNode
  selectedIds?: Set<string>
  selectionDisabled?: boolean
  onToggleSelectAll?: () => void
  onToggleSelected?: (pluginId: string) => void
  onOpenSettings?: (item: any) => void
}

export function OfficialSourcesPage({
  basePlugins = [],
  headerTabs,
  batchToolbar,
  selectedIds,
  selectionDisabled = false,
  onToggleSelectAll,
  onToggleSelected,
  onOpenSettings,
}: OfficialSourcesPageProps) {
  const queryClient = useQueryClient()
  const { data, isLoading, error: queryError, refetch } = useQuery({ queryKey: ["official-sources"], queryFn: api.officialSources, refetchInterval: 5000 })
  const [loginSession, setLoginSession] = useState<any>(null)
  const [loginDialogOpen, setLoginDialogOpen] = useState(false)
  const [selectedPlugin, setSelectedPlugin] = useState<{ id: string; name: string } | null>(null)
  const [operationError, setOperationError] = useState("")
  const intervalRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const deadlineRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const loginAttemptRef = useRef(0)

  const refreshMutation = useMutation({
    mutationFn: ({ pluginId }: { pluginId: string; name: string }) => api.pluginAuthCheck(pluginId),
    onMutate: () => setOperationError(""),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["official-sources"] }),
    onError: (_err, { name }) => setOperationError(`${name} 登录状态刷新失败，请稍后重试。`),
  })
  const globalRefreshMutation = useMutation({
    mutationFn: (pluginIds: string[]) => Promise.all(pluginIds.map(api.pluginAuthCheck)),
    onMutate: () => setOperationError(""),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["official-sources"] }),
    onError: () => setOperationError("官方源登录状态刷新失败，请稍后重试。"),
  })
  const logoutMutation = useMutation({
    mutationFn: ({ pluginId }: { pluginId: string; name: string }) => api.loginLogout(pluginId),
    onMutate: () => setOperationError(""),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["official-sources"] }),
    onError: (_err, { name }) => setOperationError(`${name} 注销失败，请稍后重试。`),
  })
  const clearCookiesMutation = useMutation({
    mutationFn: ({ pluginId }: { pluginId: string; name: string }) => api.pluginCookiesClear(pluginId),
    onMutate: () => setOperationError(""),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["official-sources"] }),
    onError: (_err, { name }) => setOperationError(`${name} Cookie 清除失败，请稍后重试。`),
  })
  const cancelLoginMutation = useMutation({
    mutationFn: (pluginId: string) => api.cancelLoginBrowser(pluginId),
    onMutate: () => {
      loginAttemptRef.current += 1
      setOperationError("")
      setLoginSession((current: any) => current ? { ...current, status: "cancelling", message: "正在取消登录...", polling: false } : current)
    },
    onSuccess: (result, pluginId) => {
      setLoginSession({
        pluginId,
        status: result?.cancelled ? "cancelled" : "failed",
        message: result?.cancelled ? "登录已取消" : "取消失败：没有活跃的登录会话",
        polling: false,
      })
    },
    onError: (err, pluginId) => setLoginSession({
      pluginId,
      status: "failed",
      message: err instanceof Error ? err.message : "取消登录失败",
      polling: false,
    }),
  })

  const startBrowserLogin = useCallback(async (pluginId: string) => {
    if (loginSession?.polling || cancelLoginMutation.isPending) return
    const attemptId = ++loginAttemptRef.current
    const deadlineAt = Date.now() + BROWSER_LOGIN_TIMEOUT_MS
    setOperationError("")
    setLoginSession({
      pluginId,
      status: "starting",
      message: "正在启动浏览器登录...",
      polling: true,
      deadlineAt,
      attemptId,
    })
    try {
      const result = await api.startLoginBrowser(pluginId)
      if (loginAttemptRef.current !== attemptId) return
      if (result?.error) throw new Error(result.error)
      const status = result.status || "running"
      setLoginSession({
        pluginId,
        status,
        message: result.message || "请在浏览器窗口完成登录",
        polling: !TERMINAL_LOGIN_STATUSES.has(status),
        deadlineAt,
        attemptId,
      })
      if (status === "success") {
        queryClient.invalidateQueries({ queryKey: ["official-sources"] })
      }
    } catch (err) {
      if (loginAttemptRef.current !== attemptId) return
      setLoginSession({
        pluginId,
        status: "failed",
        message: err instanceof Error ? err.message : "浏览器登录启动失败",
        polling: false,
      })
    }
  }, [cancelLoginMutation.isPending, loginSession?.polling, queryClient])

  useEffect(() => {
    const handleBrowserLogin = (event: Event) => {
      const pluginId = (event as CustomEvent<{ pluginId?: string }>).detail?.pluginId
      if (pluginId) void startBrowserLogin(pluginId)
    }
    window.addEventListener("official-source-browser-login", handleBrowserLogin)
    return () => window.removeEventListener("official-source-browser-login", handleBrowserLogin)
  }, [startBrowserLogin])

  useEffect(() => () => {
    loginAttemptRef.current += 1
  }, [])

  useEffect(() => {
    if (!loginSession?.polling || !loginSession.pluginId) return
    let active = true
    const pluginId = loginSession.pluginId
    const attemptId = Number(loginSession.attemptId) || loginAttemptRef.current
    const deadlineAt = Number(loginSession.deadlineAt) || Date.now() + BROWSER_LOGIN_TIMEOUT_MS
    const markTimedOut = () => {
      if (!active || loginAttemptRef.current !== attemptId) return
      loginAttemptRef.current += 1
      setLoginSession({
        pluginId,
        status: "timeout",
        message: "浏览器登录超时，请重新发起。",
        polling: false,
      })
    }
    const poll = async () => {
      if (Date.now() >= deadlineAt) {
        markTimedOut()
        return
      }
      try {
        const result = await api.getLoginBrowserStatus(pluginId)
        if (!active || loginAttemptRef.current !== attemptId) return
        if (result?.error) throw new Error(result.error)
        const status = result.status || "failed"
        const polling = !TERMINAL_LOGIN_STATUSES.has(status)
        setLoginSession({ pluginId, status, message: result.message || "登录中...", polling, deadlineAt, attemptId })
        if (status === "success") queryClient.invalidateQueries({ queryKey: ["official-sources"] })
        if (polling) intervalRef.current = setTimeout(poll, Math.min(BROWSER_LOGIN_POLL_INTERVAL_MS, Math.max(0, deadlineAt - Date.now())))
      } catch (err) {
        if (!active || loginAttemptRef.current !== attemptId) return
        setLoginSession({
          pluginId,
          status: "failed",
          message: apiErrorMessage(err, "登录状态查询失败"),
          polling: false,
        })
      }
    }
    deadlineRef.current = setTimeout(markTimedOut, Math.max(0, deadlineAt - Date.now()))
    intervalRef.current = setTimeout(poll, Math.min(BROWSER_LOGIN_POLL_INTERVAL_MS, Math.max(0, deadlineAt - Date.now())))
    return () => {
      active = false
      if (intervalRef.current) clearTimeout(intervalRef.current)
      if (deadlineRef.current) clearTimeout(deadlineRef.current)
      intervalRef.current = null
      deadlineRef.current = null
    }
  }, [loginSession?.attemptId, loginSession?.deadlineAt, loginSession?.pluginId, loginSession?.polling, queryClient])

  const basePluginById = new Map(basePlugins.map((plugin: any) => [plugin.pluginId, plugin]))
  const items = (data?.items || []).map((item: any) => ({ ...item, ...(basePluginById.get(item.pluginId) || {}), authStatus: item.authStatus }))
  const browserLoginBusy = Boolean(loginSession?.polling) || cancelLoginMutation.isPending
  const confirmLogout = (item: any) => {
    if (window.confirm(`确定注销 ${item.name}？已保存的登录状态将被清除。`)) {
      logoutMutation.mutate({ pluginId: item.pluginId, name: item.name })
    }
  }
  const confirmClearCookies = (item: any) => {
    if (window.confirm(`确定清除 ${item.name} 的 Cookie？清除后需要重新登录。`)) {
      clearCookiesMutation.mutate({ pluginId: item.pluginId, name: item.name })
    }
  }
  const refreshAllButton = (
    <Button
      variant="outline"
      aria-busy={globalRefreshMutation.isPending}
      disabled={isLoading || Boolean(queryError) || globalRefreshMutation.isPending || refreshMutation.isPending || items.length === 0}
      onClick={() => globalRefreshMutation.mutate(items.map((item: any) => item.pluginId))}
    >
      <RefreshCw className={`h-4 w-4 mr-2 ${globalRefreshMutation.isPending ? "animate-spin" : ""}`} />
      {globalRefreshMutation.isPending ? "刷新中..." : "全局刷新状态"}
    </Button>
  )
  const listToolbar = (
    <div className="flex flex-wrap items-center justify-end gap-2">
      {batchToolbar}
      {refreshAllButton}
    </div>
  )
  const selectable = Boolean(onToggleSelectAll && onToggleSelected)
  const authHeaders = (
    <>
      <TableHead className="w-44 text-center font-semibold text-slate-700">登录状态</TableHead>
      <TableHead className="w-40 text-center font-semibold text-slate-700">登录操作</TableHead>
    </>
  )
  const listPlaceholder = (
    <SourceListTable
      items={[]}
      title="官方源列表"
      description="在统一书源信息上管理账号认证与登录状态。"
      emptyMessage="暂无已配置的官方源"
      headerTabs={headerTabs}
      toolbar={listToolbar}
      loading={isLoading}
      selectable={selectable}
      selectedIds={selectedIds}
      selectionDisabled={selectionDisabled}
      onToggleSelectAll={onToggleSelectAll}
      onToggleSelected={onToggleSelected}
      testId="official-sources-table-boundary"
      extraColumnCount={2}
      extraHeaders={authHeaders}
    />
  )

  return (
    <div className="space-y-6">
      {queryError && (
        <div role="alert" className="flex items-start gap-2 rounded-md border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
          <span className="flex-1">官方源加载失败：{apiErrorMessage(queryError, "请稍后重试。")}</span>
          <Button type="button" size="sm" variant="outline" onClick={() => { void refetch() }}>重试</Button>
        </div>
      )}

      {operationError && (
        <div role="alert" className="flex items-start gap-2 rounded-md border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>{operationError}</span>
        </div>
      )}

      {isLoading ? listPlaceholder : !queryError && (
        <>
      {loginSession && (
        <Card className={loginSession.polling ? "border-blue-300 bg-blue-50/30" : ""}>
          <CardContent className="p-4">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                <div><div className="font-medium text-sm">{loginSession.polling ? "正在登录" : "登录结果"}</div><div className="text-xs text-slate-500">{loginSession.message}</div></div>
              </div>
              {loginSession.polling && <Button size="sm" variant="outline" disabled={cancelLoginMutation.isPending} onClick={() => cancelLoginMutation.mutate(loginSession.pluginId)}><XCircle className="w-3 h-3 mr-1" /> 取消</Button>}
            </div>
          </CardContent>
        </Card>
      )}

      <SourceListTable
        items={items}
        title="官方源列表"
        description="在统一书源信息上管理账号认证与登录状态。"
        emptyMessage="暂无已配置的官方源"
        headerTabs={headerTabs}
        toolbar={listToolbar}
        selectable={selectable}
        selectedIds={selectedIds}
        selectionDisabled={selectionDisabled}
        onToggleSelectAll={onToggleSelectAll}
        onToggleSelected={onToggleSelected}
        onOpenSettings={onOpenSettings}
        testId="official-sources-table-boundary"
        extraColumnCount={2}
        extraHeaders={authHeaders}
        renderExtraCells={(item: any) => {
          const auth = item.authStatus || {}
          const isCurrentSession = loginSession?.pluginId === item.pluginId
          const isRefreshing = refreshMutation.isPending && refreshMutation.variables?.pluginId === item.pluginId
          const isLoggingOut = logoutMutation.isPending && logoutMutation.variables?.pluginId === item.pluginId
          const isClearing = clearCookiesMutation.isPending && clearCookiesMutation.variables?.pluginId === item.pluginId
          const accountName = String(auth.accountName || "").trim()
          const isAuthenticated = Boolean(auth.authenticated && accountName)
            return (
              <>
              <TableCell className="text-center">
                {isAuthenticated ? (
                  <Badge variant="success" className="max-w-36 truncate">{accountName}</Badge>
                ) : auth.hasCookies || auth.authenticated ? (
                  <Badge variant="destructive">登录失效</Badge>
                ) : (
                  <Badge variant="secondary">未登录</Badge>
                )}
              </TableCell>
              <TableCell className="py-4 text-center">
                <div className="flex justify-center gap-2">
                  {isAuthenticated ? (
                    <Button type="button" aria-label={`注销 ${item.name}`} aria-busy={isLoggingOut} title="注销" variant="ghost" size="sm" className="h-8" disabled={isLoggingOut} onClick={() => confirmLogout(item)}><LogOut className={`h-4 w-4 ${isLoggingOut ? "animate-pulse" : ""}`} /></Button>
                  ) : (
                    <Button variant="outline" size="sm" className="h-8 whitespace-nowrap" disabled={browserLoginBusy} onClick={() => { setSelectedPlugin({ id: item.pluginId, name: item.name }); setLoginDialogOpen(true) }}>
                      {isCurrentSession && loginSession?.polling ? <><Monitor className="mr-1 h-3 w-3 animate-pulse" />登录中...</> : <><LogIn className="mr-1 h-3 w-3" />登录</>}
                    </Button>
                  )}
                  {!isAuthenticated && auth.hasCookies && (
                    <Button type="button" aria-label={`清除 ${item.name} Cookie`} aria-busy={isClearing} title="清除 Cookie" variant="ghost" size="sm" className="h-8" disabled={isClearing} onClick={() => confirmClearCookies(item)}><XCircle className={`h-4 w-4 ${isClearing ? "animate-pulse" : ""}`} /></Button>
                  )}
                  <Button type="button" aria-label={`刷新 ${item.name} 登录状态`} aria-busy={isRefreshing} title="刷新登录状态" variant="ghost" size="sm" className="h-8" disabled={refreshMutation.isPending || globalRefreshMutation.isPending} onClick={() => refreshMutation.mutate({ pluginId: item.pluginId, name: item.name })}><RefreshCw className={`h-4 w-4 ${isRefreshing ? "animate-spin" : ""}`} /></Button>
                </div>
              </TableCell>
            </>
          )
        }}
      />
        </>
      )}

      {queryError && listPlaceholder}

      {selectedPlugin && loginDialogOpen && (
        <OfficialSourceLoginDialog key={selectedPlugin.id} pluginId={selectedPlugin.id} sourceName={selectedPlugin.name} open={loginDialogOpen} onOpenChange={setLoginDialogOpen}
          onSuccess={() => { queryClient.invalidateQueries({ queryKey: ["official-sources"] }) }} />
      )}
    </div>
  )
}
