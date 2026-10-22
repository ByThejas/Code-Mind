// Client for the CODEMIND API (apps/api). In development Vite proxies /api and /health
// to the API server, so requests stay same-origin; set VITE_API_URL to call it directly.

const API_BASE = (import.meta.env.VITE_API_URL ?? "").replace(/\/$/, "");

export type SnapshotStatus =
  | "queued"
  | "fetching"
  | "indexing"
  | "ready"
  | "partial"
  | "failed"
  | "cancelled";

export type JobStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";

export interface Snapshot {
  id: string;
  commit_sha: string;
  requested_ref: string | null;
  status: SnapshotStatus;
  stats: {
    files_total?: number;
    files_included?: number;
    files_excluded?: Record<string, number>;
    [key: string]: unknown;
  };
  warnings: { code: string; [key: string]: unknown }[];
  created_at: string;
  indexed_at: string | null;
}

export interface Repository {
  id: string;
  provider: string;
  owner: string;
  name: string;
  default_branch: string;
  visibility: string;
  created_at: string;
  latest_snapshot: Snapshot | null;
}

export interface Job {
  id: string;
  snapshot_id: string | null;
  type: string;
  status: JobStatus;
  stage: string | null;
  progress: number;
  attempt: number;
  cancel_requested: boolean;
  error_code: string | null;
  error_message: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface IndexResponse {
  repository: Repository;
  snapshot: Snapshot;
  job: Job | null;
}

export interface Evidence {
  id: string;
  chunk_id: string;
  path: string;
  language: string | null;
  start_line: number;
  end_line: number;
  symbol_name: string | null;
  retrieved_by: string[];
  score: number;
  text: string;
}

export interface Claim {
  text: string;
  evidence_ids: string[];
  status: "supported" | "hypothesis" | "unverified";
}

export interface Answer {
  summary: string;
  claims: Claim[];
  limitations: string[];
  suggested_next_steps: string[];
}

export interface Investigation {
  id: string;
  repository_id: string;
  snapshot_id: string;
  commit_sha: string;
  question: string;
  status: "succeeded" | "failed";
  answer: Answer | null;
  evidence: Evidence[];
  validation: Record<string, unknown>;
  error_code: string | null;
  error_message: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
  finished_at: string | null;
}

export interface InvestigationSummary {
  id: string;
  snapshot_id: string;
  question: string;
  status: string;
  created_at: string;
}

export interface FileContent {
  snapshot_id: string;
  commit_sha: string;
  path: string;
  language: string | null;
  start_line: number;
  end_line: number;
  line_count: number;
  text: string;
}

export interface GraphInfo {
  available: boolean;
  stats: Record<string, unknown>;
  warnings: { code: string; [key: string]: unknown }[];
}

export interface GraphNode {
  id: string;
  type: "file" | "symbol" | "module";
  label: string;
  path: string;
  kind: string | null;
  qualified_name: string | null;
  start_line: number | null;
  end_line: number | null;
  file_count: number | null;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  relation: string;
  resolution: "static" | "inferred";
  confidence: number;
  provenance: string;
  path: string;
  line: number;
  count: number;
}

export interface GraphResponse {
  snapshot_id: string;
  commit_sha: string;
  nodes: GraphNode[];
  edges: GraphEdge[];
  truncated: boolean;
  graph: GraphInfo;
}

export interface CodeSymbol {
  id: string;
  name: string;
  qualified_name: string;
  kind: string;
  path: string;
  start_line: number;
  end_line: number;
}

export interface SymbolDetail {
  snapshot_id: string;
  commit_sha: string;
  symbol: CodeSymbol;
  parent: CodeSymbol | null;
  children: CodeSymbol[];
  outgoing: GraphEdge[];
  incoming: GraphEdge[];
  nodes: GraphNode[];
  truncated: boolean;
  graph: GraphInfo;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...init,
      headers: { "Content-Type": "application/json", ...init?.headers },
    });
  } catch {
    throw new ApiError(0, "network_error", "Could not reach the CODEMIND API.");
  }
  const body = await response.json().catch(() => null);
  if (!response.ok) {
    const error = body?.error;
    throw new ApiError(
      response.status,
      error?.code ?? "http_error",
      error?.message ?? `Request failed (${response.status}).`,
    );
  }
  return body as T;
}

function query(params: Record<string, string | number | null | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null) search.set(key, String(value));
  }
  return search.toString();
}

export const api = {
  health: () => request<unknown>("/health"),

  listRepositories: () =>
    request<{ items: Repository[]; next_cursor: string | null }>(
      "/api/v1/repositories?limit=100",
    ),

  importRepository: (url: string, ref?: string) =>
    request<IndexResponse>("/api/v1/repositories", {
      method: "POST",
      body: JSON.stringify({ url, ref: ref || null }),
    }),

  reindexRepository: (repositoryId: string, force = true) =>
    request<IndexResponse>(`/api/v1/repositories/${repositoryId}/index`, {
      method: "POST",
      body: JSON.stringify({ force }),
    }),

  getJob: (jobId: string) => request<Job>(`/api/v1/jobs/${jobId}`),

  cancelJob: (jobId: string) =>
    request<Job>(`/api/v1/jobs/${jobId}/cancel`, { method: "POST" }),

  search: (repositoryId: string, q: string, limit = 10) =>
    request<{ snapshot_id: string; commit_sha: string; items: Evidence[] }>(
      `/api/v1/repositories/${repositoryId}/search?${query({ q, limit })}`,
    ),

  ask: (repositoryId: string, question: string) =>
    request<Investigation>(`/api/v1/repositories/${repositoryId}/investigations`, {
      method: "POST",
      body: JSON.stringify({ question }),
    }),

  listInvestigations: (repositoryId: string) =>
    request<{ items: InvestigationSummary[]; next_cursor: string | null }>(
      `/api/v1/repositories/${repositoryId}/investigations?limit=20`,
    ),

  getInvestigation: (investigationId: string) =>
    request<Investigation>(`/api/v1/investigations/${investigationId}`),

  moduleGraph: (repositoryId: string, depth: number, pathPrefix?: string | null) =>
    request<GraphResponse>(
      `/api/v1/repositories/${repositoryId}/graph/modules?${query({
        depth,
        path_prefix: pathPrefix || undefined,
      })}`,
    ),

  searchSymbols: (repositoryId: string, q: string, limit = 30) =>
    request<{ snapshot_id: string; commit_sha: string; items: CodeSymbol[] }>(
      `/api/v1/repositories/${repositoryId}/symbols?${query({ q, limit })}`,
    ),

  symbolDetail: (repositoryId: string, symbolId: string, minConfidence = 0) =>
    request<SymbolDetail>(
      `/api/v1/repositories/${repositoryId}/symbols/${symbolId}?${query({
        min_confidence: minConfidence,
      })}`,
    ),

  readFile: (
    repositoryId: string,
    path: string,
    startLine: number,
    endLine: number,
    snapshotId?: string,
  ) =>
    request<FileContent>(
      `/api/v1/repositories/${repositoryId}/files/content?${query({
        path,
        start_line: startLine,
        end_line: endLine,
        snapshot_id: snapshotId,
      })}`,
    ),
};

export const ACTIVE_SNAPSHOT_STATUSES: SnapshotStatus[] = ["queued", "fetching", "indexing"];

export function isServable(repository: Repository): boolean {
  const status = repository.latest_snapshot?.status;
  return status === "ready" || status === "partial";
}

export function repositoryName(repository: Repository): string {
  return `${repository.owner}/${repository.name}`;
}
