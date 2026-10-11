import { expect, test, type Page } from "@playwright/test";

function pdfBytes() {
  const objects = [
    "<< /Type /Catalog /Pages 2 0 R >>",
    "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 6 0 R >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 7 0 R >>",
    "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ...["First research evidence", "Second research evidence"].map((text) => {
      const stream = `BT /F1 18 Tf 72 700 Td (${text}) Tj ET`;
      return `<< /Length ${stream.length} >>\nstream\n${stream}\nendstream`;
    }),
  ];
  let text = "%PDF-1.4\n";
  const offsets = [0];
  objects.forEach((object, i) => {
    offsets.push(Buffer.byteLength(text));
    text += `${i + 1} 0 obj\n${object}\nendobj\n`;
  });
  const xref = Buffer.byteLength(text);
  text += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n${offsets
    .slice(1)
    .map((offset) => `${String(offset).padStart(10, "0")} 00000 n \n`)
    .join(
      "",
    )}trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF`;
  return Buffer.from(text);
}
const domain = {
  slug: "robotics",
  label: "机器人",
  paper_count: 1,
  asset_count: 1,
};
const pdf = {
  id: "pdf-1",
  asset_id: "paper-1",
  representation_type: "pdf",
  sha256: "a".repeat(64),
  open_url: "/mock/product.pdf",
  availability: "available",
};
const asset = {
  id: "paper-1",
  title: "Research paper",
  asset_type: "paper",
  domain_slugs: ["robotics"],
  representations: [pdf],
};
async function mockBase(
  page: Page,
  custom: (
    path: string,
    method: string,
    body: unknown,
  ) => unknown | undefined = () => undefined,
) {
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname.replace("/api/v1", "");
    let body: unknown;
    try {
      body = request.postDataJSON();
    } catch {
      body = undefined;
    }
    const value = custom(path, request.method(), body);
    if (value !== undefined) {
      await route.fulfill({ json: value });
      return;
    }
    if (path === "/health") {
      await route.fulfill({ json: { status: "healthy", components: {} } });
      return;
    }
    if (path === "/domains") {
      await route.fulfill({ json: { items: [domain], next_cursor: null } });
      return;
    }
    if (path === "/domains/robotics") {
      await route.fulfill({ json: domain });
      return;
    }
    if (path === "/domains/robotics/portal") {
      await route.fulfill({ json: { domain, portal: null, renderer: "v2" } });
      return;
    }
    if (path === "/assets/paper-1") {
      await route.fulfill({ json: asset });
      return;
    }
    if (path === "/assets/paper-1/representations") {
      await route.fulfill({ json: { items: [pdf] } });
      return;
    }
    if (path === "/search") {
      await route.fulfill({
        json: {
          items: [
            {
              unit_id: "paper:1",
              unit_kind: "paper",
              primary_asset: asset,
              related_repositories: [],
              quick_actions: [],
              matched_assets: [],
              covered_asset_count: 1,
            },
          ],
          next_cursor: null,
          total: 1,
        },
      });
      return;
    }
    if (path.endsWith("reading-position")) {
      await route.fulfill({ json: { position: null, stale: false } });
      return;
    }
    await route.fulfill({ json: { items: [], next_cursor: null } });
  });
  await page.route("**/mock/product.pdf", (route) =>
    route.fulfill({ body: pdfBytes(), contentType: "application/pdf" }),
  );
}

test("PDF 与题名、领域即可入库，可选代码失败仍能打开已入库论文", async ({
  page,
}) => {
  const writes: Array<{ path: string; body: unknown }> = [];
  let committed = false;
  await mockBase(page, (path, method, body) => {
    if (method !== "GET") writes.push({ path, body });
    if (path === "/intake/drafts" && method === "POST")
      return {
        id: "draft-1",
        domain: "robotics",
        title: "New paper",
        status: "draft",
      };
    if (path === "/intake/drafts/draft-1/pdf")
      return {
        id: "draft-1",
        domain: "robotics",
        title: "New paper",
        status: "ready",
      };
    if (path === "/intake/drafts/draft-1/preview")
      return {
        draft: {
          id: "draft-1",
          domain: "robotics",
          title: "New paper",
          status: "ready",
        },
        duplicates: [],
        warnings: [],
      };
    if (path === "/intake/drafts/draft-1/commit") {
      committed = true;
      return { asset_id: "paper-1", draft_id: "draft-1", status: "committed" };
    }
    return undefined;
  });
  await page.route("**/api/v1/code-links", (route) =>
    route.fulfill({
      status: 503,
      json: { title: "代码下载失败", detail: "下载失败，请稍后重试。" },
    }),
  );
  await page.goto("/intake");
  await page.getByText("补充信息（全部选填）", { exact: true }).click();
  await page
    .getByLabel("代码仓库 URL（单篇）")
    .fill("https://github.com/example/project");
  await page.locator('input[type="file"][accept*="pdf"]').setInputFiles({
    name: "New paper.pdf",
    mimeType: "application/pdf",
    buffer: pdfBytes(),
  });
  await expect(
    page.getByRole("button", { name: "预览与重复检查" }),
  ).toBeDisabled();
  await page.getByLabel("所属领域 *").selectOption("robotics");
  await page.getByLabel("题名 *", { exact: true }).fill("");
  await expect(
    page.getByRole("button", { name: "预览与重复检查" }),
  ).toBeDisabled();
  await page.getByLabel("题名 *", { exact: true }).fill("New paper");
  await page.getByRole("button", { name: "预览与重复检查" }).click();
  await expect(
    page.getByRole("button", { name: "确认入库", exact: true }),
  ).toBeEnabled();
  expect(committed).toBe(false);
  await page.getByRole("button", { name: "确认入库", exact: true }).click();
  await expect(
    page.getByRole("link", { name: "打开已入库论文" }),
  ).toHaveAttribute("href", "/assets/paper-1");
  await expect(page.getByRole("alert")).toContainText(
    "论文已入库，代码关联失败",
  );
  expect(writes.find((v) => v.path === "/intake/drafts")?.body).toMatchObject({
    domain: "robotics",
    title: "New paper",
    storage_mode: "managed",
  });
});

test("领域研究单元不等待门户，统计与门户按标签请求", async ({ page }) => {
  let portals = 0;
  let searches = 0;
  await mockBase(page, (path) => {
    if (path.endsWith("/portal")) portals++;
    if (path === "/search") searches++;
    return undefined;
  });
  await page.goto("/domains/robotics?view=units");
  await expect(
    page.getByRole("heading", { name: "Research paper" }),
  ).toBeVisible();
  expect(portals).toBe(0);
  expect(searches).toBe(1);
  await page.getByRole("tab", { name: "统计", exact: true }).click();
  expect(portals).toBe(0);
  await page.getByRole("tab", { name: "领域总览", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "开始建立这个领域" }),
  ).toBeVisible();
  expect(portals).toBe(1);
});

test("真实 PDF 文本层可选字高亮，保存位置并从批注回到原页", async ({
  page,
}) => {
  const notes: Array<Record<string, unknown>> = [];
  let savedPage = 2;
  let version = 3;
  let savedHeaders: Record<string, string> = {};
  let anchor: Record<string, unknown> | undefined;
  await mockBase(page, (path, method, body) => {
    if (path === "/annotations" && method === "GET") return { items: notes };
    if (path === "/annotations" && method === "POST") {
      const value = body as Record<string, unknown>;
      anchor = value.anchor as Record<string, unknown>;
      notes.push({ id: "note-1", ...value });
      return notes[0];
    }
    if (path.endsWith("/reading-position")) {
      if (method === "PUT") {
        savedPage = (body as { page: number }).page;
        version++;
      }
      return {
        position: {
          representation_id: "pdf-1",
          page: savedPage,
          source_hash: pdf.sha256,
          row_version: version,
        },
        stale: false,
      };
    }
    return undefined;
  });
  page.on("request", (request) => {
    if (
      request.method() === "PUT" &&
      request.url().endsWith("reading-position")
    )
      savedHeaders = request.headers();
  });
  await page.goto("/assets/paper-1?view=pdf");
  await expect(page.getByLabel("跳转到页码")).toHaveValue("2");
  const text = page
    .locator(".textLayer span")
    .filter({ hasText: "Second research evidence" })
    .first();
  await expect(text).toBeVisible();
  expect(
    await text.evaluate((node) => getComputedStyle(node).fontSize),
  ).not.toBe("0px");
  await text.evaluate((node) => {
    const range = document.createRange();
    range.selectNodeContents(node);
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(range);
  });
  await page.locator(".pdf-page-surface").dispatchEvent("mouseup");
  await page.getByRole("button", { name: "保存高亮", exact: true }).click();
  await expect(page.locator(".pdf-highlights mark").first()).toBeVisible();
  expect(anchor).toMatchObject({
    representation_id: "pdf-1",
    source_hash: pdf.sha256,
    page: 2,
    quote: "Second research evidence",
  });
  const rects = anchor?.rects as Array<{
    x: number;
    y: number;
    width: number;
    height: number;
  }>;
  expect(rects.length).toBeGreaterThan(0);
  for (const rect of rects) {
    expect(rect.x).toBeGreaterThanOrEqual(0);
    expect(rect.y).toBeGreaterThanOrEqual(0);
    expect(rect.width).toBeLessThanOrEqual(1);
    expect(rect.height).toBeLessThanOrEqual(1);
  }
  await page.getByRole("button", { name: "上一页", exact: true }).click();
  await expect.poll(() => savedPage).toBe(1);
  expect(savedHeaders["if-match"]).toMatch(/^W\/"\d+"$/);
  await page.getByRole("tab", { name: "概览与批注" }).click();
  await page.getByRole("button", { name: "回到 PDF 第 2 页" }).click();
  await expect(page.getByLabel("跳转到页码")).toHaveValue("2");
  await expect(page.locator(".pdf-highlights mark").first()).toBeVisible();
});

test("领域包重复预览必须明确身份映射后才提交", async ({ page }) => {
  let commit: unknown;
  let headers: Record<string, string> = {};
  await mockBase(page, (path, method, body) => {
    if (path === "/imports/packages")
      return {
        id: "import-1",
        status: "preview",
        row_version: 5,
        preview: {
          assets: [
            {
              id: "old-1",
              title: "Archive paper",
              action: "needs_review",
              candidates: [{ id: "paper-1", title: "Existing paper" }],
            },
          ],
        },
      };
    if (path === "/imports/packages/import-1/commit") {
      commit = body;
      return {
        id: "import-1",
        status: "committed",
        row_version: 6,
        preview: { assets: [] },
      };
    }
    return undefined;
  });
  page.on("request", (request) => {
    if (request.url().endsWith("/commit")) headers = request.headers();
  });
  await page.goto("/domains");
  await page.locator('input[type="file"][accept*="zip"]').setInputFiles({
    name: "domain.zip",
    mimeType: "application/zip",
    buffer: Buffer.from("archive"),
  });
  await expect(
    page.getByRole("dialog", { name: "领域包导入预览" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "确认导入", exact: true }),
  ).toBeDisabled();
  await page.getByLabel("Archive paper 的导入方式").selectOption("paper-1");
  await page.getByRole("button", { name: "确认导入", exact: true }).click();
  await expect(page.getByText("此领域包已完成导入。")).toBeVisible();
  expect(commit).toEqual({
    identity_mapping: { "old-1": "paper-1" },
    include_user_state: false,
  });
  expect(headers["if-match"]).toBe('W/"5"');
});

test("集合创建、编辑与删除发送当前版本", async ({ page }) => {
  let collection: {
    id: string;
    label: string;
    row_version: number;
    member_count: number;
  } | null = null;
  const versions: string[] = [];
  await mockBase(page, (path, method, body) => {
    if (path === "/collections" && method === "GET")
      return { items: collection ? [collection] : [], next_cursor: null };
    if (path === "/collections" && method === "POST") {
      collection = {
        id: "c1",
        label: (body as { label: string }).label,
        row_version: 1,
        member_count: 0,
      };
      return collection;
    }
    if (path === "/collections/c1" && method === "PATCH") {
      collection = {
        ...collection!,
        label: (body as { label: string }).label,
        row_version: 2,
      };
      return collection;
    }
    if (path === "/collections/c1" && method === "DELETE") {
      collection = null;
      return {};
    }
    return undefined;
  });
  page.on("request", (request) => {
    if (
      request.url().endsWith("/collections/c1") &&
      ["PATCH", "DELETE"].includes(request.method())
    )
      versions.push(request.headers()["if-match"]);
  });
  await page.goto("/collections");
  await page.getByRole("button", { name: "新建集合", exact: true }).click();
  await page.getByLabel("名称 *").fill("课题阅读");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect(
    page.getByRole("button", { name: /课题阅读.*0 项/ }),
  ).toBeVisible();
  await page.getByRole("button", { name: "编辑", exact: true }).click();
  await page.getByLabel("名称 *").fill("下周阅读");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect(
    page.getByRole("button", { name: /下周阅读.*0 项/ }),
  ).toBeVisible();
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "删除", exact: true }).click();
  await expect(page.getByText("还没有收藏集合", { exact: true })).toBeVisible();
  expect(versions).toEqual(['W/"1"', 'W/"2"']);
});

test("批量阅读、标签和集合操作显示逐项失败", async ({ page }) => {
  let bulk: unknown;
  await mockBase(page, (path, method, body) => {
    if (path === "/collections")
      return {
        items: [{ id: "c1", label: "课题阅读", row_version: 1 }],
        next_cursor: null,
      };
    if (path === "/search")
      return {
        items: [asset, { ...asset, id: "paper-2", title: "Second paper" }].map(
          (a) => ({
            unit_id: `paper:${a.id}`,
            unit_kind: "paper",
            primary_asset: a,
            related_repositories: [],
            quick_actions: [],
            matched_assets: [],
            covered_asset_count: 1,
          }),
        ),
        next_cursor: null,
        total: 2,
      };
    if (path === "/assets/bulk" && method === "POST") {
      bulk = body;
      return {
        items: [
          { id: "paper-1", status: "updated" },
          {
            id: "paper-2",
            status: "failed",
            error: { code: "asset_missing", message: "第二项资料当前不可用" },
          },
        ],
        total: 2,
        succeeded: 1,
        failed: 1,
      };
    }
    return undefined;
  });
  await page.goto("/search");
  await page.getByLabel("选择当前已加载结果").check();
  const toolbar = page.getByRole("region", { name: "批量操作" });
  await toolbar.getByLabel("阅读状态", { exact: true }).selectOption("read");
  await toolbar.getByPlaceholder("逗号分隔").fill("重要");
  await toolbar
    .getByText("收藏集合", { exact: true })
    .locator("..")
    .locator("select")
    .first()
    .selectOption("c1");
  await toolbar.getByRole("button", { name: "应用批量修改" }).click();
  await expect(toolbar.getByText(/第二项资料当前不可用/)).toBeVisible();
  await expect(toolbar.getByText(/1 项已处理/)).toBeVisible();
  expect(bulk).toMatchObject({
    asset_ids: ["paper-1", "paper-2"],
    reading_status: "read",
    add_tags: ["重要"],
    collection_id: "c1",
    collection_action: "add",
  });
});

test("MCP 设置可复制真实配置，检查结果明确验证范围", async ({ page }) => {
  const config = {
    mcpServers: {
      "research-assets": {
        command: "python",
        args: ["-m", "apps.mcp_server"],
        env: { HUB_DATA_DIR: "D:/HubData" },
      },
    },
  };
  await mockBase(page, (path) => {
    if (path === "/integrations/mcp-config")
      return {
        server_name: "research-assets",
        config,
        tools: ["search_assets"],
        read_only: true,
        data_dir: "D:/HubData",
        bundled_runtime: true,
        module_available: true,
      };
    if (path === "/integrations/mcp-check")
      return {
        status: "ready",
        tool_count: 1,
        tools: ["search_assets"],
        read_only: true,
        database: "ready",
        verification: "bridge_construction_and_database_read",
        note: "仅验证服务构建与数据库读取。",
      };
    return undefined;
  });
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
  await page.goto("/settings");
  await expect(page.getByLabel("客户端配置 JSON")).toHaveValue(
    JSON.stringify(config, null, 2),
  );
  await page.getByRole("button", { name: "复制配置", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "已复制配置", exact: true }),
  ).toBeVisible();
  expect(
    JSON.parse(await page.evaluate(() => navigator.clipboard.readText())),
  ).toEqual(config);
  await page.getByRole("button", { name: "检查连接", exact: true }).click();
  await expect(page.getByText("本机 MCP 服务与资料库检查通过")).toBeVisible();
  await expect(page.getByText("仅验证服务构建与数据库读取。")).toBeVisible();
});

test("元数据冲突保留输入，核对最新版本后保存并更新详情", async ({ page }) => {
  let current = { ...asset, title: "Original title", row_version: 1, reading_status: "unread" };
  const versions: Array<string | undefined> = [];
  await mockBase(page);
  await page.route("**/api/v1/assets/paper-1", async (route) => {
    if (route.request().method() === "PATCH") {
      versions.push(route.request().headers()["if-match"]);
      if (versions.length === 1) {
        current = { ...current, title: "Concurrent title", row_version: 2 };
        await route.fulfill({ status: 412, json: { detail: "记录已被其他页面更新" } });
      } else {
        current = { ...current, ...route.request().postDataJSON(), row_version: 3 };
        await route.fulfill({ json: current });
      }
    } else {
      await route.fulfill({ json: current });
    }
  });
  await page.goto("/assets/paper-1");
  await page.getByRole("button", { name: "编辑元数据", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "编辑论文元数据" });
  await expect(dialog).toBeVisible();
  await page.screenshot({ path: test.info().outputPath("metadata-editor.png") });
  await dialog.getByLabel("题名 *", { exact: true }).fill("Revised title");
  await dialog.getByRole("button", { name: "保存元数据", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText("记录已被其他页面更新");
  await expect(dialog.getByLabel("题名 *", { exact: true })).toHaveValue("Revised title");
  await dialog.getByRole("button", { name: "读取最新版本", exact: true }).click();
  await expect(dialog.getByText("Concurrent title", { exact: true })).toBeVisible();
  await dialog.getByRole("button", { name: "保存元数据", exact: true }).click();
  await expect(dialog).not.toBeVisible();
  await expect(page.getByRole("heading", { name: "Revised title", exact: true })).toBeVisible();
  expect(versions).toEqual(['W/"1"', 'W/"2"']);
});

test("阅读状态失败回退，重试后显示真正保存的状态", async ({ page }) => {
  let current = { ...asset, reading_status: "unread", row_version: 1 };
  let saves = 0;
  await mockBase(page, (path, method) => {
    if (path === "/assets/paper-1" && method === "GET") return current;
    return undefined;
  });
  await page.route("**/api/v1/assets/paper-1/reading-state", async (route) => {
    saves++;
    if (saves === 1) {
      await route.fulfill({ status: 503, json: { detail: "临时无法保存" } });
    } else {
      current = { ...current, reading_status: "read", row_version: 2 };
      await route.fulfill({ json: current });
    }
  });
  await page.goto("/assets/paper-1");
  const status = page.getByRole("combobox", { name: "阅读状态", exact: true });
  await status.selectOption("read");
  await expect(page.getByRole("alert")).toContainText("保存失败");
  await expect(status).toHaveValue("unread");
  await page.getByRole("button", { name: "重试", exact: true }).click();
  await expect(status).toHaveValue("read");
  await expect(page.getByRole("alert")).toHaveCount(0);
  expect(saves).toBe(2);
});

test("任务历史加载后续页，刷新仍保留两页记录", async ({ page }) => {
  const requested: Array<string | null> = [];
  await mockBase(page);
  await page.route("**/api/v1/jobs**", async (route) => {
    const cursor = new URL(route.request().url()).searchParams.get("cursor");
    requested.push(cursor);
    await route.fulfill({ json: {
      items: [{ id: cursor ? "old-job" : "new-job", name: cursor ? "历史任务" : "最新任务", status: "succeeded" }],
      next_cursor: cursor ? null : "page-2", total: 2,
    } });
  });
  await page.goto("/jobs");
  await expect(page.getByRole("heading", { name: "最新任务", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "加载更多任务", exact: true }).click();
  await expect(page.getByRole("heading", { name: "历史任务", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "刷新", exact: true }).click();
  await expect.poll(() => requested).toEqual([null, "page-2", null, "page-2"]);
  await expect(page.getByRole("heading", { name: "最新任务", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "历史任务", exact: true })).toBeVisible();
});

for (const width of [360, 1280]) {
  test(`元数据弹窗在 ${width}px 矮屏内可滚动至保存并用 Escape 退出`, async ({ page }) => {
    await page.setViewportSize({ width, height: 640 });
    await mockBase(page, (path) => path === "/assets/paper-1"
      ? { ...asset, row_version: 1 }
      : undefined);
    await page.goto("/assets/paper-1");
    const trigger = page.getByRole("button", { name: "编辑元数据", exact: true });
    await trigger.click();
    const dialog = page.getByRole("dialog", { name: "编辑论文元数据" });
    await expect(dialog).toBeVisible();
    const bounds = await dialog.boundingBox();
    expect(bounds).not.toBeNull();
    expect(bounds!.y).toBeGreaterThanOrEqual(0);
    expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(640);
    expect(bounds!.x).toBeGreaterThanOrEqual(0);
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(width);
    const save = dialog.getByRole("button", { name: "保存元数据", exact: true });
    await save.scrollIntoViewIfNeeded();
    await expect(save).toBeInViewport();
    await page.keyboard.press("Escape");
    await expect(dialog).not.toBeVisible();
    await expect(trigger).toBeFocused();
  });
}

test("同步冲突合并提交自定义值及当前版本，失败后输入保留", async ({ page }) => {
  const conflict = {
    id: "conflict-1", title: "题名冲突", field: "/title", asset_id: asset.id,
    local_value: "Hub title", incoming_value: "External title", row_version: 4,
  };
  let resolved = false;
  const writes: Array<{ body: unknown; version: string | undefined }> = [];
  await mockBase(page);
  await page.route("**/api/v1/sync-conflicts**", async (route) => {
    if (route.request().method() === "POST") {
      writes.push({ body: route.request().postDataJSON(), version: route.request().headers()["if-match"] });
      if (writes.length === 1) {
        await route.fulfill({ status: 503, json: { detail: "临时同步失败" } });
      } else {
        resolved = true;
        await route.fulfill({ json: { ...conflict, status: "resolved" } });
      }
    } else {
      await route.fulfill({ json: { items: resolved ? [] : [conflict], next_cursor: null } });
    }
  });
  await page.goto("/conflicts");
  await page.getByRole("button", { name: "合并", exact: true }).click();
  await page.getByLabel("合并后的题名", { exact: true }).fill("Reviewed combined title");
  await page.getByRole("button", { name: "确认合并并同步", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("临时同步失败");
  await expect(page.getByLabel("合并后的题名", { exact: true })).toHaveValue("Reviewed combined title");
  await page.getByRole("button", { name: "确认合并并同步", exact: true }).click();
  await expect(page.getByText("没有待处理冲突", { exact: true })).toBeVisible();
  expect(writes).toEqual([
    { body: { strategy: "merge", resolved_value: "Reviewed combined title" }, version: 'W/"4"' },
    { body: { strategy: "merge", resolved_value: "Reviewed combined title" }, version: 'W/"4"' },
  ]);
});
