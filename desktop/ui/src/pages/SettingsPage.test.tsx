import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import { SettingsPage } from "./SettingsPage";

vi.mock("../components/LibraryLocationSettings", () => ({ LibraryLocationSettings: () => <section><h2>资料库位置</h2></section> }));
describe("settings workspace", () => {
  it("keeps location and MCP controls visible, with JSON and source diagnostics collapsed and secrets redacted", async () => {
    vi.spyOn(api, "settings").mockResolvedValue({ search: { api_token: "do-not-show", nested: { password: "also-secret" } }, sources: [{ alias: "source-one", adapter: "manifest", read_only: true }] });
    vi.spyOn(api, "mcpConfig").mockResolvedValue({ server_name: "research-assets", config: { mcpServers: { hub: { command: "python", args: ["-m", "hub"] } } }, tools: [], read_only: true, data_dir: "D:/library", bundled_runtime: true, module_available: true });
    const check = vi.spyOn(api, "mcpCheck").mockResolvedValue({ status: "ready", tool_count: 7, tools: [], read_only: true, database: "ready", verification: "local", note: "仅本机模块与数据库读取通过；完整协议需客户端验证。" });
    render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><SettingsPage /></QueryClientProvider>);
    expect(screen.getByRole("heading", { name: "资料库位置" })).toBeVisible();
    expect(await screen.findByRole("button", { name: "复制配置" })).toBeVisible();
    expect(document.querySelector(".mcp-config-details")).not.toHaveAttribute("open");
    expect(document.querySelector(".settings-diagnostics")).not.toHaveAttribute("open");
    expect(await screen.findByText("source-one")).not.toBeVisible();
    fireEvent.click(screen.getByText("技术诊断与来源登记（只读）"));
    expect(screen.getByText("source-one")).toBeVisible();
    expect(screen.queryByText("do-not-show")).not.toBeInTheDocument();
    expect(screen.queryByText(/also-secret/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "检查连接" }));
    expect(await screen.findByRole("status")).toHaveTextContent("完整协议需客户端验证");
    expect(check).toHaveBeenCalledTimes(1);
  });
});
