import { useState } from "react"
import { useQuery } from "@tanstack/react-query"
import { Loader2, Save, Settings2 } from "lucide-react"
import { api, apiErrorMessage } from "@/lib/api"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"

interface UiItem {
  type: string
  id: string
  label: string
  action?: string
  help?: string
  placeholder?: string
  default?: unknown
  choices?: string[]
}

interface UiGroup {
  id?: string
  title?: string
  description?: string
  items: UiItem[]
}

interface PluginSettingsDialogProps {
  pluginId: string | null
  pluginName: string
  open: boolean
  onOpenChange: (open: boolean) => void
}

export function PluginSettingsDialog({ pluginId, pluginName, open, onOpenChange }: PluginSettingsDialogProps) {
  const [values, setValues] = useState<Record<string, unknown>>({})
  const [saveNotice, setSaveNotice] = useState<{ ok: boolean; text: string } | null>(null)
  const [saving, setSaving] = useState(false)
  const [actionState, setActionState] = useState<Record<string, { pending: boolean; ok: boolean; text: string }>>({})

  const uiQuery = useQuery({
    queryKey: ["plugin-ui", pluginId],
    queryFn: () => api.pluginUi(pluginId!),
    enabled: open && !!pluginId,
  })

  const groups: UiGroup[] = Array.isArray(uiQuery.data?.groups) ? uiQuery.data.groups : []

  const effectiveValues = { ...(uiQuery.data?.values || {}), ...values }

  const setValue = (id: string, value: unknown) => {
    setValues((prev) => ({ ...prev, [id]: value }))
    setSaveNotice(null)
  }

  const handleSave = async () => {
    if (!pluginId) return
    setSaving(true)
    setSaveNotice(null)
    try {
      await api.savePluginUi(pluginId, effectiveValues)
      setSaveNotice({ ok: true, text: "设置已保存。" })
      await uiQuery.refetch()
    } catch (error) {
      setSaveNotice({ ok: false, text: apiErrorMessage(error, "保存失败，请稍后重试。") })
    } finally {
      setSaving(false)
    }
  }

  const runAction = async (item: UiItem) => {
    if (!pluginId || !item.action) return
    setActionState((prev) => ({ ...prev, [item.action!]: { pending: true, ok: true, text: "执行中…" } }))
    try {
      const result = await api.runPluginUiAction(pluginId, item.action!)
      setActionState((prev) => ({
        ...prev,
        [item.action!]: {
          pending: false,
          ok: result?.ok !== false,
          text: result?.message || "已完成",
        },
      }))
    } catch (error) {
      setActionState((prev) => ({
        ...prev,
        [item.action!]: { pending: false, ok: false, text: apiErrorMessage(error, "动作执行失败。") },
      }))
    }
  }

  const renderItem = (item: UiItem) => {
    const itemId = item.id || item.action || item.label
    if (item.type === "button") {
      const state = actionState[item.action || ""]
      return (
        <div key={itemId} className="space-y-1.5">
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={state?.pending}
            onClick={() => { void runAction(item) }}
          >
            {state?.pending && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
            {item.label || item.action}
          </Button>
          {state?.text && (
            <p className={`text-xs break-all ${state.ok ? "text-slate-500" : "text-rose-600"}`}>{state.text}</p>
          )}
        </div>
      )
    }
    if (item.type === "hint") {
      return <p key={itemId} className="text-xs leading-relaxed text-slate-500">{item.help || item.label}</p>
    }
    const value = effectiveValues[item.id]
    return (
      <div key={itemId} className="space-y-1.5">
        <Label className="text-xs font-medium text-slate-600">{item.label}</Label>
        {item.type === "toggle" ? (
          <Switch checked={!!value} onCheckedChange={(checked) => setValue(item.id, checked)} />
        ) : item.type === "select" ? (
          <Select value={String(value ?? "")} onValueChange={(next) => setValue(item.id, next)}>
            <SelectTrigger className="h-8 w-full text-sm"><SelectValue placeholder="请选择" /></SelectTrigger>
            <SelectContent>
              {(item.choices || []).map((choice) => (
                <SelectItem key={choice} value={choice}>{choice}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        ) : item.type === "color" ? (
          <div className="flex items-center gap-2">
            <input
              type="color"
              value={typeof value === "string" && /^#[0-9a-fA-F]{6}$/.test(value) ? value : "#000000"}
              onChange={(e) => setValue(item.id, e.target.value)}
              className="h-8 w-10 cursor-pointer rounded border border-slate-200 bg-transparent p-0.5"
              aria-label={item.label}
            />
            <Input
              value={String(value ?? "")}
              placeholder="#RRGGBB"
              className="h-8 w-32 font-mono text-xs"
              onChange={(e) => setValue(item.id, e.target.value)}
            />
          </div>
        ) : (
          <Input
            type={item.type === "number" ? "number" : "text"}
            value={value === undefined || value === null ? "" : String(value)}
            placeholder={item.placeholder}
            className="h-8 text-sm"
            onChange={(e) => {
              if (item.type === "number") {
                const parsed = Number(e.target.value)
                setValue(item.id, e.target.value === "" || Number.isNaN(parsed) ? e.target.value : parsed)
              } else {
                setValue(item.id, e.target.value)
              }
            }}
          />
        )}
        {item.help && <p className="text-xs text-slate-400">{item.help}</p>}
      </div>
    )
  }

  const hasSettings = groups.some((group) => (group.items || []).length > 0)

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) {
          setValues({})
          setSaveNotice(null)
          setActionState({})
        }
        onOpenChange(next)
      }}
    >
      <DialogContent className="max-h-[80vh] max-w-xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Settings2 className="h-4 w-4" /> 源设置 · {pluginName}
          </DialogTitle>
          <DialogDescription>
            书源插件在 metadata 中声明的配置项与动作；设置由插件运行时读取。
          </DialogDescription>
        </DialogHeader>

        {uiQuery.isLoading ? (
          <div className="flex min-h-[120px] items-center justify-center">
            <Loader2 className="h-5 w-5 animate-spin text-slate-400" />
          </div>
        ) : uiQuery.error ? (
          <Alert variant="destructive">
            <AlertDescription>{apiErrorMessage(uiQuery.error, "源设置加载失败。")}</AlertDescription>
          </Alert>
        ) : !hasSettings ? (
          <p className="py-6 text-center text-sm text-slate-500">该书源未声明可配置项。</p>
        ) : (
          <div className="space-y-5">
            {groups.map((group, index) => (
              <section key={group.id || group.title || index} className="space-y-3">
                {group.title && <h3 className="text-sm font-semibold text-slate-900">{group.title}</h3>}
                {group.description && <p className="text-xs text-slate-500">{group.description}</p>}
                <div className="space-y-3">{(group.items || []).map(renderItem)}</div>
              </section>
            ))}
          </div>
        )}

        {hasSettings && (
          <div className="flex items-center justify-between gap-3 border-t pt-3">
            <span className={`text-xs ${saveNotice?.ok === false ? "text-rose-600" : "text-emerald-600"}`}>
              {saveNotice?.text || ""}
            </span>
            <Button type="button" size="sm" onClick={() => { void handleSave() }} disabled={saving || !pluginId}>
              {saving ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Save className="mr-2 h-4 w-4" />}
              保存设置
            </Button>
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}
