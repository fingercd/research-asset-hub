import { AlertCircle, Database, LoaderCircle, RefreshCw, SearchX } from "lucide-react";
import { getApiErrorMessage } from "../api/client";

export function LoadingState({ label = "正在加载…" }: { label?: string }) {
  return (
    <div className="page-state" role="status" aria-live="polite">
      <LoaderCircle className="spin" size={22} aria-hidden="true" />
      <strong>{label}</strong>
      <span>正在从本地索引读取数据</span>
    </div>
  );
}

export function ErrorState({
  error,
  retry,
  title = "暂时无法读取数据",
}: {
  error: unknown;
  retry?: () => void;
  title?: string;
}) {
  return (
    <div className="page-state is-error" role="alert">
      <AlertCircle size={24} aria-hidden="true" />
      <strong>{title}</strong>
      <span>{getApiErrorMessage(error)}</span>
      <small>页面仍可浏览；启动或恢复本地 API 后即可重试。</small>
      {retry && (
        <button className="button secondary" type="button" onClick={retry}>
          <RefreshCw size={15} aria-hidden="true" />
          重新加载
        </button>
      )}
    </div>
  );
}

export function EmptyState({
  title,
  detail,
  action,
  kind = "empty",
}: {
  title: string;
  detail: string;
  action?: React.ReactNode;
  kind?: "empty" | "search" | "index";
}) {
  const Icon = kind === "search" ? SearchX : kind === "index" ? Database : Database;
  return (
    <div className="page-state">
      <Icon size={24} aria-hidden="true" />
      <strong>{title}</strong>
      <span>{detail}</span>
      {action}
    </div>
  );
}

export function IndexNotice({
  status,
  detail,
}: {
  status?: string;
  detail?: string;
}) {
  if (!status || status === "ready") return null;
  const labels: Record<string, string> = {
    building: "索引正在构建",
    stale: "索引需要更新",
    unavailable: "搜索索引暂不可用",
  };
  return (
    <div className="notice" role="status">
      <Database size={17} aria-hidden="true" />
      <div>
        <strong>{labels[status] ?? `索引状态：${status}`}</strong>
        <span>{detail ?? "结果可能不完整，后台任务完成后会自动恢复。"}</span>
      </div>
    </div>
  );
}
