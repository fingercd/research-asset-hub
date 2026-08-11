import cytoscape from "cytoscape";
import { LoaderCircle, Search, SearchX, X } from "lucide-react";
import {
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";

const MAX_NODES = 1_500;

interface GitNexusNode {
  id: string;
  label?: string;
  properties?: Record<string, unknown>;
}

interface GitNexusRelationship {
  id?: string;
  type?: string;
  sourceId: string;
  targetId: string;
  confidence?: number;
}

interface GitNexusGraph {
  nodes: GitNexusNode[];
  relationships?: GitNexusRelationship[];
}

interface GitNexusSearchResult {
  id?: string;
  nodeId?: string;
  name?: string;
  label?: string;
  filePath?: string;
  path?: string;
  score?: number;
  description?: string;
  [key: string]: unknown;
}

type ViewerState = "idle" | "loading" | "ready" | "error";

function nodeDisplayLabel(node: GitNexusNode): string {
  const name = node.properties?.name;
  return String(
    typeof name === "string" && name.trim()
      ? name
      : node.label ?? node.id,
  );
}

export function CodeGraphViewer({
  gitnexusUrl,
  repoName,
}: {
  gitnexusUrl: string;
  repoName: string | null;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const instanceRef = useRef<cytoscape.Core | null>(null);
  const searchAbortRef = useRef<AbortController | null>(null);
  const [state, setState] = useState<ViewerState>(
    repoName ? "loading" : "idle",
  );
  const [message, setMessage] = useState<string | null>(null);
  const [truncated, setTruncated] = useState(false);
  const [selectedNode, setSelectedNode] = useState<GitNexusNode | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [results, setResults] = useState<GitNexusSearchResult[]>([]);
  const [searchWarning, setSearchWarning] = useState<string | null>(null);

  useEffect(() => {
    if (!repoName) {
      setState("idle");
      setMessage(null);
      setSelectedNode(null);
      setResults([]);
      setSearchWarning(null);
      setTruncated(false);
      return;
    }

    const controller = new AbortController();
    setState("loading");
    setMessage(null);
    setSelectedNode(null);
    setResults([]);
    setSearchWarning(null);
    setTruncated(false);
    fetch(
      `${gitnexusUrl}/api/graph?repo=${encodeURIComponent(repoName)}`,
      { signal: controller.signal },
    )
      .then(async (response) => {
        if (!response.ok) {
          throw new Error(`GitNexus 返回 HTTP ${response.status}`);
        }
        return (await response.json()) as GitNexusGraph;
      })
      .then((graph) => {
        if (controller.signal.aborted || !containerRef.current) return;
        instanceRef.current?.destroy();
        const nodes = graph.nodes ?? [];
        const relationships = graph.relationships ?? [];
        const visibleNodes = nodes.slice(0, MAX_NODES);
        const nodeIds = new Set(visibleNodes.map((node) => node.id));
        const visibleRelationships = relationships.filter(
          (edge) =>
            nodeIds.has(edge.sourceId) && nodeIds.has(edge.targetId),
        );
        const instance = cytoscape({
          container: containerRef.current,
          elements: [
            ...visibleNodes.map((node) => ({
              data: {
                id: node.id,
                label: nodeDisplayLabel(node),
                type: node.label ?? "Node",
                properties: node.properties ?? {},
              },
            })),
            ...visibleRelationships.map((edge, index) => ({
              data: {
                id: edge.id ?? `${edge.sourceId}-${edge.targetId}-${index}`,
                source: edge.sourceId,
                target: edge.targetId,
                type: edge.type ?? "RELATED",
              },
            })),
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
                "text-max-width": "120px",
                "text-wrap": "ellipsis",
                "text-valign": "bottom",
                "text-margin-y": 6,
                width: 34,
                height: 34,
              },
            },
            {
              selector: 'node[type = "File"]',
              style: { "background-color": "#e2eee8" },
            },
            {
              selector: 'node[type *= "Function"]',
              style: { "background-color": "#e3ecf5" },
            },
            {
              selector: 'node[type *= "Class"]',
              style: { "background-color": "#efe7f3" },
            },
            {
              selector: 'node[type *= "Section"]',
              style: { "background-color": "#fbf0d9" },
            },
            {
              selector: "edge",
              style: {
                width: 1.4,
                "line-color": "#a8b6ad",
                "target-arrow-color": "#7f9187",
                "target-arrow-shape": "triangle",
                "curve-style": "bezier",
                label: "data(type)",
                color: "#56665d",
                "font-size": 6,
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
            name: "concentric",
            animate: false,
            fit: true,
            padding: 30,
            minNodeSpacing: 18,
            avoidOverlap: true,
          },
          minZoom: 0.15,
          maxZoom: 3,
          wheelSensitivity: 0.22,
        });
        instanceRef.current = instance;
        instance.on("tap", "node", (event) => {
          const id = String(event.target.id());
          const node = visibleNodes.find((item) => item.id === id);
          if (node) setSelectedNode(node);
        });
        setTruncated(nodes.length > MAX_NODES);
        setState("ready");
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setState("error");
        setMessage(
          error instanceof Error ? error.message : "GitNexus 图谱加载失败",
        );
      });
    return () => controller.abort();
  }, [gitnexusUrl, repoName]);

  useEffect(
    () => () => {
      instanceRef.current?.destroy();
      instanceRef.current = null;
      searchAbortRef.current?.abort();
    },
    [],
  );

  const submitSearch = (event: FormEvent) => {
    event.preventDefault();
    const query = searchQuery.trim();
    if (!repoName || !query) return;
    searchAbortRef.current?.abort();
    const controller = new AbortController();
    searchAbortRef.current = controller;
    setSearchWarning(null);
    fetch(`${gitnexusUrl}/api/search`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, repo: repoName, limit: 20 }),
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok) {
          throw new Error(`GitNexus 搜索返回 HTTP ${response.status}`);
        }
        return (await response.json()) as {
          results?: GitNexusSearchResult[];
          warning?: string;
        };
      })
      .then((payload) => {
        if (controller.signal.aborted) return;
        setResults(payload.results ?? []);
        setSearchWarning(payload.warning ?? null);
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setResults([]);
        setSearchWarning(
          error instanceof Error ? error.message : "GitNexus 搜索失败",
        );
      });
  };

  const focusResult = (result: GitNexusSearchResult) => {
    const instance = instanceRef.current;
    if (!instance) return;
    const nodeId = String(result.nodeId ?? result.id ?? "");
    const element = nodeId
      ? instance.getElementById(nodeId)
      : instance.collection();
    if (element.length) {
      instance.elements().unselect();
      element.select();
      instance.animate({ center: { eles: element }, zoom: 1.2 }, { duration: 200 });
      return;
    }
    const target = instance
      .elements()
      .filter((item) =>
        String(item.data("label"))
          .toLowerCase()
          .includes(searchQuery.trim().toLowerCase()),
      );
    if (target.length) {
      instance.elements().unselect();
      target.first().select();
      instance.animate(
        { center: { eles: target.first() }, zoom: 1.1 },
        { duration: 200 },
      );
    }
  };

  return (
    <div className="code-graph-viewer">
      <form className="code-graph-viewer-search" onSubmit={submitSearch}>
        <Search size={14} aria-hidden="true" />
        <input
          value={searchQuery}
          onChange={(event) => setSearchQuery(event.target.value)}
          placeholder={repoName ? "搜索仓库节点…" : "先选择仓库"}
          disabled={!repoName}
          aria-label="搜索 GitNexus 节点"
        />
        <button className="button secondary compact" type="submit" disabled={!repoName}>
          搜索
        </button>
        {results.length > 0 && (
          <button
            className="icon-button"
            type="button"
            aria-label="清空搜索结果"
            onClick={() => {
              setResults([]);
              setSearchWarning(null);
            }}
          >
            <X size={13} aria-hidden="true" />
          </button>
        )}
      </form>

      {searchWarning && (
        <p className="code-graph-viewer-note" role="status">
          {searchWarning}
        </p>
      )}

      {results.length > 0 && (
        <div className="code-graph-viewer-results">
          {results.map((result, index) => {
            const title =
              result.name ??
              result.filePath ??
              result.path ??
              result.label ??
              result.id ??
              `结果 ${index + 1}`;
            return (
              <button
                type="button"
                key={result.id ?? String(title)}
                onClick={() => focusResult(result)}
              >
                <strong>{String(title)}</strong>
                {result.description ? (
                  <span>{String(result.description)}</span>
                ) : null}
              </button>
            );
          })}
        </div>
      )}

      <div className="code-graph-viewer-canvas" ref={containerRef}>
        {state === "idle" && (
          <div className="code-graph-viewer-empty" role="status">
            在左侧选择仓库后，这里会展示 Hub 渲染的代码图谱。
          </div>
        )}
        {state === "loading" && (
          <div className="code-graph-viewer-empty" role="status">
            <LoaderCircle className="spin" size={22} aria-hidden="true" />
            正在读取 GitNexus 图谱…
          </div>
        )}
        {state === "error" && (
          <div className="code-graph-viewer-empty is-error" role="alert">
            <SearchX size={24} aria-hidden="true" />
            <strong>图谱加载失败</strong>
            <span>{message}</span>
          </div>
        )}
        {truncated && (
          <p className="code-graph-viewer-note">
            图谱过大，当前仅展示前 {MAX_NODES.toLocaleString()} 个节点。
          </p>
        )}
      </div>

      {selectedNode && (
        <aside className="code-graph-node-detail">
          <div className="code-graph-node-detail-head">
            <div>
              <p className="eyebrow">{selectedNode.label ?? "Node"}</p>
              <strong>{nodeDisplayLabel(selectedNode)}</strong>
            </div>
            <button
              className="icon-button"
              type="button"
              aria-label="关闭节点详情"
              onClick={() => setSelectedNode(null)}
            >
              <X size={14} aria-hidden="true" />
            </button>
          </div>
          <dl>
            <div>
              <dt>节点 ID</dt>
              <dd>{selectedNode.id}</dd>
            </div>
            {Object.entries(selectedNode.properties ?? {}).map(([key, value]) => (
              <div key={key}>
                <dt>{key}</dt>
                <dd>{String(value)}</dd>
              </div>
            ))}
          </dl>
        </aside>
      )}
    </div>
  );
}
