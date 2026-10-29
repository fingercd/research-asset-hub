import { preparePapers, filterPapers, paginate, pageNumbers } from "./catalog-core.js";

const $ = id => document.getElementById(id);
const controls = { q: $("search"), domain: $("domain"), venue: $("venue"), year: $("year"), sort: $("sort") };
const numberFormat = new Intl.NumberFormat("zh-CN");
let papers = [];
let labels = new Map();
let page = 1;
let ready = false;
let timer;

// All catalog text goes through textContent; never interpret titles as HTML.
function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function externalLink(url, text, className) {
  const anchor = element("a", className, text);
  anchor.href = url;
  anchor.target = "_blank";
  anchor.rel = "noopener noreferrer";
  return anchor;
}

function readFilters() {
  return Object.fromEntries(Object.entries(controls).map(([key, control]) => [key, control.value]));
}

function restoreLocation() {
  const params = new URLSearchParams(location.search);
  for (const [key, control] of Object.entries(controls)) {
    const value = params.get(key);
    if (value === null) continue;
    if (key === "q" || [...control.options].some(option => option.value === value)) control.value = value;
  }
  page = Number(params.get("page")) || 1;
}

function saveLocation() {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(readFilters())) {
    if (value && !(key === "sort" && value === "newest")) params.set(key, value);
  }
  if (page > 1) params.set("page", String(page));
  const query = params.toString();
  history.replaceState(null, "", `${location.pathname}${query ? `?${query}` : ""}${location.hash}`);
}

function addOption(select, value, label) {
  const option = element("option", "", label);
  option.value = value;
  select.append(option);
}

function createCard(paper, index) {
  const card = element("article", "paper-card");
  card.append(element("span", "paper-number", String(index + 1).padStart(3, "0")));
  const content = element("div", "paper-content");
  const meta = element("div", "paper-meta");
  meta.append(element("span", "paper-year", paper.year || "年份未标注"));
  if (paper.venue) {
    const separator = element("span", "meta-divider");
    separator.setAttribute("aria-hidden", "true");
    meta.append(separator, element("span", "paper-venue", paper.venue));
  }
  for (const domain of paper.domains) meta.append(element("span", "domain-tag", labels.get(domain) || domain));
  const title = element("h3", "paper-title");
  title.append(externalLink(paper.paper_urls[0], paper.title));
  content.append(meta, title);
  if (paper.title_zh && paper.title_zh !== paper.title) content.append(element("p", "paper-translation", paper.title_zh));
  const actions = element("div", "paper-actions");
  actions.setAttribute("aria-label", `${paper.title} 的外部链接`);
  actions.append(externalLink(paper.paper_urls[0], "原文 ↗", "paper-link"));
  paper.code_urls.forEach((url, i) => {
    const link = externalLink(url, `代码${paper.code_urls.length > 1 ? ` ${i + 1}` : ""} ↗`, "code-link");
    link.title = new URL(url).pathname.slice(1);
    link.setAttribute("aria-label", `代码：${link.title}（新窗口）`);
    actions.append(link);
  });
  if (!paper.code_urls.length) actions.append(element("span", "no-code", "未登记代码"));
  card.append(content, actions);
  return card;
}

function render({ scroll = false, save = true } = {}) {
  if (!ready) return;
  const filtered = filterPapers(papers, readFilters());
  const result = paginate(filtered, page);
  page = result.page;
  $("paper-list").replaceChildren(...result.items.map((paper, i) => createCard(paper, result.start + i)));
  $("empty-state").hidden = filtered.length !== 0;
  $("pagination").hidden = filtered.length === 0;
  $("result-summary").textContent = filtered.length
    ? `共 ${numberFormat.format(filtered.length)} 篇 · 显示 ${numberFormat.format(result.start + 1)}–${numberFormat.format(result.start + result.items.length)} 篇 · 第 ${page} / ${result.totalPages} 页`
    : "共 0 篇 · 未找到匹配结果";
  $("previous").disabled = page === 1;
  $("next").disabled = page === result.totalPages;
  $("page-buttons").replaceChildren(...pageNumbers(page, result.totalPages).map(value => {
    if (value === "…") return element("span", "ellipsis", "…");
    const button = element("button", "", value);
    button.type = "button";
    button.setAttribute("aria-label", `第 ${value} 页`);
    if (value === page) button.setAttribute("aria-current", "page");
    button.addEventListener("click", () => goToPage(value));
    return button;
  }));
  if (save) saveLocation();
  if (scroll) {
    $("catalog-heading").setAttribute("tabindex", "-1");
    $("catalog-heading").focus({ preventScroll: true });
    $("catalog").scrollIntoView({ block: "start", behavior: "instant" });
  }
}

