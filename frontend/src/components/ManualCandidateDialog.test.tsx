import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { render, screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, describe, expect, it, vi } from "vitest"
import { api } from "@/lib/api"
import { ManualCandidateDialog } from "./ManualCandidateDialog"

// jsdom 不提供 ResizeObserver，Radix ScrollArea 需要它。
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
;(window as any).ResizeObserver = (window as any).ResizeObserver || ResizeObserverStub

vi.mock("@/lib/api", () => ({
  apiErrorMessage: (error: any, fallback: string) => error?.message || fallback,
  api: {
    scanManualChapterCandidates: vi.fn(),
    applyManualChapterCandidate: vi.fn(),
  },
}))

const chapter = { chapterId: "ch-2", chapterIndex: 2, title: "第2章 帮" }

const scanPayload = {
  targetTitle: "第2章 帮",
  chapterIndex: 2,
  officialPreviewLength: 97,
  officialWordCount: 2145,
  sourceErrors: [{ sourceId: "browser_src", sourceName: "浏览器源", error: "超时" }],
  items: [
    {
      sourceId: "short_src",
      sourceName: "偏短源",
      sourceChapterId: "short-ch2",
      candidateTitle: "帮",
      officialWordCount: 2145,
      contentLength: 2042,
      classification: "preview",
      wordCount: { passed: false, actual: 2042, expected: 2145, ratio: 0.952 },
      alignment: {
        passed: true, titleSimilarity: 1, previewSimilarity: 0.99,
        headPreviewSimilarity: 0.98, reason: "",
      },
      autoAcceptable: false,
      head: "开头预览".padEnd(120, "甲"),
      tail: "结尾预览".padEnd(120, "乙"),
      error: "",
    },
    {
      sourceId: "good_src",
      sourceName: "合格源",
      sourceChapterId: "good-ch2",
      candidateTitle: "帮",
      officialWordCount: 2145,
      contentLength: 2100,
      classification: "full",
      wordCount: { passed: true, actual: 2100, expected: 2145, ratio: 0.979 },
      alignment: {
        passed: true, titleSimilarity: 1, previewSimilarity: 0.99,
        headPreviewSimilarity: 0.99, reason: "",
      },
      autoAcceptable: true,
      head: "合格正文开头",
      tail: "",
      error: "",
    },
  ],
}

function renderDialog(props: Partial<Parameters<typeof ManualCandidateDialog>[0]> = {}) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const onApplied = vi.fn()
  const onOpenChange = vi.fn()
  const view = render(
    <QueryClientProvider client={queryClient}>
      <ManualCandidateDialog
        bookId="book-1"
        chapter={chapter}
        open={true}
        onOpenChange={onOpenChange}
        onApplied={onApplied}
        {...props}
      />
    </QueryClientProvider>,
  )
  return { ...view, onApplied, onOpenChange }
}

describe("ManualCandidateDialog", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(api.scanManualChapterCandidates as any).mockResolvedValue({ ok: true, scan: scanPayload })
    ;(api.applyManualChapterCandidate as any).mockResolvedValue({ ok: true })
  })

  it("shows loading then candidate cards with gate diagnostics and previews", async () => {
    renderDialog()

    expect(screen.getByTestId("manual-scan-loading")).toBeInTheDocument()
    expect(api.scanManualChapterCandidates).toHaveBeenCalledWith("book-1", "ch-2")

    await waitFor(() => {
      expect(screen.getAllByTestId("manual-candidate-item")).toHaveLength(2)
    })

    // Rejected source: visible rejection reasons and head/tail previews
    const cards = screen.getAllByTestId("manual-candidate-item")
    expect(cards[0]).toHaveTextContent("偏短源")
    expect(cards[0]).toHaveTextContent(/自动分类为/)
    expect(cards[0]).toHaveTextContent(/字数未过门/)
    expect(cards[0]).toHaveTextContent("开头：")
    expect(cards[0]).toHaveTextContent("结尾：")
    expect(cards[0]).not.toHaveTextContent("自动门可通过")

    // Acceptable source: badge present, no rejection alert
    expect(cards[1]).toHaveTextContent("合格源")
    expect(cards[1]).toHaveTextContent("自动门可通过")
    expect(within(cards[1]).queryByTestId("manual-candidate-reject")).toBeNull()

    // Source-level TOC errors surfaced separately
    expect(screen.getByText(/浏览器源：超时/)).toBeInTheDocument()
  })

  it("requires a second click to confirm and calls apply with the chosen ids", async () => {
    const user = userEvent.setup()
    const { onApplied, onOpenChange } = renderDialog()

    await waitFor(() => {
      expect(screen.getAllByTestId("manual-candidate-item")).toHaveLength(2)
    })

    const applyButtons = screen.getAllByTestId("manual-candidate-apply")
    await user.click(applyButtons[0])
    expect(api.applyManualChapterCandidate).not.toHaveBeenCalled()
    expect(applyButtons[0]).toHaveTextContent("再次点击确认")

    await user.click(applyButtons[0])
    await waitFor(() => {
      expect(api.applyManualChapterCandidate).toHaveBeenCalledTimes(1)
    })
    expect(api.applyManualChapterCandidate).toHaveBeenCalledWith(
      "book-1",
      "ch-2",
      { sourceId: "short_src", sourceChapterId: "short-ch2" },
    )
    expect(onApplied).toHaveBeenCalled()
    expect(onOpenChange).toHaveBeenCalledWith(false)
  })

  it("surfaces apply failure returned by the server", async () => {
    const user = userEvent.setup()
    ;(api.applyManualChapterCandidate as any).mockResolvedValue({
      ok: false,
      error: "manual_target_not_matched",
    })
    renderDialog()

    await waitFor(() => {
      expect(screen.getAllByTestId("manual-candidate-item")).toHaveLength(2)
    })
    const applyButtons = screen.getAllByTestId("manual-candidate-apply")
    await user.click(applyButtons[1])
    await user.click(applyButtons[1])

    await waitFor(() => {
      expect(screen.getByText(/manual_target_not_matched/)).toBeInTheDocument()
    })
  })
})
