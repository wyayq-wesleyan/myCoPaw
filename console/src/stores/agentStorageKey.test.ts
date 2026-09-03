import { describe, it, expect, beforeEach } from "vitest";
import {
  getStorageUser,
  agentStorageKey,
  lastUsedAgentKey,
  AUTH_USER_KEY,
  AGENT_STORAGE_BASE,
  LAST_USED_AGENT_BASE,
} from "../utils/agentStorageKey";

describe("agentStorageKey (per-user isolation)", () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
  });

  it("returns empty user when not logged in", () => {
    expect(getStorageUser()).toBe("");
  });

  it("returns the recorded username after login", () => {
    localStorage.setItem(AUTH_USER_KEY, "admin");
    expect(getStorageUser()).toBe("admin");
  });

  it("suffixed keys differ between users", () => {
    localStorage.setItem(AUTH_USER_KEY, "a");
    const keyA = agentStorageKey();
    const lastA = lastUsedAgentKey();
    localStorage.setItem(AUTH_USER_KEY, "admin");
    const keyAdmin = agentStorageKey();
    const lastAdmin = lastUsedAgentKey();

    expect(keyA).toBe(`${AGENT_STORAGE_BASE}:a`);
    expect(keyAdmin).toBe(`${AGENT_STORAGE_BASE}:admin`);
    expect(keyA).not.toBe(keyAdmin);
    expect(lastA).toBe(`${LAST_USED_AGENT_BASE}:a`);
    expect(lastAdmin).toBe(`${LAST_USED_AGENT_BASE}:admin`);
    expect(lastA).not.toBe(lastAdmin);
  });

  it("falls back to unsuffixed keys when no user recorded", () => {
    expect(agentStorageKey()).toBe(AGENT_STORAGE_BASE);
    expect(lastUsedAgentKey()).toBe(LAST_USED_AGENT_BASE);
  });

  it("simulated isolation: user a's last agent is not visible to admin", () => {
    // 用户 a 登录并选择了 agent-x
    localStorage.setItem(AUTH_USER_KEY, "a");
    localStorage.setItem(lastUsedAgentKey(), "agent-x");

    // admin 登录，读取自己的键 —— 读不到 a 的选择
    localStorage.setItem(AUTH_USER_KEY, "admin");
    expect(localStorage.getItem(lastUsedAgentKey())).toBeNull();
  });
});
