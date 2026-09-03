/**
 * Per-user storage keys for agent selection state.
 *
 * Agent selection (last-used agent, per-agent chat ids) must be isolated
 * between accounts on the same browser: user A's last agent must never
 * become the default for admin or any other user. All storage keys used by
 * the agent store are therefore suffixed with the logged-in username.
 */
import { getAuthUser } from "../api/config";

/** localStorage key holding the logged-in username (written at login). */
export const AUTH_USER_KEY = "qwenpaw_auth_user";

/** Base keys shared by agentStore / authHeaders / workspace / messageQueue. */
export const AGENT_STORAGE_BASE = "qwenpaw-agent-storage";
export const LAST_USED_AGENT_BASE = "qwenpaw-last-used-agent";

/** Read the logged-in username from localStorage. Empty when not logged in
 *  or when upgrading from a build that did not record the username. */
export function getStorageUser(): string {
  try {
    return localStorage.getItem(AUTH_USER_KEY)?.trim() ?? "";
  } catch {
    return "";
  }
}

/** Agent-store persist key (sessionStorage + localStorage) for this user. */
export function agentStorageKey(): string {
  const user = getStorageUser();
  return user ? `${AGENT_STORAGE_BASE}:${user}` : AGENT_STORAGE_BASE;
}

/** Last-used-agent key for this user. */
export function lastUsedAgentKey(): string {
  const user = getStorageUser();
  return user ? `${LAST_USED_AGENT_BASE}:${user}` : LAST_USED_AGENT_BASE;
}

export { getAuthUser };
