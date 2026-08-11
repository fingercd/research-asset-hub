import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import { CodeGraphPage } from "./CodeGraphPage";

const codeGraphResponse = {
  total_count: 2,
  indexed_count: 1,
  hidden_count: 1,
  domains: [
    {
      slug: "agent",
      label: "Agent 学习",
      repository_count: 1,
      indexed_count: 1,
      repositories: [
        {
          id: "repo-1",
          title: "OpenHands",
          paths: ["D:/code/openhands"],
          gitnexus_indexed: true,
          gitnexus_repo_name: "openhands",
        },
      ],
      hidden_repositories: [
        {
          id: "repo-hidden",
          title: "OpenBMB/BMTools",
          paths: ["D:/code/bmtools"],
          gitnexus_indexed: true,
          gitnexus_repo_name: "BMTools",
          hidden: true,
        },
      ],
      hidden_count: 1,
    },
    {
      slug: "infra",
      label: "AI Infra",
      repository_count: 1,
      indexed_count: 0,
      repositories: [
        {
          id: "repo-2",
          title: "TVM",
          paths: ["D:/code/tvm"],
          gitnexus_indexed: false,
        },
      ],
    },
  ],
};

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const result = render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <CodeGraphPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...result, client };
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("CodeGraphPage", () => {
  function installMocks(options?: { online?: boolean }) {
    vi.spyOn(api, "codeGraph").mockResolvedValue(codeGraphResponse);
    vi.spyOn(api, "settings").mockResolvedValue({
      app: { gitnexus_url: "http://127.0.0.1:4747" },
    });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(() =>
        options?.online === false
          ? Promise.reject(new Error("offline"))
          : Promise.resolve({ ok: true } as Response),
      ),
    );
  }

  it("groups repositories by research direction in a window grid", async () => {
    installMocks();
    const { container } = renderPage();

    await waitFor(() =>
      expect(screen.getByRole("heading", { name: "Agent 学习" })).toBeInTheDocument(),
    );
    expect(screen.getByRole("heading", { name: "AI Infra" })).toBeInTheDocument();
    expect(screen.getByText("OpenHands")).toBeInTheDocument();
    expect(screen.getByText("TVM")).toBeInTheDocument();
    expect(container.querySelector(".code-graph-window-grid")).not.toBeNull();
    expect(container.querySelectorAll(".code-graph-window").length).toBe(2);
    expect(container.querySelectorAll(".code-graph-window-scroll").length).toBe(2);
    expect(screen.getAllByText("已索引")).toHaveLength(1);
    expect(screen.getAllByText("未索引")).toHaveLength(1);
    const viewer = screen.getByTitle("GitNexus 代码图谱");
    expect(viewer).toHaveAttribute("src", "http://127.0.0.1:4747");
    await waitFor(() =>
      expect(screen.getAllByText("服务在线").length).toBeGreaterThan(0),
    );
    expect(
      screen.getAllByRole("link", { name: /资产详情/ })[0],
    ).toHaveAttribute("href", "/assets/repo-1");
  });

  it("deep links the embedded viewer to the selected repository", async () => {
    installMocks();
    renderPage();

    await waitFor(() =>
      expect(screen.getByRole("heading", { name: "Agent 学习" })).toBeInTheDocument(),
    );
    const openButtons = screen.getAllByRole("button", { name: /打开图谱/ });
    const enabledOpen = openButtons.find(
      (button) => !(button as HTMLButtonElement).disabled,
    );
    expect(enabledOpen).toBeDefined();
    fireEvent.click(enabledOpen as HTMLButtonElement);

    await waitFor(() =>
      expect(screen.getByTitle("GitNexus 代码图谱")).toHaveAttribute(
        "src",
        "http://127.0.0.1:4747/?project=openhands",
      ),
    );
  });

  it("hides repositories by default and reveals them on demand", async () => {
    installMocks();
    renderPage();

    await waitFor(() =>
      expect(screen.getByRole("heading", { name: "Agent 学习" })).toBeInTheDocument(),
    );
    expect(screen.queryByText("OpenBMB/BMTools")).toBeNull();
    expect(screen.getByText("已隐藏仓库（1）", { exact: false })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /显示已隐藏仓库/ }));
    await waitFor(() =>
      expect(screen.getByText("OpenBMB/BMTools")).toBeInTheDocument(),
    );

    const openButtons = screen.getAllByRole("button", { name: /打开图谱/ });
    const bmtoolsButton = openButtons.find((button) =>
      button.closest(".code-graph-repo")?.textContent?.includes("OpenBMB/BMTools"),
    );
    expect(bmtoolsButton).toBeDefined();
    fireEvent.click(bmtoolsButton as HTMLButtonElement);
    await waitFor(() =>
      expect(screen.getByTitle("GitNexus 代码图谱")).toHaveAttribute(
        "src",
        "http://127.0.0.1:4747/?project=BMTools",
      ),
    );
  });

  it("keeps the repository list usable when GitNexus is offline", async () => {
    installMocks({ online: false });
    renderPage();

    await waitFor(() =>
      expect(
        screen.getAllByText("GitNexus 服务未启动").length,
      ).toBeGreaterThan(0),
    );
    expect(screen.getByText("OpenHands")).toBeInTheDocument();
    expect(screen.queryByTitle("GitNexus 代码图谱")).toBeNull();
  });
});
