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
      local_available: true,
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
  slug: "agent",
  label: "智能体系统",
  description: "智能体研究资料。",
  asset_count: 1,
  paper_count: 1,
  reading_counts: { unread: 1, reading: 0, read: 0 },
};

const portalSection = (
  id: string,
  title: string,
  content: Record<string, unknown> = {},
) => ({ id, kind: id.replaceAll("-", "_"), title, status: "reviewed", content });

const portal = {
  schema: "research-asset-hub.domain-portal/v2",
  slug: domain.slug,
  label: domain.label,
  description: domain.description,
  content_version: "2026.08.04",
  content_digest: "a".repeat(64),
  review_status: "reviewed",
  stale: false,
  stale_reasons: [],
  original_selector: {
    source_id: "domain-portals-20260726",
    source_record_id: "overview:agent",
  },
  sections: [
    portalSection("scope", "领域定位与边界", { markdown: "智能体研究范围。" }),
    portalSection("timeline", "发展时间线", { stages: [] }),
    portalSection("capability-method-map", "能力与方法版图", {
      capabilities: [],
      methods: [],
      cells: [],
    }),
    portalSection("landmarks", "代表工作与技术路线", { items: [] }),
    portalSection("benchmarks", "任务、数据集与评测", { rows: [] }),
    portalSection("reading-routes", "推荐阅读路线", { routes: [] }),
    portalSection("catalog", "论文、代码与导读目录", {
      summary: "正式库动态生成完整目录。",
    }),
    portalSection("frontiers", "前沿趋势与开放问题", { markdown: "开放问题。" }),
    portalSection("evidence", "证据、来源与版本说明", { source_inputs: [] }),
  ],
};

const portalResponse = {
  schema: "research-asset-hub.domain-portal-response/v2",
  renderer: "v2",
  domain: {
    ...domain,
    repository_count: 0,
    research_unit_count: 1,
  },
  portal,
};

