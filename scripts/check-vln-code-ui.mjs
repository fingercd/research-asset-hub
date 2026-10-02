import { readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { resolve, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(fileURLToPath(new URL("..", import.meta.url)));
const require = createRequire(join(root, "apps/web/package.json"));
const { chromium, expect } = require("@playwright/test");
const cases = [
  ["LookStep", "01a06831-3d19-7a11-9f8a-e817e81f49aa", "data_construction/build_short_label_dataset.py"],
  ["FlowDec", "01a0685f-869b-7a34-a8de-8a9eed75da50", "train.py"],
];
const report = { checked_at: new Date().toISOString(), status: "passed", read_only_user_state: true, items: [] };
const browser = await chromium.launch({ headless: true, args: ["--disable-gpu"] });
try {
  for (const [name, assetId, codePath] of cases) {
    const result = { name, asset_id: assetId, status: "passed", errors: [], files_verified: [] };
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const page = await context.newPage();
    page.on("pageerror", () => result.errors.push("page_error"));
    await context.route("**/*", async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const local = ["127.0.0.1", "localhost"].includes(url.hostname);
      const safeMethod = ["GET", "HEAD", "OPTIONS"].includes(request.method());
      const openAction = request.method() === "POST" && /^\/api\/v1\/representations\/[0-9a-f-]+\/open-actions$/.test(url.pathname);
      if (!local || (!safeMethod && !openAction)) {
        result.errors.push("unexpected_external_or_mutating_request");
        await route.abort();
        return;
      }
      await route.continue();
    });
    try {
      await page.goto(`http://127.0.0.1:8765/assets/${assetId}`, { waitUntil: "domcontentloaded" });
      await expect(page.getByText("官方代码已开源", { exact: true })).toBeVisible();
      await expect(page.getByTitle("代码核验日期：2026-09-12", { exact: true })).toBeVisible();
      await expect(page.getByTitle("Checkpoint 核验日期：2026-09-03", { exact: true })).toBeVisible();
      await page.getByRole("tab", { name: "代码", exact: true }).click();
      for (const file of ["README.md", codePath]) {
        const parts = file.split("/");
        for (let index = 1; index < parts.length; index += 1) {
          await page.getByRole("button", { name: `打开目录 ${parts.slice(0, index).join("/")}`, exact: true }).click();
        }
        await page.getByRole("button", { name: `打开文件 ${file}`, exact: true }).click();
        const expected = readFileSync(join(root, "..", "共享代码/2026-09-12-vln-code-supplement/upstream", name, file), "utf8");
        await expect(page.locator(".code-browser-source code")).toHaveText(expected);
        result.files_verified.push(file);
      }
    } catch (error) {
      result.errors.push(String(error.message).replace(/https?:[^\s]+/g, "[url]").slice(0, 700));
    } finally {
      await context.close();
    }
    if (result.errors.length) {
      result.status = "failed";
      report.status = "failed";
    }
    report.items.push(result);
  }
} finally {
  await browser.close();
}
writeFileSync(join(root, "work/validation/vln-code-ui-final-20260912.json"), JSON.stringify(report, null, 2) + "\n");
console.log(JSON.stringify(report));
if (report.status !== "passed") process.exitCode = 1;
