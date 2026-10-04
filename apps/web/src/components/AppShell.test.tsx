import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Link, MemoryRouter, useLocation } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { api, ApiError } from "../api/client";
import { CompareProvider } from "../state/CompareContext";
import { AppShell } from "./AppShell";

function NavigationProbe() {
  const location = useLocation();
  return <div>
    <output aria-label="当前地址">{location.pathname}{location.search}</output>
    <Link to="/assets/paper-1">示例详情</Link>
    <Link to="/search?q=新的检索">新的检索</Link>
  </div>;
}

function renderShell(initialEntry = "/") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <CompareProvider>
          <AppShell>
            <div>页面内容</div>
            <NavigationProbe />
          </AppShell>
        </CompareProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("AppShell", () => {
  it("opens the global search with Ctrl+K and focuses the search box", async () => {
    vi.spyOn(api, "health").mockRejectedValue(new Error("offline"));
    renderShell();

    fireEvent.keyDown(window, { key: "k", ctrlKey: true });

    const dialog = screen.getByRole("dialog", { name: "快速搜索" });
    expect(dialog).toBeInTheDocument();
    expect(await screen.findByRole("searchbox", { name: "全局搜索关键词" })).toHaveFocus();
  });

  it("includes intake and collections and moves system tools into a panel", async () => {
    vi.spyOn(api, "health").mockRejectedValue(new Error("offline"));
    renderShell();

    const navigation = screen.getByRole("navigation", { name: "主导航" });
    expect(within(navigation).getAllByRole("link")).toHaveLength(5);
    expect(within(navigation).getByRole("link", { name: "资料库" })).toHaveAttribute(
      "href",
      "/",
    );
    expect(within(navigation).getByRole("link", { name: "领域" })).toHaveAttribute(
      "href",
      "/domains",
    );
    expect(within(navigation).getByRole("link", { name: "笔记" })).toHaveAttribute(
      "href",
      "/annotations",
    );
    expect(within(navigation).queryByRole("link", { name: /图谱/ })).toBeNull();

    fireEvent.click(await screen.findByRole("button", { name: /无法确认状态/ }));
    const panel = screen.getByRole("dialog", { name: "系统状态" });
    expect(within(panel).queryByRole("link", { name: /图谱/ })).toBeNull();
    expect(within(panel).getByRole("link", { name: /后台任务/ })).toHaveAttribute(
      "href",
      "/jobs",
    );
    expect(within(panel).getByRole("link", { name: /同步冲突/ })).toHaveAttribute(
      "href",
      "/conflicts",
    );
    expect(within(panel).getByRole("link", { name: /设置与 MCP/ })).toHaveAttribute(
      "href",
      "/settings",
    );
  });

  it("offers visible search, traps dialog focus, and restores its trigger", async () => {
    vi.spyOn(api, "health").mockRejectedValue(new Error("offline"));
    const user = userEvent.setup();
    renderShell();
    const trigger = screen.getByRole("button", { name: "搜索全部资料" });
    await user.click(trigger);
    const input = screen.getByRole("searchbox", { name: "全局搜索关键词" });
    expect(input).toHaveFocus();
    await user.tab({ shift: true });
    expect(within(screen.getByRole("dialog")).getByRole("button", { name: "笔记" })).toHaveFocus();
    await user.tab();
    expect(input).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("returns to the previous search with its filters and supports forward navigation", async () => {
    vi.spyOn(api, "health").mockRejectedValue(new Error("offline"));
    const user = userEvent.setup();
    renderShell("/search?q=VLN&domain=navigation&status=reading");
    const back = screen.getByRole("button", { name: "返回上一页" });
    const forward = screen.getByRole("button", { name: "前进到下一页" });
    expect(back).toBeDisabled();
    expect(forward).toBeDisabled();
    await user.click(screen.getByRole("link", { name: "示例详情" }));
    expect(back).toBeEnabled();
    await user.click(back);
    expect(screen.getByLabelText("当前地址")).toHaveTextContent("/search?q=VLN&domain=navigation&status=reading");
    expect(forward).toBeEnabled();
    await user.click(forward);
    expect(screen.getByLabelText("当前地址")).toHaveTextContent("/assets/paper-1");
    await user.click(back);
    await user.click(screen.getByRole("link", { name: "新的检索" }));
    expect(forward).toBeDisabled();
  });

  it("does not show a connected message offline and can recheck the services", async () => {
    const health = vi.spyOn(api, "health").mockRejectedValue(new Error("offline"));
    const user = userEvent.setup();
    renderShell();
    await user.click(await screen.findByRole("button", { name: /无法确认状态/ }));
    expect(screen.queryByText("本地资料库已连接")).not.toBeInTheDocument();
    const panel = screen.getByRole("dialog", { name: "系统状态" });
    expect(within(panel).getByRole("alert")).toHaveTextContent("offline");
    health.mockResolvedValue({
      service: "research-asset-hub", status: "healthy", checked_at: "2026-09-26T00:00:00Z",
      components: { database: { status: "healthy" }, meilisearch: { status: "healthy" } },
    });
    await user.click(within(panel).getByRole("button", { name: "重新检查" }));
    await waitFor(() => expect(within(panel).getByText("系统就绪")).toBeInTheDocument());
    expect(within(panel).getByText("数据库")).toBeInTheDocument();
    expect(within(panel).getByText("关键词搜索")).toBeInTheDocument();
    expect(within(panel).queryByRole("alert")).not.toBeInTheDocument();
  });

  it("reports a slow health check without claiming the API is offline", async () => {
    vi.spyOn(api, "health").mockRejectedValue(new ApiError({
      status: 0, title: "请求超时", detail: "本地 API 响应时间过长，请稍后重试。",
    }));
    renderShell();
    expect(await screen.findByRole("button", { name: "状态检查超时" })).toBeInTheDocument();
    expect(screen.queryByText("API 离线")).not.toBeInTheDocument();
  });
});
