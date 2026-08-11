import { useQuery } from "@tanstack/react-query";
import cytoscape from "cytoscape";
import {
  ArrowRight,
  GitCompareArrows,
  Network,
  Route,
  X,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type FormEvent,
} from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api/client";
import type {
  Asset,
  GraphEdge,
  GraphNode,
  GraphResponse,
} from "../api/types";
import { GraphNodeDrawer } from "../components/GraphNodeDrawer";
import { EmptyState, ErrorState, LoadingState } from "../components/PageState";
import { PageHeader } from "../components/PageHeader";
import { useCompare } from "../state/CompareContext";

function graphParts(data?: GraphResponse) {
  const flexible = data as
    | (GraphResponse & {
        assets?: GraphNode[];
        relations?: GraphEdge[];
        path?: GraphNode[];
      })
    | undefined;
  return {
    nodes: flexible?.nodes ?? flexible?.assets ?? flexible?.path ?? [],
    edges: flexible?.edges ?? flexible?.relations ?? [],
  };
}

function edgeEnds(edge: GraphEdge) {
  return {
    source: edge.source ?? edge.subject_asset_id ?? "",
    target: edge.target ?? edge.object_asset_id ?? "",
  };
}

function assetFromNode(node: GraphNode): Asset {
  return {
    id: node.id,
    asset_type: node.asset_type,
    title: node.title ?? node.label ?? node.id,
    year: node.year,
    domain_slugs: node.domain_slugs,
    reading_status: node.reading_status,
  };
}

function GraphCanvas({
  data,
  compareIds,
  onSelectNode,
  onToggleCompare,
}: {
  data: GraphResponse;
  compareIds: readonly string[];
  onSelectNode: (id: string) => void;
  onToggleCompare: (id: string) => void;
}) {
  const { nodes, edges } = graphParts(data);
  const containerRef = useRef<HTMLDivElement>(null);
  const instanceRef = useRef<cytoscape.Core | null>(null);
  const selectedSetRef = useRef(new Set(compareIds));

  useEffect(() => {
    selectedSetRef.current = new Set(compareIds);
  }, [compareIds]);

  useEffect(() => {
    if (!containerRef.current) return;
    const nodeIds = new Set(nodes.slice(0, 1_000).map((node) => node.id));
    const instance = cytoscape({
      container: containerRef.current,
      elements: [
        ...nodes.slice(0, 1_000).map((node, index) => ({
          data: {
            id: node.id,
            label: (node.title ?? node.label ?? node.id).slice(0, 36),
            type: node.asset_type ?? "asset",
            status: node.reading_status ?? "unread",
            root: index === 0 ? "true" : "false",
            compare: selectedSetRef.current.has(node.id) ? "true" : "false",
          },
        })),
        ...edges.flatMap((edge, index) => {
          const ends = edgeEnds(edge);
          if (!nodeIds.has(ends.source) || !nodeIds.has(ends.target)) return [];
          return [
            {
              data: {
                id: edge.id ?? `${ends.source}-${ends.target}-${index}`,
                source: ends.source,
                target: ends.target,
                label: edge.label ?? edge.relation_type ?? "related",
              },
            },
          ];
        }),
      ],
      style: [
        {
          selector: "node",
          style: {
            "background-color": "#ffffff",
            "border-color": "#4f8a6d",
            "border-width": 2,
            color: "#263a31",
            label: "data(label)",
            "font-family": "Segoe UI, Microsoft YaHei UI, sans-serif",
            "font-size": 9,
            "text-max-width": "110px",
            "text-wrap": "ellipsis",
            "text-valign": "bottom",
            "text-margin-y": 7,
            width: 36,
            height: 36,
          },
        },
        {
          selector: 'node[root = "true"]',
          style: {
            "background-color": "#cfe5d9",
            "border-color": "#246b53",
            "border-width": 3,
            width: 48,
            height: 48,
          },
        },
        {
          selector: 'node[status = "read"]',
          style: { "background-color": "#e2eee8" },
        },
        {
          selector: 'node[status = "reading"]',
          style: { "background-color": "#fff0c9" },
        },
        {
          selector: 'node[compare = "true"]',
          style: {
            "background-color": "#d7e6f8",
            "border-color": "#2d7ed3",
            "border-width": 3,
            width: 44,
            height: 44,
          },
        },
        {
          selector: "edge",
          style: {
            width: 1.5,
            "line-color": "#a8b6ad",
            "target-arrow-color": "#7f9187",
            "target-arrow-shape": "triangle",
            "curve-style": "bezier",
            label: "data(label)",
            color: "#56665d",
            "font-size": 7,
            "text-background-color": "#fafbf9",
            "text-background-opacity": 0.9,
            "text-background-padding": "2px",
          },
        },
        {
          selector: ":selected",
          style: {
            "border-color": "#2d7ed3",
            "line-color": "#2d7ed3",
            "target-arrow-color": "#2d7ed3",
          },
        },
      ],
      layout: {
        name: "cose",
        animate: false,
        fit: true,
        padding: 34,
        nodeRepulsion: () => 420_000,
        idealEdgeLength: () => 90,
      },
      minZoom: 0.2,
      maxZoom: 3,
      wheelSensitivity: 0.22,
    });
    instanceRef.current = instance;
    instance.on("tap", "node", (event) => {
      const id = String(event.target.id());
      const original = event.originalEvent;
      if (original && (original.ctrlKey || original.metaKey)) {
        onToggleCompare(id);
      } else {
        onSelectNode(id);
      }
    });
    return () => {
      instanceRef.current = null;
      instance.destroy();
    };
  }, [edges, nodes, onSelectNode, onToggleCompare]);

  useEffect(() => {
    instanceRef.current?.elements().forEach((element) => {
      element.data(
        "compare",
        selectedSetRef.current.has(element.id()) ? "true" : "false",
      );
    });
  }, [compareIds]);

  return (
    <div
      className="graph-canvas cytoscape-canvas"
      ref={containerRef}
      role="img"
      aria-label={`资产关系图，包含 ${nodes.length} 个节点和 ${edges.length} 条关系。单击查看详情，Ctrl/Cmd 点击多选对比。`}
    />
  );
}

