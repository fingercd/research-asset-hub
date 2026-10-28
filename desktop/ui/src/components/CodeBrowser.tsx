import { useQuery } from "@tanstack/react-query";
import {
  ArrowLeft,
  Check,
  ChevronRight,
  Copy,
  FileCode2,
  Folder,
  FolderOpen,
  RefreshCw,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api } from "../api/client";
import type { RepresentationOpenAction } from "../api/types";
import { formatBytes } from "../lib/format";
import { ErrorState, LoadingState } from "./PageState";

function parentPath(path: string): string {
  const parts = path.split("/").filter(Boolean);
  return parts.slice(0, -1).join("/");
}

function pathLabel(path: string): string {
  return path.split("/").filter(Boolean).at(-1) ?? "项目根目录";
}

export function CodeBrowser({
  representationId,
  repositoryLabel,
  openAction,
  compact = false,
}: {
  representationId: string;
  repositoryLabel?: string;
  openAction?: RepresentationOpenAction | null;
  compact?: boolean;
}) {
  const [currentPath, setCurrentPath] = useState("");
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    setCurrentPath("");
    setSelectedFile(null);
    setCopied(false);
  }, [representationId]);

  const localOpenAction = useQuery({
    queryKey: ["code-open-action", representationId],
    queryFn: () => api.openRepresentation(representationId),
    enabled: !openAction,
    staleTime: 60_000,
  });
  const effectiveOpenAction = openAction ?? localOpenAction.data;
  const editor = effectiveOpenAction?.editor;

  const tree = useQuery({
    queryKey: ["code-tree", representationId, currentPath],
    queryFn: ({ signal }) => api.codeTree(representationId, currentPath, signal),
    staleTime: 30_000,
  });
  const file = useQuery({
    queryKey: ["code-file", representationId, selectedFile],
    queryFn: ({ signal }) => api.codeFile(representationId, selectedFile!, signal),
    enabled: Boolean(selectedFile),
  });

  const breadcrumbs = useMemo(() => {
    const parts = currentPath.split("/").filter(Boolean);
    return parts.map((part, index) => ({
      label: part,
      path: parts.slice(0, index + 1).join("/"),
    }));
  }, [currentPath]);

  const openEditor = () => {
    if (editor?.available && editor.uri) {
      window.open(editor.uri, "_self");
    }
  };

  const copyFile = async () => {
    if (!file.data?.previewable || file.data.content === null) return;
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(file.data.content);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1400);
    }
  };

  if (tree.isPending) {
    return (
      <section className={`code-browser${compact ? " is-compact" : ""}`}>
        <LoadingState label="正在加载本地代码目录…" />
      </section>
    );
  }
  if (tree.isError || !tree.data) {
    return (
      <section className={`code-browser${compact ? " is-compact" : ""}`}>
        <div className="code-browser-unavailable">
          <FolderOpen size={28} aria-hidden="true" />
          <strong>本地代码不可用</strong>
          <p>
            {tree.error instanceof Error
              ? tree.error.message
              : "源码未下载、未开源或不属于可预览的公开代码仓库。"}
          </p>
        </div>
      </section>
    );
  }

  const entries = tree.data.entries ?? [];
  const selectedContent = file.data?.content ?? "";
  const codeLines = selectedContent.endsWith("\n")
    ? selectedContent.slice(0, -1).split("\n")
    : selectedContent.split("\n");
  const displayName = tree.data.repository_name || repositoryLabel || "代码仓库";

  return (
    <section
      className={`code-browser${compact ? " is-compact" : ""}`}
      aria-label="站内代码浏览器"
    >
      <header className="code-browser-toolbar">
        <div className="code-browser-heading">
          <p className="eyebrow">本地只读代码</p>
          <strong>{displayName}</strong>
          <span>文件内容在本机展示，不打开外部代码网页</span>
        </div>
        <div className="code-browser-toolbar-actions">
          {editor?.available && editor.uri && (
            <button
              className="button secondary compact"
              type="button"
              onClick={openEditor}
            >
              用 VS Code 打开
            </button>
          )}
          <button
            className="icon-button"
            type="button"
            aria-label="刷新代码目录"
            title="刷新代码目录"
            onClick={() => void tree.refetch()}
          >
            <RefreshCw size={15} aria-hidden="true" />
          </button>
        </div>
      </header>

      <div className="code-browser-layout">
        <aside className="code-browser-tree" aria-label="代码目录">
          <div className="code-browser-tree-header">
            <span>目录</span>
            {tree.data.truncated && <small>已截断</small>}
          </div>
          <div className="code-browser-breadcrumbs">
            <button
              type="button"
              className={!currentPath ? "is-current" : ""}
              onClick={() => {
                setCurrentPath("");
                setSelectedFile(null);
              }}
            >
              {displayName}
            </button>
            {breadcrumbs.map((crumb) => (
              <span key={crumb.path}>
                <ChevronRight size={12} aria-hidden="true" />
                <button
                  type="button"
                  className={crumb.path === currentPath ? "is-current" : ""}
                  onClick={() => {
                    setCurrentPath(crumb.path);
                    setSelectedFile(null);
                  }}
                >
                  {crumb.label}
                </button>
              </span>
            ))}
          </div>
          {currentPath && (
            <button
              type="button"
              className="code-browser-entry is-parent"
              onClick={() => {
                setCurrentPath(parentPath(currentPath));
                setSelectedFile(null);
              }}
            >
              <ArrowLeft size={14} aria-hidden="true" />
              <span>返回上级目录</span>
            </button>
          )}
          <div className="code-browser-entries">
            {entries.map((entry) => {
              const isDirectory = entry.kind === "directory";
              const isSelected = selectedFile === entry.path;
              return (
                <button
                  type="button"
                  className={`code-browser-entry${isSelected ? " is-selected" : ""}`}
                  key={entry.path}
                  onClick={() => {
                    if (isDirectory) {
                      setCurrentPath(entry.path);
                      setSelectedFile(null);
                    } else {
                      setSelectedFile(entry.path);
                    }
                  }}
                  aria-label={`${isDirectory ? "打开目录" : "打开文件"} ${entry.path}`}
                >
                  {isDirectory ? (
                    <Folder size={14} aria-hidden="true" />
                  ) : (
                    <FileCode2 size={14} aria-hidden="true" />
                  )}
                  <span>{entry.name}</span>
                  {!isDirectory && <small>{formatBytes(entry.size_bytes)}</small>}
                </button>
              );
            })}
            {!entries.length && <p className="code-browser-empty">该目录为空。</p>}
          </div>
        </aside>

        <div className="code-browser-content">
          {!selectedFile ? (
            <div className="code-browser-placeholder">
              <FileCode2 size={30} aria-hidden="true" />
              <strong>{pathLabel(currentPath)}</strong>
              <p>从左侧选择源码文件，在站内查看只读内容。</p>
            </div>
          ) : file.isPending ? (
            <LoadingState label="正在加载文件…" />
          ) : file.isError || !file.data ? (
            <ErrorState error={file.error ?? new Error("代码文件无法读取。")} />
          ) : !file.data.previewable ? (
            <div className="code-browser-placeholder">
              <FileCode2 size={30} aria-hidden="true" />
              <strong>该文件不支持文本预览</strong>
              <p>{file.data.reason ?? "二进制文件不会在站内打开。"}</p>
            </div>
          ) : (
            <div className="code-browser-file">
              <header className="code-browser-file-header">
                <div>
                  <strong>{file.data.path}</strong>
                  <span>
                    {file.data.language} · {file.data.line_count} 行 ·{" "}
                    {formatBytes(file.data.size_bytes)}
                    {file.data.truncated ? " · 仅展示前 512 KB" : ""}
                  </span>
                </div>
                <button
                  className="button secondary compact"
                  type="button"
                  onClick={() => void copyFile()}
                >
                  {copied ? (
                    <Check size={14} aria-hidden="true" />
                  ) : (
                    <Copy size={14} aria-hidden="true" />
                  )}
                  {copied ? "已复制" : "复制内容"}
                </button>
              </header>
              <div className="code-browser-source" role="region" aria-label="代码内容">
                <div className="code-browser-line-numbers" aria-hidden="true">
                  {codeLines.map((_, index) => (
                    <span key={index}>{index + 1}</span>
                  ))}
                </div>
                <pre>
                  <code>{selectedContent}</code>
                </pre>
              </div>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
