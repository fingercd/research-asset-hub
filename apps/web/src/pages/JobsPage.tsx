import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  Ban,
  CheckCircle2,
  Clock3,
  LoaderCircle,
  RefreshCw,
} from "lucide-react";
import { api, getApiErrorMessage } from "../api/client";
import type { Job } from "../api/types";
import { EmptyState, ErrorState, LoadingState } from "../components/PageState";
import { PageHeader } from "../components/PageHeader";
import { formatDate, jobError } from "../lib/format";

const statusLabels: Record<string, string> = {
  pending: "等待中",
  running: "运行中",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

function JobStatus({ job }: { job: Job }) {
  const Icon =
    job.status === "succeeded"
      ? CheckCircle2
      : job.status === "failed"
        ? AlertCircle
        : job.status === "running"
          ? LoaderCircle
          : job.status === "cancelled"
            ? Ban
            : Clock3;
  return (
    <span className={`job-status status-${job.status ?? "pending"}`}>
      <Icon className={job.status === "running" ? "spin" : undefined} size={15} aria-hidden="true" />
      {statusLabels[job.status ?? "pending"] ?? job.status ?? "等待中"}
    </span>
  );
}

function JobRow({ job }: { job: Job }) {
  const queryClient = useQueryClient();
  const retry = useMutation({
    mutationFn: () => api.retryJob(job.id, job.row_version),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["jobs"] }),
  });
  const cancel = useMutation({
    mutationFn: () => api.cancelJob(job.id, job.row_version),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["jobs"] }),
  });
  const error = retry.error ?? cancel.error;
  const progress =
    typeof job.progress === "number"
      ? Math.min(100, Math.max(0, job.progress <= 1 ? job.progress * 100 : job.progress))
      : null;

  return (
    <article className="job-card">
      <div className="job-card-header">
        <div>
          <JobStatus job={job} />
          <h2>{job.name ?? job.type ?? "后台任务"}</h2>
          <span className="mono">{job.id}</span>
        </div>
        <time>{formatDate(job.created_at, true)}</time>
      </div>
      {job.detail && <p>{job.detail}</p>}
      {progress !== null && (
        <div className="progress-row">
          <div className="progress-track" aria-label={`进度 ${Math.round(progress)}%`}>
            <span style={{ width: `${progress}%` }} />
          </div>
          <span>{Math.round(progress)}%</span>
        </div>
      )}
      {jobError(job) && <div className="job-error">{jobError(job)}</div>}
      <footer>
        <span>
          {job.started_at ? `开始于 ${formatDate(job.started_at, true)}` : "尚未开始"}
          {job.finished_at ? ` · 结束于 ${formatDate(job.finished_at, true)}` : ""}
        </span>
        <div>
          {job.status === "failed" && (
            <button
              className="button secondary compact"
              type="button"
              disabled={retry.isPending}
              onClick={() => retry.mutate()}
            >
              <RefreshCw size={14} aria-hidden="true" /> 重试
            </button>
          )}
          {["pending", "running"].includes(job.status ?? "") && (
            <button
              className="button ghost compact"
              type="button"
              disabled={cancel.isPending}
              onClick={() => cancel.mutate()}
            >
              <Ban size={14} aria-hidden="true" /> 取消
            </button>
          )}
        </div>
      </footer>
      {error && <span className="inline-error">{getApiErrorMessage(error)}</span>}
    </article>
  );
}

export function JobsPage() {
  const jobs = useQuery({
    queryKey: ["jobs"],
    queryFn: ({ signal }) => api.jobs(signal),
    refetchInterval: (query) => {
      const active = query.state.data?.items.some((job) =>
        ["pending", "running"].includes(job.status ?? ""),
      );
      return active ? 5_000 : 20_000;
    },
  });

  return (
    <div className="page">
      <PageHeader
        eyebrow="运行状态"
        title="后台任务"
        description="查看导入、索引与嵌入任务；运行中的任务会自动刷新。"
        actions={
          <button
            className="button secondary"
            type="button"
            disabled={jobs.isFetching}
            onClick={() => void jobs.refetch()}
          >
            <RefreshCw size={15} aria-hidden="true" /> 刷新
          </button>
        }
      />
      {jobs.isPending ? (
        <LoadingState label="正在加载任务…" />
      ) : jobs.isError ? (
        <ErrorState error={jobs.error} retry={() => void jobs.refetch()} />
      ) : jobs.data.items.length ? (
        <section className="job-list" aria-label="后台任务列表">
          {jobs.data.items.map((job) => <JobRow job={job} key={job.id} />)}
        </section>
      ) : (
        <EmptyState
          title="没有后台任务"
          detail="导入、重建全文索引或生成向量时，任务会显示在这里。"
        />
      )}
    </div>
  );
}
