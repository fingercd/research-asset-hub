import { createReadStream, mkdirSync, readFileSync, realpathSync, statSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { createRequire } from "node:module";
import { dirname, isAbsolute, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";

const projectRoot = resolve(fileURLToPath(new URL("..", import.meta.url)));
const require = createRequire(join(projectRoot, "apps", "web", "package.json"));
const { chromium } = require("@playwright/test");
const axeModule = require("@axe-core/playwright");
const AxeBuilder = axeModule.default ?? axeModule;
const { values } = parseArgs({ options: {
  "output-root": { type: "string", default: join(projectRoot, "..", "个人新增资料", "2026-09-12-vln-guides-v1") },
  report: { type: "string", default: join(projectRoot, "work", "validation", "vln-guide-browser-audit.json") },
  inventory: { type: "string", default: join(projectRoot, "work", "mineru", "vln-20260904", "inventory.json") },
  "api-origin": { type: "string", default: "http://127.0.0.1:8767" },
  "source-id": { type: "string" },
  workers: { type: "string", default: "2" },
  limit: { type: "string" },
} });
const outputRoot = resolve(values["output-root"]);
const reportPath = resolve(values.report);
const apiOrigin = new URL(values["api-origin"]);
const gatewayMode = Boolean(values["source-id"]);
const workers = Number(values.workers);
const limit = values.limit === undefined ? undefined : Number(values.limit);
if (![2, 3].includes(workers)) throw new Error("--workers must be 2 or 3");
if (limit !== undefined && (!Number.isInteger(limit) || limit < 1)) throw new Error("--limit must be positive");
if (apiOrigin.pathname !== "/" || apiOrigin.search || apiOrigin.hash || apiOrigin.username || apiOrigin.password ||
    !["http:", "https:"].includes(apiOrigin.protocol) ||
    !["127.0.0.1", "localhost", "[::1]"].includes(apiOrigin.hostname)) {
  throw new Error("--api-origin must be an uncredentialed local origin");
}
const inventory = JSON.parse(readFileSync(values.inventory, "utf8"));
const allAssetIds = inventory.items.map((item) => String(item.asset_id)).sort();
if (new Set(allAssetIds).size !== allAssetIds.length ||
    allAssetIds.some((id) => !/^[0-9a-f-]{36}$/i.test(id)) ||
    allAssetIds.length !== inventory.expected_paper_count || inventory.status !== "passed") {
  throw new Error("Inventory must be passed, complete and contain unique asset IDs");
}
const assetIds = limit === undefined ? allAssetIds : allAssetIds.slice(0, limit);
const viewports = [
  { name: "desktop", width: 1440, height: 1000, axe: true },
  { name: "mobile-390", width: 390, height: 844, axe: true },
  { name: "mobile-320", width: 320, height: 780, axe: false },
];
const staticCsp = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; " +
  "script-src 'none'; connect-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'";
const securityHeaders = {
  "content-type": "text/html; charset=utf-8", "content-security-policy": staticCsp,
  "x-content-type-options": "nosniff", "referrer-policy": "no-referrer", "cache-control": "no-store",
};

function safeMessage(value) {
  // No raw URL, token, response body or stack is persisted by this audit.
  return String(value).replace(/(?:https?|file|blob):[^\s'"<>]+/gi, "[url]")
    .replace(/[A-Za-z0-9_-]{40,}(?:\.[A-Za-z0-9_-]+)*/g, "[redacted]").slice(0, 500);
}

function createLocalServer() {
  const realRoot = realpathSync(outputRoot);
  const allowedPaths = new Set(assetIds.map((id) => `/papers/${id}/guide.html`));
  return createServer((request, response) => {
    try {
      if (!["GET", "HEAD"].includes(request.method)) {
        response.writeHead(405).end("method not allowed");
        return;
      }
      const pathname = decodeURIComponent(new URL(request.url, "http://127.0.0.1").pathname);
      if (!allowedPaths.has(pathname)) {
        response.writeHead(403).end("forbidden");
        return;
      }
      const candidate = realpathSync(join(realRoot, pathname.slice(1)));
      const fromRoot = relative(realRoot, candidate);
      if (isAbsolute(fromRoot) || fromRoot === ".." || fromRoot.startsWith("../") || fromRoot.startsWith("..\\")) {
        response.writeHead(403).end("forbidden");
        return;
      }
      if (!statSync(candidate).isFile()) {
        response.writeHead(404).end("not found");
        return;
      }
      response.writeHead(200, securityHeaders);
      if (request.method === "HEAD") response.end();
      else createReadStream(candidate).on("error", () => response.destroy()).pipe(response);
    } catch {
      response.writeHead(404).end("not found");
    }
  });
}

async function apiJson(path, body) {
  const response = await fetch(new URL(path, apiOrigin), {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? {} : { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body), signal: AbortSignal.timeout(20_000),
  });
  if (response.status !== 200) throw new Error(`api_http_status:${response.status}`);
  return response.json();
}

async function selectGatewayGuide(assetId) {
  const [asset, representations] = await Promise.all([
    apiJson(`/api/v1/assets/${assetId}`), apiJson(`/api/v1/assets/${assetId}/representations`),
  ]);
  const domains = asset.domain_slugs ?? asset.domains ?? [];
  if (asset.id !== assetId || (asset.asset_type ?? asset.type) !== "paper" ||
      !domains.some((item) => (typeof item === "string" ? item : item.slug) === "vision-language-navigation")) {
    throw new Error("asset_not_active_vln_paper");
  }
  const candidates = (Array.isArray(representations) ? representations : representations.items).filter(
    (item) => item.availability === "available" && item.representation_type === "html" &&
      item.role === "guide" && item.attributes?.unified_guide === true &&
      item.attributes?.source_alias === values["source-id"],
  );
  if (candidates.length !== 1) throw new Error(`unified_guide_candidate_count:${candidates.length}`);
  if (candidates[0].attributes.sandbox_policy !== "static") throw new Error("guide_policy_not_static");
  return candidates[0].id;
}

async function freshGatewayUrl(representationId) {
  const action = await apiJson(`/api/v1/representations/${representationId}/open-actions`, {
    presentation: "original", embed: true,
  });
  const target = new URL(action.url);
  if (action.kind !== "gateway_session" || !target.searchParams.has("token") ||
      !["127.0.0.1", "localhost", "[::1]"].includes(target.hostname) ||
      !["http:", "https:"].includes(target.protocol) || target.username || target.password ||
      target.pathname !== `/session/${representationId}`) {
    throw new Error("open_action_not_local_signed_gateway_session");
  }
  return target;
}

function headerErrors(headers) {
  const directives = new Map((headers["content-security-policy"] ?? "").toLowerCase()
    .split(";").map((part) => part.trim().split(/\s+/)).map(([name, ...sources]) => [name, sources.join(" ")]));
  return [
    ...(directives.get("script-src") === "'none'" ? [] : ["csp_script_src_not_none"]),
    ...(directives.get("connect-src") === "'none'" ? [] : ["csp_connect_src_not_none"]),
    ...(headers["x-content-type-options"] === "nosniff" ? [] : ["missing_nosniff"]),
    ...(headers["referrer-policy"] === "no-referrer" ? [] : ["missing_no_referrer"]),
  ];
}

async function auditOne(browser, origin, assetId, viewport, representationId) {
  const result = { asset_id: assetId, representation_id: representationId ?? null,
    viewport: viewport.name, axe_ran: false, status: "failed", errors: [],
    console_errors: [], page_errors: [], request_failures: [], external_requests: [], accessibility: [],
    accessibility_counts: { critical: 0, serious: 0, moderate: 0, minor: 0, unknown: 0 } };
  let context;
  try {
    const target = gatewayMode ? await freshGatewayUrl(representationId) :
      new URL(`/papers/${assetId}/guide.html`, origin);
    context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height },
      serviceWorkers: "block" });
    const page = await context.newPage();
    page.setDefaultTimeout(30_000);
    page.on("console", (message) => {
      if (message.type() === "error") result.console_errors.push(safeMessage(message.text()));
    });
    page.on("pageerror", (error) => result.page_errors.push(safeMessage(error.message)));
    page.on("requestfailed", (request) => result.request_failures.push({
      resource_type: request.resourceType(), error: safeMessage(request.failure()?.errorText ?? "failed"),
    }));
    await context.route("**/*", async (route) => {
      const request = route.request();
      const requested = new URL(request.url());
      if (["http:", "https:"].includes(requested.protocol) && requested.origin !== target.origin) {
        result.external_requests.push({ resource_type: request.resourceType(), origin: requested.origin });
        await route.abort("blockedbyclient");
      } else await route.continue();
    });
    const response = await page.goto(target.href, { waitUntil: "networkidle", timeout: 30_000 });
    result.http_status = response?.status() ?? null;
    if (result.http_status !== 200) result.errors.push(`http_status:${result.http_status ?? "none"}`);
    const headers = response ? await response.allHeaders() : {};
    const securityErrors = headerErrors(headers);
    result.errors.push(...securityErrors);
    result.security_headers = { csp_static: securityErrors.length === 0,
      nosniff: headers["x-content-type-options"] === "nosniff",
      no_referrer: headers["referrer-policy"] === "no-referrer" };
    const layout = await page.evaluate(() => ({
      horizontalOverflow: document.documentElement.scrollWidth > window.innerWidth + 1,
      mainVisible: Boolean(document.querySelector("main")?.getBoundingClientRect().height),
      tocVisible: Boolean(document.querySelector("nav[aria-label='本文目录']")?.getBoundingClientRect().height),
    }));
    if (layout.horizontalOverflow) result.errors.push("horizontal_overflow");
    if (!layout.mainVisible) result.errors.push("main_not_visible");
    if (!layout.tocVisible) result.errors.push("toc_not_visible");
    if (viewport.axe) {
      const axe = await new AxeBuilder({ page }).analyze();
      result.axe_ran = true;
      result.accessibility = axe.violations
        .map((item) => ({ id: item.id, impact: item.impact, nodes: item.nodes.length }));
      for (const violation of result.accessibility) {
        result.accessibility_counts[violation.impact ?? "unknown"] += 1;
      }
    }
  } catch (error) {
    result.errors.push(`audit_exception:${safeMessage(error.message ?? error)}`);
  } finally {
    if (context) await context.close();
  }
  if (result.console_errors.length) result.errors.push("console_errors");
  if (result.page_errors.length) result.errors.push("page_errors");
  if (result.request_failures.length) result.errors.push("request_failures");
  if (result.external_requests.length) result.errors.push("external_active_requests");
  if (result.accessibility_counts.critical || result.accessibility_counts.serious) {
    result.errors.push("serious_accessibility_violations");
  }
  result.status = result.errors.length ? "failed" : "passed";
  return result;
}

async function pooledMap(items, action) {
  const results = new Array(items.length);
  let index = 0;
  await Promise.all(Array.from({ length: Math.min(workers, items.length) }, async () => {
    while (index < items.length) {
      const current = index++;
      results[current] = await action(items[current]);
    }
  }));
  return results;
}

let server;
let browser;
let results = [];
const setupErrors = [];
try {
  let origin;
  if (!gatewayMode) {
    server = createLocalServer();
    await new Promise((ready, reject) => { server.once("error", reject); server.listen(0, "127.0.0.1", ready); });
    const address = server.address();
    origin = `http://127.0.0.1:${address.port}`;
  }
  const selections = gatewayMode ? await pooledMap(assetIds, async (assetId) => {
    try { return { assetId, representationId: await selectGatewayGuide(assetId) }; }
    catch (error) { return { assetId, selectionError: safeMessage(error.message ?? error) }; }
  }) : assetIds.map((assetId) => ({ assetId }));
  browser = await chromium.launch({ headless: true });
  const jobs = viewports.flatMap((viewport) => selections.map((selection) => ({ viewport, ...selection })));
  results = await pooledMap(jobs, async ({ assetId, representationId, selectionError, viewport }) => {
    if (selectionError) return { asset_id: assetId, viewport: viewport.name, status: "failed",
      axe_ran: false, errors: [selectionError] };
    return auditOne(browser, origin, assetId, viewport, representationId);
  });
} catch (error) {
  setupErrors.push(safeMessage(error.message ?? error));
} finally {
  try {
    if (browser) await browser.close();
  } finally {
    if (server?.listening) {
      server.closeAllConnections();
      await new Promise((closed) => server.close(closed));
    }
  }
}
const failed = results.filter((item) => item.status !== "passed");
const accessibilityCounts = { critical: 0, serious: 0, moderate: 0, minor: 0, unknown: 0 };
for (const result of results) {
  for (const impact of Object.keys(accessibilityCounts)) {
    accessibilityCounts[impact] += result.accessibility_counts?.[impact] ?? 0;
  }
}
const report = {
  schema: "research-asset-hub.vln-guide-browser-audit/v2",
  mode: gatewayMode ? "signed_gateway" : "offline_local",
  scope: assetIds.length === allAssetIds.length ? "full_inventory" : "sample",
  status: setupErrors.length || failed.length || results.length !== assetIds.length * viewports.length ? "failed" : "passed",
  inventory_papers: allAssetIds.length, papers_requested: assetIds.length,
  asset_id_set_sha256: inventory.asset_id_set_sha256, source_id: values["source-id"] ?? null,
  workers, desktop_requested: assetIds.length, mobile_390_requested: assetIds.length,
  mobile_320_requested: assetIds.length, axe_requested: assetIds.length * 2,
  axe_completed: results.filter((item) => item.axe_ran).length,
  accessibility_blocking_impacts: ["critical", "serious"],
  accessibility_counts: accessibilityCounts,
  passed: results.length - failed.length, failed: failed.length, setup_errors: setupErrors, results,
};
mkdirSync(dirname(reportPath), { recursive: true });
writeFileSync(reportPath, `${JSON.stringify(report, null, 2)}\n`, "utf8");
console.log(JSON.stringify({ status: report.status, mode: report.mode, scope: report.scope,
  papers: report.papers_requested, passed: report.passed, failed: report.failed, report: reportPath }));
process.exitCode = report.status === "passed" ? 0 : 1;
