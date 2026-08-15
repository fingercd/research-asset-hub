import type {
  Annotation,
  ApiList,
  Asset,
  CodeFileResponse,
  CodeTreeResponse,
  CodeGraphResponse,
  Comparison,
  Dashboard,
  Domain,
  DomainPortalResponse,
  GraphResponse,
  HealthResponse,
  Job,
  ReadingStatus,
  Representation,
  RepresentationOpenAction,
  RepresentationPresentation,
  ResearchUnitSummary,
  SearchGroupBy,
  SearchMode,
  Settings,
  SyncConflict,
} from "./types";

declare global {
  interface Window {
    __HUB_API_BASE__?: string;
  }
}

export function resolveApiBase(): string {
  if (typeof window !== "undefined" && window.__HUB_API_BASE__) {
    return window.__HUB_API_BASE__;
  }
  const fromEnv = import.meta.env.VITE_API_BASE;
  return typeof fromEnv === "string" && fromEnv ? fromEnv : "/api/v1";
}

const API_BASE = resolveApiBase();
const DEFAULT_TIMEOUT = 12_000;
const SEARCH_TIMEOUT = 120_000;

export class ApiError extends Error {
  readonly status: number;
  readonly title: string;
  readonly detail: string;
  readonly type?: string;

  constructor(input: {
    status: number;
    title?: string;
    detail?: string;
    type?: string;
  }) {
    const title = input.title || "请求失败";
    const detail = input.detail || `API 返回 HTTP ${input.status}`;
    super(detail);
    this.name = "ApiError";
    this.status = input.status;
    this.title = title;
    this.detail = detail;
    this.type = input.type;
  }
}

interface RequestOptions extends Omit<RequestInit, "body"> {
  body?: unknown;
  timeout?: number;
}

function params(
  values: Record<string, string | number | boolean | null | undefined>,
): string {
  const result = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") {
      result.set(key, String(value));
    }
  });
  const query = result.toString();
  return query ? `?${query}` : "";
}

function ifMatch(
  rowVersion: number | undefined,
  { required = false }: { required?: boolean } = {},
): Record<string, string> {
  if (typeof rowVersion === "number") {
    return { "If-Match": `W/"${rowVersion}"` };
  }
  if (required) {
    throw new ApiError({
      status: 428,
      title: "缺少资源版本",
      detail: "当前数据没有 row_version，请刷新后再保存，避免覆盖较新的修改。",
    });
  }
  return {};
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = (options.method ?? "GET").toUpperCase();
  const changesState = ["POST", "PUT", "PATCH", "DELETE"].includes(method);
  const controller = new AbortController();
  const timeout = window.setTimeout(
    () => controller.abort(new DOMException("请求超时", "TimeoutError")),
    options.timeout ?? DEFAULT_TIMEOUT,
  );
  const onAbort = () => controller.abort(options.signal?.reason);
  options.signal?.addEventListener("abort", onAbort, { once: true });

  try {
    const response = await fetch(path, {
      ...options,
      signal: controller.signal,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      headers: {
        Accept: "application/json, application/problem+json",
        ...(options.body === undefined ? {} : { "Content-Type": "application/json" }),
        ...(changesState ? { "X-Research-Hub-CSRF": "1" } : {}),
        ...options.headers,
      },
    });

    const contentType = response.headers.get("content-type") ?? "";
    const payload: unknown =
      response.status === 204
        ? undefined
        : contentType.includes("json")
          ? await response.json()
          : await response.text();

    if (!response.ok) {
      const problem =
        payload && typeof payload === "object"
          ? (payload as Record<string, unknown>)
          : undefined;
      throw new ApiError({
        status: response.status,
        title: typeof problem?.title === "string" ? problem.title : undefined,
        detail:
          typeof problem?.detail === "string"
            ? problem.detail
            : typeof payload === "string"
              ? payload.trim().startsWith("<")
                ? `API 返回 HTTP ${response.status}`
                : payload
              : undefined,
        type: typeof problem?.type === "string" ? problem.type : undefined,
      });
    }
    return payload as T;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (controller.signal.aborted && !options.signal?.aborted) {
      throw new ApiError({
        status: 0,
        title: "请求超时",
        detail: "本地 API 响应时间过长，请稍后重试。",
      });
    }
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError({
      status: 0,
      title: "无法连接本地 API",
      detail:
        error instanceof TypeError
          ? "无法连接 /api/v1，请确认本地 API 服务已经启动。"
          : error instanceof Error
            ? error.message
            : "请确认 API 服务已经启动。",
    });
  } finally {
    window.clearTimeout(timeout);
    options.signal?.removeEventListener("abort", onAbort);
  }
}

