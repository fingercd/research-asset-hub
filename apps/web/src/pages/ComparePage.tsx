import {
  useMutation,
  useQueries,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  Download,
  GitCompareArrows,
  History,
  Plus,
  Search,
  X,
} from "lucide-react";
import { useMemo, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api, getApiErrorMessage } from "../api/client";
import {
  getAssetDomains,
  getRepresentationType,
  type Annotation,
  type Asset,
  type Comparison,
  type GraphResponse,
} from "../api/types";
import { EmptyState, ErrorState } from "../components/PageState";
import { PageHeader } from "../components/PageHeader";
import {
  annotationBody,
  annotationSnippet,
  formatAuthors,
  safeJson,
} from "../lib/format";
import { useCompare } from "../state/CompareContext";

interface ComparisonContext {
  annotations: Annotation[];
  graph: GraphResponse;
}

interface ComparisonRow {
  key: string;
  label: string;
  values: Array<string | number>;
}

function structured(asset: Asset, keys: string[]): unknown {
  for (const source of [asset.analysis, asset.attributes]) {
    if (!source) continue;
    for (const key of keys) {
      const value = source[key];
      if (value !== undefined && value !== null && value !== "") return value;
    }
  }
  return undefined;
}

function readable(value: unknown, fallback = "无"): string {
  if (value === undefined || value === null || value === "") return fallback;
  if (Array.isArray(value)) {
    return value.length ? value.map((item) => readable(item, "")).join("；") : fallback;
  }
  if (typeof value === "object") return safeJson(value);
  return String(value);
}

function codeSummary(asset: Asset): string {
  const code = asset.representations?.filter((item) =>
    ["repository", "source_code", "notebook"].includes(getRepresentationType(item)),
  );
  if (code?.length) {
    return code
      .map(
        (item) =>
          item.label ??
          item.external_url ??
          item.path_ref ??
          getRepresentationType(item),
      )
      .join("；");
  }
  const source = asset.source_urls?.find((url) =>
    /github|gitlab|gitee|codeberg/i.test(url),
  );
  return source ?? "无";
}

function annotationSummary(
  asset: Asset,
  context?: ComparisonContext,
): string {
  const notes = context?.annotations
    .map((item) => item.summary || annotationBody(item))
    .filter(Boolean);
  return notes?.length ? notes.join("；") : annotationSnippet(asset);
}

function relationSummary(
  asset: Asset,
  context?: ComparisonContext,
): string {
  if (!context) return readable(structured(asset, ["relations", "relation_summary"]));
  const nodes = context.graph.nodes ?? [];
  const edges = context.graph.edges ?? [];
  const labels = nodes
    .filter((node) => node.id !== asset.id)
    .slice(0, 6)
    .map((node) => node.title ?? node.label ?? node.id);
  if (!edges.length && !labels.length) return "无";
  return `${edges.length} 条关系${labels.length ? `：${labels.join("、")}` : ""}`;
}

function mergeAssets(comparison: Comparison, fallback: Asset[]): Asset[] {
  const serverAssets = comparison.assets?.length ? comparison.assets : fallback;
  const fallbackById = new Map(fallback.map((asset) => [asset.id, asset]));
  return serverAssets.map((asset) => {
    const local = fallbackById.get(asset.id);
    return {
      ...local,
      ...asset,
      representations: asset.representations ?? local?.representations,
      annotation_summary: asset.annotation_summary ?? local?.annotation_summary,
    };
  });
}

function comparisonRows(
  comparison: Comparison,
  assets: Asset[],
  contexts: Map<string, ComparisonContext>,
): ComparisonRow[] {
  const rows: ComparisonRow[] = [
    {
      key: "title",
      label: "标题",
      values: assets.map((asset) => asset.title_zh || asset.title),
    },
    {
      key: "authors",
      label: "作者",
      values: assets.map((asset) => formatAuthors(asset.authors)),
    },
    {
      key: "year",
      label: "年份 / 发表场所",
      values: assets.map(
        (asset) => [asset.year, asset.venue].filter(Boolean).join(" · ") || "无",
      ),
    },
    {
      key: "problem",
      label: "研究问题",
      values: assets.map((asset) =>
        readable(
          structured(asset, [
            "research_question",
            "problem",
            "problem_statement",
            "motivation",
          ]),
          asset.summary || asset.abstract || "无",
        ),
      ),
    },
    {
      key: "mechanism",
      label: "机制 / 方法",
      values: assets.map((asset) =>
        readable(
          structured(asset, ["mechanism", "method", "approach", "methodology"]),
        ),
      ),
    },
    {
      key: "contribution",
      label: "主要贡献",
      values: assets.map((asset) =>
        readable(
          structured(asset, ["contributions", "contribution", "novelty"]),
          asset.summary || "无",
        ),
      ),
    },
    {
      key: "experiments",
      label: "实验与结果",
      values: assets.map((asset) =>
        readable(
          structured(asset, [
            "experiments",
            "evaluation",
            "results",
            "benchmarks",
          ]),
        ),
      ),
    },
    {
      key: "limitations",
      label: "限制",
      values: assets.map((asset) =>
        readable(
          structured(asset, ["limitations", "limitation", "weaknesses"]),
        ),
      ),
    },
    {
      key: "code",
      label: "代码",
      values: assets.map(codeSummary),
    },
    {
      key: "annotations",
      label: "个人批注",
      values: assets.map((asset) =>
        annotationSummary(asset, contexts.get(asset.id)),
      ),
    },
    {
      key: "relations",
      label: "关系",
      values: assets.map((asset) =>
        relationSummary(asset, contexts.get(asset.id)),
      ),
    },
    {
      key: "domains",
      label: "领域 / 标签",
      values: assets.map((asset) => {
        const values = [...getAssetDomains(asset), ...(asset.tags ?? [])];
        return values.length ? values.join("、") : "无";
      }),
    },
  ];

  const known = new Set(rows.map((row) => row.key));
  for (const column of comparison.columns ?? []) {
    const key = column.key ?? column.label ?? "field";
    if (known.has(key)) continue;
    rows.push({
      key,
      label: column.label ?? column.key ?? "其他字段",
      values: assets.map((asset) => readable(column.values?.[asset.id])),
    });
  }
  return rows;
}

