// Pure search / pagination helpers, shared by the browser and regression tests.
export const PAGE_SIZE = 20;

export function venueKey(venue) {
  return venue.replace(/\b(?:19|20)\d{2}\b/g, "").replace(/\s+/g, " ").trim() || "未标注会议 / 期刊";
}

export function preparePapers(papers, domainLabels) {
  return papers.map(paper => ({
    ...paper,
    venueKey: venueKey(paper.venue),
    searchText: [paper.title, paper.title_zh, paper.venue, paper.year,
      ...paper.domains.map(id => domainLabels.get(id) || id), ...paper.domains]
      .join(" ").normalize("NFKC").toLocaleLowerCase(),
  }));
}

export function filterPapers(papers, filters) {
  const tokens = filters.q.normalize("NFKC").toLocaleLowerCase().trim().split(/\s+/).filter(Boolean);
  const result = papers.filter(paper =>
    tokens.every(token => paper.searchText.includes(token)) &&
    (!filters.domain || paper.domains.includes(filters.domain)) &&
    (!filters.venue || paper.venueKey === filters.venue) &&
    (!filters.year || String(paper.year || "unknown") === filters.year));
  return result.sort((a, b) => {
    const titleOrder = a.title.localeCompare(b.title, "en", { sensitivity: "base" });
    if (filters.sort === "title") return titleOrder;
    if (a.year === null && b.year !== null) return 1;
    if (b.year === null && a.year !== null) return -1;
    const difference = (a.year || 0) - (b.year || 0);
    return (filters.sort === "oldest" ? difference : -difference) || titleOrder;
  });
}

export function paginate(items, requestedPage, pageSize = PAGE_SIZE) {
  const totalPages = Math.max(1, Math.ceil(items.length / pageSize));
  const page = Math.max(1, Math.min(totalPages, Math.trunc(Number(requestedPage)) || 1));
  const start = (page - 1) * pageSize;
  return { page, totalPages, start, items: items.slice(start, start + pageSize) };
}

export function pageNumbers(page, totalPages) {
  const selected = new Set([1, totalPages, page - 1, page, page + 1]);
  const numbers = [...selected].filter(n => n > 0 && n <= totalPages).sort((a, b) => a - b);
  const result = [];
  for (const number of numbers) {
    if (result.length && number - result[result.length - 1] > 1) result.push("…");
    result.push(number);
  }
  return result;
}
