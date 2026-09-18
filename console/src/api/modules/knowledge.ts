import { getApiUrl } from "../config";
import { buildAuthHeaders } from "../authHeaders";
import { request } from "../request";

export interface KnowledgeDocument {
  id: string;
  job_id?: string;
  filename: string;
  status: "queued" | "processing" | "completed" | "failed";
  processing_stage?: string | null;
  error?: string | null;
  attempts?: number;
  log_excerpt?: string | null;
  size?: number;
  created_at?: string;
  updated_at?: string;
}

export interface KnowledgeEvidence {
  id: string;
  document_id: string;
  filename: string;
  page?: number | null;
  location?: string | null;
  content: string;
  score?: number;
}

export interface KnowledgeSearchResult {
  query: string;
  evidence: KnowledgeEvidence[];
  documents: Array<{
    document_id: string;
    filename: string;
    snippet_count: number;
    pages: number[];
  }>;
  total_count: number;
  document_count: number;
  semantic_search_enabled: boolean;
}

export interface KnowledgeDocumentPreview {
  document_id: string;
  filename: string;
  content: string;
  truncated: boolean;
}

export interface KnowledgeSearchFilterResult {
  document_ids: string[];
  total_count: number;
  document_count: number;
}

export interface KnowledgeJobItem {
  id: string;
  document_id: string;
  filename: string;
  attempts: number;
  status: "queued" | "processing" | "completed" | "failed";
  error?: string | null;
  created_at?: string;
  updated_at?: string;
}

export interface KnowledgeDocumentStats {
  document_id: string;
  filename: string;
  status: string;
  error?: string | null;
  size: number;
  chunk_count: number;
  entity_count: number;
  relation_count: number;
  created_at?: string;
  started_at?: string;
  completed_at?: string;
  processing_duration?: number;
  processing_stage?: string | null;
}

export interface KnowledgeDocumentPage {
  items: KnowledgeDocument[];
  total: number;
  page: number;
  page_size: number;
  pending_count?: number;
}

export interface KnowledgeHealth {
  configured: boolean;
  status: string;
  service?: {
    upload_ready?: boolean;
    embedding_configured?: boolean;
    embedding_available?: boolean;
    upload_block_reason?: string | null;
    upload_notice?: string | null;
    worker?: {
      available?: boolean;
      status?: string | null;
      stage?: string | null;
      reason?: string | null;
    };
  };
}

export const knowledgeApi = {
  health: () => request<KnowledgeHealth>("/knowledge/health"),

  listDocuments: (params?: {
    filename?: string;
    created_after?: string;
    created_before?: string;
    doc_ids?: string;
    status?: KnowledgeDocument["status"];
    page?: number;
    page_size?: number;
  }) => {
    const query = new URLSearchParams();
    if (params?.filename) query.set("filename", params.filename);
    if (params?.created_after) query.set("created_after", params.created_after);
    if (params?.created_before) query.set("created_before", params.created_before);
    if (params?.doc_ids) query.set("doc_ids", params.doc_ids);
    if (params?.status) query.set("status", params.status);
    if (params?.page) query.set("page", String(params.page));
    if (params?.page_size) query.set("page_size", String(params.page_size));
    const qs = query.toString();
    return request<KnowledgeDocumentPage>(`/knowledge/documents${qs ? `?${qs}` : ""}`);
  },

  reprocessDocument: (documentId: string) =>
    request<KnowledgeDocument>(`/knowledge/documents/${encodeURIComponent(documentId)}/reprocess`, {
      method: "POST",
    }),

  deleteDocument: (documentId: string) =>
    request<{ deleted: boolean }>(`/knowledge/documents/${encodeURIComponent(documentId)}`, {
      method: "DELETE",
    }),

  deleteDocuments: async (documentIds: string[]): Promise<{ deleted: number }> => {
    let deleted = 0;
    for (const id of documentIds) {
      try {
        await request<{ deleted: boolean }>(`/knowledge/documents/${encodeURIComponent(id)}`, { method: "DELETE" });
        deleted++;
      } catch { /* skip failed */ }
    }
    return { deleted };
  },

  getDocumentStats: (documentId: string) =>
    request<KnowledgeDocumentStats>(`/knowledge/documents/${encodeURIComponent(documentId)}/stats`),

  downloadJobLog: async (jobId: string) => {
    const response = await fetch(getApiUrl(`/knowledge/jobs/${encodeURIComponent(jobId)}/log`), { headers: buildAuthHeaders() });
    if (!response.ok) throw new Error(await response.text());
    const blob = await response.blob(); const url = URL.createObjectURL(blob);
    const link = document.createElement("a"); link.href = url; link.download = `job-${jobId}.log`; link.click(); URL.revokeObjectURL(url);
  },

  searchFilter: (query: string) =>
    request<KnowledgeSearchFilterResult>("/knowledge/search/filter", {
      method: "POST",
      body: JSON.stringify({ query, top_k: 50 }),
    }),

  upload: async (file: File): Promise<KnowledgeDocument> => {
    const form = new FormData();
    form.append("file", file);
    const response = await fetch(getApiUrl("/knowledge/documents/upload"), {
      method: "POST",
      headers: buildAuthHeaders(),
      body: form,
    });
    if (!response.ok) {
      throw new Error(await response.text());
    }
    return response.json();
  },

  previewDocument: (documentId: string) =>
    request<KnowledgeDocumentPreview>(`/knowledge/documents/${encodeURIComponent(documentId)}/preview`),

  listJobs: () => request<KnowledgeJobItem[]>("/knowledge/jobs"),

  getDocumentChunks: async (documentId: string, maxChunks: number = 20) => {
    const response = await fetch(getApiUrl("/knowledge/documents/chunks"), {
      method: "POST",
      headers: {
        ...buildAuthHeaders(),
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ document_id: documentId, max_chunks: maxChunks }),
    });
    if (!response.ok) {
      throw new Error(await response.text());
    }
    return response.json();
  },

  getFileUrl: (documentId: string) => {
    return getApiUrl(`/knowledge/documents/${encodeURIComponent(documentId)}/file`);
  },

  getFileHeaders: () => {
    return buildAuthHeaders();
  },
};