const researchUnit = {
  unit_id: asset.id,
  unit_kind: "paper",
  primary_asset: asset,
  related_repositories: [],
  quick_actions: [
    {
      kind: "pdf",
      status: "available",
      representation_id: "pdf-1",
      owner_asset_id: asset.id,
      relation_type: null,
      alternatives: [],
    },
    {
      kind: "html",
      status: "available",
      representation_id: "html-1",
      owner_asset_id: asset.id,
      relation_type: null,
      alternatives: [],
    },
    {
      kind: "guide",
      status: "available",
      representation_id: "guide-1",
      owner_asset_id: asset.id,
      relation_type: null,
      alternatives: [],
    },
    {
      kind: "code",
      status: "available",
      representation_id: "repository-1",
      owner_asset_id: asset.id,
      relation_type: "implemented_by",
      alternatives: [],
    },
  ],
  matched_assets: [asset],
  covered_asset_count: 1,
  score: 0.95,
  snippet: "论文主卡搜索片段。",
  match_source: "paper",
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

    // 前端 health 请求经 API_BASE 拼接:桌面壳为 /api/v1/health,
    // 浏览器托管模式(本 e2e 场景)默认 API_BASE=/api/v1 同样命中此路径。
    if (path === "/api/health" || path === "/api/v1/health") {
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
    if (path === "/api/v1/domains/agent/portal") return json(route, portalResponse);
    if (path === "/api/v1/assets/paper-1/representations") {
      return json(route, list(asset.representations));
    }
    if (path === "/api/v1/representations/repository-1/code/tree") {
      return json(route, {
        representation_id: "repository-1",
        repository_name: "demo-repository",
        path: "",
        entries: [
          {
            name: "src",
            path: "src",
            kind: "directory",
            size_bytes: null,
            language: null,
          },
          {
            name: "README.md",
            path: "README.md",
            kind: "file",
            size_bytes: 18,
            language: "markdown",
          },
        ],
        truncated: false,
      });
    }
    if (path === "/api/v1/representations/repository-1/code/file") {
      return json(route, {
        representation_id: "repository-1",
        path: "README.md",
        language: "markdown",
        size_bytes: 18,
        line_count: 2,
        content: "# demo\nlocal code\n",
        previewable: true,
        truncated: false,
      });
    }
    if (path === "/api/v1/assets/paper-1") return json(route, asset);
    if (path === "/api/v1/assets") return json(route, list([asset]));
    if (path === "/api/v1/annotations") return json(route, list([]));
    if (path === "/api/v1/similar/paper-1") return json(route, list([secondAsset]));
    if (path === "/api/v1/search") {
      return json(
        route,
        url.searchParams.get("group_by") === "paper"
          ? { ...list([researchUnit]), group_by: "paper" }
          : list([asset]),
      );
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

test("桌面 index.html 入口进入资料库并显示导航和全局搜索", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/index.html");
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByRole("heading", { name: "今天想研究什么？" })).toBeVisible();
  await expect(page.getByRole("button", { name: "返回上一页" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "搜索全部资料" })).toBeVisible();
  await page.screenshot({ path: "../../work/desktop-polish-20260926/screenshots/updated-home.png" });
});

test("桌面页面历史保留搜索条件，弹窗支持键盘退出和焦点恢复", async ({ page }) => {
  await page.goto("/search?q=agent&domain=agent&status=reading");
  await page.getByRole("link", { name: "打开资料" }).click();
  await expect(page.getByRole("heading", { name: "示例论文", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "返回上一页" }).click();
  await expect(page).toHaveURL(/search\?q=agent&domain=agent&status=reading$/);
  await page.getByRole("button", { name: "前进到下一页" }).click();
  await expect(page).toHaveURL(/assets\/paper-1$/);
  const trigger = page.getByRole("button", { name: "搜索全部资料" });
  await trigger.click();
  const input = page.getByRole("searchbox", { name: "全局搜索关键词" });
  await expect(input).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(page.getByRole("dialog").getByRole("button", { name: "笔记" })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(trigger).toBeFocused();
});

const routes = [
  ["/", "今天想研究什么？"],
  ["/search", "浏览资料库"],
  ["/assets/paper-1", "示例论文"],
  ["/domains", "研究领域"],
  ["/domains/agent", "智能体系统"],
  ["/compare", "论文对比"],
  ["/annotations", "全部批注"],
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

test("搜索卡片默认保持简洁，更多菜单提供快捷打开", async ({ page }) => {
  await page.goto("/search?q=agent");

  const card = page.locator(".asset-card").filter({ hasText: "示例论文" });
  await expect(card).toBeVisible();
  await expect(card.locator(".annotation-snippet")).toHaveCount(0);
  await expect(card.getByRole("link", { name: "打开资料" })).toBeVisible();
  await card.getByRole("button", { name: "更多" }).click();
  for (const label of ["PDF", "HTML", "代码"]) {
    await expect(card.getByRole("button", { name: label, exact: true })).toBeEnabled();
  }
  const openRequest = page.waitForRequest(
    (request) =>
      request.url().endsWith("/api/v1/representations/pdf-1/open-actions") &&
      request.method() === "POST",
  );
  await card.getByRole("button", { name: "PDF", exact: true }).click();
  await openRequest;

  await page.getByText("更多筛选", { exact: true }).click();
  await page.getByLabel("资产类型").selectOption("paper");
  await page.getByLabel("作者").fill("研究者甲");
  await page.getByLabel("起始年份").fill("2020");
  await page.getByRole("checkbox", { name: "PDF", exact: true }).click();
  await expect(page).toHaveURL(/has_pdf=true/);
  await page.getByRole("button", { name: "应用筛选" }).click();
  await expect(page).toHaveURL(/type=paper/);
  await expect(page).toHaveURL(/author=%E7%A0%94%E7%A9%B6%E8%80%85%E7%94%B2/);
  await expect(page).toHaveURL(/year_from=2020/);
  await expect(page).toHaveURL(/has_pdf=true/);
});

test("详情页统一标签覆盖 PDF、代码、导读、相似论文和 HTML", async ({
  page,
}) => {
  await page.goto("/assets/paper-1");

  for (const label of ["PDF", "代码", "导读", "相似论文", "原始 HTML"]) {
    await expect(page.getByRole("tab", { name: label, exact: true })).toBeVisible();
  }

  await page.getByRole("tab", { name: "PDF", exact: true }).click();
  const pdfCanvas = page.locator(".pdf-canvas-wrap canvas");
  await expect(pdfCanvas).toBeVisible();
  await expect
    .poll(() => pdfCanvas.evaluate((canvas: HTMLCanvasElement) => canvas.width))
    .toBeGreaterThan(0);

  await page.getByRole("tab", { name: "代码", exact: true }).click();
  await expect(page.getByRole("region", { name: "站内代码浏览器" })).toBeVisible();
  await page.getByRole("button", { name: "打开文件 README.md" }).click();
  await expect(page.locator(".code-browser-source code")).toContainText("# demo");

  await page.getByRole("tab", { name: "相似论文", exact: true }).click();
  await expect(page.getByRole("heading", { name: "第二篇论文" })).toBeVisible();

  await page.getByRole("tab", { name: "原始 HTML", exact: true }).click();
  await expect(page.getByTitle("原始 HTML")).toBeVisible();
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
  ]) {
    await expect(page.getByRole("rowheader", { name: dimension })).toBeVisible();
  }
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "导出 Markdown" }).click();
  await expect((await download).suggestedFilename()).toMatch(/\.md$/);
});

test("核心页面没有 axe 可访问性违规", async ({ page }) => {
  const pages = [
    ["/search?q=agent", "“agent”的搜索结果"],
    ["/assets/paper-1", "示例论文"],
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
