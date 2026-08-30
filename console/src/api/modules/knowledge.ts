import { getApiUrl } from "../config";
import { buildAuthHeaders } from "../authHeaders";
import { request } from "../request";

export interface KnowledgeDocument {
  id: string;
  job_id?: string;
  filename: string;
  status: "queued" | "processing" | "completed" | "failed";
  error?: string | null;
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

export interface KnowledgeQueryResult {
  answer: string;
  evidence: KnowledgeEvidence[];
  relations: Array<{
    subject: string;
    predicate: string;
    object: string;
    filename?: string;
  }>;
  semantic_search_enabled: boolean;
}

export interface StreamQueryCallbacks {
  onEvidence?: (evidence: KnowledgeEvidence[]) => void;
  onAnswerChunk?: (chunk: string) => void;
  onDone?: () => void;
  onError?: (error: Error) => void;
}

export interface KnowledgeDocumentPreview {
  document_id: string;
  filename: string;
  preview: string;
  page_count: number;
}

export interface KnowledgeSearchFilterResult {
  document_ids: string[];
  total_count: number;
  document_count: number;
}

export interface KnowledgeJobItem {
  id: string;
  document_id: string;
  status: "queued" | "processing" | "completed" | "failed";
  error?: string | null;
  created_at?: string;
  updated_at?: string;
}

export const knowledgeApi = {
  health: () => request<{ configured: boolean; status: string }>("/knowledge/health"),

  listDocuments: (params?: {
    filename?: string;
    created_after?: string;
    created_before?: string;
    doc_ids?: string;
  }) => {
    const query = new URLSearchParams();
    if (params?.filename) query.set("filename", params.filename);
    if (params?.created_after) query.set("created_after", params.created_after);
    if (params?.created_before) query.set("created_before", params.created_before);
    if (params?.doc_ids) query.set("doc_ids", params.doc_ids);
    const qs = query.toString();
    return request<KnowledgeDocument[]>(`/knowledge/documents${qs ? `?${qs}` : ""}`);
  },

  reprocessDocument: (documentId: string) =>
    request<KnowledgeDocument>(`/knowledge/documents/${encodeURIComponent(documentId)}/reprocess`, {
      method: "POST",
    }),

  searchFilter: (query: string) =>
    request<KnowledgeSearchFilterResult>("/knowledge/search/filter", {
      method: "POST",
      body: JSON.stringify({ query, top_k: 50 }),
    }),

  query: (spaceId: string, query: string) =>
    request<KnowledgeQueryResult>("/knowledge/query", {
      method: "POST",
      timeout: 10 * 60 * 1000,
      body: JSON.stringify({ space_id: spaceId, query, top_k: 20 }),
    }),

  queryStream: async (spaceId: string, query: string, callbacks: StreamQueryCallbacks) => {
    const response = await fetch(getApiUrl("/knowledge/query/stream"), {
      method: "POST",
      headers: {
        ...buildAuthHeaders(),
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ space_id: spaceId, query, top_k: 20 }),
    });

    if (!response.ok) {
      throw new Error(await response.text());
    }

    const reader = response.body?.getReader();
    if (!reader) {
      throw new Error("Failed to get response reader");
    }

    const decoder = new TextDecoder();
    let buffer = "";

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";

        for (const line of lines) {
          if (line.startsWith("data: ")) {
            try {
              const data = JSON.parse(line.slice(6));
              if (data.type === "evidence") {
                callbacks.onEvidence?.(data.data);
              } else if (data.type === "answer") {
                callbacks.onAnswerChunk?.(data.data);
              } else if (data.type === "done") {
                callbacks.onDone?.();
              }
            } catch (e) {
              // Ignore parse errors
            }
          }
        }
      }
    } catch (error) {
      callbacks.onError?.(error instanceof Error ? error : new Error(String(error)));
    }
  },

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
