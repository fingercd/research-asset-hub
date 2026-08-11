export type ReadingStatus = "unread" | "reading" | "read";
export type SearchMode = "exact" | "keyword";
export type SearchGroupBy = "paper" | "none";
export type RepresentationPresentation = "hub" | "original";
export type QuickActionKind = "pdf" | "html" | "code";
export type QuickActionStatus =
  | "available"
  | "missing"
  | "not_provided"
  | "network_required";
export type ResearchUnitMatchSource =
  | "paper"
  | "related_repository"
  | "paper_and_related_repository"
  | "repository"
  | "browse";

export interface ApiList<T> {
  items: T[];
  next_cursor: string | null;
  total?: number;
}

export interface Representation {
  id: string;
  asset_id?: string;
  representation_type?: string;
  type?: string;
  role?: string;
  label?: string;
  materialization_kind?: string;
  availability?: string;
  path_ref?: string | null;
  entrypoint?: string | null;
  external_url?: string | null;
  open_url?: string | null;
  url?: string | null;
  mime_type?: string | null;
  size_bytes?: number | null;
  sha256?: string | null;
  network_required?: boolean;
  local_available?: boolean;
  local_reason?: string | null;
  attributes?: Record<string, unknown>;
  row_version?: number;
}

export interface RepresentationOpenAction {
  kind?: "gateway_session" | "external_url" | string;
  url: string;
  representation_id?: string;
  expires_in?: number;
  network_required?: boolean;
  presentation?: RepresentationPresentation;
  embed?: boolean;
  warning?: string | null;
  editor?: EditorOpenAction | null;
}

export interface CodeTreeEntry {
  name: string;
  path: string;
  kind: "directory" | "file" | string;
  size_bytes?: number | null;
  language?: string | null;
}

export interface CodeTreeResponse {
  representation_id: string;
  repository_name: string;
  path: string;
  entries: CodeTreeEntry[];
  truncated?: boolean;
}

export interface CodeFileResponse {
  representation_id: string;
  path: string;
  language: string;
  size_bytes: number;
  line_count: number;
  content: string | null;
  previewable: boolean;
  truncated?: boolean;
  reason?: string | null;
}

export interface EditorOpenAction {
  available: boolean;
  editor: "vscode" | string;
  path?: string | null;
  uri?: string | null;
  reason?: string | null;
}

export interface Annotation {
  id: string;
  asset_id: string;
  representation_id?: string | null;
  kind?: string;
  title?: string | null;
  summary?: string | null;
  body_markdown?: string;
  body?: string;
  pinned?: boolean;
  created_at?: string;
  updated_at?: string;
  row_version?: number;
}

export interface Asset {
  id: string;
  asset_type?: string;
  type?: string;
  title: string;
  title_zh?: string | null;
  subtitle?: string | null;
  authors?: string[];
  year?: number | null;
  venue?: string | null;
  language?: string | null;
  abstract?: string | null;
  summary?: string | null;
  content?: string | null;
  analysis?: Record<string, unknown>;
  attributes?: Record<string, unknown>;
  domain_slugs?: string[];
  domains?: Array<string | { slug: string; label?: string }>;
  tags?: string[];
  source_urls?: string[];
  reading_status?: ReadingStatus;
  reading_state?: { status?: ReadingStatus } | ReadingStatus | null;
  annotation_summary?: string | null;
  annotations?: Annotation[];
  representations?: Representation[];
  score?: number;
  updated_at?: string;
  created_at?: string;
  deleted_at?: string | null;
  row_version?: number;
  [key: string]: unknown;
}

export interface QuickActionAlternative {
  representation_id?: string | null;
  owner_asset_id: string;
  label?: string | null;
  role?: string | null;
  status: QuickActionStatus;
  relation_type?: string | null;
}

export interface QuickAction {
  kind: QuickActionKind;
  status: QuickActionStatus;
  representation_id?: string | null;
  owner_asset_id: string | null;
  relation_type?: string | null;
  reason?: string | null;
  alternatives?: QuickActionAlternative[];
}

export interface ResearchUnitSummary {
  unit_id: string;
  unit_kind: string;
  primary_asset: Asset;
  related_repositories: Asset[];
  quick_actions: QuickAction[];
  matched_assets: Asset[];
  covered_asset_count: number;
  score?: number | null;
  snippet?: string | null;
  match_source?: ResearchUnitMatchSource | string | null;
}

export function isResearchUnitSummary(
  value: Asset | ResearchUnitSummary,
): value is ResearchUnitSummary {
  return "primary_asset" in value && "quick_actions" in value;
}

export interface Dashboard {
  stats?: {
    assets?: number;
    papers?: number;
    unread?: number;
    reading?: number;
    read?: number;
    domains?: number;
    relations?: number;
    annotations?: number;
  };
  total_assets?: number;
  total_papers?: number;
  unread_count?: number;
  reading_count?: number;
  read_count?: number;
  domain_count?: number;
  relation_count?: number;
  annotation_count?: number;
  recent_assets?: Asset[];
  continue_reading?: Asset[];
  domains?: Domain[];
  index?: IndexState;
  index_state?: IndexState;
}

export interface IndexState {
  status?: "ready" | "building" | "stale" | "unavailable" | string;
  indexed_assets?: number;
  pending_assets?: number;
  updated_at?: string;
  detail?: string;
}

