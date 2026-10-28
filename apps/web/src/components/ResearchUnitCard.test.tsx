import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import type { ResearchUnitSummary } from "../api/types";
import { CompareProvider } from "../state/CompareContext";
import { ResearchUnitCard } from "./ResearchUnitCard";

function renderCard(unit: ResearchUnitSummary, compact = false) {
  window.localStorage.clear();
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <CompareProvider>
          <ResearchUnitCard unit={unit} compact={compact} />
        </CompareProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("ResearchUnitCard", () => {
  it.each([
    ["paper", "命中论文"],
    ["related_repository", "命中关联代码"],
    ["paper_and_related_repository", "命中论文与关联代码"],
    ["repository", "命中代码仓库"],
  ])("explains the %s match without converting rank scores to a percentage", (source, label) => {
    renderCard({
      unit_id: "search-unit",
      unit_kind: "paper",
      primary_asset: {
        id: "search-paper",
        asset_type: "paper",
        title: "Search result",
      },
      related_repositories: [],
      matched_assets: [],
      covered_asset_count: 1,
      quick_actions: [],
      score: 1 / 61,
      match_source: source,
      snippet: "命中标题中的 StreamVLN。",
    });
    expect(screen.getByText(label)).toBeInTheDocument();
    expect(screen.getByText("命中标题中的 StreamVLN。")).toBeInTheDocument();
    expect(screen.queryByText(/% 匹配/)).not.toBeInTheDocument();
    expect(screen.queryByText(/排序分数/)).not.toBeInTheDocument();
  });

  it("labels unexplained scores as ranking scores", () => {
    renderCard({
      unit_id: "search-unit",
      unit_kind: "paper",
      primary_asset: {
        id: "search-paper",
        asset_type: "paper",
        title: "Search result",
      },
      related_repositories: [],
      matched_assets: [],
      covered_asset_count: 1,
      quick_actions: [],
      score: 1 / 61,
    });
    expect(screen.getByText("排序分数 0.0164")).toHaveAttribute(
      "title",
      "用于结果排序，不表示匹配概率。",
    );
    expect(screen.queryByText(/% 匹配/)).not.toBeInTheDocument();
  });

  it("does not present browsing as a search match", () => {
    renderCard({
      unit_id: "browse-unit",
      unit_kind: "paper",
      primary_asset: {
        id: "browse-paper",
        asset_type: "paper",
        title: "Browse result",
      },
      related_repositories: [],
      matched_assets: [],
      covered_asset_count: 1,
      quick_actions: [],
      score: 1,
      match_source: "browse",
    });
    expect(document.querySelector(".score")).toBeNull();
  });

  it("keeps arXiv venue at the original size", () => {
    renderCard({
      unit_id: "paper:arxiv-1",
      unit_kind: "paper",
      primary_asset: {
        id: "arxiv-1",
        asset_type: "paper",
        title: "Preprint",
        venue: "arXiv 2026",
      },
      related_repositories: [],
      matched_assets: [],
      covered_asset_count: 1,
      quick_actions: [],
    });
    expect(screen.getByText("arXiv 2026")).not.toHaveClass("research-unit-venue");
  });

  it("uses quick-action status and binds personal state to the primary paper", async () => {
    const open = vi
      .spyOn(api, "openRepresentation")
      .mockResolvedValue({ url: "/cached/paper.html" });
    const openWindow = vi
      .spyOn(window, "open")
      .mockImplementation(() => null);

    renderCard({
      unit_id: "paper:paper-1",
      unit_kind: "paper",
      primary_asset: {
        id: "paper-1",
        asset_type: "paper",
        title: "Primary Paper",
        venue: "ICML",
        year: 2026,
        reading_status: "reading",
        annotation_summary: "主论文批注",
      },
      related_repositories: [
        {
          id: "repo-1",
          asset_type: "repository",
          title: "Companion Code",
          annotation_summary: "仓库批注不应显示",
        },
      ],
      matched_assets: [
        { id: "repo-1", asset_type: "repository", title: "Companion Code" },
      ],
      covered_asset_count: 2,
      score: 0.91,
      snippet: "命中仓库 README 中的训练说明。",
      match_source: "related_repository",
      quick_actions: [
        {
          kind: "pdf",
          status: "missing",
          owner_asset_id: "paper-1",
          reason: "本地 PDF 路径不存在。",
          alternatives: [],
        },
        {
          kind: "html",
          status: "available",
          representation_id: "html-1",
          owner_asset_id: "paper-1",
          alternatives: [],
        },
        {
          kind: "code",
          status: "network_required",
          representation_id: "code-remote",
          owner_asset_id: "repo-1",
          alternatives: [],
        },
      ],
    });

    expect(screen.getByRole("heading", { name: "Primary Paper" })).toBeInTheDocument();
    expect(screen.getByText("ICML 2026")).toHaveClass("research-unit-venue");
    expect(screen.getByText("命中关联代码")).toBeInTheDocument();
    expect(screen.queryByText(/% 匹配/)).not.toBeInTheDocument();
    expect(screen.getByText("主论文批注")).toBeInTheDocument();
    expect(screen.queryByText("仓库批注不应显示")).not.toBeInTheDocument();
    expect(screen.getByLabelText("阅读状态")).toHaveValue("reading");
    expect(screen.getByText("关联 1 个代码仓库")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Companion Code" })).toBeNull();
    expect(document.querySelector(".card-more-menu")).not.toHaveAttribute("open");

    fireEvent.click(screen.getByRole("button", { name: "更多" }));
    expect(screen.getByRole("button", { name: "复制ID" })).toBeInTheDocument();

    const actionGroup = screen.getByLabelText("快速打开");
    const actionButtons = within(actionGroup).getAllByRole("button");
    expect(actionButtons.map((button) => button.getAttribute("aria-label"))).toEqual([
      "PDF缺失",
      "HTML",
      "代码（需联网）",
    ]);
    expect(actionButtons[0]).toBeDisabled();
    expect(actionButtons[0]).toHaveAttribute(
      "title",
      expect.stringContaining("本地 PDF 路径不存在"),
    );
    expect(actionButtons[1]).toBeEnabled();
    expect(actionButtons[2]).toBeEnabled();
    expect(actionButtons[2]).toHaveClass("requires-network");
    expect(actionButtons[2]).toHaveTextContent("需联网");
    expect(actionButtons[2]).toHaveAttribute(
      "title",
      expect.stringContaining("需要联网"),
    );

    fireEvent.click(actionButtons[0]!);
    expect(open).not.toHaveBeenCalled();
    fireEvent.click(actionButtons[1]!);
    await waitFor(() => expect(open).toHaveBeenCalledWith("html-1"));
    expect(await screen.findByRole("button", { name: "HTML" })).toBeInTheDocument();
    await waitFor(() =>
      expect(openWindow).toHaveBeenCalledWith(
        "/cached/paper.html",
        "_blank",
        "noopener,noreferrer",
      ),
    );
    fireEvent.click(actionButtons[2]!);
    expect(screen.getByRole("dialog", { name: "代码打开方式" })).toBeInTheDocument();
    expect(open).not.toHaveBeenCalledWith("code-remote");

    fireEvent.click(screen.getByRole("button", { name: "加入对比" }));
    expect(JSON.parse(window.localStorage.getItem("rah.compare-assets") ?? "[]")).toEqual([
      expect.objectContaining({ id: "paper-1", title: "Primary Paper" }),
    ]);
  });

  it("keeps a one-click default and exposes every representation in a choice menu", async () => {
    const open = vi
      .spyOn(api, "openRepresentation")
      .mockResolvedValue({ url: "/opened" });
    vi.spyOn(window, "open").mockImplementation(() => null);

    renderCard({
      unit_id: "paper:multi-entry",
      unit_kind: "paper",
      primary_asset: {
        id: "multi-entry",
        asset_type: "paper",
        title: "Multiple PDF Sources",
      },
      related_repositories: [],
      matched_assets: [],
      covered_asset_count: 1,
      match_source: "paper",
      quick_actions: [
        {
          kind: "pdf",
          status: "available",
          representation_id: "pdf-primary",
          owner_asset_id: "multi-entry",
          alternatives: [
            {
              representation_id: "pdf-mirror",
              owner_asset_id: "multi-entry",
              label: "本地镜像",
              role: "cached",
              status: "available",
              relation_type: null,
            },
            {
              representation_id: "pdf-remote",
              owner_asset_id: "multi-entry",
              label: "在线源",
              role: "original",
              status: "network_required",
              relation_type: null,
            },
          ],
        },
        {
          kind: "html",
          status: "available",
          representation_id: "html-only",
          owner_asset_id: "multi-entry",
          alternatives: [],
        },
        {
          kind: "code",
          status: "not_provided",
          owner_asset_id: null,
          alternatives: [],
        },
      ],
    });

    fireEvent.click(screen.getByRole("button", { name: "更多" }));
    const defaultPdf = screen.getByRole("button", { name: "PDF" });
    expect(defaultPdf).toHaveTextContent("+2");
    fireEvent.click(defaultPdf);
    await waitFor(() => expect(open).toHaveBeenLastCalledWith("pdf-primary"));

    expect(
      screen.queryByLabelText("选择HTML入口，共 1 个"),
    ).not.toBeInTheDocument();
    const pdfSelector = screen.getByLabelText("选择PDF入口，共 3 个");
    fireEvent.click(pdfSelector);

    const menu = screen.getByRole("group", { name: "PDF入口" });
    const defaultChoice = within(menu).getByRole("button", {
      name: "PDF 默认入口",
    });
    const mirrorChoice = within(menu).getByRole("button", {
      name: "本地镜像",
    });
    const remoteChoice = within(menu).getByRole("button", {
      name: "在线源（需联网）",
    });
    expect(remoteChoice).toHaveTextContent("需联网");

    fireEvent.click(defaultChoice);
    expect(pdfSelector).toHaveFocus();
    await waitFor(() => expect(open).toHaveBeenLastCalledWith("pdf-primary"));

    fireEvent.click(pdfSelector);
    fireEvent.click(mirrorChoice);
    await waitFor(() => expect(open).toHaveBeenLastCalledWith("pdf-mirror"));

    fireEvent.click(pdfSelector);
    fireEvent.click(remoteChoice);
    await waitFor(() => expect(open).toHaveBeenLastCalledWith("pdf-remote"));
  });

  it("greys out code when the paper does not provide an open-source repository", () => {
    renderCard({
      unit_id: "paper:no-code",
      unit_kind: "paper",
      primary_asset: {
        id: "paper-no-code",
        asset_type: "paper",
        title: "Paper Without Code",
      },
      related_repositories: [],
      matched_assets: [],
      covered_asset_count: 1,
      match_source: "paper",
      quick_actions: [
        {
          kind: "code",
          status: "not_provided",
          owner_asset_id: "paper-no-code",
          reason: "没有已确认且可用的代码仓库。",
          alternatives: [],
        },
      ],
    });

    fireEvent.click(screen.getByRole("button", { name: "更多" }));
    const codeButton = screen.getByRole("button", { name: "代码" });
    expect(codeButton).toBeDisabled();
    expect(codeButton).toHaveClass("is-unavailable");
    expect(codeButton).toHaveAttribute(
      "title",
      expect.stringContaining("没有已确认且可用的代码仓库"),
    );
  });

  it("shows only code and linked-paper information for an independent repository", async () => {
    renderCard({
      unit_id: "repository:repo-standalone",
      unit_kind: "repository",
      primary_asset: {
        id: "repo-standalone",
        asset_type: "repository",
        title: "Standalone Toolkit",
      },
      related_repositories: [],
      matched_assets: [
        {
          id: "paper-linked",
          asset_type: "paper",
          title: "Linked Research Paper",
        },
      ],
      covered_asset_count: 1,
      match_source: "repository",
      quick_actions: [
        {
          kind: "pdf",
          status: "not_provided",
          owner_asset_id: "repo-standalone",
          alternatives: [],
        },
        {
          kind: "html",
          status: "not_provided",
          owner_asset_id: "repo-standalone",
          alternatives: [],
        },
        {
          kind: "code",
          status: "available",
          owner_asset_id: "repo-standalone",
          alternatives: [],
        },
      ],
    });

    expect(screen.queryByRole("button", { name: "加入对比" })).toBeNull();
    expect(screen.getByText("关联 1 篇论文")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Linked Research Paper" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "更多" }));
    const actionGroup = screen.getByLabelText("快速打开");
    const codeButton = within(actionGroup).getByRole("button", { name: "代码" });
    fireEvent.click(codeButton);
    expect(await screen.findByText("该仓库未提供源码")).toBeInTheDocument();
    expect(within(actionGroup).queryByRole("button", { name: /PDF/ })).toBeNull();
    expect(within(actionGroup).queryByRole("button", { name: "HTML" })).toBeNull();
    expect(screen.queryByRole("button", { name: "加入对比" })).toBeNull();
  });
  it("keeps long titles, reading status, and exact quick actions visible in compact mode", () => {
    renderCard({
      unit_id: "paper:compact", unit_kind: "paper", primary_asset: {
        id: "paper-compact", asset_type: "paper", title: "A very long original research title with identifying details", title_zh: "保留完整中文题名及能够区分论文的细节", year: 2025, venue: "CVPR", reading_status: "reading",
      }, related_repositories: [], matched_assets: [], covered_asset_count: 1,
      quick_actions: [{kind: "pdf", status: "available", owner_asset_id: "paper-compact", representation_id: "pdf-compact"}, {kind: "html", status: "missing", owner_asset_id: "paper-compact"}],
    }, true);
    expect(screen.getByRole("heading", {name: "保留完整中文题名及能够区分论文的细节"})).toBeInTheDocument();
    expect(screen.getByText("A very long original research title with identifying details")).toBeInTheDocument();
    expect(screen.getByText("CVPR 2025")).toBeInTheDocument();
    expect(screen.getByText("阅读中", {selector: "span"})).toBeVisible();
    const actions = screen.getByLabelText("快速打开");
    expect(actions.closest("details")).toBeNull();
    expect(within(actions).getByRole("button", {name: "PDF"})).not.toBeDisabled();
    expect(within(actions).getByRole("button", {name: "HTML"})).toBeDisabled();
    expect(screen.getByText("摘要与作者").closest("details")).not.toHaveAttribute("open");
    expect(screen.getByText("暂无摘要。")).not.toBeVisible();
  });

});
