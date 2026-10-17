import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { useSessions } from "./useSessions";

const mockMessage = { success: vi.fn(), error: vi.fn() };

vi.mock("../../../api", () => ({
  default: {
    updateSession: vi.fn(),
    deleteSession: vi.fn(),
    batchDeleteSessions: vi.fn(),
  },
}));
vi.mock("../../../api/modules/chat", () => ({
  chatApi: {
    listChats: vi.fn(),
    archiveChat: vi.fn(),
    unarchiveChat: vi.fn(),
    batchArchiveChats: vi.fn(),
    batchUnarchiveChats: vi.fn(),
    exportChat: vi.fn(),
    importChat: vi.fn(),
  },
}));
vi.mock("../../../stores/agentStore", () => ({
  useAgentStore: vi.fn(() => ({ selectedAgent: "agent-1" })),
}));
vi.mock("../../../hooks/useAppMessage", () => ({
  useAppMessage: vi.fn(() => ({ message: mockMessage })),
}));
vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (k: string) => k }),
}));

import api from "../../../api";
import { chatApi } from "../../../api/modules/chat";

type Session = { id: string; name: string; [key: string]: unknown };

describe("useSessions", () => {
  const mockSessions: Session[] = [
    { id: "s1", name: "Session 1" },
    { id: "s2", name: "Session 2" },
  ];

  beforeEach(() => {
    vi.clearAllMocks();
    (chatApi.listChats as ReturnType<typeof vi.fn>).mockResolvedValue(
      mockSessions,
    );
  });

  // 1. 初始 loading=true，fetchSessions 成功后 loading=false
  it("初始 loading=true，fetchSessions 成功后 loading=false", async () => {
    const { result } = renderHook(() => useSessions());

    expect(result.current.loading).toBe(true);

    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });
  });

  // 2. 初始挂载时调用 listSessions，sessions 被设置
  it("初始挂载时调用 listSessions，sessions 被设置", async () => {
    const { result } = renderHook(() => useSessions());

    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });

    expect(chatApi.listChats).toHaveBeenCalledTimes(1);
    expect(result.current.sessions).toEqual(mockSessions);
  });

  // 3. updateSession 成功时更新 sessions 列表并调用 message.success
  it("updateSession 成功时更新 sessions 列表并调用 message.success", async () => {
    const updatedSession: Session = { id: "s1", name: "Updated Session 1" };
    (api.updateSession as ReturnType<typeof vi.fn>).mockResolvedValue(
      updatedSession,
    );

    const { result } = renderHook(() => useSessions());

    // 等待初始数据加载完成
    await waitFor(() => {
      expect(result.current.sessions).toEqual(mockSessions);
    });

    let returnValue: boolean | undefined;
    await act(async () => {
      returnValue = await result.current.updateSession("s1", {
        name: "Updated Session 1",
      });
    });

    expect(returnValue).toBe(true);
    expect(result.current.sessions[0]).toEqual(updatedSession);
    expect(result.current.sessions[1]).toEqual(mockSessions[1]);
    expect(mockMessage.success).toHaveBeenCalledWith("sessions.saveSuccess");
  });

  // 4. updateSession 失败时调用 message.error，返回 false
  it("updateSession 失败时调用 message.error，返回 false", async () => {
    (api.updateSession as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error("update failed"),
    );

    const { result } = renderHook(() => useSessions());

    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });

    let returnValue: boolean | undefined;
    await act(async () => {
      returnValue = await result.current.updateSession("s1", { name: "fail" });
    });

    expect(returnValue).toBe(false);
    expect(mockMessage.error).toHaveBeenCalledWith("sessions.saveFailed");
  });

  // 5. deleteSession 成功时从 sessions 移除并调用 message.success
  it("deleteSession 成功时从 sessions 移除并调用 message.success", async () => {
    (api.deleteSession as ReturnType<typeof vi.fn>).mockResolvedValue(
      undefined,
    );

    const { result } = renderHook(() => useSessions());

    await waitFor(() => {
      expect(result.current.sessions).toEqual(mockSessions);
    });

    let returnValue: boolean | undefined;
    await act(async () => {
      returnValue = await result.current.deleteSession("s1");
    });

    expect(returnValue).toBe(true);
    expect(result.current.sessions).toHaveLength(1);
    expect(result.current.sessions[0].id).toBe("s2");
    expect(mockMessage.success).toHaveBeenCalledWith("sessions.deleteSuccess");
  });

  // 6. deleteSession 失败时调用 message.error，返回 false
  it("deleteSession 失败时调用 message.error，返回 false", async () => {
    (api.deleteSession as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error("delete failed"),
    );

    const { result } = renderHook(() => useSessions());

    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });

    let returnValue: boolean | undefined;
    await act(async () => {
      returnValue = await result.current.deleteSession("s1");
    });

    expect(returnValue).toBe(false);
    expect(mockMessage.error).toHaveBeenCalledWith("sessions.deleteFailed");
  });

  // 7. batchDeleteSessions 成功时批量移除并调用 message.success
  it("batchDeleteSessions 成功时批量移除并调用 message.success", async () => {
    (api.batchDeleteSessions as ReturnType<typeof vi.fn>).mockResolvedValue(
      undefined,
    );

    const { result } = renderHook(() => useSessions());

    await waitFor(() => {
      expect(result.current.sessions).toEqual(mockSessions);
    });

    let returnValue: boolean | undefined;
    await act(async () => {
      returnValue = await result.current.batchDeleteSessions(["s1"]);
    });

    expect(returnValue).toBe(true);
    expect(result.current.sessions).toHaveLength(1);
    expect(result.current.sessions[0].id).toBe("s2");
    expect(mockMessage.success).toHaveBeenCalledWith(
      "sessions.batchDeleteSuccess",
    );
  });

  // 8. batchDeleteSessions 失败时调用 message.error，返回 false
  it("batchDeleteSessions 失败时调用 message.error，返回 false", async () => {
    (api.batchDeleteSessions as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error("batch delete failed"),
    );

    const { result } = renderHook(() => useSessions());

    await waitFor(() => {
      expect(result.current.loading).toBe(false);
    });

    let returnValue: boolean | undefined;
    await act(async () => {
      returnValue = await result.current.batchDeleteSessions(["s1", "s2"]);
    });

    expect(returnValue).toBe(false);
    expect(mockMessage.error).toHaveBeenCalledWith(
      "sessions.batchDeleteFailed",
    );
  });

  // 控制 → 会话 这一页是**第三个**会话列表（表格形态）：导出在「操作」列，
  // 导入在筛选条旁边。前面已经踩过一次「只接了其中一个列表」的坑，所以这里
  // 也补上 hook 层的契约。
  describe("会话导入导出", () => {
    it("exportSession 用会话 id 与名称调用导出", async () => {
      const { result } = renderHook(() => useSessions());

      await act(async () => {
        await result.current.exportSession("s1", "会话一");
      });

      expect(chatApi.exportChat).toHaveBeenCalledWith("s1", "会话一");
    });

    it("exportSession 失败时提示而不抛出", async () => {
      vi.mocked(chatApi.exportChat).mockRejectedValueOnce(new Error("boom"));
      const { result } = renderHook(() => useSessions());

      await act(async () => {
        await result.current.exportSession("s1", "会话一");
      });

      expect(mockMessage.error).toHaveBeenCalled();
    });

    it("importSession 导入后刷新列表并返回新会话", async () => {
      vi.mocked(chatApi.importChat).mockResolvedValue({
        id: "imported-1",
        name: "导入的会话",
      } as Awaited<ReturnType<typeof chatApi.importChat>>);
      vi.mocked(chatApi.listChats).mockResolvedValue([]);
      const { result } = renderHook(() => useSessions());
      const before = vi.mocked(chatApi.listChats).mock.calls.length;

      let created: unknown = null;
      await act(async () => {
        created = await result.current.importSession(
          new File(["{}"], "chat.session.json", { type: "application/json" }),
        );
      });

      expect(chatApi.importChat).toHaveBeenCalledTimes(1);
      // 必须刷新：不刷新的话导入的会话要等下一次加载才出现。
      expect(vi.mocked(chatApi.listChats).mock.calls.length).toBeGreaterThan(
        before,
      );
      expect((created as { id: string }).id).toBe("imported-1");
    });

    it("importSession 失败时透出服务端原因并返回 null", async () => {
      vi.mocked(chatApi.importChat).mockRejectedValueOnce(
        new Error("Not a session export file (missing the expected format marker)"),
      );
      const { result } = renderHook(() => useSessions());

      let created: unknown = "sentinel";
      await act(async () => {
        created = await result.current.importSession(
          new File(["{}"], "notes.json", { type: "application/json" }),
        );
      });

      expect(created).toBeNull();
      expect(mockMessage.error).toHaveBeenCalledWith(
        "Not a session export file (missing the expected format marker)",
      );
    });
  });
});
