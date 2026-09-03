import { getApiToken } from "./config";
import { agentStorageKey } from "../utils/agentStorageKey";

/** Authorization + X-Agent-Id for API requests. Caller sets Content-Type when needed. */
export function getStoredSelectedAgentId(): string {
  try {
    const storageKey = agentStorageKey();
    const agentStorage =
      sessionStorage.getItem(storageKey) || localStorage.getItem(storageKey);
    if (!agentStorage) return "";

    const selectedAgent = JSON.parse(agentStorage)?.state?.selectedAgent;
    return typeof selectedAgent === "string" ? selectedAgent : "";
  } catch (error) {
    console.warn("Failed to get selected agent from storage:", error);
    return "";
  }
}

/** Build a URL for APIs whose state belongs to the selected agent. */
export function getAgentScopedApiPath(path: string, agentId?: string): string {
  const selectedAgent = (
    agentId ||
    getStoredSelectedAgentId() ||
    "default"
  ).trim();
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  return `/agents/${encodeURIComponent(selectedAgent)}${normalizedPath}`;
}

export function buildAuthHeaders(): Record<string, string> {
  const headers: Record<string, string> = {};
  const token = getApiToken();
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }
  try {
    // Read from sessionStorage first (per-tab agent), fall back to localStorage
    const storageKey = agentStorageKey();
    const agentStorage =
      sessionStorage.getItem(storageKey) || localStorage.getItem(storageKey);
    if (agentStorage) {
      const selectedAgent = JSON.parse(agentStorage)?.state?.selectedAgent;
      if (selectedAgent) {
        headers["X-Agent-Id"] = selectedAgent;
      }
    }
  } catch (error) {
    console.warn("Failed to get selected agent from storage:", error);
  }
  return headers;
}
