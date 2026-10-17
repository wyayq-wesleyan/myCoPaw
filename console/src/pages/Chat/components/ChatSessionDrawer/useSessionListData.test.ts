/**
 * Regression tests for cross-agent ownership in the shared session-list CRUD
 * handlers: a delete started under agent A must not mutate the view after the
 * user switches to agent B, while same-agent CRUD keeps working.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import React from "react";
import { App } from "antd";
import { MemoryRouter } from "react-router-dom";
import type { ChatSpec } from "../../../../api";
import api from "../../../../api";
import { chatApi } from "../../../../api/modules/chat";
import sessionApi from "../../sessionApi";
import { useAgentStore } from "../../../../stores/agentStore";
import { useSessionListStore } from "../../../../stores/sessionListStore";
import {
  useSessionListData,
  type ExtendedChatSession,
} from "./useSessionListData";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const A_CHAT = "33333333-aaaa-4aaa-8aaa-333333333333";

const makeSession = (id: string): ExtendedChatSession =>
  ({ id, name: id, messages: [] }) as unknown as ExtendedChatSession;

function wrapper({ children }: { children: ReactNode }) {
  return React.createElement(
    App,
    null,
    React.createElement(MemoryRouter, null, children),
  );
}

function renderListData(sessions: ExtendedChatSession[]) {
  const setSessions = vi.fn();
  const hook = renderHook(
    () =>
      useSessionListData(sessions, setSessions, {
        active: false,
        currentSessionId: A_CHAT,
        onSessionClick: vi.fn(),
      }),
    { wrapper },
  );
  return { hook, setSessions };
}

beforeEach(() => {
  sessionApi.resetForTests();
  useAgentStore.setState({ selectedAgent: "agent-a", lastChatIdByAgent: {} });
  useSessionListStore.setState({
    sessions: [],
    lastUpdated: 0,
    _setLibrarySessions: null,
  });
});

afterEach(() => {
  vi.restoreAllMocks();
  sessionApi.resetForTests();
});

describe("useSessionListData cross-agent ownership", () => {
  it("a delete finishing after an agent switch does not mutate the new view", async () => {
    sessionApi.setActiveAgent("agent-a");
    const onSessionRemoved = vi.fn();
    sessionApi.onSessionRemoved = onSessionRemoved;
    const newChatListener = vi.fn();
    window.addEventListener("qwenpaw:sidebar-new-chat", newChatListener);

    const dDelete = deferred<Awaited<ReturnType<typeof chatApi.deleteChat>>>();
    vi.spyOn(chatApi, "deleteChat").mockReturnValue(dDelete.promise);
    const listSpy = vi.spyOn(api, "listChats").mockResolvedValue([]);

    const { hook, setSessions } = renderListData([makeSession(A_CHAT)]);

    act(() => {
      hook.result.current.handleDelete(A_CHAT);
    });

    // The user switches to agent B while the backend delete is in flight.
    sessionApi.setActiveAgent("agent-b");
    useSessionListStore.setState({ sessions: [makeSession("chat-b")] });

    dDelete.resolve({} as Awaited<ReturnType<typeof chatApi.deleteChat>>);
    await act(async () => {
      await new Promise((res) => setTimeout(res, 0));
    });

    // The late completion must not touch B's view or callbacks.
    expect(onSessionRemoved).not.toHaveBeenCalled();
    expect(setSessions).not.toHaveBeenCalled();
    expect(useSessionListStore.getState().sessions.map((s) => s.id)).toEqual([
      "chat-b",
    ]);
    expect(newChatListener).not.toHaveBeenCalled();
    expect(listSpy).not.toHaveBeenCalled();

    window.removeEventListener("qwenpaw:sidebar-new-chat", newChatListener);
  });

  it("a same-agent delete still refreshes the list and fires callbacks", async () => {
    sessionApi.setActiveAgent("agent-a");
    const onSessionRemoved = vi.fn();
    sessionApi.onSessionRemoved = onSessionRemoved;

    vi.spyOn(chatApi, "deleteChat").mockResolvedValue(
      {} as Awaited<ReturnType<typeof chatApi.deleteChat>>,
    );
    vi.spyOn(api, "listChats").mockResolvedValue([] as ChatSpec[]);

    const { hook, setSessions } = renderListData([makeSession(A_CHAT)]);

    await act(async () => {
      hook.result.current.handleDelete(A_CHAT);
      await new Promise((res) => setTimeout(res, 0));
    });

    expect(onSessionRemoved).toHaveBeenCalledWith(A_CHAT);
    expect(setSessions).toHaveBeenCalled();
  });

  it("refetches the list immediately when the selected agent changes", async () => {
    sessionApi.setActiveAgent("agent-a");
    const listSpy = vi.spyOn(api, "listChats").mockResolvedValue([]);
    const setSessions = vi.fn();

    renderHook(
      () =>
        useSessionListData([], setSessions, {
          active: true,
          currentSessionId: undefined,
          onSessionClick: vi.fn(),
        }),
      { wrapper },
    );
    await waitFor(() => expect(listSpy).toHaveBeenCalledTimes(1));

    act(() => {
      useAgentStore.setState({ selectedAgent: "agent-b" });
    });

    await waitFor(() => expect(listSpy).toHaveBeenCalledTimes(2));
  });

});
describe("useSessionListData session file transfer", () => {
  // 侧边栏（simple mode）与抽屉共用这个 hook。导出/导入原先只接在抽屉上，
  // 于是简单模式下的用户根本看不到这两个入口 —— 这组测试钉住 hook 这一层，
  // 两处列表都靠它。
  const BACKEND_ID = "11111111-1111-4111-8111-111111111111";

  it("handleExport uses the backend id and passes the chat name", async () => {
    sessionApi.setActiveAgent("agent-a");
    const exportSpy = vi
      .spyOn(chatApi, "exportChat")
      .mockResolvedValue(undefined);
    const session = {
      ...makeSession("s1"),
      realId: BACKEND_ID,
      name: "季度复盘",
    } as ExtendedChatSession;

    const { hook } = renderListData([session]);

    await act(async () => {
      await hook.result.current.handleExport("s1");
    });

    expect(exportSpy).toHaveBeenCalledWith(BACKEND_ID, "季度复盘");
  });

  it("handleImportFile opens the newly imported chat", async () => {
    sessionApi.setActiveAgent("agent-a");
    vi.spyOn(chatApi, "importChat").mockResolvedValue({
      id: "imported-uuid",
      name: "Imported Chat",
      session_id: "console:imported:abc",
    } as Awaited<ReturnType<typeof chatApi.importChat>>);
    const listSpy = vi.spyOn(api, "listChats").mockResolvedValue([] as ChatSpec[]);
    const onSessionClick = vi.fn();

    const hook = renderHook(
      () =>
        useSessionListData([], vi.fn(), {
          active: false,
          currentSessionId: undefined,
          onSessionClick,
        }),
      { wrapper },
    );
    const before = listSpy.mock.calls.length;

    await act(async () => {
      await hook.result.current.handleImportFile(
        new File(["{}"], "chat.session.json", { type: "application/json" }),
      );
      await new Promise((res) => setTimeout(res, 0));
    });

    // 刷新必须在跳转之前：切换是按列表解析的，没进列表就会跳到一个找不到的会话。
    expect(listSpy.mock.calls.length).toBeGreaterThan(before);
    expect(onSessionClick).toHaveBeenCalledWith("imported-uuid");
  });

  it("a rejected import does not navigate", async () => {
    sessionApi.setActiveAgent("agent-a");
    vi.spyOn(chatApi, "importChat").mockRejectedValue(
      new Error("Not a session export file (missing the expected format marker)"),
    );
    const onSessionClick = vi.fn();

    const hook = renderHook(
      () =>
        useSessionListData([], vi.fn(), {
          active: false,
          currentSessionId: undefined,
          onSessionClick,
        }),
      { wrapper },
    );

    await act(async () => {
      await hook.result.current.handleImportFile(
        new File(["{}"], "notes.json", { type: "application/json" }),
      );
      await new Promise((res) => setTimeout(res, 0));
    });

    expect(onSessionClick).not.toHaveBeenCalled();
  });
});