function goToPage(nextPage) {
  page = nextPage;
  render({ scroll: true });
}

function clearFilters() {
  clearTimeout(timer);
  for (const [key, control] of Object.entries(controls)) control.value = key === "sort" ? "newest" : "";
  page = 1;
  render();
  controls.q.focus();
}

async function load() {
  ready = false;
  $("loading-state").hidden = false;
  $("error-state").hidden = true;
  $("paper-list").setAttribute("aria-busy", "true");
  try {
    const response = await fetch(new URL("./catalog.json", import.meta.url));
    if (!response.ok) throw new Error("Catalog fetch failed");
    const catalog = await response.json();
    if (catalog.schema !== "research-asset-hub.public-bibliography/v1" || !Array.isArray(catalog.papers)) throw new Error("Invalid catalog");
    labels = new Map(catalog.domains.map(domain => [domain.id, domain.label]));
    papers = preparePapers(catalog.papers, labels);
    for (const domain of catalog.domains) addOption(controls.domain, domain.id, domain.label);
    const venues = [...new Set(papers.map(paper => paper.venueKey))].sort((a, b) => a.localeCompare(b, "en"));
    for (const venue of venues) addOption(controls.venue, venue, venue);
    const years = [...new Set(papers.map(paper => paper.year).filter(Boolean))].sort((a, b) => b - a);
    for (const year of years) addOption(controls.year, String(year), `${year} 年`);
    if (papers.some(paper => !paper.year)) addOption(controls.year, "unknown", "年份未标注");
    $("snapshot-date").textContent = catalog.snapshot_date;
    $("snapshot-date").dateTime = catalog.snapshot_date;
    $("paper-count").textContent = numberFormat.format(papers.length);
    $("domain-count").textContent = numberFormat.format(catalog.domains.length);
    $("code-count").textContent = numberFormat.format(papers.filter(paper => paper.code_urls.length).length);
    restoreLocation();
    ready = true;
    $("loading-state").hidden = true;
    render();
  } catch (error) {
    $("loading-state").hidden = true;
    $("error-state").hidden = false;
    $("result-summary").textContent = "目录加载失败，请重试";
  } finally {
    $("paper-list").setAttribute("aria-busy", "false");
  }
}

$("filters").addEventListener("submit", event => event.preventDefault());
controls.q.addEventListener("input", () => {
  clearTimeout(timer);
  timer = setTimeout(() => { page = 1; render(); }, 140);
});
for (const [key, control] of Object.entries(controls)) {
  if (key !== "q") control.addEventListener("change", () => { page = 1; render(); });
}
$("clear").addEventListener("click", clearFilters);
$("empty-clear").addEventListener("click", clearFilters);
$("previous").addEventListener("click", () => goToPage(page - 1));
$("next").addEventListener("click", () => goToPage(page + 1));
$("retry").addEventListener("click", load);
window.addEventListener("popstate", () => {
  for (const [key, control] of Object.entries(controls)) control.value = key === "sort" ? "newest" : "";
  restoreLocation();
  render({ save: false });
});
document.addEventListener("keydown", event => {
  if (event.key === "/" && !event.ctrlKey && !event.metaKey && !event.altKey &&
      !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName) && !document.activeElement.isContentEditable) {
    event.preventDefault();
    controls.q.focus();
  }
});
load();
