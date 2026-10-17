import { request } from "../request";
import { getApiUrl, getApiToken } from "../config";
import { buildAuthHeaders, getAgentScopedApiPath } from "../authHeaders";
import {
  DownloadCancelledError,
  downloadFileFromUrl,
} from "../../utils/downloadFileFromUrl";
import type {
  ChatSpec,
  ChatHistory,
  ChatDeleteResponse,
  ChatUpdateRequest,
  BatchArchiveResult,
  Session,
} from "../types";

/** Response from POST /console/upload. url = filename only; agent_id from header. */
export interface ChatUploadResponse {
  url: string;
  file_name: string;
  stored_name?: string;
}

const FILES_PREVIEW = "/files/preview";

export const chatApi = {
  /** Upload a file for chat attachment. Returns URL path for content. */
  uploadFile: async (file: File): Promise<ChatUploadResponse> => {
    const formData = new FormData();
    formData.append("file", file);
    const response = await fetch(getApiUrl("/console/upload"), {
      method: "POST",
      headers: buildAuthHeaders(),
      body: formData,
    });
    if (!response.ok) {
      const text = await response.text().catch(() => "");
      throw new Error(
        `Upload failed: ${response.status} ${response.statusText}${
          text ? ` - ${text}` : ""
        }`,
      );
    }
    return response.json();
  },

  filePreviewUrl: (filename: string): string => {
    if (!filename) return "";
    if (filename.startsWith("http://") || filename.startsWith("https://"))
      return filename;
    let cleaned = filename.replace(/^\/+/, "");
    const path = `${FILES_PREVIEW}/${cleaned}`;
    const url = getApiUrl(path);

    const token = getApiToken();
    if (token) {
      return `${url}?token=${encodeURIComponent(token)}`;
    }

    return url;
  },
  listChats: (params?: {
    user_id?: string;
    channel?: string;
    archived?: boolean;
  }) => {
    const searchParams = new URLSearchParams();
    if (params?.user_id) searchParams.append("user_id", params.user_id);
    if (params?.channel) searchParams.append("channel", params.channel);
    if (params?.archived !== undefined)
      searchParams.append("archived", String(params.archived));
    const query = searchParams.toString();
    return request<ChatSpec[]>(`/chats${query ? `?${query}` : ""}`);
  },

  createChat: (chat: Partial<ChatSpec>) =>
    request<ChatSpec>("/chats", {
      method: "POST",
      body: JSON.stringify(chat),
    }),

  getChat: (chatId: string, options?: { signal?: AbortSignal }) =>
    request<ChatHistory>(`/chats/${encodeURIComponent(chatId)}`, {
      signal: options?.signal,
    }),

  updateChat: (chatId: string, chat: ChatUpdateRequest) =>
    request<ChatSpec>(`/chats/${encodeURIComponent(chatId)}`, {
      method: "PUT",
      body: JSON.stringify(chat),
    }),

  deleteChat: (chatId: string) =>
    request<ChatDeleteResponse>(`/chats/${encodeURIComponent(chatId)}`, {
      method: "DELETE",
    }),

  batchDeleteChats: (chatIds: string[]) =>
    request<{ success: boolean; deleted_count: number }>(
      "/chats/batch-delete",
      {
        method: "POST",
        body: JSON.stringify(chatIds),
      },
    ),

  archiveChat: (chatId: string) =>
    request<ChatSpec>(`/chats/${encodeURIComponent(chatId)}/archive`, {
      method: "POST",
    }),

  unarchiveChat: (chatId: string) =>
    request<ChatSpec>(`/chats/${encodeURIComponent(chatId)}/unarchive`, {
      method: "POST",
    }),

  batchArchiveChats: (chatIds: string[]) =>
    request<BatchArchiveResult>("/chats/actions/batch-archive", {
      method: "POST",
      body: JSON.stringify({ chat_ids: chatIds }),
    }),

  batchUnarchiveChats: (chatIds: string[]) =>
    request<BatchArchiveResult>("/chats/actions/batch-unarchive", {
      method: "POST",
      body: JSON.stringify({ chat_ids: chatIds }),
    }),

  stopChat: (chatId: string) =>
    request<void>(`/console/chat/stop?chat_id=${encodeURIComponent(chatId)}`, {
      method: "POST",
    }),

  /**
   * Download one conversation as a `.session.json` file.
   *
   * `fallbackName` is only used where the runtime cannot read the response
   * headers (the pywebview and Tauri save dialogs), so the server's
   * Content-Disposition filename is preferred when it is available — it carries
   * the chat name and the export date, which the client cannot reconstruct
   * reliably.
   */
  exportChat: async (chatId: string, fallbackName: string): Promise<void> => {
    const stem =
      (fallbackName || "session")
        .replace(/[<>:"/\\|?*]+/g, "_")
        .trim()
        .slice(0, 80) || "session";
    const stamp = new Date().toISOString().slice(0, 10).replace(/-/g, "");
    const url = getApiUrl(`/chats/${encodeURIComponent(chatId)}/export`);

    try {
      await downloadFileFromUrl(url, `${stem}-${stamp}.session.json`, {
        headers: buildAuthHeaders(),
        errorMessage: "Export failed",
        preferResponseFilename: true,
      });
    } catch (error) {
      if (error instanceof DownloadCancelledError) {
        return;
      }
      throw error;
    }
  },

  /**
   * Create a new conversation from an exported file and return it.
   *
   * Not routed through `request()`: that helper forces a JSON Content-Type on
   * writes, which breaks the multipart body. The server's validation messages
   * are surfaced verbatim — "wrong file type" is the most likely outcome and
   * the user needs to know which rule they hit.
   */
  importChat: async (file: File): Promise<ChatSpec> => {
    const formData = new FormData();
    formData.append("file", file);
    const response = await fetch(getApiUrl("/chats/import"), {
      method: "POST",
      headers: buildAuthHeaders(),
      body: formData,
    });
    if (!response.ok) {
      const detail = await response
        .json()
        .then((body) => (body as { detail?: string }).detail)
        .catch(() => undefined);
      throw new Error(
        detail || `Import failed: ${response.status} ${response.statusText}`,
      );
    }
    return response.json();
  },
};

export const sessionApi = {
  listSessions: (params?: { user_id?: string; channel?: string }) => {
    const searchParams = new URLSearchParams();
    if (params?.user_id) searchParams.append("user_id", params.user_id);
    if (params?.channel) searchParams.append("channel", params.channel);
    const query = searchParams.toString();
    return request<Session[]>(`/chats${query ? `?${query}` : ""}`);
  },

  getSession: (sessionId: string) =>
    request<ChatHistory>(`/chats/${encodeURIComponent(sessionId)}`),

  deleteSession: (sessionId: string) =>
    request<ChatDeleteResponse>(`/chats/${encodeURIComponent(sessionId)}`, {
      method: "DELETE",
    }),

  createSession: (session: Partial<Session>) =>
    request<Session>("/chats", {
      method: "POST",
      body: JSON.stringify(session),
    }),

  updateSession: (sessionId: string, session: ChatUpdateRequest) =>
    request<Session>(`/chats/${encodeURIComponent(sessionId)}`, {
      method: "PUT",
      body: JSON.stringify(session),
    }),
  shareSession: (sessionId: string, targetUsernames: string[]) =>
    request<{ created: number; skipped: number }>(
      getAgentScopedApiPath(`/chats/${encodeURIComponent(sessionId)}/share`),
      {
        method: "POST",
        body: JSON.stringify({ target_usernames: targetUsernames }),
      },
    ),

  batchDeleteSessions: (sessionIds: string[]) =>
    request<{ success: boolean; deleted_count: number }>(
      "/chats/batch-delete",
      {
        method: "POST",
        body: JSON.stringify(sessionIds),
      },
    ),
};
