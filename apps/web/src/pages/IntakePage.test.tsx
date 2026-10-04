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
