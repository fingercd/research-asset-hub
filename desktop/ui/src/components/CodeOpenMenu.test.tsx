import {
  QueryClient,
  QueryClientProvider,
} from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import type { QuickAction, RepresentationOpenAction } from "../api/types";
import { CodeOpenMenu } from "./CodeOpenMenu";

const remoteAction: QuickAction = {
  kind: "code",
  status: "network_required",
  representation_id: "code-remote",
  owner_asset_id: "repo-1",
  alternatives: [],
};

afterEach(() => {
  vi.restoreAllMocks();
});

function renderMenu(
  action: QuickAction = remoteAction,
  onClose: () => void = () => {},
) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <CodeOpenMenu
        repositoryLabel="OpenRobotLab/StreamVLN"
        action={action}
        onClose={onClose}
      />
    </QueryClientProvider>,
  );
}

describe("CodeOpenMenu", () => {
  it("opens the local project in VS Code while keeping the browser view in-app", async () => {
    vi.spyOn(api, "openRepresentation").mockResolvedValue({
      kind: "external_url",
      url: "https://github.com/OpenRobotLab/StreamVLN",
      editor: {
        available: true,
        editor: "vscode",
        path: "D:/repos/streamvln",
        uri: "vscode://file/D:/repos/streamvln",
      },
    } satisfies RepresentationOpenAction);
    vi.spyOn(api, "codeTree").mockResolvedValue({
      representation_id: "code-remote",
      repository_name: "StreamVLN",
      path: "",
      entries: [],
    });
    const openWindow = vi.spyOn(window, "open").mockImplementation(() => null);

    renderMenu({
      ...remoteAction,
      status: "available",
    });
    expect(await screen.findByRole("dialog", { name: "代码打开方式" })).toBeInTheDocument();
    expect((await screen.findAllByText("StreamVLN")).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: "用 VS Code 打开" }));
    await waitFor(() =>
      expect(openWindow).toHaveBeenCalledWith("vscode://file/D:/repos/streamvln", "_self"),
    );
  });

  it("does not offer an external browser fallback for a remote-only code entry", async () => {
    const open = vi.spyOn(api, "openRepresentation");

    renderMenu();
    expect(await screen.findByText("本地代码不可用")).toBeInTheDocument();
    expect(screen.getByText(/源码尚未本地化/)).toBeInTheDocument();
    expect(open).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "在浏览器打开" })).not.toBeInTheDocument();
  });

  it("switches between multiple code representations", async () => {
    const open = vi.spyOn(api, "openRepresentation").mockResolvedValue({
      kind: "external_url",
      url: "https://github.com/example/repo",
      editor: {
        available: false,
        editor: "vscode",
        reason: "源码未下载/未开源",
      },
    } satisfies RepresentationOpenAction);
    vi.spyOn(api, "codeTree").mockResolvedValue({
      representation_id: "code-default",
      repository_name: "example",
      path: "",
      entries: [],
    });
    renderMenu({
      kind: "code",
      status: "available",
      representation_id: "code-default",
      owner_asset_id: "repo-1",
      alternatives: [
        {
          representation_id: "code-mirror",
          owner_asset_id: "repo-1",
          label: "本地镜像",
          status: "available",
        },
      ],
    });
    await waitFor(() => expect(open).toHaveBeenCalledWith("code-default"));
    fireEvent.click(screen.getByRole("button", { name: "选择代码入口：本地镜像" }));
    await waitFor(() => expect(open).toHaveBeenLastCalledWith("code-mirror"));
  });

  it("closes on Escape and shows an empty state without a representation", async () => {
    const onClose = vi.fn();
    renderMenu(
      {
        kind: "code",
        status: "not_provided",
        owner_asset_id: null,
        alternatives: [],
      },
      onClose,
    );
    expect(screen.getByText("该仓库未提供源码")).toBeInTheDocument();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