function normalizeList<T>(value: unknown): ApiList<T> {
  if (Array.isArray(value)) {
    return { items: value as T[], next_cursor: null, total: value.length };
  }
  if (!value || typeof value !== "object") {
    return { items: [], next_cursor: null, total: 0 };
  }
  const record = value as Record<string, unknown>;
  const rawItems = record.items ?? record.results ?? record.data;
  const items = Array.isArray(rawItems) ? (rawItems as T[]) : [];
  return {
    items,
    next_cursor:
      typeof record.next_cursor === "string"
        ? record.next_cursor
        : typeof record.nextCursor === "string"
          ? record.nextCursor
          : null,
    total: typeof record.total === "number" ? record.total : undefined,
  };
}

export interface SearchParams {
  q: string;
  mode?: SearchMode;
  group_by?: SearchGroupBy;
  domain?: string;
  status?: ReadingStatus | "";
  author?: string;
  type?: string;
  year_from?: number;
  year_to?: number;
  tag?: string;
  has_pdf?: boolean;
  has_html?: boolean;
  has_code?: boolean;
  has_guide?: boolean;
  has_annotation?: boolean;
  cursor?: string;
}

type SearchParamsWithoutGrouping = Omit<SearchParams, "group_by">;

async function search(
  searchParams: SearchParamsWithoutGrouping & { group_by: "paper" },
  signal?: AbortSignal,
): Promise<ApiList<ResearchUnitSummary>>;
async function search(
  searchParams: SearchParamsWithoutGrouping & { group_by?: "none" },
  signal?: AbortSignal,
): Promise<ApiList<Asset>>;
async function search(
  searchParams: SearchParams,
  signal?: AbortSignal,
): Promise<ApiList<Asset | ResearchUnitSummary>>;
async function search(
  searchParams: SearchParams,
  signal?: AbortSignal,
): Promise<ApiList<Asset | ResearchUnitSummary>> {
  return normalizeList<Asset | ResearchUnitSummary>(
    await request<unknown>(
      `${API_BASE}/search${params({
        q: searchParams.q,
        mode: searchParams.mode ?? "keyword",
        group_by: searchParams.group_by,
        domain: searchParams.domain,
        status: searchParams.status,
        author: searchParams.author,
        type: searchParams.type,
        year_from: searchParams.year_from,
        year_to: searchParams.year_to,
        tag: searchParams.tag,
        has_pdf: searchParams.has_pdf,
        has_html: searchParams.has_html,
        has_code: searchParams.has_code,
        has_guide: searchParams.has_guide,
        has_annotation: searchParams.has_annotation,
        cursor: searchParams.cursor,
      })}`,
      { signal, timeout: SEARCH_TIMEOUT },
    ),
  );
}

