import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { AgentSummary } from "../api/types/agents";
import { agentsApi } from "../api/modules/agents";
import { menuRegistry } from "../plugins/registry/store";
import { agentStorageKey, lastUsedAgentKey } from "../utils/agentStorageKey";

/**
 * Base storage key. The active key is suffixed with the logged-in username
 * (see agentStorageKey()) so agent selection never leaks between accounts
 * on the same browser. The last-used-agent base key lives in
 * utils/agentStorageKey.ts.
 */
const STORAGE_KEY = "qwenpaw-agent-storage";

/** Returns true for temporary local session ids like 1785114733908-0l0jmai. */
const isLocalTimestamp = (id: string): boolean => /^\d+-[a-z0-9]+$/.test(id);

let agentRefreshPromise: Promise<void> | null = null;

interface AgentStore {
  selectedAgent: string;
  agents: AgentSummary[];
  /** Per-agent last active chat ID for restoring on agent switch */
  lastChatIdByAgent: Record<string, string>;
  setSelectedAgent: (agentId: string) => void;
  /**
   * Re-read the selected agent from per-user storage. Called after login:
   * the store is created on the login page before the username is known,
   * so the initial value cannot come from the user's own namespace.
   */
  hydrateFromStorage: () => void;
  setAgents: (agents: AgentSummary[]) => void;
  refreshAgents: () => Promise<void>;
  addAgent: (agent: AgentSummary) => void;
  removeAgent: (agentId: string) => void;
  updateAgent: (agentId: string, updates: Partial<AgentSummary>) => void;
  setLastChatId: (agentId: string, chatId: string) => void;
  removeLastChatId: (agentId: string) => void;
  getLastChatId: (agentId: string) => string | undefined;
}

/**
 * Determines the initial selectedAgent for this tab.
 *
 * Priority:
 *  1. sessionStorage (returning to a tab that already picked an agent)
 *  2. localStorage lastUsedAgent (new tab inherits the most recent choice)
 *  3. "default"
 */
function getInitialSelectedAgent(): string {
  const storageKey = agentStorageKey();
  const lastUsedKey = lastUsedAgentKey();
  // 1. sessionStorage: returning to a tab that already picked an agent
  try {
    const sessionValue = sessionStorage.getItem(storageKey);
    if (sessionValue) {
      const parsed = JSON.parse(sessionValue);
      const agent = parsed?.state?.selectedAgent;
      if (agent) return agent;
    }
  } catch {
    /* ignore */
  }
  // 2. Dedicated localStorage key (written by setSelectedAgent)
  try {
    const lastUsed = localStorage.getItem(lastUsedKey);
    if (lastUsed) return lastUsed;
  } catch {
    /* ignore */
  }
  // 3. Shared localStorage state (written by persist middleware)
  try {
    const shared = localStorage.getItem(storageKey);
    if (shared) {
      const parsed = JSON.parse(shared);
      const agent = parsed?.state?.selectedAgent;
      if (agent) return agent;
    }
  } catch {
    /* ignore */
  }
  return "default";
}

