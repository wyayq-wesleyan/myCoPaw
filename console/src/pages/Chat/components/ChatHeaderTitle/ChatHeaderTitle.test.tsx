import { describe, it, expect, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/react";
import { renderWithProviders } from "@/test/common_setup";
import ChatHeaderTitle from "./index";

const { mockUseChatAnywhereSessionsState } = vi.hoisted(() => ({
  mockUseChatAnywhereSessionsState: vi.fn(),
}));

vi.mock("@agentscope-ai/chat", () => ({
  useChatAnywhereSessionsState: mockUseChatAnywhereSessionsState,
}));

describe("ChatHeaderTitle", () => {
  it("displays the current session name", () => {
    mockUseChatAnywhereSessionsState.mockReturnValue({
      sessions: [{ id: "sess-1", name: "My Chat" }],
      currentSessionId: "sess-1",
    });
    renderWithProviders(<ChatHeaderTitle />);
    expect(screen.getAllByText("My Chat")[0]).toBeInTheDocument();
  });

  it('displays "新对话" when session name is empty', () => {
    mockUseChatAnywhereSessionsState.mockReturnValue({
      sessions: [{ id: "sess-1", name: "" }],
      currentSessionId: "sess-1",
    });
    renderWithProviders(<ChatHeaderTitle />);
    expect(screen.getAllByText("新对话")[0]).toBeInTheDocument();
  });

  it('displays "新对话" when no matching session exists', () => {
    mockUseChatAnywhereSessionsState.mockReturnValue({
      sessions: [],
      currentSessionId: null,
    });
    renderWithProviders(<ChatHeaderTitle />);
    expect(screen.getAllByText("新对话")[0]).toBeInTheDocument();
  });

  it("displays the correct session name after switching currentSessionId", () => {
    mockUseChatAnywhereSessionsState.mockReturnValue({
      sessions: [
        { id: "sess-1", name: "Chat A" },
        { id: "sess-2", name: "Chat B" },
      ],
      currentSessionId: "sess-2",
    });
    renderWithProviders(<ChatHeaderTitle />);
    expect(screen.getAllByText("Chat B")[0]).toBeInTheDocument();
    expect(screen.queryByText("Chat A")).not.toBeInTheDocument();
  });

  it("shows the total session count and opens a scrollable session list", async () => {
    const setCurrentSessionId = vi.fn();
    mockUseChatAnywhereSessionsState.mockReturnValue({
      sessions: [
        { id: "sess-1", name: "Chat A" },
        { id: "sess-2", name: "Chat B" },
        { id: "sess-3", name: "Chat C" },
      ],
      currentSessionId: "sess-2",
      setCurrentSessionId,
    });

    renderWithProviders(<ChatHeaderTitle />);

    expect(screen.getByTestId("chat-session-count")).toHaveTextContent("3");
    fireEvent.click(screen.getAllByText("Chat B")[0]);
    const panel = await screen.findByTestId("chat-session-panel");
    expect(panel).toHaveTextContent("Chat A");
    expect(panel).toHaveTextContent("Chat C");

    fireEvent.click(screen.getByText("Chat A"));
    expect(setCurrentSessionId).toHaveBeenCalledWith("sess-1");
  });
});