export function GraphPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  const compare = useCompare();
  const assetId = searchParams.get("asset_id") ?? "";
  const from = searchParams.get("from") ?? "";
  const to = searchParams.get("to") ?? "";
  const depth = Math.min(3, Math.max(1, Number(searchParams.get("depth") ?? 1)));
  const [idInput, setIdInput] = useState(assetId);
  const [fromInput, setFromInput] = useState(from);
  const [toInput, setToInput] = useState(to);
  const [pathMode, setPathMode] = useState(Boolean(from || to));
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [compareNotice, setCompareNotice] = useState<string | null>(null);

  useEffect(() => setIdInput(assetId), [assetId]);

  const neighbors = useQuery({
    queryKey: ["graph", "neighbors", assetId, depth],
    queryFn: ({ signal }) => api.graphNeighbors(assetId, depth, signal),
    enabled: Boolean(assetId) && !pathMode,
  });
  const path = useQuery({
    queryKey: ["graph", "path", from, to],
    queryFn: ({ signal }) => api.graphPath(from, to, signal),
    enabled: Boolean(from && to) && pathMode,
  });
  const active = pathMode ? path : neighbors;
  const parts = graphParts(active.data);
  const selectedNode =
    parts.nodes.find((node) => node.id === selectedNodeId) ?? null;
  const compareIds = useMemo(
    () => compare.assets.map((item) => item.id),
    [compare.assets],
  );

  const handleSelectNode = useCallback((id: string) => {
    setSelectedNodeId(id);
  }, []);

  const toggleCompare = useCallback(
    (id: string) => {
      const node = parts.nodes.find((item) => item.id === id);
      if (!node) return;
      if (!compare.has(id) && compare.assets.length >= 4) {
        setCompareNotice("最多同时对比 4 篇论文");
        window.setTimeout(() => setCompareNotice(null), 2500);
        return;
      }
      compare.toggle(assetFromNode(node));
    },
    [compare, parts.nodes],
  );

  const submitNeighbors = (event: FormEvent) => {
    event.preventDefault();
    const next = new URLSearchParams();
    if (idInput.trim()) next.set("asset_id", idInput.trim());
    next.set("depth", String(depth));
    setPathMode(false);
    setSearchParams(next);
  };
  const submitPath = (event: FormEvent) => {
    event.preventDefault();
    const next = new URLSearchParams();
    if (fromInput.trim()) next.set("from", fromInput.trim());
    if (toInput.trim()) next.set("to", toInput.trim());
    setPathMode(true);
    setSearchParams(next);
  };

  return (
    <div className="page">
      <PageHeader
        eyebrow="知识网络"
        title="关系图谱"
        description="查看资产邻域、节点详情，或寻找两篇论文之间的关系路径。"
      />

      <div className="graph-controls section-block">
        <div className="graph-toolbar">
          <p className="muted">
            单击节点查看详情，Ctrl/Cmd 点击可多选对比。
          </p>
        </div>

        <div className="tab-list" role="tablist" aria-label="图谱查询方式">
          <button
            role="tab"
            aria-selected={!pathMode}
            className={!pathMode ? "is-active" : ""}
            type="button"
            onClick={() => setPathMode(false)}
          >
            <Network size={15} aria-hidden="true" /> 邻域
          </button>
          <button
            role="tab"
            aria-selected={pathMode}
            className={pathMode ? "is-active" : ""}
            type="button"
            onClick={() => setPathMode(true)}
          >
            <Route size={15} aria-hidden="true" /> 路径
          </button>
        </div>

        {!pathMode ? (
          <form className="inline-query-form" onSubmit={submitNeighbors}>
            <label className="grow">
              <span>中心资产 ID</span>
              <input
                value={idInput}
                onChange={(event) => setIdInput(event.target.value)}
                placeholder="粘贴资产 UUID"
              />
            </label>
            <label>
              <span>深度</span>
              <select
                value={depth}
                onChange={(event) => {
                  const next = new URLSearchParams(searchParams);
                  next.set("depth", event.target.value);
                  setSearchParams(next);
                }}
              >
                <option value="1">1 层</option>
                <option value="2">2 层</option>
                <option value="3">3 层</option>
              </select>
            </label>
            <button className="button primary" type="submit" disabled={!idInput.trim()}>
              查看邻域
            </button>
          </form>
        ) : (
          <form className="inline-query-form" onSubmit={submitPath}>
            <label className="grow">
              <span>起点资产 ID</span>
              <input value={fromInput} onChange={(event) => setFromInput(event.target.value)} />
            </label>
            <ArrowRight size={17} aria-hidden="true" />
            <label className="grow">
              <span>终点资产 ID</span>
              <input value={toInput} onChange={(event) => setToInput(event.target.value)} />
            </label>
            <button
              className="button primary"
              type="submit"
              disabled={!fromInput.trim() || !toInput.trim()}
            >
              寻找路径
            </button>
          </form>
        )}
      </div>

      {(!pathMode && !assetId) || (pathMode && (!from || !to)) ? (
        <EmptyState
          title={pathMode ? "输入起点和终点" : "选择一个中心资产"}
          detail="可从资产详情页直接打开图谱，也可以在上方粘贴资产 ID。"
        />
      ) : active.isPending ? (
        <LoadingState label="正在展开关系图谱…" />
      ) : active.isError ? (
        <ErrorState error={active.error} retry={() => void active.refetch()} />
      ) : active.data && parts.nodes.length ? (
        <div className={`graph-layout ${selectedNode ? "has-drawer" : ""}`}>
          <GraphCanvas
            data={active.data}
            compareIds={compareIds}
            onSelectNode={handleSelectNode}
            onToggleCompare={toggleCompare}
          />
          {selectedNode && (
            <GraphNodeDrawer
              node={selectedNode}
              onClose={() => setSelectedNodeId(null)}
            />
          )}
          <aside className="section-block graph-list">
            {compare.assets.length > 0 && (
              <div className="graph-compare-selection">
                <div className="section-heading">
                  <h2>已选对比</h2>
                  <span className="count-badge">{compare.assets.length}/4</span>
                </div>
                {compareNotice && <p className="muted">{compareNotice}</p>}
                <div className="graph-selected-list">
                  {compare.assets.map((item) => (
                    <div key={item.id}>
                      <span>{item.title_zh || item.title}</span>
                      <button
                        className="icon-button"
                        type="button"
                        aria-label={`移除 ${item.title_zh || item.title}`}
                        onClick={() => compare.remove(item.id)}
                      >
                        <X size={13} aria-hidden="true" />
                      </button>
                    </div>
                  ))}
                </div>
                <button
                  className="button primary compact"
                  type="button"
                  disabled={compare.assets.length < 2}
                  onClick={() => navigate("/compare")}
                >
                  <GitCompareArrows size={14} aria-hidden="true" /> 并排对比
                </button>
              </div>
            )}
            <div className="section-heading">
              <h2>节点</h2>
              <span className="count-badge">{parts.nodes.length}</span>
            </div>
            <div>
              {parts.nodes.map((node) => (
                <Link to={`/assets/${encodeURIComponent(node.id)}`} key={node.id}>
                  <span>{node.title ?? node.label ?? node.id}</span>
                  <small>{node.asset_type ?? node.year ?? "资产"}</small>
                </Link>
              ))}
            </div>
            {active.data.truncated && (
              <p className="muted">图谱过大，当前仅展示部分节点。</p>
            )}
          </aside>
        </div>
      ) : (
        <EmptyState
          title={pathMode ? "没有找到关系路径" : "该资产暂无关系"}
          detail="关系索引建立或确认候选关系后，再来查看。"
          kind="index"
        />
      )}
    </div>
  );
}
