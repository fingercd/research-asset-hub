import { mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";

const projectRoot = resolve(fileURLToPath(new URL("..", import.meta.url)));
const require = createRequire(join(projectRoot, "apps", "web", "package.json"));
const { chromium } = require("@playwright/test");
const { values } = parseArgs({ options: {
  "web-origin": { type: "string", default: "http://127.0.0.1:8765" },
  "api-origin": { type: "string", default: "http://127.0.0.1:8767" },
  report: { type: "string", default: join(projectRoot, "work", "validation", "vln-guide-ui-20260912.json") },
} });
function localOrigin(value) {
  const url = new URL(value);
  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password ||
      url.pathname !== "/" || url.search || url.hash ||
      !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)) {
    throw new Error("UI audit origins must be uncredentialed local HTTP origins");
  }
  return url.origin;
}
const webOrigin = localOrigin(values["web-origin"]);
const apiOrigin = localOrigin(values["api-origin"]);
const reportPath = resolve(values.report);
const integration = JSON.parse(readFileSync(join(projectRoot, "work", "validation",
  "vln-guide-integration-20260912.json"), "utf8"));
const inventory = JSON.parse(readFileSync(join(projectRoot, "work", "mineru", "vln-20260904", "inventory.json"), "utf8"));
const expected = new Map(integration.items.map((item) => [item.asset_id, item.representation_id]));
const authoredRoot = join(projectRoot, "config", "vln-guides", "v1", "content");
const authored = new Map();
for (const filename of readdirSync(authoredRoot).filter((name) => name.endsWith(".json")).sort()) {
  const payload = JSON.parse(readFileSync(join(authoredRoot, filename), "utf8"));
  for (const item of payload.items ?? [payload]) {
    if (authored.has(item.asset_id)) throw new Error(`duplicate_authored_asset:${item.asset_id}`);
    authored.set(item.asset_id, item);
  }
}
if (expected.size !== 109 || integration.items.some((item) => item.errors.length || !item.representation_id)) {
  throw new Error("Integration audit must contain 109 valid new guide representations");
}

