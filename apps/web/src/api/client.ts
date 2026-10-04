import type {
  WorkflowResult,
  PackageImport,
  Collection,
  IntakeDraft,
  IntakePreview,
  SavedSearch,
  ReadingPosition,
  ReadingPositionResponse,
  McpConfig,
  McpCheck,
  Annotation,
  ApiList,
  Asset,
  CodeFileResponse,
  CodeTreeResponse,
  Comparison,
  Dashboard,
  Domain,
  DomainPortalResponse,
  HealthResponse,
  Job,
  ReadingStatus,
  Representation,
  RepresentationOpenAction,
  RepresentationPresentation,
  ResearchUnitSummary,
  SearchGroupBy,
  SearchMode,
  SearchSort,
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
  rawBody?: BodyInit;
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

async function request<T>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
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
      body:
        options.rawBody ??
        (options.body === undefined ? undefined : JSON.stringify(options.body)),
      headers: {
        Accept: "application/json, application/problem+json",
        ...(options.body === undefined
          ? {}
          : { "Content-Type": "application/json" }),
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
    if (error instanceof DOMException && error.name === "AbortError")
      throw error;
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

async function allPages<T>(
  path: string,
  signal?: AbortSignal,
): Promise<ApiList<T>> {
  const items: T[] = [];
  let cursor: string | null = null;
  let total: number | undefined;
  do {
    const page: ApiList<T> = normalizeList<T>(
      await request(`${path}${params({ cursor })}`, { signal }),
    );
    items.push(...page.items);
    total = page.total;
    cursor = page.next_cursor;
  } while (cursor);
  return { items, next_cursor: null, total: total ?? items.length };
}

export interface SearchParams {
  q: string;
  mode?: SearchMode;
  sort?: SearchSort;
  group_by?: SearchGroupBy;
  domain?: string;
  status?: ReadingStatus | "";
  author?: string;
  type?: string;
  year_from?: number;
  year_to?: number;
  date_from?: string;
  date_to?: string;
  tag?: string;
  has_pdf?: boolean;
  has_html?: boolean;
  has_code?: boolean;
  has_guide?: boolean;
  has_annotation?: boolean;
  cursor?: string;
  collection_id?: string;
  include_facets?: boolean;
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
        sort: searchParams.sort ?? "date_desc",
        group_by: searchParams.group_by,
        domain: searchParams.domain,
        include_facets: searchParams.include_facets,
        status: searchParams.status,
        author: searchParams.author,
        type: searchParams.type,
        year_from: searchParams.year_from,
        year_to: searchParams.year_to,
        date_from: searchParams.date_from,
        date_to: searchParams.date_to,
        tag: searchParams.tag,
        has_pdf: searchParams.has_pdf,
        has_html: searchParams.has_html,
        has_code: searchParams.has_code,
        has_guide: searchParams.has_guide,
        has_annotation: searchParams.has_annotation,
        cursor: searchParams.cursor,
        collection_id: searchParams.collection_id,
      })}`,
      { signal, timeout: SEARCH_TIMEOUT },
    ),
  );
}

export const api = {
  createDomain: (body: {
    label: string;
    description?: string;
    aliases?: string[];
  }) => request<Domain>(`${API_BASE}/domains`, { method: "POST", body }),
  updateDomain: (
    slug: string,
    body: { label?: string; description?: string; aliases?: string[] },
  ) =>
    request<Domain>(`${API_BASE}/domains/${encodeURIComponent(slug)}`, {
      method: "PATCH",
      body,
    }),
  intakeDrafts: async (signal?: AbortSignal) =>
    normalizeList<IntakeDraft>(
      await request(`${API_BASE}/intake/drafts`, { signal }),
    ),
  createIntakeDraft: (body: Record<string, unknown>) =>
    request<IntakeDraft>(`${API_BASE}/intake/drafts`, { method: "POST", body }),
  updateIntakeDraft: (id: string, body: Record<string, unknown>) =>
    request<IntakeDraft>(
      `${API_BASE}/intake/drafts/${encodeURIComponent(id)}`,
      { method: "PATCH", body },
    ),
  uploadIntakePdf: (id: string, file: File) =>
    request<IntakeDraft>(
      `${API_BASE}/intake/drafts/${encodeURIComponent(id)}/pdf${params({ filename: file.name })}`,
      {
        method: "PUT",
        rawBody: file,
        headers: { "Content-Type": "application/pdf" },
        timeout: 120_000,
      },
    ),
  previewIntakeDraft: (id: string) =>
    request<IntakePreview>(
      `${API_BASE}/intake/drafts/${encodeURIComponent(id)}/preview`,
    ),
  commitIntakeDraft: (
    id: string,
    body: { action: "create" | "attach"; asset_id?: string },
  ) =>
    request<{ asset_id: string; draft_id: string; status: string }>(
      `${API_BASE}/intake/drafts/${encodeURIComponent(id)}/commit`,
      { method: "POST", body, timeout: 120_000 },
    ),
  cancelIntakeDraft: (id: string) =>
    request<void>(`${API_BASE}/intake/drafts/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
  collections: (signal?: AbortSignal) =>
    allPages<Collection>(`${API_BASE}/collections`, signal),
  collection: (id: string, signal?: AbortSignal) =>
    request<Collection>(`${API_BASE}/collections/${encodeURIComponent(id)}`, {
      signal,
    }),
  createCollection: (body: { label: string; description?: string }) =>
    request<Collection>(`${API_BASE}/collections`, { method: "POST", body }),
  updateCollection: (
    id: string,
    body: { label: string; description?: string },
    rowVersion?: number,
  ) =>
    request<Collection>(`${API_BASE}/collections/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body,
      headers: ifMatch(rowVersion, { required: true }),
    }),
  deleteCollection: (id: string, rowVersion?: number) =>
    request<void>(`${API_BASE}/collections/${encodeURIComponent(id)}`, {
      method: "DELETE",
      headers: ifMatch(rowVersion, { required: true }),
    }),
  collectionAssets: async (id: string, signal?: AbortSignal, cursor?: string) =>
    normalizeList<Asset>(
      await request(
        `${API_BASE}/collections/${encodeURIComponent(id)}/members${params({ cursor })}`,
        { signal },
      ),
    ),
  collectionMembers: (id: string, assetIds: string[], remove = false) =>
    request<WorkflowResult>(
      `${API_BASE}/collections/${encodeURIComponent(id)}/members`,
      { method: remove ? "DELETE" : "POST", body: { asset_ids: assetIds } },
    ),
  savedSearches: (signal?: AbortSignal) =>
    allPages<SavedSearch>(`${API_BASE}/saved-searches`, signal),
  createSavedSearch: (body: {
    name: string;
    definition: Record<string, unknown>;
  }) =>
    request<SavedSearch>(`${API_BASE}/saved-searches`, {
      method: "POST",
      body,
    }),
  updateSavedSearch: (
    id: string,
    body: { name: string; definition: Record<string, unknown> },
    rowVersion?: number,
  ) =>
    request<SavedSearch>(
      `${API_BASE}/saved-searches/${encodeURIComponent(id)}`,
      {
        method: "PATCH",
        body,
        headers: ifMatch(rowVersion, { required: true }),
      },
    ),
  deleteSavedSearch: (id: string, rowVersion?: number) =>
    request<void>(`${API_BASE}/saved-searches/${encodeURIComponent(id)}`, {
      method: "DELETE",
      headers: ifMatch(rowVersion, { required: true }),
    }),
  bulkAssets: (body: Record<string, unknown>) =>
    request<WorkflowResult>(`${API_BASE}/assets/bulk`, {
      method: "POST",
      body,
    }),
  readingPosition: (id: string, signal?: AbortSignal) =>
    request<ReadingPositionResponse>(
      `${API_BASE}/assets/${encodeURIComponent(id)}/reading-position`,
      { signal },
    ),
  saveReadingPosition: (
    id: string,
    body: ReadingPosition,
    rowVersion?: number,
  ) =>
    request<ReadingPositionResponse>(
      `${API_BASE}/assets/${encodeURIComponent(id)}/reading-position`,
      { method: "PUT", body, headers: ifMatch(rowVersion) },
    ),
  exportPackage: (body: {
    domain?: string;
    asset_ids?: string[];
    include_user_state: boolean;
  }) =>
    request<{ job_id: string; status: string }>(`${API_BASE}/exports`, {
      method: "POST",
      body,
    }),
  importPackage: (file: File) =>
    request<PackageImport>(
      `${API_BASE}/imports/packages${params({ filename: file.name })}`,
      {
        method: "POST",
        rawBody: file,
        headers: { "Content-Type": "application/zip" },
        timeout: 120_000,
      },
    ),
  commitPackage: (
    id: string,
    body: {
      identity_mapping?: Record<string, string | null>;
      include_user_state: boolean;
    },
    rowVersion: number,
  ) =>
    request<PackageImport>(
      `${API_BASE}/imports/packages/${encodeURIComponent(id)}/commit`,
      {
        method: "POST",
        body,
        headers: ifMatch(rowVersion, { required: true }),
        timeout: 120_000,
      },
    ),
  exportDownloadUrl: (id: string) =>
    `${API_BASE}/exports/${encodeURIComponent(id)}/download`,
  linkCode: (body: {
    paper_id: string;
    url?: string;
    existing_asset_id?: string;
    local_directory?: string;
    kind: string;
    download: boolean;
    revision?: string;
  }) =>
    request<{
      repository_asset_id?: string;
      job_id?: string;
      [key: string]: unknown;
    }>(`${API_BASE}/code-links`, { method: "POST", body }),
  repositorySnapshot: (body: {
    repository_asset_id: string;
    revision?: string;
  }) =>
    request<{ job_id: string }>(`${API_BASE}/repository-snapshots`, {
      method: "POST",
      body,
    }),
  mcpConfig: (signal?: AbortSignal) =>
    request<McpConfig>(`${API_BASE}/integrations/mcp-config`, { signal }),
  mcpCheck: () =>
    request<McpCheck>(`${API_BASE}/integrations/mcp-check`, { method: "POST" }),
  health: (signal?: AbortSignal) =>
    // 必须经 API_BASE 拼接:裸相对路径 /api/health 在桌面壳(origin 为
    // tauri://localhost)下会被 SPA fallback 返回 index.html,status 解析为
    // undefined,健康徽章误报"部分服务不可用"。桌面壳走 /api/v1/health,
    // 浏览器托管模式经 web_host /api/* 代理到同一后端路由。
    // 冷启动时健康检查还会验证数据库结构，5 秒会把仍可用的服务误判为超时。
    request<HealthResponse>(`${API_BASE}/health`, { signal, timeout: 30_000 }),

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
    normalizeList<Job>(await request<unknown>(`${API_BASE}/jobs`, { signal })),

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
