import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "@/test/common_setup";
import ChatSessionDrawer from "./index";
import { useChatAnywhereSessionsState } from "@agentscope-ai/chat";
import { useAgentStore } from "../../../../stores/agentStore";

// Mock react-window's VariableSizeList to render all items directly
// (jsdom has no layout, so the virtual list never renders rows).
// Must use forwardRef because the component uses ref={listRef} for resetAfterIndex.
const { MockVariableSizeList } = vi.hoisted(() => {
  const React = require("react");
  const MockVariableSizeList = React.forwardRef((props: any, ref: any) => {
    React.useImperativeHandle(ref, () => ({
      resetAfterIndex: () => {},
    }));
    const Row = props.children;
    return (
      <>
        {Array.from({ length: props.itemCount }, (_: any, i: number) => (
          <Row key={i} index={i} style={{}} data={props.itemData} />
        ))}
      </>
    );
  });
  return { MockVariableSizeList };
});
vi.mock("react-window", () => ({
  VariableSizeList: MockVariableSizeList,
}));

const {
  mockCreateSession,
  mockSetCurrentSessionId,
  mockSetSessions,
  mockDeleteChat,
  mockUpdateChat,
  mockGetSessionList,
  mockNavigate,
  mockGetEffectiveSessionId,
  mockExportChat,
  mockImportChat,
} = vi.hoisted(() => ({
  mockCreateSession: vi.fn().mockResolvedValue(undefined),
  mockSetCurrentSessionId: vi.fn(),
  mockSetSessions: vi.fn(),
  mockDeleteChat: vi.fn().mockResolvedValue(undefined),
  mockUpdateChat: vi.fn().mockResolvedValue(undefined),
  mockGetSessionList: vi.fn().mockResolvedValue([]),
  mockNavigate: vi.fn(),
  mockGetEffectiveSessionId: vi.fn((id: string) => id),
  mockExportChat: vi.fn().mockResolvedValue(undefined),
  mockImportChat: vi.fn().mockResolvedValue({
    id: "imported-uuid",
    name: "Imported Chat",
    session_id: "console:imported:abc",
  }),
}));

vi.mock("@agentscope-ai/chat", () => ({
  useChatAnywhereSessionsState: vi.fn(() => ({
    sessions: [],
    currentSessionId: null,
    setCurrentSessionId: mockSetCurrentSessionId,
    setSessions: mockSetSessions,
  })),
  useChatAnywhereSessions: vi.fn(() => ({ createSession: mockCreateSession })),
}));

vi.mock("@/api/modules/chat", () => ({
  chatApi: {
    deleteChat: mockDeleteChat,
    updateChat: mockUpdateChat,
    exportChat: mockExportChat,
    importChat: mockImportChat,
  },
  sessionApi: {
    listChats: vi.fn(),
    createChat: vi.fn(),
    getChat: vi.fn(),
    updateChat: mockUpdateChat,
    deleteChat: mockDeleteChat,
    batchDeleteChats: vi.fn(),
    stopChat: vi.fn(),
  },
}));

vi.mock("../../sessionApi", () => ({
  default: {
    getSessionList: mockGetSessionList,
    isSessionSwitching: false,
    startNewSwitch: vi.fn(() => ({ signal: { aborted: false } })),
    preloadSession: vi.fn().mockResolvedValue({ session: {}, realId: null }),
    finishSessionSwitch: vi.fn(),
    lastNavigatedChatId: null,
    getEffectiveSessionId: mockGetEffectiveSessionId,
    // Ownership epoch helpers: tests run under a single stable owner.
    getActiveOwner: vi.fn(() => ({ agentId: "default", generation: 0 })),
    isActiveOwner: vi.fn(() => true),
  },
}));

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual("react-router-dom");
  return { ...actual, useNavigate: () => mockNavigate };
});

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (k: string) => k }),
}));

