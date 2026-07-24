import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page, type Route } from "@playwright/test";

const asset = {
  id: "paper-1",
  asset_type: "paper",
  title: "示例论文",
  authors: ["研究者甲", "研究者乙"],
  year: 2025,
  domain_slugs: ["agents"],
  reading_status: "unread",
  annotation_summary: null,
  row_version: 3,
  representations: [
    {
      id: "pdf-1",
      representation_type: "pdf",
      role: "primary",
      open_url: "/mock/paper.pdf",
    },
    {
      id: "repository-1",
      representation_type: "repository",
      role: "official",
      external_url: "https://example.test/repository",
    },
    {
      id: "guide-1",
      representation_type: "guide",
      role: "guide",
      open_url: "/mock/guide",
    },
    {
      id: "html-1",
      representation_type: "html",
      role: "primary",
      open_url: "/mock/original-html",
    },
  ],
};

const secondAsset = {
  ...asset,
  id: "paper-2",
  title: "第二篇论文",
  year: 2024,
  annotation_summary: "对照批注",
  row_version: 2,
  representations: [],
};

const domain = {
  slug: "agents",
  label: "智能体系统",
  description: "智能体研究资料。",
  asset_count: 1,
  paper_count: 1,
  reading_counts: { unread: 1, reading: 0, read: 0 },
};

function list<T>(items: T[]) {
  return { items, next_cursor: null, total: items.length };
}

function minimalPdf(): Buffer {
  const stream = "BT /F1 18 Tf 50 90 Td (PDF.js smoke) Tj ET";
  const objects = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 180] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
    `<< /Length ${Buffer.byteLength(stream, "ascii")} >>\nstream\n${stream}\nendstream`,
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
  ];
  let document = "%PDF-1.4\n";
  const offsets = [0];
  objects.forEach((body, index) => {
    offsets.push(Buffer.byteLength(document, "ascii"));
    document += `${index + 1} 0 obj\n${body}\nendobj\n`;
  });
  const xref = Buffer.byteLength(document, "ascii");
  document += `xref\n0 ${objects.length + 1}\n`;
  document += "0000000000 65535 f \n";
  document += offsets
    .slice(1)
    .map((offset) => `${String(offset).padStart(10, "0")} 00000 n \n`)
    .join("");
  document += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(document, "ascii");
}

async function json(route: Route, body: unknown) {
  await route.fulfill({
    status: 200,
    contentType: "application/json; charset=utf-8",
    body: JSON.stringify(body),
  });
}

async function installApiMock(page: Page) {
  await page.route("**/mock/paper.pdf", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/pdf",
      body: minimalPdf(),
    }),
  );
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;

    if (path === "/api/health") {
      return json(route, {
        service: "research-asset-hub",
        status: "healthy",
        checked_at: "2026-07-24T12:00:00Z",
        components: { api: { status: "healthy" } },
      });
    }
    if (path === "/api/v1/dashboard") {
      return json(route, {
        stats: {
          assets: 1,
          papers: 1,
          unread: 1,
          reading: 0,
          read: 0,
          domains: 1,
          relations: 0,
          annotations: 0,
        },
        recent_assets: [asset],
        domains: [domain],
        index_state: { status: "ready", indexed_assets: 1, pending_assets: 0 },
      });
    }
    if (path === "/api/v1/domains") return json(route, list([domain]));
    if (path === "/api/v1/domains/agents") return json(route, domain);
    if (path === "/api/v1/assets/paper-1/representations") {
      return json(route, list(asset.representations));
    }
    if (path === "/api/v1/assets/paper-1") return json(route, asset);
    if (path === "/api/v1/assets") return json(route, list([asset]));
    if (path === "/api/v1/annotations") return json(route, list([]));
    if (path === "/api/v1/similar/paper-1") return json(route, list([secondAsset]));
    if (path === "/api/v1/search") return json(route, list([asset]));
    if (path === "/api/v1/graph/neighbors") {
      return json(route, {
        nodes: [
          { id: "paper-1", title: "示例论文", asset_type: "paper" },
          { id: "paper-2", title: "第二篇论文", asset_type: "paper" },
        ],
        edges: [
          {
            id: "relation-1",
            source: "paper-1",
            target: "paper-2",
            relation_type: "related_to",
          },
        ],
      });
    }
    if (path === "/api/v1/comparisons") {
      if (route.request().method() === "POST") {
        return json(route, {
          id: "comparison-1",
          title: "示例论文 vs. 第二篇论文",
          asset_ids: ["paper-1", "paper-2"],
          assets: [asset, secondAsset],
          columns: [],
        });
      }
      return json(route, list([]));
    }
    if (path === "/api/v1/jobs") return json(route, list([]));
    if (path === "/api/v1/sync-conflicts") return json(route, list([]));
    if (path === "/api/v1/settings") {
      return json(route, {
        app: { mode: "local" },
        search: { status: "ready" },
        rag: { provider: "configured" },
        sources: [],
        integrations: [],
      });
    }
    if (path.startsWith("/api/v1/representations/")) {
      return json(route, { open_url: "/mock/opened" });
    }

    return json(route, list([]));
  });
}

