import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { Search, SlidersHorizontal } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import {
  isResearchUnitSummary,
  type ReadingStatus,
  type SearchMode,
  type SearchSort,
} from "../api/types";
import { EmptyState, ErrorState, LoadingState } from "../components/PageState";
import { PageHeader } from "../components/PageHeader";
import { SelectionToolbar } from "../components/SelectionToolbar";
import { SavedSearchControls } from "../components/SavedSearchControls";
import { ResearchUnitCard } from "../components/ResearchUnitCard";
import "../styles/catalog-workspace.css";
import { researchUnitFromAsset } from "../lib/researchUnits";

const modeLabels: Record<SearchMode, string> = {
  keyword: "关键词检索",
  exact: "精确匹配",
};

const sortLabels: Record<SearchSort, string> = {
  date_desc: "时间：新到旧",
  date_asc: "时间：旧到新",
  relevance: "相关度",
};

export function SearchPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const q = searchParams.get("q") ?? "";
  const [selected, setSelected] = useState<string[]>([]);
  useEffect(() => setSelected([]), [searchParams]);
  const mode = (searchParams.get("mode") as SearchMode | null) ?? "keyword";
  const sortParam = searchParams.get("sort");
  const sort: SearchSort =
    sortParam === "date_asc" || sortParam === "relevance"
      ? sortParam
      : "date_desc";
  const domain = searchParams.get("domain") ?? "";
  const collectionId = searchParams.get("collection_id") ?? "";
  const status = (searchParams.get("status") as ReadingStatus | null) ?? "";
  const assetType = searchParams.get("type") ?? "";
  const author = searchParams.get("author") ?? "";
  const venue = searchParams.get("venue") ?? "";
  const year = searchParams.get("year") ?? "";
  const [density, setDensity] = useState<"list" | "card">("list");
  const tag = searchParams.get("tag") ?? "";
  const legacyYearFrom = searchParams.get("year_from") ?? "";
  const legacyYearTo = searchParams.get("year_to") ?? "";
  const dateFrom = searchParams.get("date_from") ?? "";
  const dateTo = searchParams.get("date_to") ?? "";
  const booleanFilter = (key: string) => {
    const value = searchParams.get(key);
    return value === "true" ? true : value === "false" ? false : undefined;
  };
  const hasPdf = booleanFilter("has_pdf");
  const hasHtml = booleanFilter("has_html");
  const hasCode = booleanFilter("has_code");
  const hasAnnotation = booleanFilter("has_annotation");
  const groupBy = assetType === "repository" ? "none" : "paper";
  const [input, setInput] = useState(q);
  const [advanced, setAdvanced] = useState({
    author,
    tag,
    venue,
    year,
  });

  useEffect(() => setInput(q), [q]);
  useEffect(() => setAdvanced({ author, tag, venue, year }), [author, tag, venue, year]);
  useEffect(() => {
    if (!searchParams.has("has_guide")) return;
    setSearchParams(
      (current) => {
        const next = new URLSearchParams(current);
        next.delete("has_guide");
        return next;
      },
      { replace: true },
    );
  }, [searchParams, setSearchParams]);

  const domains = useQuery({
    queryKey: ["domains"],
    queryFn: ({ signal }) => api.domains(signal),
  });
  const collections = useQuery({
    queryKey: ["collections"],
    queryFn: ({ signal }) => api.collections(signal),
  });
  const results = useInfiniteQuery({
    queryKey: [
      "search",
      q,
      mode,
      sort,
      domain,
      collectionId,
      status,
      assetType,
      author,
      tag,
      venue,
      year,
      dateFrom,
      dateTo,
      legacyYearFrom,
      legacyYearTo,
      hasPdf,
      hasHtml,
      hasCode,
      hasAnnotation,
      groupBy,
    ],
    queryFn: ({ pageParam, signal }) =>
      api.search(
        {
          q,
          mode,
          sort,
          group_by: groupBy,
          domain,
          collection_id: collectionId || undefined,
          status,
          type: assetType,
          author,
          tag,
          venue: venue || undefined,
          year: year ? Number(year) : undefined,
          year_from: legacyYearFrom ? Number(legacyYearFrom) : undefined,
          year_to: legacyYearTo ? Number(legacyYearTo) : undefined,
          date_from: dateFrom || undefined,
          date_to: dateTo || undefined,
          has_pdf: hasPdf,
          has_html: hasHtml,
          has_code: hasCode,
          has_annotation: hasAnnotation,
          cursor: pageParam,
        },
        signal,
      ),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
  });

  const updateFilter = (key: string, value: string) => {
    setSearchParams((current) => {
      const next = new URLSearchParams(current);
      if (value) {
        next.set(key, value);
      } else {
        next.delete(key);
      }
      return next;
    });
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const value = input.trim();
    setSearchParams((current) => {
      const next = new URLSearchParams(current);
      if (value) next.set("q", value);
      else next.delete("q");
      Object.entries({ author: advanced.author, tag: advanced.tag, venue: advanced.venue, year: advanced.year }).forEach(([key, text]) => {
        if (text.trim()) next.set(key, text.trim());
        else next.delete(key);
      });
      return next;
    });
  };

  const filterLabels: Record<string, string> = {
    q: "关键词", domain: "领域", collection_id: "收藏集合", status: "阅读状态",
    type: "类型", author: "作者", tag: "标签", venue: "会议/期刊", year: "发表年份",
    year_from: "起始年份", year_to: "结束年份", date_from: "起始公开日期", date_to: "结束公开日期",
    has_pdf: "有 PDF", has_html: "有 HTML", has_code: "有代码", has_annotation: "有批注", mode: "检索模式",
  };
  const activeFilters = Object.entries(filterLabels).flatMap(([key, label]) => {
    const value = searchParams.get(key);
    if (!value || (key.startsWith("has_") && value !== "true" && value !== "false") || (key === "mode" && value === "keyword")) return [];
    let display = value;
    if (key === "status") display = ({ unread: "未读", reading: "阅读中", read: "已读" })[value] ?? value;
    if (key === "type") display = ({ paper: "论文", repository: "代码仓库", dataset: "数据集" })[value] ?? value;
    if (key === "domain") display = domains.data?.items.find((item) => item.slug === value)?.label ?? value;
    if (key === "collection_id") { const item = collections.data?.items.find((item) => item.id === value); display = item?.label ?? item?.name ?? item?.title ?? value; }
    if (key === "mode") display = modeLabels[mode];
    return [{ key, label: key.startsWith("has_") ? (value === "false" ? label.replace(/^有/, "无") : label) : `${label}：${display}` }];
  });
  const advancedCount = [mode !== "keyword", collectionId, author, tag, dateFrom, dateTo, legacyYearFrom, legacyYearTo, hasPdf !== undefined, hasHtml !== undefined, hasCode !== undefined, hasAnnotation !== undefined].filter(Boolean).length;
  const [advancedOpen, setAdvancedOpen] = useState(false);

  const units =
    results.data?.pages
      .flatMap((page) => page.items)
      .map((item) =>
        isResearchUnitSummary(item) ? item : researchUnitFromAsset(item),
      ) ?? [];
  const total = results.data?.pages[0]?.total;

  return (
    <div className="page catalog-workspace">
      <PageHeader
        eyebrow="资料库"
        title={q ? `“${q}”的搜索结果` : "浏览资料库"}
        description="关键词匹配题名、摘要与论文标识；会议和年份按库内登记元数据筛选。"
      />

      <form className="search-workbench" onSubmit={submit} role="search">
        <div className="search-input-row">
          <Search size={19} aria-hidden="true" />
          <label className="sr-only" htmlFor="search-query">
            搜索关键词
          </label>
          <input
            id="search-query"
            type="search"
            value={input}
            onChange={(event) => setInput(event.target.value)}
            placeholder="例如：model-based reinforcement learning"
          />
          <button className="button primary" type="submit">
            搜索
          </button>
        </div>
        <div className="filter-row" aria-label="常用筛选">
          <label className={domain ? "is-active" : ""}>
            <span>领域</span>
            <select
              aria-label="领域"
              value={domain}
              onChange={(event) => updateFilter("domain", event.target.value)}
            >
              <option value="">全部领域</option>
              {domains.data?.items.map((item) => (
                <option value={item.slug} key={item.slug}>
                  {item.label ?? item.name ?? item.slug}
                </option>
              ))}
            </select>
          </label>
          <label className={assetType ? "is-active" : ""}>
            <span>类型</span>
            <select
              aria-label="资产类型"
              value={assetType}
              onChange={(event) => updateFilter("type", event.target.value)}
            >
              <option value="">全部类型</option>
              <option value="paper">论文</option>
              <option value="repository">代码仓库</option>
              <option value="dataset">数据集</option>
            </select>
          </label>
          <label className={status ? "is-active" : ""}>
            <span>阅读状态</span>
            <select
              aria-label="阅读状态"
              value={status}
              onChange={(event) => updateFilter("status", event.target.value)}
            >
              <option value="">全部阅读状态</option>
              <option value="unread">未读</option>
              <option value="reading">阅读中</option>
              <option value="read">已读</option>
            </select>
          </label>
          <label className={venue ? "is-active" : ""}>
            <span>会议 / 期刊</span>
            <input aria-label="会议或期刊" value={advanced.venue} placeholder="例如 CVPR（精确）"
              onChange={(event) => setAdvanced((current) => ({ ...current, venue: event.target.value }))} />
          </label>
          <label className={`year-filter ${year ? "is-active" : ""}`}>
            <span>发表年份</span>
            <input aria-label="发表年份" type="number" min="1450" max="2200" step="1" placeholder="全部"
              value={advanced.year} onChange={(event) => setAdvanced((current) => ({ ...current, year: event.target.value }))} />
          </label>
          <label className={sort !== "date_desc" ? "is-active" : ""}>
            <span>排序</span>
            <select
              aria-label="排序"
              value={sort}
              onChange={(event) => updateFilter("sort", event.target.value)}
            >
              {(Object.keys(sortLabels) as SearchSort[]).map((value) => (
                <option value={value} key={value}>
                  {sortLabels[value]}
                </option>
              ))}
            </select>
          </label>
        </div>
        <details
          className="advanced-filters"
          open={advancedOpen}
          onToggle={(event) => setAdvancedOpen(event.currentTarget.open)}
        >
          <summary>
            <SlidersHorizontal size={15} aria-hidden="true" />
            更多筛选
            {advancedCount > 0 && <span>{advancedCount}</span>}
          </summary>
          <div className="advanced-filter-grid">
          <fieldset
            className={`time-range-filter ${dateFrom || dateTo ? "is-active" : ""}`}
          >
            <legend>首次公开日期范围</legend>
            <div className="time-range-inputs">
              <label>
                <span className="sr-only">起始日期</span>
                <input
                  aria-label="起始日期"
                  type="date"
                  value={dateFrom}
                  max={dateTo || undefined}
                  onChange={(event) =>
                    updateFilter("date_from", event.target.value)
                  }
                />
              </label>
              <span aria-hidden="true">至</span>
              <label>
                <span className="sr-only">结束日期</span>
                <input
                  aria-label="结束日期"
                  type="date"
                  value={dateTo}
                  min={dateFrom || undefined}
                  onChange={(event) =>
                    updateFilter("date_to", event.target.value)
                  }
                />
              </label>
            </div>
          </fieldset>

            <label>
              <span>收藏集合</span>
              <select
                aria-label="收藏集合筛选"
                value={collectionId}
                onChange={(event) =>
                  updateFilter("collection_id", event.target.value)
                }
              >
                <option value="">全部资料</option>
                {collections.data?.items.map((item) => (
                  <option value={item.id} key={item.id}>
                    {item.label ?? item.name ?? item.title}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>检索模式</span>
              <select
                value={mode}
                onChange={(event) => updateFilter("mode", event.target.value)}
              >
                {(Object.keys(modeLabels) as SearchMode[]).map((value) => (
                  <option value={value} key={value}>
                    {modeLabels[value]}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>作者</span>
              <input
                value={advanced.author}
                onChange={(event) =>
                  setAdvanced((current) => ({
                    ...current,
                    author: event.target.value,
                  }))
                }
                placeholder="姓名或机构"
              />
            </label>
            <label>
              <span>标签</span>
              <input
                value={advanced.tag}
                onChange={(event) =>
                  setAdvanced((current) => ({
                    ...current,
                    tag: event.target.value,
                  }))
                }
                placeholder="单个标签"
              />
            </label>
          </div>
          <fieldset className="availability-filters">
            <legend>可用内容</legend>
            {[
              ["has_pdf", "PDF", hasPdf],
              ["has_code", "代码", hasCode],
              ["has_html", "HTML", hasHtml],
              ["has_annotation", "有批注", hasAnnotation],
            ].map(([key, label, checked]) => (
              <label key={String(key)}>
                <input
                  type="checkbox"
                  checked={Boolean(checked)}
                  onChange={(event) =>
                    updateFilter(
                      String(key),
                      event.target.checked ? "true" : "",
                    )
                  }
                />
                <span>{String(label)}</span>
              </label>
            ))}
          </fieldset>
          <div className="advanced-filter-actions">
            <button
              className="button primary compact"
              type="submit"
            >
              应用筛选
            </button>
          </div>
        </details>
      </form>

      {activeFilters.length > 0 && <div className="active-search-filters" aria-label="已应用筛选">
        {activeFilters.map(({ key, label }) => <button className="filter-chip" key={key} type="button"
          aria-label={`清除${label}`} onClick={() => updateFilter(key, "")}>
          {label}<span aria-hidden="true">×</span>
        </button>)}
        <button className="button ghost compact" type="button" onClick={() => setSearchParams((current) => {
          const next = new URLSearchParams(current);
          Object.keys(filterLabels).forEach((key) => next.delete(key));
          return next;
        })}>清除全部筛选</button>
      </div>}
      <SavedSearchControls
        definition={Object.fromEntries(searchParams.entries())}
      />
      <SelectionToolbar ids={selected} clear={() => setSelected([])} />
      {results.isPending ? (
        <LoadingState label={q ? `正在搜索“${q}”…` : "正在加载研究资产…"} />
      ) : results.isError && !units.length ? (
        <ErrorState
          error={results.error}
          retry={() => void results.refetch()}
        />
      ) : units.length ? (
        <section aria-label="搜索结果">
          <h2 className="sr-only">搜索结果</h2>
          <div className="result-summary">
            <div className="result-density" role="group" aria-label="结果显示方式">
              <button type="button" aria-pressed={density === "list"} onClick={() => setDensity("list")}>紧凑列表</button>
              <button type="button" aria-pressed={density === "card"} onClick={() => setDensity("card")}>详细卡片</button>
            </div>
            <label className="check-label">
              <input
                type="checkbox"
                checked={
                  units.length > 0 &&
                  units.every((u) => selected.includes(u.primary_asset.id))
                }
                onChange={(e) =>
                  setSelected(
                    e.target.checked
                      ? units.map((u) => u.primary_asset.id)
                      : [],
                  )
                }
              />
              <span>选择当前已加载结果</span>
            </label>
            <div>
              <strong>
                {typeof total === "number" ? total : units.length}
              </strong>
              <span>{groupBy === "paper" ? " 个研究单元" : " 条代码仓库"}</span>
              <small>
                {q ? `“${q}”` : "全部资产"} · {modeLabels[mode]} ·{" "}
                {sortLabels[sort]}
              </small>
            </div>
          </div>
          <div className={`asset-list catalog-results is-${density}`}>
            {units.map((unit) => (
              <div className="selectable-asset" key={unit.unit_id}>
                <label className="asset-select">
                  <input
                    type="checkbox"
                    checked={selected.includes(unit.primary_asset.id)}
                    onChange={(e) =>
                      setSelected((current) =>
                        e.target.checked
                          ? [...current, unit.primary_asset.id]
                          : current.filter(
                              (id) => id !== unit.primary_asset.id,
                            ),
                      )
                    }
                  />
                  <span className="sr-only">
                    选择 {unit.primary_asset.title}
                  </span>
                </label>
                <ResearchUnitCard unit={unit} compact={density === "list"} />
              </div>
            ))}
          </div>
          {results.isError && results.data && <div className="pagination-error" role="alert">
            <p>{results.isFetchNextPageError ? "下一页加载失败，已加载的结果和选择仍然保留。" : "刷新失败，仍显示上次加载的结果。"}</p>
            <button className="button secondary compact" type="button" disabled={results.isFetching}
              onClick={() => void (results.isFetchNextPageError ? results.fetchNextPage() : results.refetch())}>
              {results.isFetchNextPageError ? "重试加载更多" : "重试刷新"}
            </button>
          </div>}
          {results.hasNextPage && !results.isFetchNextPageError && (
            <div className="load-more">
              <button
                className="button secondary"
                type="button"
                disabled={results.isFetchingNextPage}
                onClick={() => void results.fetchNextPage()}
              >
                {results.isFetchingNextPage ? "正在加载…" : "加载更多"}
              </button>
            </div>
          )}
        </section>
      ) : (
        <EmptyState
          title="没有找到匹配资产"
          detail="尝试减少筛选条件、切换精确匹配，或换用更宽泛的关键词。"
          kind="search"
        />
      )}
    </div>
  );
}
