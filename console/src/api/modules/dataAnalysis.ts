import { getApiUrl } from "../config";
import { buildAuthHeaders } from "../authHeaders";

export interface DataTableItem {
  id: string;
  table_name: string;
  display_name: string;
  description: string;
  column_count: number;
  row_count: number;
  columns: Array<{ comment: string; column_name: string | null; type: string | null; nullable: boolean; is_primary_key: boolean }>;
  db_name: string;
  initialized: boolean;
  import_count?: number;
  total_imported?: number;
  created_at: string;
  updated_at: string;
}

export interface DataImportJob {
  id: string;
  table_id: string;
  table_name: string;
  filename: string;
  status: string;
  total_rows: number;
  success_rows: number;
  failed_rows: number;
  errors: unknown[];
  created_at: string;
  finished_at: string | null;
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
  const response = await fetch(apiPath, {
    headers: buildAuthHeaders(),
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.json();
}

async function apiPost<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(getApiUrl(path), {
    method: "POST",
    headers: {
      ...buildAuthHeaders(),
      "Content-Type": "application/json",
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.json();
}

async function apiDelete<T>(path: string): Promise<T> {
  const response = await fetch(getApiUrl(path), {
    method: "DELETE",
    headers: buildAuthHeaders(),
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.json();
}

async function apiUpload(path: string, files: File[]): Promise<unknown> {
  const form = new FormData();
  for (const file of files) {
    form.append("files", file);
  }
  const response = await fetch(getApiUrl(path), {
    method: "POST",
    headers: buildAuthHeaders(),
    body: form,
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.json();
}

export const dataAnalysisApi = {
  listTables: (params: Record<string, unknown>) =>
    apiGet<{ items: DataTableItem[]; total: number }>("/data-tables", params),

  getTable: (id: string) =>
    apiGet<DataTableItem>(`/data-tables/${id}`),

  createTable: (data: { display_name: string; description: string; columns: string[] }) =>
    apiPost<{ id: string; table_name: string }>("/data-tables", data),

  deleteTable: (id: string) =>
    apiDelete<{ ok: boolean }>(`/data-tables/${id}`),

  batchDeleteTables: (ids: string[]) =>
    apiPost<{ ok: boolean; deleted: number }>("/data-tables/batch-delete", { ids }),

  previewTable: (id: string, limit: number = 100) =>
    apiGet<{ columns: string[]; rows: Record<string, unknown>[]; total: number }>(`/data-tables/${id}/preview`, { limit }),

  importFiles: (tableId: string, files: File[]) =>
    apiUpload(`/data-tables/${tableId}/import`, files),

  getImportJobs: (params: Record<string, unknown>) =>
    apiGet<{ items: DataImportJob[]; total: number }>("/data-import-jobs", params),

  getImportJob: (id: string) =>
    apiGet<DataImportJob>(`/data-import-jobs/${id}`),
};

// Re-export convenience functions
export const listTables = dataAnalysisApi.listTables;
export const getTableDetail = dataAnalysisApi.getTable;
export const createTable = dataAnalysisApi.createTable;
export const deleteTable = dataAnalysisApi.deleteTable;
export const batchDeleteTables = dataAnalysisApi.batchDeleteTables;
export const previewTable = dataAnalysisApi.previewTable;
export const importFiles = dataAnalysisApi.importFiles;
export const getImportJobs = dataAnalysisApi.getImportJobs;