function markdownTable(
  title: string,
  assets: Asset[],
  rows: ComparisonRow[],
): string {
  const escape = (value: string | number) =>
    String(value).replaceAll("|", "\\|").replace(/\r?\n/g, "<br>");
  const header = `| 对比维度 | ${assets
    .map((asset) => escape(asset.title_zh || asset.title))
    .join(" | ")} |`;
  const separator = `| --- | ${assets.map(() => "---").join(" | ")} |`;
  const body = rows.map(
    (row) =>
      `| ${escape(row.label)} | ${row.values.map(escape).join(" | ")} |`,
  );
  return [`# ${title}`, "", header, separator, ...body, ""].join("\n");
}

function downloadMarkdown(title: string, content: string) {
  const blob = new Blob([content], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `${title.replace(/[\\/:*?"<>|]/g, "-").slice(0, 80) || "论文对比"}.md`;
  anchor.click();
  URL.revokeObjectURL(url);
}

function ComparisonTable({
  comparison,
  fallback,
  contexts,
}: {
  comparison: Comparison;
  fallback: Asset[];
  contexts: Map<string, ComparisonContext>;
}) {
  const assets = mergeAssets(comparison, fallback);
  const rows = comparisonRows(comparison, assets, contexts);

  return (
    <div className="comparison-result">
      {comparison.synthesis && (
        <section className="summary-callout">
          <GitCompareArrows size={19} aria-hidden="true" />
          <p>{comparison.synthesis}</p>
        </section>
      )}
      <div className="comparison-table-wrap">
        <table className="comparison-table">
          <thead>
            <tr>
              <th scope="col">对比维度</th>
              {assets.map((asset) => (
                <th scope="col" key={asset.id}>
                  <Link to={`/assets/${encodeURIComponent(asset.id)}`}>
                    {asset.title_zh || asset.title}
                  </Link>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.key}>
                <th scope="row">{row.label}</th>
                {row.values.map((value, index) => (
                  <td key={assets[index]?.id ?? index}>{value}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export function ComparePage() {
  const compare = useCompare();
  const queryClient = useQueryClient();
  const [searchInput, setSearchInput] = useState("");
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<Comparison | null>(null);
  const search = useQuery({
    queryKey: ["search", "compare", query],
    queryFn: ({ signal }) => api.search({ q: query, mode: "hybrid" }, signal),
    enabled: Boolean(query),
  });
  const saved = useQuery({
    queryKey: ["comparisons"],
    queryFn: ({ signal }) => api.comparisons(signal),
  });
  const create = useMutation({
    mutationFn: () => api.createComparison(compare.assets.map((asset) => asset.id)),
    onSuccess: (value) => {
      setResult(value);
      void queryClient.invalidateQueries({ queryKey: ["comparisons"] });
    },
  });
  const displayedAssets = useMemo(
    () => (result ? mergeAssets(result, compare.assets) : compare.assets),
    [compare.assets, result],
  );
  const contextQueries = useQueries({
    queries: displayedAssets.map((asset) => ({
      queryKey: ["comparison-context", asset.id],
      queryFn: async ({ signal }: { signal: AbortSignal }) => {
        const [annotations, graph] = await Promise.all([
          api.annotations(asset.id, signal),
          api.graphNeighbors(asset.id, 1, signal),
        ]);
        return { annotations: annotations.items, graph };
      },
      staleTime: 30_000,
      retry: false,
    })),
  });
  const contexts = useMemo(
    () =>
      new Map(
        displayedAssets.flatMap((asset, index) => {
          const context = contextQueries[index]?.data;
          return context ? [[asset.id, context] as const] : [];
        }),
      ),
    [contextQueries, displayedAssets],
  );

  const submitSearch = (event: FormEvent) => {
    event.preventDefault();
    setQuery(searchInput.trim());
  };

  const exportCurrent = () => {
    if (!result) return;
    const assets = mergeAssets(result, compare.assets);
    const rows = comparisonRows(result, assets, contexts);
    const title = result.title ?? "论文对比";
    downloadMarkdown(title, markdownTable(title, assets, rows));
  };

  return (
    <div className="page">
      <PageHeader
        eyebrow="并排阅读"
        title="论文对比"
        description="选择 2–4 篇论文，对照研究问题、机制、贡献、实验、限制、代码、批注与关系。"
      />

      <section className="section-block compare-selection">
        <div className="section-heading">
          <div>
            <p className="eyebrow">已选择</p>
            <h2>{compare.assets.length}/4 篇论文</h2>
          </div>
          {compare.assets.length > 0 && (
            <button
              className="button ghost compact"
              type="button"
              onClick={compare.clear}
            >
              清空
            </button>
          )}
        </div>
        {compare.assets.length ? (
          <div className="selected-paper-grid">
            {compare.assets.map((asset, index) => (
              <article key={asset.id}>
                <span>{index + 1}</span>
                <div>
                  <strong>{asset.title_zh || asset.title}</strong>
                  <small>
                    {[asset.year, formatAuthors(asset.authors)]
                      .filter(Boolean)
                      .join(" · ")}
                  </small>
                </div>
                <button
                  className="icon-button"
                  type="button"
                  onClick={() => compare.remove(asset.id)}
                >
                  <X size={15} aria-hidden="true" />
                  <span className="sr-only">移除</span>
                </button>
              </article>
            ))}
          </div>
        ) : (
          <EmptyState
            title="先选择论文"
            detail="可从搜索结果加入对比，或在下方检索并添加。"
          />
        )}

        <form className="compare-search" onSubmit={submitSearch}>
          <Search size={16} aria-hidden="true" />
          <label className="sr-only" htmlFor="compare-search">
            查找要对比的论文
          </label>
          <input
            id="compare-search"
            type="search"
            value={searchInput}
            onChange={(event) => setSearchInput(event.target.value)}
            placeholder="查找要加入对比的论文…"
          />
          <button className="button secondary compact" type="submit">
            查找
          </button>
        </form>
        {search.isFetching && <span className="muted">正在搜索…</span>}
        {search.isError && (
          <span className="inline-error">
            {getApiErrorMessage(search.error)}
          </span>
        )}
        {!!search.data?.items.length && (
          <div className="compare-search-results">
            {search.data.items
              .filter(
                (asset) => !asset.asset_type || asset.asset_type === "paper",
              )
              .slice(0, 6)
              .map((asset) => {
                const selected = compare.has(asset.id);
                return (
                  <div key={asset.id}>
                    <span>
                      <strong>{asset.title_zh || asset.title}</strong>
                      <small>{asset.year ?? "年份未知"}</small>
                    </span>
                    <button
                      className="button ghost compact"
                      type="button"
                      disabled={selected || compare.assets.length >= 4}
                      onClick={() => compare.add(asset)}
                    >
                      <Plus size={14} aria-hidden="true" />
                      {selected ? "已添加" : "添加"}
                    </button>
                  </div>
                );
              })}
          </div>
        )}

        <div className="form-actions">
          {create.isError && (
            <span className="inline-error">
              {getApiErrorMessage(create.error)}
            </span>
          )}
          <button
            className="button primary"
            type="button"
            disabled={
              compare.assets.length < 2 ||
              compare.assets.length > 4 ||
              create.isPending
            }
            onClick={() => create.mutate()}
          >
            <GitCompareArrows size={16} aria-hidden="true" />
            {create.isPending ? "正在保存…" : "保存并生成对比"}
          </button>
        </div>
      </section>

      {saved.isPending ? null : saved.isError ? (
        <ErrorState error={saved.error} retry={() => void saved.refetch()} />
      ) : saved.data.items.length ? (
        <details className="section-block saved-comparisons">
          <summary>
            <History size={16} aria-hidden="true" />
            已保存对比（{saved.data.items.length}）
          </summary>
          <div>
            {saved.data.items.map((comparison) => (
              <button
                type="button"
                onClick={() => setResult(comparison)}
                key={comparison.id ?? comparison.title}
              >
                <strong>{comparison.title ?? "未命名对比"}</strong>
                <span>{comparison.asset_ids?.length ?? 0} 篇论文</span>
              </button>
            ))}
          </div>
        </details>
      ) : null}

      {result ? (
        <section className="section-block">
          <div className="section-heading">
            <div>
              <p className="eyebrow">已保存</p>
              <h2>{result.title ?? "论文异同"}</h2>
            </div>
            <button
              className="button secondary"
              type="button"
              onClick={exportCurrent}
            >
              <Download size={15} aria-hidden="true" />
              导出 Markdown
            </button>
          </div>
          {contextQueries.some((item) => item.isPending) && (
            <p className="muted">正在补充批注与关系上下文…</p>
          )}
          <ComparisonTable
            comparison={result}
            fallback={compare.assets}
            contexts={contexts}
          />
        </section>
      ) : (
        <EmptyState
          title="对比结果将在这里显示"
          detail={
            compare.assets.length < 2
              ? "至少选择 2 篇论文。"
              : "点击“保存并生成对比”开始分析。"
          }
        />
      )}
    </div>
  );
}