vi.mock("@agentscope-ai/design", () => ({
  IconButton: ({
    onClick,
    icon,
  }: {
    onClick?: () => void;
    icon: React.ReactNode;
  }) => <button onClick={onClick}>{icon}</button>,
}));

// Mock ResizeObserver so FixedSizeList gets a non-zero height and renders rows.
const mockResizeObserver = vi.fn().mockImplementation(function (
  this: unknown,
  callback: (entries: ResizeObserverEntry[]) => void,
) {
  return {
    observe: vi.fn((el: HTMLElement) => {
      callback([
        {
          target: el,
          contentRect: { height: 600 },
        } as unknown as ResizeObserverEntry,
      ]);
    }),
    unobserve: vi.fn(),
    disconnect: vi.fn(),
  };
});
(globalThis as any).ResizeObserver = mockResizeObserver;

// jsdom returns 0 for clientHeight; the drawer uses it as a fallback to
// set listHeight when ResizeObserver hasn't fired yet.
Object.defineProperty(HTMLElement.prototype, "clientHeight", {
  configurable: true,
  get(this: HTMLElement) {
    return 600;
  },
});

vi.mock("../../../../components/SessionItem", () => ({
  default: ({
    sessionId,
    name,
    onClick,
    onEdit,
    onDelete,
    onPin,
    onExport,
    onEditSubmit,
    onEditCancel,
  }: any) => (
    <div data-testid="session-item">
      <span onClick={() => onClick?.(sessionId)}>{name}</span>
      <button data-testid="edit-btn" onClick={() => onEdit?.(sessionId, name)}>
        edit
      </button>
      <button data-testid="delete-btn" onClick={() => onDelete?.(sessionId)}>
        delete
      </button>
      <button data-testid="pin-btn" onClick={() => onPin?.(sessionId)}>
        pin
      </button>
      <button data-testid="export-btn" onClick={() => onExport?.(sessionId)}>
        export
      </button>
      <button data-testid="edit-submit-btn" onClick={onEditSubmit}>
        submit
      </button>
      <button data-testid="edit-cancel-btn" onClick={onEditCancel}>
        cancel
      </button>
    </div>
  ),
}));

vi.mock("../../../../Control/Channels/components", () => ({
  getChannelLabel: () => undefined,
  ChannelIcon: ({ channelKey }: { channelKey: string }) => (
    <span data-testid="channel-icon">{channelKey}</span>
  ),
}));

vi.mock("@agentscope-ai/icons", () => ({
  SparkOperateRightLine: () => (
    <span data-icon="SparkOperateRightLine">icon</span>
  ),
  SparkLockLine: () => <span data-testid="icon">lock</span>,
  SparkLockFill: () => <span data-testid="icon">lock-fill</span>,
  SparkDownArrowLine: ({ size }: { size?: number }) => (
    <span data-testid="icon">chevron-{size}</span>
  ),
}));

vi.mock("../../../../components/ContextMenu", () => ({
  ContextMenu: ({ children }: { children: React.ReactNode }) => (
    <div>{children}</div>
  ),
  useContextMenu: () => ({ show: vi.fn(), hide: vi.fn() }),
}));

const defaultProps = { open: true, onClose: vi.fn() };

function withSession(overrides: Record<string, unknown> = {}) {
  const session = {
    id: "s1",
    name: "Session One",
    updatedAt: new Date().toISOString(),
    ...overrides,
  } as any;
  mockGetSessionList.mockResolvedValue([session]);
  vi.mocked(useChatAnywhereSessionsState).mockReturnValue({
    sessions: [session],
    currentSessionId: null,
    setCurrentSessionId: mockSetCurrentSessionId,
    setSessions: mockSetSessions,
  } as any);
}