function safeMessage(value) {
  return String(value).replace(/(?:https?|file|blob):[^\s'"<>]+/gi, "[url]")
    .replace(/[A-Za-z0-9_-]{40,}(?:\.[A-Za-z0-9_-]+)*/g, "[redacted]").slice(0, 700);
}
async function getJson(url) {
  const response = await fetch(url, { signal: AbortSignal.timeout(30_000) });
  if (response.status !== 200) throw new Error(`api_http_status:${response.status}`);
  return response.json();
}

async function auditDefaultActions() {
  const seenCursors = new Set();
  const units = [];
  let cursor;
  let pages = 0;
  do {
    const url = new URL("/api/v1/search", apiOrigin);
    url.search = new URLSearchParams({ domain: "vision-language-navigation", type: "paper",
      group_by: "paper", mode: "keyword", sort: "date_desc", ...(cursor ? { cursor } : {}) });
    const payload = await getJson(url);
    units.push(...payload.items);
    pages += 1;
    cursor = payload.next_cursor;
    if (cursor && seenCursors.has(cursor)) throw new Error("pagination_cursor_repeated");
    if (cursor) seenCursors.add(cursor);
  } while (cursor);
  const seenAssets = new Set();
  const items = units.map((unit) => {
    const assetId = unit.primary_asset?.id;
    const html = unit.quick_actions?.find((action) => action.kind === "html");
    const errors = [];
    if (unit.unit_kind !== "paper" || !expected.has(assetId)) errors.push("unexpected_paper_unit");
    if (seenAssets.has(assetId)) errors.push("duplicate_paper_unit");
    seenAssets.add(assetId);
    if (html?.status !== "available") errors.push("default_html_unavailable");
    if (html?.representation_id !== expected.get(assetId)) errors.push("default_html_not_new_guide");
    return { asset_id: assetId, expected_representation_id: expected.get(assetId) ?? null,
      actual_representation_id: html?.representation_id ?? null, errors };
  });
  const missing = [...expected.keys()].filter((id) => !seenAssets.has(id));
  return { status: units.length === 109 && !missing.length && items.every((item) => !item.errors.length) ? "passed" : "failed",
    pages, expected_papers: 109, checked_papers: units.length, missing_asset_ids: missing, items };
}

const representativePatterns = [
  { name: "GA-VLN", pattern: /^GA-VLN:/ },
  { name: "InternVLA", pattern: /^InternVLA-N1:/ },
  { name: "DUET", pattern: /^Think Global, Act Local: Dual-scale Graph Transformer/ },
];
const representatives = representativePatterns.map(({ name, pattern }) => {
  const matches = inventory.items.filter((item) => pattern.test(item.title));
  if (matches.length !== 1 || !expected.has(matches[0].asset_id)) throw new Error(`representative_not_unique:${name}`);
  return { name, ...matches[0] };
});

async function auditDetail(browser, paper) {
  const assetId = paper.asset_id;
  const representationId = expected.get(assetId);
  const content = authored.get(assetId);
  if (!content) throw new Error(`authored_content_missing:${assetId}`);
  const result = { name: paper.name, asset_id: assetId, representation_id: representationId,
    status: "failed", errors: [], console_errors: [], page_errors: [], request_failures: [],
    blocked_mutations: [], external_requests: [], opened_representation_ids: [], iframe_http_statuses: [] };
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  try {
    const page = await context.newPage();
    page.setDefaultTimeout(30_000);
    page.on("console", (message) => {
      if (message.type() === "error") result.console_errors.push(safeMessage(message.text()));
    });
    page.on("pageerror", (error) => result.page_errors.push(safeMessage(error.message)));
    page.on("requestfailed", (request) => result.request_failures.push({
      resource_type: request.resourceType(), error: safeMessage(request.failure()?.errorText ?? "failed"),
    }));
    page.on("response", (response) => {
      const request = response.request();
      if (request.isNavigationRequest() && request.frame() !== page.mainFrame()) {
        result.iframe_http_statuses.push(response.status());
      }
    });
    await context.route("**/*", async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (["http:", "https:"].includes(url.protocol) &&
          !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)) {
        result.external_requests.push({ method: request.method(), resource_type: request.resourceType() });
        await route.abort("blockedbyclient");
        return;
      }
      const openMatch = url.pathname.match(/^\/api\/v1\/representations\/([0-9a-f-]{36})\/open-actions$/i);
      if (request.method() === "POST" && openMatch) {
        result.opened_representation_ids.push(openMatch[1]);
      } else if (!["GET", "HEAD", "OPTIONS"].includes(request.method())) {
        result.blocked_mutations.push({ method: request.method() });
        await route.abort("blockedbyclient");
        return;
      }
      await route.continue();
    });
    const response = await page.goto(`${webOrigin}/assets/${assetId}`, { waitUntil: "domcontentloaded" });
    result.page_http_status = response?.status() ?? null;
    if (result.page_http_status !== 200) result.errors.push("detail_http_not_200");
    await page.getByRole("tab", { name: "导读", exact: true }).click();
    const iframe = page.locator('iframe[title="导读"]');
    await iframe.waitFor({ state: "visible" });
    result.iframe_sandbox = await iframe.getAttribute("sandbox");
    if (result.iframe_sandbox !== "") result.errors.push("guide_sandbox_not_empty");
    const frame = page.frameLocator('iframe[title="导读"]');
    await frame.getByRole("heading", { name: "一分钟看懂", exact: true }).waitFor({ state: "visible" });
    result.guide_version = await frame.locator("html").getAttribute("data-guide-version");
    result.guide_content_source = await frame.locator("html").getAttribute("data-content-source");
    if (result.guide_version !== "vln-unified-guide/v1") result.errors.push("guide_version_not_v1");
    const summary = (await frame.locator("#takeaway").innerText()).replace(/\s+/g, " ");
    result.expected_summary_visible = summary.includes(String(content.one_minute[0]).replace(/\s+/g, " "));
    if (!result.expected_summary_visible) result.errors.push("reviewed_summary_not_visible");
    // The app polls health and jobs. Rendering is proven by the visible guide
    // content above, not by waiting for unrelated background traffic to stop.
    if (!result.opened_representation_ids.includes(representationId)) result.errors.push("detail_did_not_open_new_guide");
    if (result.opened_representation_ids.some((id) => id !== representationId)) result.errors.push("detail_opened_other_representation");
    if (!result.iframe_http_statuses.includes(200)) result.errors.push("guide_iframe_http_not_200");
  } catch (error) {
    result.errors.push(`ui_exception:${safeMessage(error.message ?? error)}`);
  } finally {
    await context.close();
  }
  for (const key of ["console_errors", "page_errors", "request_failures", "blocked_mutations", "external_requests"]) {
    if (result[key].length) result.errors.push(key);
  }
  result.status = result.errors.length ? "failed" : "passed";
  return result;
}

let browser;
const report = { schema: "research-asset-hub.vln-guide-ui-audit/v1", status: "failed",
  checked_at: new Date().toISOString(), read_only_user_state: true,
  asset_id_set_sha256: integration.asset_id_set_sha256, errors: [], default_actions: null, detail_pages: [] };
try {
  report.default_actions = await auditDefaultActions();
  browser = await chromium.launch({ headless: true });
  for (const paper of representatives) report.detail_pages.push(await auditDetail(browser, paper));
  if (report.default_actions.status !== "passed") report.errors.push("default_html_actions_failed");
  if (report.detail_pages.some((item) => item.status !== "passed")) report.errors.push("detail_guide_ui_failed");
  report.status = report.errors.length ? "failed" : "passed";
} catch (error) {
  report.errors.push(safeMessage(error.message ?? error));
} finally {
  if (browser) await browser.close();
}
mkdirSync(dirname(reportPath), { recursive: true });
writeFileSync(reportPath, `${JSON.stringify(report, null, 2)}\n`, "utf8");
console.log(JSON.stringify({ status: report.status, default_actions: report.default_actions?.status,
  papers: report.default_actions?.checked_papers, detail_pages: report.detail_pages.length, report: reportPath }));
process.exitCode = report.status === "passed" ? 0 : 1;
