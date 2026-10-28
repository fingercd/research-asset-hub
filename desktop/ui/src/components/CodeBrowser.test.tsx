import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api/client";
import type { RepresentationOpenAction } from "../api/types";
import { CodeBrowser } from "./CodeBrowser";

function renderBrowser() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <CodeBrowser
        representationId="rep-1"
        repositoryLabel="demo-repo"
        openAction={
          {
            kind: "external_url",
            url: "https://github.com/example/demo-repo",
            editor: {
              available: true,
              editor: "vscode",
              uri: "vscode://file/D:/repos/demo-repo",
            },
          } satisfies RepresentationOpenAction
        }
      />
    </QueryClientProvider>,
  );
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("CodeBrowser", () => {
  it("browses a local tree and renders a selected file with line numbers", async () => {
    vi.spyOn(api, "codeTree").mockResolvedValue({
      representation_id: "rep-1",
      repository_name: "demo-repo",
      path: "",
      entries: [
        { name: "src", path: "src", kind: "directory" },
        { name: "README.md", path: "README.md", kind: "file", size_bytes: 12, language: "markdown" },
      ],
    });
    vi.spyOn(api, "codeFile").mockResolvedValue({
      representation_id: "rep-1",
      path: "README.md",
      language: "markdown",
      size_bytes: 12,
      line_count: 2,
      content: "# demo\nready\n",
      previewable: true,
      truncated: false,
    });

    renderBrowser();
    expect((await screen.findAllByText("demo-repo")).length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: "打开文件 README.md" }));
    expect(
      await screen.findByText((content) => content.includes("# demo")),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("代码内容")).toBeInTheDocument();
    await waitFor(() =>
      expect(api.codeFile).toHaveBeenCalledWith(
        "rep-1",
        "README.md",
        expect.anything(),
      ),
    );
  });

  it("shows the local-unavailable state without an external fallback", async () => {
    vi.spyOn(api, "codeTree").mockRejectedValue(new Error("源码未下载/未开源"));

    renderBrowser();
    expect(await screen.findByText("本地代码不可用")).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });
});
