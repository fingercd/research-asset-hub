import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import type { IntakeDraft } from "../api/types";
import { IntakePage } from "./IntakePage";
function renderIntake() {
  return render(
    <QueryClientProvider
      client={
        new QueryClient({
          defaultOptions: {
            queries: { retry: false },
            mutations: { retry: false },
          },
        })
      }
    >
      <MemoryRouter initialEntries={["/intake?domain=robotics"]}>
        <IntakePage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}
const draft: IntakeDraft = {
  id: "draft-1",
  domain: "robotics",
  title: "New paper",
  status: "ready",
  storage_mode: "managed",
};
beforeEach(() => {
  vi.spyOn(api, "domains").mockResolvedValue({
    items: [{ slug: "robotics", label: "机器人" }],
    next_cursor: null,
  });
  vi.spyOn(api, "intakeDrafts").mockResolvedValue({
    items: [],
    next_cursor: null,
  });
});
afterEach(() => vi.restoreAllMocks());
function upload(fileName = "New paper.pdf") {
  fireEvent.change(screen.getByLabelText(/选择一篇或多篇 PDF/), {
    target: {
      files: [new File(["%PDF-1.7"], fileName, { type: "application/pdf" })],
    },
  });
}
describe("manual PDF intake", () => {
  it("clears optional metadata after a preview instead of restoring the saved values", async () => {
    const saved: IntakeDraft = { ...draft, authors: ["Alice"], year: 2025,
      doi: "10.1234/old", arxiv_id: "2501.12345", html_url: "https://example.org/paper",
      tags: ["robot"], summary: "Saved summary", pdf_uploaded: true };
    vi.spyOn(api, "createIntakeDraft").mockResolvedValue(saved);
    vi.spyOn(api, "previewIntakeDraft").mockResolvedValue({ draft: saved, duplicates: [], warnings: [] });
    const update = vi.spyOn(api, "updateIntakeDraft").mockResolvedValue(saved);
    renderIntake();
    upload();
    const inputs: Array<[string, string]> = [
      ["作者（逗号分隔）", "Alice"], ["年份", "2025"], ["DOI（单篇）", saved.doi!],
      ["arXiv ID（单篇）", saved.arxiv_id!], ["HTML URL（单篇，选填）", saved.html_url!],
      ["标签（逗号分隔）", "robot"], ["简介", "Saved summary"],
    ];
    for (const [label, value] of inputs) fireEvent.change(screen.getByLabelText(label), { target: { value } });
    fireEvent.click(screen.getByRole("button", { name: "预览与重复检查" }));
    await screen.findByText(/未发现重复候选/);
    for (const [label] of inputs) fireEvent.change(screen.getByLabelText(label), { target: { value: "" } });
    expect(screen.queryByRole("button", { name: "确认入库" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "预览与重复检查" }));
    await waitFor(() => expect(update).toHaveBeenCalledWith(saved.id, {
      domain: "robotics", title: "New paper", authors: [], year: null, doi: null,
      arxiv_id: null, html_url: null, tags: [], summary: null,
    }));
  });
  it("restores every optional field from a historical draft and previews it unchanged", async () => {
    const saved: IntakeDraft = { ...draft, authors: ["Alice", "Bob"], year: 2025,
      doi: "10.1234/old", arxiv_id: "2501.12345", html_url: "https://example.org/paper",
      tags: ["robot", "navigation"], summary: "Saved summary", pdf_uploaded: true };
    vi.mocked(api.intakeDrafts).mockResolvedValue({ items: [saved], next_cursor: null });
    const update = vi.spyOn(api, "updateIntakeDraft").mockResolvedValue(saved);
    vi.spyOn(api, "previewIntakeDraft").mockResolvedValue({ draft: saved, duplicates: [], warnings: [] });
    renderIntake();
    fireEvent.click(await screen.findByRole("button", { name: /New paper.*继续编辑/ }));
    expect(screen.getByLabelText("作者（逗号分隔）")).toHaveValue("Alice, Bob");
    expect(screen.getByLabelText("年份")).toHaveValue(2025);
    expect(screen.getByLabelText("DOI（单篇）")).toHaveValue(saved.doi);
    expect(screen.getByLabelText("arXiv ID（单篇）")).toHaveValue(saved.arxiv_id);
    expect(screen.getByLabelText("HTML URL（单篇，选填）")).toHaveValue(saved.html_url);
    expect(screen.getByLabelText("标签（逗号分隔）")).toHaveValue("robot, navigation");
    expect(screen.getByLabelText("简介")).toHaveValue(saved.summary);
    fireEvent.click(screen.getByRole("button", { name: "预览与重复检查" }));
    await waitFor(() => expect(update).toHaveBeenCalledWith(saved.id, expect.objectContaining({
      authors: saved.authors, year: saved.year, doi: saved.doi, arxiv_id: saved.arxiv_id,
      html_url: saved.html_url, tags: saved.tags, summary: saved.summary,
    })));
  });
  it("keeps distinct historical metadata until a shared field is explicitly edited", async () => {
    const first = { ...draft, authors: ["Alice"], year: 2024, summary: "First summary" };
    const second = { ...draft, id: "draft-2", title: "Second paper", authors: ["Bob"], year: 2025, summary: "Second summary" };
    vi.mocked(api.intakeDrafts).mockResolvedValue({ items: [first, second], next_cursor: null });
    const update = vi.spyOn(api, "updateIntakeDraft").mockImplementation(async (id) => id === first.id ? first : second);
    vi.spyOn(api, "previewIntakeDraft").mockImplementation(async (id) => ({
      draft: id === first.id ? first : second, duplicates: [], warnings: [],
    }));
    renderIntake();
    fireEvent.click(await screen.findByRole("button", { name: /New paper.*继续编辑/ }));
    fireEvent.click(screen.getByRole("button", { name: /Second paper.*继续编辑/ }));
    fireEvent.click(screen.getByRole("button", { name: "全部预览" }));
    await waitFor(() => expect(update).toHaveBeenCalledTimes(2));
    expect(update).toHaveBeenNthCalledWith(1, first.id, expect.objectContaining({ authors: ["Alice"], year: 2024, summary: "First summary" }));
    expect(update).toHaveBeenNthCalledWith(2, second.id, expect.objectContaining({ authors: ["Bob"], year: 2025, summary: "Second summary" }));
    await waitFor(() => expect(screen.getAllByText(/未发现重复候选/)).toHaveLength(2));
    await waitFor(() => expect(screen.getByLabelText("作者（逗号分隔）")).toBeEnabled());
    fireEvent.change(screen.getByLabelText("作者（逗号分隔）"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "全部预览" }));
    await waitFor(() => expect(update).toHaveBeenCalledTimes(4));
    expect(update).toHaveBeenNthCalledWith(3, first.id, expect.objectContaining({ authors: [], year: 2024, summary: "First summary" }));
    expect(update).toHaveBeenNthCalledWith(4, second.id, expect.objectContaining({ authors: [], year: 2025, summary: "Second summary" }));
  });
  it("uploads a PDF, previews identity, commits only on explicit confirmation and keeps the paper if optional code fails", async () => {
    const create = vi.spyOn(api, "createIntakeDraft").mockResolvedValue(draft);
    const pdf = vi.spyOn(api, "uploadIntakePdf").mockResolvedValue(draft);
    vi.spyOn(api, "previewIntakeDraft").mockResolvedValue({
      draft,
      duplicates: [],
      warnings: [],
    });
    const commit = vi
      .spyOn(api, "commitIntakeDraft")
      .mockResolvedValue({
        asset_id: "paper-new",
        draft_id: draft.id,
        status: "committed",
      });
    vi.spyOn(api, "linkCode").mockRejectedValue(new Error("下载失败"));
    renderIntake();
    upload();
    fireEvent.change(screen.getByLabelText("代码仓库 URL（单篇）"), {
      target: { value: "https://github.com/example/project" },
    });
    fireEvent.click(screen.getByRole("button", { name: "预览与重复检查" }));
    await screen.findByText("未发现重复候选。", { exact: false });
    expect(create).toHaveBeenCalledWith(
      expect.objectContaining({
        domain: "robotics",
        title: "New paper",
        storage_mode: "managed",
      }),
    );
    expect(pdf).toHaveBeenCalledWith(draft.id, expect.any(File));
    expect(commit).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "确认入库" }));
    expect(
      await screen.findByRole("link", { name: "打开已入库论文" }),
    ).toHaveAttribute("href", "/assets/paper-new");
    expect(
      await screen.findByText(/论文已入库，代码关联失败：下载失败/),
    ).toBeInTheDocument();
  });
  it("requires an explicit duplicate decision before attaching to an existing paper", async () => {
    vi.spyOn(api, "createIntakeDraft").mockResolvedValue(draft);
    vi.spyOn(api, "uploadIntakePdf").mockResolvedValue(draft);
    vi.spyOn(api, "previewIntakeDraft").mockResolvedValue({
      draft,
      duplicates: [{ asset_id: "existing", title: "Existing paper" }],
      warnings: [],
    });
    const commit = vi
      .spyOn(api, "commitIntakeDraft")
      .mockResolvedValue({
        asset_id: "existing",
        draft_id: draft.id,
        status: "committed",
      });
    renderIntake();
    upload();
    fireEvent.click(screen.getByRole("button", { name: "预览与重复检查" }));
    await screen.findByLabelText("保存方式");
    expect(screen.getByRole("button", { name: "确认入库" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("保存方式"), {
      target: { value: "existing" },
    });
    fireEvent.click(screen.getByRole("button", { name: "确认入库" }));
    await waitFor(() =>
      expect(commit).toHaveBeenCalledWith(draft.id, {
        action: "attach",
        asset_id: "existing",
      }),
    );
  });
  it("does not commit when PDF upload fails and keeps the draft available for retry", async () => {
    vi.spyOn(api, "createIntakeDraft").mockResolvedValue(draft);
    vi.spyOn(api, "uploadIntakePdf").mockRejectedValue(
      new Error("PDF 文件损坏"),
    );
    const preview = vi.spyOn(api, "previewIntakeDraft");
    const commit = vi.spyOn(api, "commitIntakeDraft");
    renderIntake();
    upload();
    fireEvent.click(screen.getByRole("button", { name: "预览与重复检查" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("PDF 文件损坏");
    expect(preview).not.toHaveBeenCalled();
    expect(commit).not.toHaveBeenCalled();
    expect(
      screen.getByRole("button", { name: "预览与重复检查" }),
    ).toBeEnabled();
  });
});
