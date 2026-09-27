import { getApiUrl } from "../config";
import { buildAuthHeaders } from "../authHeaders";

/**
 * Path scoping: the second isolation dimension inside one account.
 *
 * A path is a node in a tree that knowledge spaces and data tables can belong
 * to, and that agents are granted read access to. The account's root path is its
 * **public area** — resources not filed under any path are visible there, which
 * is why the tree marks it explicitly: "unfiled" means "public", not "isolated".
 *
 * A resource belongs to at most one path, so binding moves it rather than
 * copying the association. Sharing happens by granting a common ancestor.
 */

export interface PathNode {
  id: string;
  name: string;
  parent_id: string | null;
  is_root: boolean;
  children: PathNode[];
  /** Present on `/paths/tree` (the enriched view) only. */
  document_count?: number;
  table_count?: number;
  space_count?: number;
  agents?: string[];
  is_public?: boolean;
}

export interface PathResource {
  resource_kind: "knowledge_space" | "data_table";
  resource_id: string;
}

/**
 * An error carrying the HTTP status, so callers can distinguish "this backend
 * has no paths API" from a genuine failure.
 */
export class PathsApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "PathsApiError";
    this.status = status;
  }
}

/**
 * Turn a failed response into something a person can act on.
 *
 * The raw body is `{"detail":"Not Found"}` in the most common failure — a new
 * frontend talking to a backend that predates the paths API, which is exactly
 * what a deploy that updates static assets before restarting the app looks like.
 * Surfacing that JSON tells the user nothing; the message has to name the cause.
 */
async function failure(response: Response): Promise<PathsApiError> {
  let detail = "";
  try {
    detail = (await response.text()).slice(0, 500);
  } catch {
    /* body unreadable — the status alone is enough */
  }
  if (response.status === 404) {
    return new PathsApiError(
      404,
      "当前后端版本不含路径接口（404）。升级后需要重启应用，新的后端代码才会生效。",
    );
  }
  if (response.status === 401) {
    return new PathsApiError(401, "登录状态已失效，请重新登录。");
  }
  return new PathsApiError(
    response.status,
    `路径请求失败（${response.status}）${detail ? `：${detail}` : ""}`,
  );
}

async function apiGet<T>(path: string, params?: Record<string, unknown>): Promise<T> {
  const url = new URLSearchParams();
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      if (v !== undefined && v !== null) url.set(k, String(v));
    }
  }
  const query = url.toString();
  const apiPath = getApiUrl(path) + (query ? `?${query}` : "");
  const response = await fetch(apiPath, { headers: buildAuthHeaders() });
  if (!response.ok) throw await failure(response);
  return response.json();
}

async function apiSend<T>(path: string, method: string, body?: unknown): Promise<T> {
  const response = await fetch(getApiUrl(path), {
    method,
    headers: { ...buildAuthHeaders(), "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) throw await failure(response);
  return response.json();
}

export const pathsApi = {
  /** The plain tree — structure only, cheap. Used by pickers and drag targets. */
  list: () => apiGet<{ tree: PathNode[] }>("/paths"),

  /**
   * The tree enriched with per-node document/table counts and granted agents.
   * Separate from `list` because the counts require reaching the knowledge
   * service, so callers that only need structure should not pay for them.
   */
  tree: () => apiGet<{ tree: PathNode[] }>("/paths/tree"),

  /** Reverse view: which paths each agent of this account may read. */
  grants: () => apiGet<{ grants: Record<string, string[]> }>("/paths/grants"),

  create: (name: string, parentId?: string | null) =>
    apiSend<{ id: string; name: string; parent_id: string | null }>(
      "/paths",
      "POST",
      { name, parent_id: parentId ?? null },
    ),

  rename: (pathId: string, name: string) =>
    apiSend<{ id: string; name: string }>(`/paths/${pathId}`, "PATCH", { name }),

  move: (pathId: string, parentId: string | null) =>
    apiSend<{ id: string; parent_id: string | null }>(
      `/paths/${pathId}/move`,
      "POST",
      { parent_id: parentId },
    ),

  /**
   * Delete an **empty** path. The server refuses when the path still holds
   * resources, children, or granted agents — deleting would cascade their
   * bindings away, so the caller must move them out first.
   */
  remove: (pathId: string) =>
    apiSend<{ deleted: boolean; id: string }>(`/paths/${pathId}`, "DELETE"),

  resources: (pathId: string) =>
    apiGet<{ path_id: string; resources: PathResource[] }>(
      `/paths/${pathId}/resources`,
    ),

  bindResource: (pathId: string, kind: PathResource["resource_kind"], resourceId: string) =>
    apiSend<{ path_id: string; resource_id: string }>(
      `/paths/${pathId}/resources`,
      "PUT",
      { resource_kind: kind, resource_id: resourceId },
    ),

  unbindResource: (pathId: string, kind: string, resourceId: string) =>
    apiSend<{ unbound: boolean }>(
      `/paths/${pathId}/resources/${encodeURIComponent(kind)}/${encodeURIComponent(resourceId)}`,
      "DELETE",
    ),

  agents: (pathId: string) =>
    apiGet<{ path_id: string; agents: string[] }>(`/paths/${pathId}/agents`),

  bindAgent: (pathId: string, agentId: string) =>
    apiSend<{ path_id: string; agent_id: string }>(
      `/paths/${pathId}/agents`,
      "PUT",
      { agent_id: agentId },
    ),

  unbindAgent: (pathId: string, agentId: string) =>
    apiSend<{ unbound: boolean }>(
      `/paths/${pathId}/agents/${encodeURIComponent(agentId)}`,
      "DELETE",
    ),
};

export const listPaths = pathsApi.list;
export const pathTree = pathsApi.tree;
export const pathGrants = pathsApi.grants;

/** Flatten a tree to `id → node`, for lookups and ancestor walks. */
export function flattenPaths(nodes: PathNode[]): Map<string, PathNode> {
  const out = new Map<string, PathNode>();
  const walk = (list: PathNode[]) => {
    for (const node of list) {
      out.set(node.id, node);
      walk(node.children || []);
    }
  };
  walk(nodes);
  return out;
}

/** A node's ancestor chain (root first), used for breadcrumbs. */
export function pathBreadcrumb(nodes: PathNode[], pathId: string): PathNode[] {
  const chain: PathNode[] = [];
  const walk = (list: PathNode[], trail: PathNode[]): boolean => {
    for (const node of list) {
      const next = [...trail, node];
      if (node.id === pathId) {
        chain.push(...next);
        return true;
      }
      if (walk(node.children || [], next)) return true;
    }
    return false;
  };
  walk(nodes, []);
  return chain;
}