test.beforeEach(async ({ page }) => {
  await installApiMock(page);
});

const routes = [
  ["/", "你的科研资产，一处整理"],
  ["/search", "跨领域搜索"],
  ["/assets/paper-1", "示例论文"],
  ["/domains", "研究领域"],
  ["/domains/agents", "智能体系统"],
  ["/graph", "关系图谱"],
  ["/compare", "论文对比"],
  ["/annotations", "全部批注"],
  ["/chat", "向资料库提问"],
  ["/jobs", "后台任务"],
  ["/conflicts", "同步冲突"],
  ["/settings", "设置"],
] as const;

for (const [path, heading] of routes) {
  test(`${path} 可渲染`, async ({ page }) => {
    await page.goto(path);
    await expect(page.getByRole("heading", { name: heading, exact: true })).toBeVisible();
  });
}

test("搜索卡片提供四种快捷打开并显示空批注", async ({ page }) => {
  await page.goto("/search?q=agent");

  const card = page.locator(".asset-card").filter({ hasText: "示例论文" });
  await expect(card).toBeVisible();
  await expect(card.locator(".annotation-snippet")).toContainText("批注：无");
  await expect(card.getByRole("link", { name: "PDF", exact: true })).toHaveAttribute(
    "href",
    "/mock/paper.pdf",
  );
  await expect(card.getByRole("link", { name: "代码", exact: true })).toHaveAttribute(
    "href",
    "https://example.test/repository",
  );
  await expect(card.getByRole("link", { name: "导读", exact: true })).toHaveAttribute(
    "href",
    "/mock/guide",
  );
  await expect(
    card.getByRole("link", { name: "原始 HTML", exact: true }),
  ).toHaveAttribute("href", "/mock/original-html");

  await page.getByText("更多筛选", { exact: true }).click();
  await page.getByLabel("资产类型").selectOption("paper");
  await page.getByLabel("作者").fill("研究者甲");
  await page.getByLabel("起始年份").fill("2020");
  await page.getByLabel("PDF", { exact: true }).check();
  await page.getByRole("button", { name: "应用更多筛选" }).click();
  await expect(page).toHaveURL(/type=paper/);
  await expect(page).toHaveURL(/author=%E7%A0%94%E7%A9%B6%E8%80%85%E7%94%B2/);
  await expect(page).toHaveURL(/year_from=2020/);
  await expect(page).toHaveURL(/has_pdf=true/);
});