describe("ChatSessionDrawer", () => {
  beforeEach(() => {
    useAgentStore.setState({ selectedAgent: "default" });
  });

  afterEach(() => vi.clearAllMocks());

  it("renders nothing when open=false", () => {
    renderWithProviders(<ChatSessionDrawer open={false} onClose={vi.fn()} />);
    expect(screen.queryByText("chat.allChats")).not.toBeInTheDocument();
  });

  it("renders title chat.allChats when open=true", () => {
    renderWithProviders(<ChatSessionDrawer {...defaultProps} />);
    expect(screen.getByText("chat.allChats")).toBeInTheDocument();
  });

  it("clicking new chat calls createSession", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    renderWithProviders(<ChatSessionDrawer open onClose={onClose} />);
    await user.click(screen.getByText("chat.createNewChat"));
    expect(mockCreateSession).toHaveBeenCalledOnce();
  });

  it("renders SessionItem for each session", async () => {
    withSession();
    renderWithProviders(<ChatSessionDrawer {...defaultProps} />);
    await waitFor(() =>
      expect(screen.getByText("Session One")).toBeInTheDocument(),
    );
  });

  it("clicking a session item navigates to the session path", async () => {
    withSession();
    const user = userEvent.setup();
    renderWithProviders(<ChatSessionDrawer {...defaultProps} />);
    await waitFor(() =>
      expect(screen.getByText("Session One")).toBeInTheDocument(),
    );
    await user.click(screen.getByText("Session One"));
    expect(mockNavigate).toHaveBeenCalledWith("/chat/s1");
  });

  it("clicking the close button calls onClose", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    renderWithProviders(<ChatSessionDrawer open onClose={onClose} />);
    await user.click(
      document
        .querySelector('[data-icon="SparkOperateRightLine"]')!
        .closest("button")!,
    );
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("delete calls deleteChat with backend id and refreshes", async () => {
    withSession({ realId: "uuid-1" });
    const user = userEvent.setup();
    renderWithProviders(<ChatSessionDrawer {...defaultProps} />);
    await waitFor(() =>
      expect(screen.getByTestId("delete-btn")).toBeInTheDocument(),
    );
    await user.click(screen.getByTestId("delete-btn"));
    expect(mockDeleteChat).toHaveBeenCalledWith("uuid-1");
    expect(mockGetSessionList).toHaveBeenCalled();
  });

  it("delete with numeric id skips deleteChat API", async () => {
    withSession({ id: "12345" });
    const user = userEvent.setup();
    renderWithProviders(<ChatSessionDrawer {...defaultProps} />);
    await waitFor(() =>
      expect(screen.getByTestId("delete-btn")).toBeInTheDocument(),
    );
    await user.click(screen.getByTestId("delete-btn"));
    expect(mockDeleteChat).not.toHaveBeenCalled();
  });

  it("delete clears the message queue for both local id and backend id", async () => {
    const { useMessageQueueStore } = await import("@/stores/messageQueueStore");
    useMessageQueueStore.getState().enqueue("s1", { text: "local" });
    useMessageQueueStore.getState().enqueue("uuid-1", { text: "backend" });
    expect(useMessageQueueStore.getState().getQueue("s1")).toHaveLength(1);
    expect(useMessageQueueStore.getState().getQueue("uuid-1")).toHaveLength(1);

    withSession({ realId: "uuid-1" });
    const user = userEvent.setup();
    renderWithProviders(<ChatSessionDrawer {...defaultProps} />);
    await waitFor(() =>
      expect(screen.getByTestId("delete-btn")).toBeInTheDocument(),
    );
    await user.click(screen.getByTestId("delete-btn"));

    await waitFor(() => {
      expect(useMessageQueueStore.getState().getQueue("s1")).toEqual([]);
      expect(useMessageQueueStore.getState().getQueue("uuid-1")).toEqual([]);
    });
  });

  it("edit start sets editing state and edit submit calls updateChat", async () => {
    withSession({ realId: "uuid-1" });
    const user = userEvent.setup();
    renderWithProviders(<ChatSessionDrawer {...defaultProps} />);
    await waitFor(() =>
      expect(screen.getByTestId("edit-btn")).toBeInTheDocument(),
    );
    await user.click(screen.getByTestId("edit-btn"));
    await user.click(screen.getByTestId("edit-submit-btn"));
    expect(mockUpdateChat).toHaveBeenCalledWith("uuid-1", {
      name: "Session One",
    });
  });

  it("edit cancel resets editing state without API call", async () => {
    withSession({ realId: "uuid-1" });
    const user = userEvent.setup();
    renderWithProviders(<ChatSessionDrawer {...defaultProps} />);
    await waitFor(() =>
      expect(screen.getByTestId("edit-btn")).toBeInTheDocument(),
    );
    await user.click(screen.getByTestId("edit-btn"));
    await user.click(screen.getByTestId("edit-cancel-btn"));
    expect(mockUpdateChat).not.toHaveBeenCalled();
  });

  it("pin toggle calls updateChat with toggled pinned state", async () => {
    withSession({ realId: "uuid-1", pinned: false });
    const user = userEvent.setup();
    renderWithProviders(<ChatSessionDrawer {...defaultProps} />);
    await waitFor(() =>
      expect(screen.getByTestId("pin-btn")).toBeInTheDocument(),
    );
    await user.click(screen.getByTestId("pin-btn"));
    expect(mockUpdateChat).toHaveBeenCalledWith("uuid-1", { pinned: true });
  });

  it("on open=true triggers session list refresh", async () => {
    renderWithProviders(<ChatSessionDrawer {...defaultProps} />);
    await vi.waitFor(() => expect(mockGetSessionList).toHaveBeenCalled());
  });

  it("clears stale sessions and reloads when the selected agent changes", async () => {
    let resolveAgentB!: (sessions: Array<Record<string, unknown>>) => void;
    const agentBList = new Promise<Array<Record<string, unknown>>>(
      (resolve) => {
        resolveAgentB = resolve;
      },
    );

    mockGetSessionList
      .mockResolvedValueOnce([
        {
          id: "agent-a-chat",
          name: "Agent A Chat",
          updatedAt: new Date().toISOString(),
        },
      ])
      .mockReturnValueOnce(agentBList);

    renderWithProviders(<ChatSessionDrawer {...defaultProps} />);
    await screen.findByText("Agent A Chat");

    act(() => {
      useAgentStore.setState({ selectedAgent: "agent-b" });
    });

    await waitFor(() => {
      expect(screen.queryByText("Agent A Chat")).not.toBeInTheDocument();
      expect(mockGetSessionList).toHaveBeenCalledTimes(2);
    });

    await act(async () => {
      resolveAgentB([
        {
          id: "agent-b-chat",
          name: "Agent B Chat",
          updatedAt: new Date().toISOString(),
        },
      ]);
      await agentBList;
    });

    expect(await screen.findByText("Agent B Chat")).toBeInTheDocument();
  });

  it("pinned sessions sort before unpinned", async () => {
    mockGetSessionList.mockResolvedValue([
      {
        id: "s1",
        name: "Unpinned",
        pinned: false,
        updatedAt: new Date().toISOString(),
      },
      {
        id: "s2",
        name: "Pinned",
        pinned: true,
        updatedAt: new Date().toISOString(),
      },
    ]);
    renderWithProviders(<ChatSessionDrawer {...defaultProps} />);
    const items = await screen.findAllByTestId("session-item");
    expect(items[0]).toHaveTextContent("Pinned");
    expect(items[1]).toHaveTextContent("Unpinned");
  });

  describe("session export / import", () => {
    it("export calls the export API with the backend id and the chat name", async () => {
      withSession({ realId: "uuid-1", name: "Session One" });
      const user = userEvent.setup();
      renderWithProviders(<ChatSessionDrawer {...defaultProps} />);
      await waitFor(() =>
        expect(screen.getByTestId("export-btn")).toBeInTheDocument(),
      );

      await user.click(screen.getByTestId("export-btn"));

      // The backend needs the real UUID (the display id can be a timestamp),
      // and the name is only the fallback used by the native save dialogs.
      expect(mockExportChat).toHaveBeenCalledWith("uuid-1", "Session One");
    });

    it("a failed export does not throw out of the click handler", async () => {
      mockExportChat.mockRejectedValueOnce(new Error("boom"));
      withSession({ realId: "uuid-1" });
      const user = userEvent.setup();
      renderWithProviders(<ChatSessionDrawer {...defaultProps} />);
      await waitFor(() =>
        expect(screen.getByTestId("export-btn")).toBeInTheDocument(),
      );

      await user.click(screen.getByTestId("export-btn"));

      await waitFor(() => expect(mockExportChat).toHaveBeenCalledOnce());
    });

    it("import uploads the picked file and opens the new chat", async () => {
      const user = userEvent.setup();
      renderWithProviders(<ChatSessionDrawer {...defaultProps} />);
      const input = screen.getByTestId("session-import-input") as HTMLInputElement;
      const file = new File(["{}"], "chat.session.json", {
        type: "application/json",
      });
      // Count, not "was ever called": the drawer also polls, so a bare
      // toHaveBeenCalled() would pass even with the refresh removed.
      const listsBefore = mockGetSessionList.mock.calls.length;

      await user.upload(input, file);

      await waitFor(() => expect(mockImportChat).toHaveBeenCalledOnce());
      expect(mockImportChat.mock.calls[0][0]).toBe(file);
      // The list must be refreshed before navigating: the switch resolves
      // against it, so an unlisted session would open as "not found".
      expect(mockGetSessionList.mock.calls.length).toBeGreaterThan(listsBefore);
      await waitFor(() =>
        expect(mockNavigate).toHaveBeenCalledWith("/chat/imported-uuid"),
      );
    });

    it("import prefers the list's own id for the imported chat", async () => {
      const user = userEvent.setup();
      renderWithProviders(<ChatSessionDrawer {...defaultProps} />);
      // The refreshed list surfaces the new chat under a display id that
      // differs from the backend UUID.
      mockGetSessionList.mockResolvedValue([
        {
          id: "s-new",
          realId: "imported-uuid",
          name: "Imported Chat",
          updatedAt: new Date().toISOString(),
        },
      ]);
      const input = screen.getByTestId("session-import-input") as HTMLInputElement;

      await user.upload(
        input,
        new File(["{}"], "chat.session.json", { type: "application/json" }),
      );

      await waitFor(() =>
        expect(mockNavigate).toHaveBeenCalledWith("/chat/s-new"),
      );
    });

    it("a rejected import surfaces the server's reason", async () => {
      mockImportChat.mockRejectedValueOnce(
        new Error("Not a session export file (missing the expected format marker)"),
      );
      const user = userEvent.setup();
      renderWithProviders(<ChatSessionDrawer {...defaultProps} />);
      const input = screen.getByTestId("session-import-input") as HTMLInputElement;

      await user.upload(
        input,
        new File(["{}"], "notes.json", { type: "application/json" }),
      );

      await waitFor(() => expect(mockImportChat).toHaveBeenCalledOnce());
      // Importing a bad file must not navigate anywhere.
      expect(mockNavigate).not.toHaveBeenCalled();
    });

    it("the import button is outside the session list", async () => {
      withSession();
      renderWithProviders(<ChatSessionDrawer {...defaultProps} />);
      await waitFor(() =>
        expect(screen.getByTestId("session-item")).toBeInTheDocument(),
      );

      // Import creates a new chat, so it belongs next to "new chat" and not
      // inside any session row's action column (that is where export lives).
      const list = document.querySelector("div[class*=listWrapper]");
      // Guard against a vacuous pass: if that selector stopped matching, the
      // "not inside the list" assertion below would hold for the wrong reason.
      expect(list).not.toBeNull();
      expect(list!.querySelector('[data-testid="session-item"]')).not.toBeNull();

      const input = screen.getByTestId("session-import-input");
      expect(list!.contains(input)).toBe(false);
      expect(input.closest("div[class*=createSection]")).not.toBeNull();
    });
  });
});
