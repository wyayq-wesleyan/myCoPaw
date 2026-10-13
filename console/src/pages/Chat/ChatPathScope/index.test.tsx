import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import ChatPathScope from "./index";

/**
 * The path selector decides what an agent may read, and it lives in the chat
 * header. Two properties matter and are easy to get wrong:
 *
 *  1. A toggle writes the **whole set**, not a delta — the checkbox list is a
 *     statement about the agent's reach, so what is ticked must be what the agent
 *     can read. A failed write must roll the checkbox back rather than leave the
 *     display claiming something untrue.
 *  2. It edits the **agent**, so the change applies to that agent's conversations
 *     generally — which is why the header passes the selected agent id through.
 */

const mocks = vi.hoisted(() => ({
  pathTree: vi.fn(),
  agentGrants: vi.fn(),
  setAgentGrants: vi.fn(),
}));

vi.mock("../../../api/modules/paths", () => ({
  pathTree: mocks.pathTree,
  pathsApi: {
    agentGrants: mocks.agentGrants,
    setAgentGrants: mocks.setAgentGrants,
  },
}));

let selectedAgent = "ag-1";
vi.mock("../../../stores/agentStore", () => ({
  useAgentStore: () => ({ selectedAgent }),
}));

vi.mock("antd", async () => {
  const actual = await vi.importActual<typeof import("antd")>("antd");
  return {
    ...actual,
    message: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
  };
});

const TREE = {
  tree: [
    {
      id: "root-1",
      name: "默认",
      parent_id: null,
      is_root: true,
      children: [
        {
          id: "case-1",
          name: "案件一",
          parent_id: "root-1",
          is_root: false,
          children: [],
        },
      ],
    },
  ],
};

beforeEach(() => {
  vi.clearAllMocks();
  selectedAgent = "ag-1";
  mocks.pathTree.mockResolvedValue(TREE);
  mocks.agentGrants.mockResolvedValue({ agent_id: "ag-1", path_ids: ["root-1"] });
  mocks.setAgentGrants.mockImplementation((_id: string, ids: string[]) =>
    Promise.resolve({ agent_id: "ag-1", path_ids: ids }),
  );
});


/**
 * The panel is a dropdown: its rows only mount once opened. Every test that reads
 * or clicks a path must open it first, which also mirrors what a user does.
 */
async function openPanel(kind: "knowledge" | "data" = "knowledge") {
  const trigger = await screen.findByRole("button", { name: new RegExp(kind === "knowledge" ? "知识空间" : "数据空间") });
  fireEvent.click(trigger);
  await screen.findByText(kind === "knowledge" ? "知识空间" : "数据空间", { selector: "strong" });
}

describe("ChatPathScope", () => {
  it("shows the paths with the agent's current grants ticked", async () => {
    render(<ChatPathScope kind="knowledge" />);
    await openPanel();

    const rootRow = await screen.findByRole("button", { name: /公共区（未归类）/ });
    const caseRow = screen.getByRole("button", { name: /案件一/ });

    // Root is granted, the case is not — the tick must reflect the server value.
    expect(rootRow.getAttribute("aria-pressed")).toBe("true");
    expect(caseRow.getAttribute("aria-pressed")).toBe("false");
  });

  it("writes the whole set when a path is granted", async () => {
    render(<ChatPathScope kind="knowledge" />);
    await openPanel();
    const caseRow = await screen.findByRole("button", { name: /案件一/ });

    fireEvent.click(caseRow);

    // Both paths, not just the delta: the list states the agent's whole reach.
    await waitFor(() =>
      expect(mocks.setAgentGrants).toHaveBeenCalledWith("ag-1", ["root-1", "case-1"]),
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /案件一/ }).getAttribute("aria-pressed")).toBe("true"),
    );
  });

  it("removes a path from the set when it is unticked", async () => {
    render(<ChatPathScope kind="knowledge" />);
    await openPanel();
    const rootRow = await screen.findByRole("button", { name: /公共区（未归类）/ });

    fireEvent.click(rootRow);

    await waitFor(() => expect(mocks.setAgentGrants).toHaveBeenCalledWith("ag-1", []));
  });

  it("rolls the tick back when the write fails", async () => {
    mocks.setAgentGrants.mockRejectedValue(new Error("boom"));
    render(<ChatPathScope kind="knowledge" />);
    await openPanel();
    const caseRow = await screen.findByRole("button", { name: /案件一/ });

    fireEvent.click(caseRow);

    // The tick must not keep claiming a grant the server refused; otherwise the
    // user believes the agent can read a case it cannot.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /案件一/ }).getAttribute("aria-pressed")).toBe("false"),
    );
  });

  it("requests the grants of the currently selected agent", async () => {
    render(<ChatPathScope kind="data" />);
    await waitFor(() => expect(mocks.agentGrants).toHaveBeenCalledWith("ag-1"));
  });

  it("switches to the newly selected agent's grants", async () => {
    const { rerender } = render(<ChatPathScope kind="knowledge" />);
    await openPanel();
    await waitFor(() => expect(mocks.agentGrants).toHaveBeenCalledWith("ag-1"));

    selectedAgent = "ag-2";
    mocks.agentGrants.mockResolvedValue({ agent_id: "ag-2", path_ids: [] });
    rerender(<ChatPathScope kind="knowledge" />);

    // The header must follow the agent selector, not keep showing ag-1's scope.
    await waitFor(() => expect(mocks.agentGrants).toHaveBeenCalledWith("ag-2"));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /公共区（未归类）/ }).getAttribute("aria-pressed")).toBe("false"),
    );
  });
});
