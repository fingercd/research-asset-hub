import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { chromium } from "@playwright/test";

const root = path.resolve("..", "..", "work", "validation", "domain-rework-acceptance");
await mkdir(root, { recursive: true });
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 });
const errors = [];
page.on("pageerror", (error) => errors.push(error.message));

try {
  await page.goto("http://127.0.0.1:4173/domains", { waitUntil: "networkidle" });
  for (const label of ["图像生成与编辑", "视频生成", "具身智能与导航压缩"]) {
    assert.equal(await page.getByRole("heading", { name: label, exact: true }).count(), 1);
  }
  assert.equal(await page.getByRole("heading", { name: "图像与视频生成", exact: true }).count(), 0);
  assert.equal(await page.getByRole("heading", { name: "导航压缩", exact: true }).count(), 0);
  await page.screenshot({ path: path.join(root, "01-domain-directory.png"), fullPage: true });

  await page.goto("http://127.0.0.1:4173/domains/image-editing", { waitUntil: "networkidle" });
  assert.equal(await page.locator("article[data-domain-portal]").count(), 1);
  assert.equal(await page.locator(".domain-portal-metrics > div").count(), 3);
  assert.equal(await page.locator(".domain-portal-metrics").getByText("研究单元（聚合视图）").count(), 0);
  await page.screenshot({ path: path.join(root, "02-image-overview.png") });
  await page.getByRole("tab", { name: "研究单元" }).click();
  await page.locator(".research-unit-card").first().waitFor();
  const imageConference = page.locator(".research-unit-card .research-unit-venue").filter({ hasText: /ICML/ }).first();
  assert.match(await imageConference.innerText(), /ICML 2026/);
  const conferenceSize = Number.parseFloat(await imageConference.evaluate((node) => getComputedStyle(node).fontSize));
  assert.ok(conferenceSize >= 16, `ICML font size is ${conferenceSize}px`);
  const imageArxiv = page.locator(".research-unit-card .asset-card-kicker span").filter({ hasText: /arxiv/i }).first();
  const arxivSize = Number.parseFloat(await imageArxiv.evaluate((node) => getComputedStyle(node).fontSize));
  assert.equal(arxivSize, 12);
  await page.screenshot({ path: path.join(root, "03-image-units.png") });

  await page.goto("http://127.0.0.1:4173/domains/video-generation", { waitUntil: "networkidle" });
  assert.equal(await page.locator("article[data-domain-portal]").count(), 1);
  await page.getByRole("tab", { name: "研究单元" }).click();
  await page.locator(".research-unit-card").first().waitFor();
  assert.ok((await page.locator(".research-unit-card").count()) > 0);
  const videoConference = page.locator(".research-unit-card .research-unit-venue").filter({ hasText: /ICML/ }).first();
  assert.match(await videoConference.innerText(), /ICML 2026/);
  assert.equal(await page.locator(".research-unit-card .asset-domain-chip[href='/domains/image-editing']").count(), 0);
  await page.screenshot({ path: path.join(root, "04-video-units.png") });

  await page.goto("http://127.0.0.1:4173/search?q=DFSAttn", { waitUntil: "networkidle" });
  await page.locator(".research-unit-card").first().waitFor();
  assert.match(await page.locator(".research-unit-card").first().innerText(), /DFSAttn/);
  await page.screenshot({ path: path.join(root, "05-search-result.png") });
  await page.locator(".research-unit-card").first().getByRole("link", { name: "打开资料" }).click();
  await page.getByRole("tab", { name: "PDF", exact: true }).click();
  await page.locator(".pdf-toolbar").waitFor({ timeout: 30_000 });
  const pdfCanvasWidth = await page.locator(".pdf-canvas-wrap canvas").evaluate((node) => node.width);
  assert.ok(pdfCanvasWidth > 0);
  await page.screenshot({ path: path.join(root, "06-pdf-reader.png") });

  await page.goto("http://127.0.0.1:4173/domains/embodied-ai-compression", { waitUntil: "networkidle" });
  await page.getByRole("tab", { name: "研究单元" }).click();
  await page.locator(".research-unit-card").first().waitFor();
  assert.ok((await page.locator(".research-unit-card").count()) > 0);
  assert.match(await page.locator(".research-unit-card").first().innerText(), /Hydra-Nav/);
  assert.equal(await page.locator(".research-unit-card .asset-domain-chip[href='/domains/navigation-compression']").count(), 0);
  await page.screenshot({ path: path.join(root, "07-embodied-navigation-units.png") });

  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("http://127.0.0.1:4173/domains/image-editing", { waitUntil: "networkidle" });
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  assert.ok(overflow <= 1, `mobile horizontal overflow is ${overflow}px`);
  await page.screenshot({ path: path.join(root, "08-image-mobile.png") });

  assert.deepEqual(errors, []);
  const report = {
    status: "passed",
    data_source: "outputs/open-catalog/catalog.jsonl and current domain portal configuration",
    browser: "Chromium",
    checks: [
      "Three requested domains render in the directory without legacy mixed headings",
      "Image and video papers render in separate domain pages",
      "Navigation papers render inside embodied compression",
      "Red-box metric is absent from the domain hero",
      "ICML 2026 is enlarged while arXiv retains its original size",
      "Search opens an exported paper and renders its real local PDF",
      "390px mobile viewport has no horizontal overflow",
    ],
    conference_font_px: conferenceSize,
    arxiv_font_px: arxivSize,
    mobile_horizontal_overflow_px: overflow,
    pdf_canvas_width_px: pdfCanvasWidth,
    screenshots: root,
  };
  await writeFile(path.join(root, "report.json"), JSON.stringify(report, null, 2), "utf8");
  console.log(JSON.stringify(report));
} finally {
  await browser.close();
}