test("详情页统一标签覆盖 PDF、代码、导读、关系、相似论文和 HTML", async ({
  page,
}) => {
  await page.goto("/assets/paper-1");

  for (const label of ["PDF", "代码", "导读", "关系", "相似论文", "原始 HTML"]) {
    await expect(page.getByRole("tab", { name: label, exact: true })).toBeVisible();
  }

  await page.getByRole("tab", { name: "PDF", exact: true }).click();
  const pdfCanvas = page.locator(".pdf-canvas-wrap canvas");
  await expect(pdfCanvas).toBeVisible();
  await expect
    .poll(() => pdfCanvas.evaluate((canvas: HTMLCanvasElement) => canvas.width))
    .toBeGreaterThan(0);

  await page.getByRole("tab", { name: "代码", exact: true }).click();
  await expect(page.getByRole("link", { name: "打开代码仓库" })).toBeVisible();

  await page.getByRole("tab", { name: "关系", exact: true }).click();
  await expect(page.getByRole("heading", { name: "关系资产" })).toBeVisible();
  await expect(page.getByText("第二篇论文")).toBeVisible();

  await page.getByRole("tab", { name: "相似论文", exact: true }).click();
  await expect(page.getByRole("heading", { name: "第二篇论文" })).toBeVisible();

  await page.getByRole("tab", { name: "原始 HTML", exact: true }).click();
  await expect(page.getByTitle("原始 HTML")).toBeVisible();
});

test("Cytoscape 渲染可交互图谱", async ({ page }) => {
  await page.goto("/graph?asset_id=paper-1");
  const canvas = page.locator(".cytoscape-canvas");
  await expect(canvas).toBeVisible();
  await expect(canvas.locator("canvas").first()).toBeVisible();
  await expect(page.getByText("第二篇论文")).toBeVisible();
});

test("比较页保存完整研究维度并导出 Markdown", async ({ page }) => {
  await page.addInitScript(
    ([first, second]) => {
      window.localStorage.setItem("rah.compare-assets", JSON.stringify([first, second]));
    },
    [asset, secondAsset],
  );
  await page.goto("/compare");
  await page.getByRole("button", { name: "保存并生成对比" }).click();

  for (const dimension of [
    "研究问题",
    "机制 / 方法",
    "主要贡献",
    "实验与结果",
    "限制",
    "代码",
    "个人批注",
    "关系",
  ]) {
    await expect(page.getByRole("rowheader", { name: dimension })).toBeVisible();
  }
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "导出 Markdown" }).click();
  await expect((await download).suggestedFilename()).toMatch(/\.md$/);
});

test("核心页面没有 axe 可访问性违规", async ({ page }) => {
  const pages = [
    ["/search?q=agent", "跨领域搜索"],
    ["/assets/paper-1", "示例论文"],
    ["/graph?asset_id=paper-1", "关系图谱"],
    ["/compare", "论文对比"],
    ["/settings", "设置"],
  ] as const;

  for (const [path, heading] of pages) {
    await page.goto(path);
    await expect(
      page.getByRole("heading", { name: heading, exact: true }),
    ).toBeVisible();
    const results = await new AxeBuilder({ page }).analyze();
    expect(results.violations, `${path} 的 axe 违规`).toEqual([]);
  }
});

test("360px 窄屏没有页面级横向溢出", async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 800 });

  for (const [path, heading] of routes) {
    await page.goto(path);
    await expect(page.getByRole("heading", { name: heading, exact: true })).toBeVisible();
    const dimensions = await page.evaluate(() => ({
      viewport: document.documentElement.clientWidth,
      scroll: document.documentElement.scrollWidth,
    }));
    expect(dimensions.scroll, `${path} 横向溢出`).toBeLessThanOrEqual(
      dimensions.viewport,
    );
  }

  await page.goto("/search?q=agent");
  await expect(page.locator(".asset-card")).toBeVisible();
  const searchDimensions = await page.evaluate(() => ({
    viewport: document.documentElement.clientWidth,
    scroll: document.documentElement.scrollWidth,
  }));
  expect(searchDimensions.scroll, "搜索结果横向溢出").toBeLessThanOrEqual(
    searchDimensions.viewport,
  );

  await page.goto("/assets/paper-1");
  await page.getByRole("tab", { name: "PDF", exact: true }).click();
  await expect(page.locator(".pdf-canvas-wrap canvas")).toBeVisible();
  const pdfDimensions = await page.evaluate(() => ({
    viewport: document.documentElement.clientWidth,
    scroll: document.documentElement.scrollWidth,
  }));
  expect(pdfDimensions.scroll, "PDF 阅读器横向溢出").toBeLessThanOrEqual(
    pdfDimensions.viewport,
  );
});
