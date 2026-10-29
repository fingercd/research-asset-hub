import { AlertCircle, Database, LoaderCircle, RefreshCw, SearchX } from "lucide-react";
import { getApiErrorMessage } from "../api/client";

export function LoadingState({ label = "正在加载…" }: { label?: string }) {
  return (
    <div className="page-state" role="status" aria-live="polite">
      <LoaderCircle className="spin" size={22} aria-hidden="true" />
      <strong>{label}</strong>
      <span>正在加载，请稍候</span>
    </div>
  );
}

export function ErrorState({
  error,
  retry,
  title,
}: {
  error: unknown;
  retry?: () => void;
  title?: string;
}) {
  const status = error && typeof error === "object" && "status" in error
    ? error.status
    : undefined;
  const recovery = status === 404
    ? { title: "资料或集合不可用", hint: "该记录可能已移除或不在当前资料库中。请返回列表重新选择；重启服务不会恢复这条记录。" }
    : status === 401 || status === 403
      ? { title: "无法访问这项资料", hint: "请检查当前资料库与访问权限。安全会话失效时可重新加载，不必修改原始文件。" }
      : status === 409 || status === 412 || status === 428
        ? { title: "数据版本已变化", hint: "请先保留尚未保存的内容，再重新加载最新版本；不要反复提交覆盖其他修改。" }
        : status === 422
          ? { title: "请检查输入条件", hint: "请根据错误详情调整字段或筛选范围，再重试。" }
          : status === 0
            ? { title: "本机连接需要检查", hint: "请查看系统状态后重试。你的资料和已有操作不会因此被删除。" }
            : { title: "这项操作暂未完成", hint: "可重新加载这项内容；如果问题持续，请在系统状态中查看诊断信息。" };
  return (
    <div className="page-state is-error" role="alert">
      <AlertCircle size={24} aria-hidden="true" />
      <strong>{title ?? recovery.title}</strong>
      <span>{getApiErrorMessage(error)}</span>
      <small>{recovery.hint}</small>
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
  const Icon = kind === "search" ? SearchX : Database;
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
