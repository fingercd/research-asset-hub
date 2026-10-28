import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { invoke } from "@tauri-apps/api/core";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LibraryLocationSettings } from "./LibraryLocationSettings";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
const nativeInvoke = vi.mocked(invoke);
const current = { path: "D:/HubData", locked_by_environment: false };
function renderSettings() {
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
      <LibraryLocationSettings />
    </QueryClientProvider>,
  );
}
function calls(command: string) {
  return nativeInvoke.mock.calls.filter((call) => call[0] === command);
}
async function chooseDirectory() {
  const button = screen.getByRole("button", { name: "选择资料库目录" });
  await waitFor(() => expect(button).toBeEnabled());
  fireEvent.click(button);
}
beforeEach(() => {
  nativeInvoke.mockImplementation(async (command) =>
    command === "get_data_directory" ? current : undefined,
  );
});
afterEach(() => {
  vi.restoreAllMocks();
  nativeInvoke.mockReset();
});

describe("library location settings", () => {
  it("reads the native path without selecting or switching a directory", async () => {
    renderSettings();
    expect(await screen.findByText(current.path)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "选择资料库目录" }),
    ).toBeEnabled();
    expect(calls("get_data_directory")).toHaveLength(1);
    expect(calls("choose_data_directory")).toHaveLength(0);
    expect(calls("switch_data_directory")).toHaveLength(0);
  });
  it("requires explicit switch-and-restart confirmation for an empty directory", async () => {
    nativeInvoke.mockImplementation(async (command) =>
      command === "get_data_directory"
        ? current
        : command === "choose_data_directory"
          ? { path: "E:/NewLibrary", is_existing_library: false }
          : undefined,
    );
    renderSettings();
    await chooseDirectory();
    expect(
      await screen.findByText("将在此目录创建空资料库。"),
    ).toBeInTheDocument();
    expect(calls("switch_data_directory")).toHaveLength(0);
    expect(screen.getByText(/当前资料库仍保存在原位置/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "切换并重启" }));
    expect(
      await screen.findByText("已提交切换，正在重启应用…"),
    ).toBeInTheDocument();
    expect(nativeInvoke).toHaveBeenCalledWith("switch_data_directory", {
      path: "E:/NewLibrary",
    });
    expect(screen.getByText(current.path)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "正在切换并重启…" }),
    ).toBeDisabled();
  });
  it("explains when the selected directory contains an existing library", async () => {
    nativeInvoke.mockImplementation(async (command) =>
      command === "get_data_directory"
        ? current
        : command === "choose_data_directory"
          ? { path: "E:/ExistingLibrary", is_existing_library: true }
          : undefined,
    );
    renderSettings();
    await chooseDirectory();
    expect(
      await screen.findByText("将在此目录打开已有资料库。"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "取消切换" }));
    expect(screen.queryByRole("button", { name: "切换并重启" })).toBeNull();
    expect(calls("switch_data_directory")).toHaveLength(0);
  });
  it("keeps the current library when the native picker is cancelled", async () => {
    nativeInvoke.mockImplementation(async (command) =>
      command === "get_data_directory" ? current : null,
    );
    renderSettings();
    await chooseDirectory();
    await waitFor(() => expect(calls("choose_data_directory")).toHaveLength(1));
    expect(screen.queryByText("已选择目录")).toBeNull();
    expect(calls("switch_data_directory")).toHaveLength(0);
    expect(screen.getByText(current.path)).toBeInTheDocument();
  });
  it("disables the picker when the environment fixes the directory", async () => {
    nativeInvoke.mockResolvedValue({ ...current, locked_by_environment: true });
    renderSettings();
    expect(
      await screen.findByText(/当前位置由启动配置 HUB_DATA_DIR 固定/),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "选择资料库目录" }),
    ).toBeDisabled();
    expect(calls("choose_data_directory")).toHaveLength(0);
  });
  it("shows a readable native read error and allows a retry", async () => {
    nativeInvoke
      .mockRejectedValueOnce("资料库位置无法读取")
      .mockResolvedValue(current);
    renderSettings();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "资料库位置无法读取",
    );
    expect(
      screen.getByRole("button", { name: "选择资料库目录" }),
    ).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "重新读取位置" }));
    expect(await screen.findByText(current.path)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "选择资料库目录" }),
    ).toBeEnabled();
  });
  it("surfaces directory validation errors without offering a switch", async () => {
    nativeInvoke.mockImplementation(async (command) => {
      if (command === "get_data_directory") return current;
      throw "所选目录既不是空目录，也不是有效资料库";
    });
    renderSettings();
    await chooseDirectory();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "所选目录既不是空目录，也不是有效资料库",
    );
    expect(screen.queryByRole("button", { name: "切换并重启" })).toBeNull();
    expect(calls("switch_data_directory")).toHaveLength(0);
  });
  it("keeps the selected path after a failed switch and allows retry", async () => {
    let attempts = 0;
    nativeInvoke.mockImplementation(async (command) => {
      if (command === "get_data_directory") return current;
      if (command === "choose_data_directory")
        return { path: "E:/NewLibrary", is_existing_library: false };
      if (++attempts === 1) throw "资料库设置无法保存";
      return undefined;
    });
    renderSettings();
    await chooseDirectory();
    fireEvent.click(await screen.findByRole("button", { name: "切换并重启" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "切换未完成：资料库设置无法保存",
    );
    expect(screen.getByText("E:/NewLibrary")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "切换并重启" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "切换并重启" }));
    expect(
      await screen.findByText("已提交切换，正在重启应用…"),
    ).toBeInTheDocument();
    expect(calls("switch_data_directory")).toHaveLength(2);
  });
});
