import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { Database, Search, SlidersHorizontal } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import type { ReadingStatus, SearchMode } from "../api/types";
import { AssetCard } from "../components/AssetCard";
import { EmptyState, ErrorState, LoadingState } from "../components/PageState";
import { PageHeader } from "../components/PageHeader";

const modeLabels: Record<SearchMode, string> = {
  hybrid: "混合检索",
  exact: "精确匹配",
  semantic: "语义检索",
};

export function SearchPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const q = searchParams.get("q") ?? "";
  const mode = (searchParams.get("mode") as SearchMode | null) ?? "hybrid";
  const domain = searchParams.get("domain") ?? "";
  const status = (searchParams.get("status") as ReadingStatus | null) ?? "";
  const assetType = searchParams.get("type") ?? "";
  const author = searchParams.get("author") ?? "";
  const tag = searchParams.get("tag") ?? "";
  const yearFrom = searchParams.get("year_from") ?? "";
  const yearTo = searchParams.get("year_to") ?? "";
  const hasPdf = searchParams.get("has_pdf") === "true";
  const hasHtml = searchParams.get("has_html") === "true";
  const hasCode = searchParams.get("has_code") === "true";
  const hasGuide = searchParams.get("has_guide") === "true";
  const hasAnnotation = searchParams.get("has_annotation") === "true";
  const [input, setInput] = useState(q);
  const [advanced, setAdvanced] = useState({
    author,
    tag,
    yearFrom,
    yearTo,
  });

  useEffect(() => setInput(q), [q]);
  useEffect(
    () => setAdvanced({ author, tag, yearFrom, yearTo }),
    [author, tag, yearFrom, yearTo],
  );

  const domains = useQuery({
    queryKey: ["domains"],
    queryFn: ({ signal }) => api.domains(signal),
  });
  const results = useInfiniteQuery({
    queryKey: [
      "search",
      q,
      mode,
      domain,
      status,
      assetType,
      author,
      tag,
      yearFrom,
      yearTo,
      hasPdf,
      hasHtml,
      hasCode,
      hasGuide,
      hasAnnotation,
    ],
    queryFn: ({ pageParam, signal }) =>
      api.search(
        {
          q,
          mode,
          domain,
          status,
          type: assetType,
          author,
          tag,
          year_from: yearFrom ? Number(yearFrom) : undefined,
          year_to: yearTo ? Number(yearTo) : undefined,
          has_pdf: hasPdf || undefined,
          has_html: hasHtml || undefined,
          has_code: hasCode || undefined,
          has_guide: hasGuide || undefined,
          has_annotation: hasAnnotation || undefined,
          cursor: pageParam,
        },
        signal,
      ),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    enabled: Boolean(q.trim()),
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
      if (value) {
        next.set("q", value);
      } else {
        next.delete("q");
      }
      return next;
    });
  };

  const applyAdvanced = () => {
    const values = {
      author: advanced.author.trim(),
      tag: advanced.tag.trim(),
      year_from: advanced.yearFrom.trim(),
      year_to: advanced.yearTo.trim(),
    };
    setSearchParams((current) => {
      const next = new URLSearchParams(current);
      Object.entries(values).forEach(([key, value]) => {
        if (value) next.set(key, value);
        else next.delete(key);
      });
      return next;
    });
  };

  const clearAdvanced = () => {
    setSearchParams((current) => {
      const next = new URLSearchParams(current);
      [
        "author",
        "tag",
        "type",
        "year_from",
        "year_to",
        "has_pdf",
        "has_html",
        "has_code",
        "has_guide",
        "has_annotation",
      ].forEach((key) => next.delete(key));
      return next;
    });
  };

  const advancedActive = Boolean(
    assetType ||
      author ||
      tag ||
      yearFrom ||
      yearTo ||
      hasPdf ||
      hasHtml ||
      hasCode ||
      hasGuide ||
      hasAnnotation,
  );

  const assets = results.data?.pages.flatMap((page) => page.items) ?? [];
  const total = results.data?.pages[0]?.total;

  return (
    <div className="page">
      <PageHeader
        eyebrow="统一检索"
        title="跨领域搜索"
        description="在论文、代码、导读和批注中查找证据；可切换精确、混合或语义模式。"
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
        <div className="filter-row">
          <span>
            <SlidersHorizontal size={15} aria-hidden="true" />
            筛选
          </span>
          <label>
            <span className="sr-only">检索模式</span>
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
            <span className="sr-only">领域</span>
            <select
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
          <label>
            <span className="sr-only">阅读状态</span>
            <select
              value={status}
              onChange={(event) => updateFilter("status", event.target.value)}
            >
              <option value="">全部阅读状态</option>
              <option value="unread">未读</option>
              <option value="reading">阅读中</option>
              <option value="read">已读</option>
            </select>
          </label>
          <label>
            <span className="sr-only">资产类型</span>
            <select
              value={assetType}
              onChange={(event) => updateFilter("type", event.target.value)}
            >
              <option value="">全部类型</option>
              <option value="paper">论文</option>
              <option value="repository">代码仓库</option>
              <option value="dataset">数据集</option>
            </select>
          </label>
        </div>
        <details className="advanced-filters" open={advancedActive || undefined}>
          <summary>更多筛选{advancedActive ? "（已启用）" : ""}</summary>
          <div className="advanced-filter-grid">
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
            <label>
              <span>起始年份</span>
              <input
                type="number"
                min="1450"
                max="2200"
                value={advanced.yearFrom}
                onChange={(event) =>
                  setAdvanced((current) => ({
                    ...current,
                    yearFrom: event.target.value,
                  }))
                }
              />
            </label>
            <label>
              <span>结束年份</span>
              <input
                type="number"
                min="1450"
                max="2200"
                value={advanced.yearTo}
                onChange={(event) =>
                  setAdvanced((current) => ({
                    ...current,
                    yearTo: event.target.value,
                  }))
                }
              />
            </label>
          </div>
          <fieldset className="availability-filters">
            <legend>可用内容</legend>
            {[
              ["has_pdf", "PDF", hasPdf],
              ["has_code", "代码", hasCode],
              ["has_guide", "导读", hasGuide],
              ["has_html", "HTML", hasHtml],
              ["has_annotation", "有批注", hasAnnotation],
            ].map(([key, label, checked]) => (
              <label key={String(key)}>
                <input
                  type="checkbox"
                  checked={Boolean(checked)}
                  onChange={(event) =>
                    updateFilter(String(key), event.target.checked ? "true" : "")
                  }
                />
                <span>{String(label)}</span>
              </label>
            ))}
          </fieldset>
          <div className="advanced-filter-actions">
            <button
              className="button primary compact"
              type="button"
              onClick={applyAdvanced}
            >
              应用更多筛选
            </button>
            {advancedActive && (
              <button
                className="button ghost compact"
                type="button"
                onClick={clearAdvanced}
              >
                清除更多筛选
              </button>
            )}
          </div>
        </details>
      </form>

      {!q ? (
        <EmptyState
          title="输入关键词开始检索"
          detail="支持标题、作者、摘要、正文、代码说明和个人批注。按 Ctrl+K 可在任意页面快速搜索。"
          kind="search"
        />
      ) : results.isPending ? (
        <LoadingState label={`正在搜索“${q}”…`} />
      ) : results.isError ? (
        <ErrorState error={results.error} retry={() => void results.refetch()} />
      ) : assets.length ? (
        <section aria-label="搜索结果">
          <h2 className="sr-only">搜索结果</h2>
          <div className="result-summary">
            <div>
              <strong>{typeof total === "number" ? total : assets.length}</strong>
              <span> 条结果</span>
              <small>
                “{q}” · {modeLabels[mode]}
              </small>
            </div>
            {mode === "semantic" && (
              <span className="index-chip">
                <Database size={13} aria-hidden="true" />
                向量索引
              </span>
            )}
          </div>
          <div className="asset-list">
            {assets.map((asset) => (
              <AssetCard asset={asset} key={asset.id} />
            ))}
          </div>
          {results.hasNextPage && (
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
          detail="尝试减少筛选条件、切换混合检索，或换用更宽泛的关键词。"
          kind="search"
        />
      )}
    </div>
  );
}
