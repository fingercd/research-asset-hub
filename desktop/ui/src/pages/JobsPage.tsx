import {
  useInfiniteQuery,
  useMutation,
  useQueryClient,
} from "@tanstack/react-query";
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

const jobLabels: Record<string, string> = {
  extract_representation: "提取论文内容",
  index_asset: "更新资料关键词索引",
  index_representation: "更新文件索引",
  repository_snapshot: "下载代码快照",
  "repository.snapshot": "下载代码快照",
  "representation.extract": "提取论文内容",
  "representation.index": "更新文件索引",
  "asset.index": "更新资料关键词索引",
  export_package: "导出领域包",
  reindex_assets: "更新关键词索引",
  import_package: "导入领域包",
};

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
      <Icon
        className={job.status === "running" ? "spin" : undefined}
        size={15}
        aria-hidden="true"
      />
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
  const storedMessage = jobError(job);
  const storedMessageClass =
    job.status === "cancelled" ? "job-note" : "job-error";
  const progress =
    typeof job.progress === "number"
      ? Math.min(
          100,
          Math.max(0, job.progress <= 1 ? job.progress * 100 : job.progress),
        )
      : null;

  return (
    <article className="job-card">
      <div className="job-card-header">
        <div>
          <JobStatus job={job} />
          <h2>
            {jobLabels[job.type ?? ""] ?? job.name ?? job.type ?? "后台任务"}
          </h2>
          <span className="mono">{job.id}</span>
        </div>
        <time>{formatDate(job.created_at, true)}</time>
      </div>
      {job.detail && <p>{job.detail}</p>}
      {progress !== null && (
        <div className="progress-row">
          <div
            className="progress-track"
            aria-label={`进度 ${Math.round(progress)}%`}
          >
            <span style={{ width: `${progress}%` }} />
          </div>
          <span>{Math.round(progress)}%</span>
        </div>
      )}
      {storedMessage && (
        <div className={storedMessageClass}>{storedMessage}</div>
      )}
      <footer>
        <span>
          {job.started_at
            ? `开始于 ${formatDate(job.started_at, true)}`
            : "尚未开始"}
          {job.finished_at
            ? ` · 结束于 ${formatDate(job.finished_at, true)}`
            : ""}
        </span>
        <div>
          {job.status === "succeeded" &&
            (job.type === "package_export" ||
              job.type === "domain_export" ||
              job.type === "export_package") && (
              <a
                className="button primary compact"
                href={api.exportDownloadUrl(job.id)}
                download
              >
                下载领域包
              </a>
            )}
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
      {error && (
        <span className="inline-error">{getApiErrorMessage(error)}</span>
      )}
    </article>
  );
}

export function JobsPage() {
  const jobs = useInfiniteQuery({
    queryKey: ["jobs"],
    queryFn: ({ signal, pageParam }) => api.jobs(signal, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    refetchInterval: (query) => {
      const active = query.state.data?.pages.some((page) =>
        page.items.some((job) => ["pending", "running"].includes(job.status ?? "")),
      );
      return active ? 5_000 : 20_000;
    },
  });
  const items = jobs.data?.pages.flatMap((page) => page.items) ?? [];

  return (
    <div className="page">
      <PageHeader
        eyebrow="运行状态"
        title="后台任务"
        description="查看 PDF 抽取、关键词索引、代码快照与领域包任务；运行中的任务会自动刷新。"
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
      ) : jobs.isError && !jobs.data ? (
        <ErrorState error={jobs.error} retry={() => void jobs.refetch()} />
      ) : items.length ? (
        <section className="job-list" aria-label="后台任务列表">
          {items.map((job) => (
            <JobRow job={job} key={job.id} />
          ))}
        </section>
      ) : (
        <EmptyState
          title="没有后台任务"
          detail="导入或重建全文索引时，任务会显示在这里。"
        />
      )}
      {jobs.isError && jobs.data && (
        <ErrorState
          error={jobs.error}
          retry={() =>
            void (jobs.isFetchNextPageError ? jobs.fetchNextPage() : jobs.refetch())
          }
        />
      )}
      {jobs.hasNextPage && (
        <div className="load-more">
          <button
            className="button secondary"
            type="button"
            disabled={jobs.isFetching}
            onClick={() => void jobs.fetchNextPage()}
          >
            {jobs.isFetchingNextPage ? "正在加载…" : "加载更多任务"}
          </button>
        </div>
      )}
    </div>
  );
}