export const api = {
  health: (signal?: AbortSignal) =>
    // 必须经 API_BASE 拼接:裸相对路径 /api/health 在桌面壳(origin 为
    // tauri://localhost)下会被 SPA fallback 返回 index.html,status 解析为
    // undefined,健康徽章误报"部分服务不可用"。桌面壳走 /api/v1/health,
    // 浏览器托管模式经 web_host /api/* 代理到同一后端路由。
    request<HealthResponse>(`${API_BASE}/health`, { signal, timeout: 5_000 }),

  dashboard: (signal?: AbortSignal) =>
    request<Dashboard>(`${API_BASE}/dashboard`, { signal }),

  search,

  assets: async (
    query: { cursor?: string; domain?: string; status?: ReadingStatus } = {},
    signal?: AbortSignal,
  ) =>
    normalizeList<Asset>(
      await request<unknown>(`${API_BASE}/assets${params(query)}`, { signal }),
    ),

  asset: (id: string, signal?: AbortSignal) =>
    request<Asset>(`${API_BASE}/assets/${encodeURIComponent(id)}`, { signal }),

  createAsset: (body: Partial<Asset>) =>
    request<Asset>(`${API_BASE}/assets`, { method: "POST", body }),

  updateAsset: (id: string, body: Partial<Asset>, rowVersion?: number) =>
    request<Asset>(`${API_BASE}/assets/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body,
      headers: ifMatch(rowVersion, { required: true }),
    }),

  deleteAsset: (id: string, rowVersion?: number) =>
    request<void>(`${API_BASE}/assets/${encodeURIComponent(id)}`, {
      method: "DELETE",
      headers: ifMatch(rowVersion, { required: true }),
    }),

  restoreAsset: (id: string) =>
    request<Asset>(`${API_BASE}/assets/${encodeURIComponent(id)}/restore`, {
      method: "POST",
    }),

  updateReadingStatus: (id: string, status: ReadingStatus) =>
    request<Asset | { status: ReadingStatus }>(
      `${API_BASE}/assets/${encodeURIComponent(id)}/reading-state`,
      { method: "PUT", body: { status } },
    ),

  representations: async (id: string, signal?: AbortSignal) =>
    normalizeList<Representation>(
      await request<unknown>(
        `${API_BASE}/assets/${encodeURIComponent(id)}/representations`,
        { signal },
      ),
    ).items,

  openRepresentation: (
    id: string,
    options?: { presentation?: RepresentationPresentation; embed?: boolean },
  ) =>
    request<RepresentationOpenAction>(
      `${API_BASE}/representations/${encodeURIComponent(id)}/open-actions`,
      { method: "POST", body: options },
    ),

  codeTree: (id: string, path = "", signal?: AbortSignal) =>
    request<CodeTreeResponse>(
      `${API_BASE}/representations/${encodeURIComponent(id)}/code/tree${params({
        path,
      })}`,
      { signal },
    ),

  codeFile: (id: string, path: string, signal?: AbortSignal) =>
    request<CodeFileResponse>(
      `${API_BASE}/representations/${encodeURIComponent(id)}/code/file${params({
        path,
      })}`,
      { signal },
    ),

  annotations: async (assetId?: string, signal?: AbortSignal) =>
    normalizeList<Annotation>(
      await request<unknown>(
        `${API_BASE}/annotations${params({ asset_id: assetId })}`,
        { signal },
      ),
    ),

  createAnnotation: (body: Partial<Annotation> & { asset_id: string }) =>
    request<Annotation>(`${API_BASE}/annotations`, { method: "POST", body }),

  updateAnnotation: (
    id: string,
    body: Partial<Annotation>,
    rowVersion?: number,
  ) =>
    request<Annotation>(`${API_BASE}/annotations/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body,
      headers: ifMatch(rowVersion, { required: true }),
    }),

  deleteAnnotation: (id: string, rowVersion?: number) =>
    request<void>(`${API_BASE}/annotations/${encodeURIComponent(id)}`, {
      method: "DELETE",
      headers: ifMatch(rowVersion, { required: true }),
    }),

  domains: async (signal?: AbortSignal) =>
    normalizeList<Domain>(
      await request<unknown>(`${API_BASE}/domains`, { signal }),
    ),

  domain: (slug: string, signal?: AbortSignal) =>
    request<Domain>(`${API_BASE}/domains/${encodeURIComponent(slug)}`, {
      signal,
    }),

  domainPortal: (slug: string, signal?: AbortSignal) =>
    request<DomainPortalResponse>(
      `${API_BASE}/domains/${encodeURIComponent(slug)}/portal`,
      { signal },
    ),

  graphNeighbors: (assetId: string, depth = 1, signal?: AbortSignal) =>
    request<GraphResponse>(
      `${API_BASE}/graph/neighbors${params({ asset_id: assetId, depth })}`,
      { signal },
    ),

  graphPath: (from: string, to: string, signal?: AbortSignal) =>
    request<GraphResponse>(
      `${API_BASE}/graph/path${params({ from, to })}`,
      { signal },
    ),

  codeGraph: (signal?: AbortSignal) =>
    request<CodeGraphResponse>(`${API_BASE}/code-graph`, { signal }),

  comparisons: async (signal?: AbortSignal) =>
    normalizeList<Comparison>(
      await request<unknown>(`${API_BASE}/comparisons`, { signal }),
    ),

  createComparison: (assetIds: string[]) =>
    request<Comparison>(`${API_BASE}/comparisons`, {
      method: "POST",
      body: { asset_ids: assetIds },
    }),

  similar: async (id: string, signal?: AbortSignal) =>
    normalizeList<Asset>(
      await request<unknown>(`${API_BASE}/similar/${encodeURIComponent(id)}`, {
        signal,
      }),
    ),

  jobs: async (signal?: AbortSignal) =>
    normalizeList<Job>(
      await request<unknown>(`${API_BASE}/jobs`, { signal }),
    ),

  retryJob: (id: string, rowVersion?: number) =>
    request<Job>(`${API_BASE}/jobs/${encodeURIComponent(id)}/retry`, {
      method: "POST",
      headers: ifMatch(rowVersion),
    }),

  cancelJob: (id: string, rowVersion?: number) =>
    request<Job>(`${API_BASE}/jobs/${encodeURIComponent(id)}/cancel`, {
      method: "POST",
      headers: ifMatch(rowVersion),
    }),

  conflicts: async (signal?: AbortSignal) =>
    normalizeList<SyncConflict>(
      await request<unknown>(`${API_BASE}/sync-conflicts`, { signal }),
    ),

  resolveConflict: (
    id: string,
    strategy: "keep_local" | "accept_incoming" | "merge",
    rowVersion?: number,
  ) =>
    request<SyncConflict>(
      `${API_BASE}/sync-conflicts/${encodeURIComponent(id)}/resolve`,
      { method: "POST", body: { strategy }, headers: ifMatch(rowVersion) },
    ),

  settings: (signal?: AbortSignal) =>
    request<Settings>(`${API_BASE}/settings`, { signal }),
};

export function getApiErrorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.detail;
  return error instanceof Error ? error.message : "发生未知错误";
}