export const useAgentStore = create<AgentStore>()(
  persist(
    (set, get) => ({
      selectedAgent: getInitialSelectedAgent(),
      agents: [],
      lastChatIdByAgent: {},

      setSelectedAgent: (agentId) => {
        set({ selectedAgent: agentId });
        menuRegistry.refresh();
        // Persist to localStorage so new tabs inherit this choice
        try {
          localStorage.setItem(lastUsedAgentKey(), agentId);
        } catch {
          /* ignore */
        }
      },

      hydrateFromStorage: () => {
        const stored = getInitialSelectedAgent();
        if (stored && stored !== get().selectedAgent) {
          set({ selectedAgent: stored });
        }
        menuRegistry.refresh();
      },

      setAgents: (agents) => set({ agents }),

      refreshAgents: async () => {
        if (agentRefreshPromise !== null) {
          return agentRefreshPromise;
        }

        agentRefreshPromise = agentsApi.listAgents().then((response) => {
          const nextAgents = response.agents;
          const currentSelected = get().selectedAgent;
          const currentAgent = nextAgents.find(
            (agent) => agent.id === currentSelected && agent.enabled,
          );
          const fallbackAgent = nextAgents.find((agent) => agent.enabled) ??
            nextAgents[0];

          if (!currentAgent && fallbackAgent) {
            set({ agents: nextAgents, selectedAgent: fallbackAgent.id });
            try {
              localStorage.setItem(lastUsedAgentKey(), fallbackAgent.id);
            } catch {
              /* ignore */
            }
            menuRegistry.refresh();
          } else {
            set({ agents: nextAgents });
          }
        });
        try {
          await agentRefreshPromise;
        } finally {
          agentRefreshPromise = null;
        }
      },

      addAgent: (agent) =>
        set((state) => ({
          agents: [...state.agents, agent],
        })),

      removeAgent: (agentId) => {
        const shouldRefresh = get().selectedAgent === agentId;
        set((state) => {
          const remainingChatIds = { ...state.lastChatIdByAgent };
          delete remainingChatIds[agentId];
          return {
            agents: state.agents.filter((a) => a.id !== agentId),
            lastChatIdByAgent: remainingChatIds,
            ...(state.selectedAgent === agentId
              ? { selectedAgent: "default" }
              : {}),
          };
        });
        if (shouldRefresh) menuRegistry.refresh();
      },

      updateAgent: (agentId, updates) =>
        set((state) => ({
          agents: state.agents.map((a) =>
            a.id === agentId ? { ...a, ...updates } : a,
          ),
        })),

      setLastChatId: (agentId, chatId) => {
        // Never persist a temporary local timestamp id. These ids only exist
        // in memory before the first message is sent and should not be
        // restored on agent switch.
        if (isLocalTimestamp(chatId)) {
          set((state) => {
            const remainingChatIds = { ...state.lastChatIdByAgent };
            delete remainingChatIds[agentId];
            return { lastChatIdByAgent: remainingChatIds };
          });
          return;
        }
        set((state) => ({
          lastChatIdByAgent: { ...state.lastChatIdByAgent, [agentId]: chatId },
        }));
      },

      removeLastChatId: (agentId) =>
        set((state) => {
          const remainingChatIds = { ...state.lastChatIdByAgent };
          delete remainingChatIds[agentId];
          return { lastChatIdByAgent: remainingChatIds };
        }),

      getLastChatId: (agentId) => get().lastChatIdByAgent[agentId],
    }),
    {
      name: STORAGE_KEY,
      storage: {
        getItem: (name) => {
          // Keys are suffixed with the logged-in username so state never
          // leaks between accounts on the same browser.
          const key = agentStorageKey();
          try {
            // Read per-tab state from sessionStorage
            const value = sessionStorage.getItem(key);
            if (value) return JSON.parse(value);
          } catch {
            /* ignore */
          }
          // Fall back to localStorage for shared data (agents list, etc.)
          try {
            const shared = localStorage.getItem(key);
            return shared ? JSON.parse(shared) : null;
          } catch (error) {
            console.error(`Failed to parse agent storage "${name}":`, error);
            localStorage.removeItem(key);
            return null;
          }
        },
        setItem: (name, value) => {
          const key = agentStorageKey();
          try {
            // Per-tab state (includes selectedAgent)
            sessionStorage.setItem(key, JSON.stringify(value));
          } catch {
            /* ignore */
          }
          try {
            // Shared state (agents list, lastChatIdByAgent)
            localStorage.setItem(key, JSON.stringify(value));
          } catch (error) {
            console.error(`Failed to save agent storage "${name}":`, error);
          }
        },
        removeItem: (name) => {
          void name;
          const key = agentStorageKey();
          sessionStorage.removeItem(key);
          localStorage.removeItem(key);
        },
      },
    },
  ),
);