export interface Domain {
  slug: string;
  label?: string;
  name?: string;
  description?: string | null;
  asset_count?: number;
  paper_count?: number;
  repository_count?: number;
  research_unit_count?: number;
  annotation_count?: number;
  reading_counts?: Partial<Record<ReadingStatus, number>>;
  overview_status?:
    | "available"
    | "missing"
    | "network_required"
    | "unavailable"
    | string;
  overview?: DomainOverview | null;
  tags?: string[];
  recent_assets?: Asset[];
  updated_at?: string;
  [key: string]: unknown;
}

export interface DomainOverview {
  asset_id: string | null;
  representation_id: string | null;
  status: "available" | "missing" | "network_required" | "unavailable";
  default_presentation: RepresentationPresentation;
  last_verified_at: string | null;
  warning: string | null;
}

export type DomainPortalReviewStatus =
  | "generated"
  | "reviewed"
  | "not_applicable"
  | "needs_review";

export interface DomainPortalSection {
  id:
    | "scope"
    | "timeline"
    | "capability-method-map"
    | "landmarks"
    | "benchmarks"
    | "reading-routes"
    | "catalog"
    | "frontiers"
    | "evidence";
  kind: string;
  title: string;
  status: DomainPortalReviewStatus;
  content: Record<string, unknown>;
}

export interface DomainPortal {
  schema: "research-asset-hub.domain-portal/v2";
  slug: string;
  label: string;
  description: string;
  content_version: string;
  content_digest: string;
  review_status: DomainPortalReviewStatus;
  stale: boolean;
  stale_reasons: Array<Record<string, unknown>>;
  original_selector: {
    source_id: string;
    source_record_id: string;
  };
  sections: DomainPortalSection[];
}

export interface DomainPortalResponse {
  schema:
    | "research-asset-hub.domain-portal-response/v2"
    | "research-asset-hub.domain-portal/v1";
  renderer: "v1" | "v2";
  domain: Domain;
  portal: DomainPortal | null;
}

export interface GraphNode {
  id: string;
  label?: string;
  title?: string;
  asset_type?: string;
  domain_slugs?: string[];
  year?: number | null;
  reading_status?: ReadingStatus;
}

export interface GraphEdge {
  id?: string;
  source?: string;
  target?: string;
  subject_asset_id?: string;
  object_asset_id?: string;
  relation_type?: string;
  label?: string;
  confidence?: number | null;
}

export interface GraphResponse {
  nodes: GraphNode[];
  edges: GraphEdge[];
  truncated?: boolean;
}

export interface CodeGraphRepository {
  id: string;
  title: string;
  title_zh?: string | null;
  asset_type?: string;
  year?: number | null;
  domain_slugs?: string[];
  paths?: string[];
  representation_count?: number;
  gitnexus_indexed?: boolean;
  gitnexus_repo_name?: string | null;
  hidden?: boolean;
}

export interface CodeGraphDomain {
  slug: string;
  label: string;
  repository_count: number;
  indexed_count: number;
  repositories: CodeGraphRepository[];
  hidden_repositories?: CodeGraphRepository[];
  hidden_count?: number;
}

export interface CodeGraphResponse {
  total_count: number;
  indexed_count: number;
  hidden_count?: number;
  domains: CodeGraphDomain[];
}

export interface Comparison {
  id?: string;
  title?: string;
  asset_ids?: string[];
  assets?: Asset[];
  columns?: Array<{
    key?: string;
    label?: string;
    values?: Record<string, string | string[] | number | null>;
  }>;
  synthesis?: string | null;
  created_at?: string;
  [key: string]: unknown;
}

export interface Job {
  id: string;
  type?: string;
  name?: string;
  status?: "pending" | "running" | "succeeded" | "failed" | "cancelled" | string;
  progress?: number | null;
  detail?: string | null;
  error?: string | { detail?: string; message?: string } | null;
  created_at?: string;
  started_at?: string | null;
  finished_at?: string | null;
  row_version?: number;
  [key: string]: unknown;
}

export interface SyncConflict {
  id: string;
  entity_type?: string;
  asset_id?: string | null;
  title?: string;
  field?: string;
  status?: string;
  local_value?: unknown;
  incoming_value?: unknown;
  source?: string;
  detected_at?: string;
  row_version?: number;
  [key: string]: unknown;
}

export interface Settings {
  app?: {
    mode?: string;
    database_status?: string;
    database_revision?: string;
    gateway_origin?: string;
    gitnexus_url?: string;
  };
  search?: Record<string, unknown>;
  sources?: Array<Record<string, unknown>>;
  integrations?: Array<Record<string, unknown>>;
  [key: string]: unknown;
}

export interface ComponentHealth {
  status: "healthy" | "unavailable" | "stale" | string;
  detail?: string;
  latency_ms?: number | null;
}

export interface HealthResponse {
  service: string;
  status: "healthy" | "degraded";
  checked_at: string;
  components: Record<string, ComponentHealth>;
}

export function getReadingStatus(asset: Asset): ReadingStatus {
  if (asset.reading_status) return asset.reading_status;
  if (typeof asset.reading_state === "string") return asset.reading_state;
  return asset.reading_state?.status ?? "unread";
}

export function getAssetDomains(asset: Asset): string[] {
  if (asset.domain_slugs?.length) return asset.domain_slugs;
  return (asset.domains ?? []).map((domain) =>
    typeof domain === "string" ? domain : domain.slug,
  );
}

export function getRepresentationType(representation: Representation): string {
  return (representation.representation_type ?? representation.type ?? "").toLowerCase();
}
