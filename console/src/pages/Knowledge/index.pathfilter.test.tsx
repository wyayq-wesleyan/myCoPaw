/**
 * 知识空间页：路径筛选与选中的联动。
 *
 * 这里锁定两个真实发生过的操作问题：
 *
 * 1. 点「全部」不显示列表、点「公共区」正常。原因是 ``loadDocuments`` 先展开传入
 *    的 params、之后才补 ``path_id`` —— 多出来的键没法靠「后面再展开一次」删掉，
 *    于是哨兵值 ``__all__`` 被发到后端，后端按未知路径返回空页。
 * 2. 切换路径后右侧仍保留上一个路径勾选的文件。选中项属于「当时那份列表」，
 *    跨路径保留会让批量操作命中用户已经看不到的行。
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { screen, waitFor, fireEvent } from "@testing-library/react";
import { renderWithProviders } from "@/test/common_setup";

const { mockListDocuments, mockHealth } = vi.hoisted(() => ({
  mockListDocuments: vi.fn(),
  mockHealth: vi.fn(),
}));

vi.mock("../../api/modules/knowledge", () => ({
  knowledgeApi: {
    health: mockHealth,
    listDocuments: mockListDocuments,
    deleteDocuments: vi.fn(),
    searchFilter: vi.fn(),
    previewDocument: vi.fn(),
    getDocumentStats: vi.fn(),
    reprocessDocument: vi.fn(),
    deleteDocument: vi.fn(),
    upload: vi.fn(),
  },
}));

const TREE = {
  tree: [
    {
      id: "root-1",
      name: "默认",
      parent_id: null,
      is_root: true,
      is_public: true,
      document_count: 2,
      table_count: 0,
      agents: [],
      children: [
        {
          id: "case-1",
          name: "案件一",
          parent_id: "root-1",
          is_root: false,
          is_public: false,
          document_count: 1,
          table_count: 0,
          agents: [],
          children: [],
        },
      ],
    },
  ],
};

vi.mock("../../api/modules/paths", async () => {
  const actual = await vi.importActual<typeof import("../../api/modules/paths")>(
    "../../api/modules/paths",
  );
  return {
    ...actual,
    pathTree: async () => TREE,
    pathsApi: {
      tree: async () => TREE,
      assignable: async () => ({ items: [] }),
      agents: async () => ({ agents: [] }),
      bindResource: vi.fn(),
      unbindResource: vi.fn(),
      unbindResourceAnyPath: vi.fn(),
      create: vi.fn(),
      rename: vi.fn(),
      remove: vi.fn(),
      bindAgent: vi.fn(),
      unbindAgent: vi.fn(),
    },
  };
});

vi.mock("../../api/modules/agents", () => ({
  agentsApi: { listAgents: async () => ({ agents: [] }) },
}));

// The page pulls in a markdown/preview stack that is irrelevant here.
vi.mock("./DocumentPreview", () => ({
  default: () => null,
}));

import KnowledgePage from "./index";

/** A document row as the listing returns it. */
const doc = (id: string, filename: string) => ({
  id,
  filename,
  status: "completed" as const,
});

beforeEach(() => {
  vi.clearAllMocks();
  mockHealth.mockResolvedValue({ configured: true, status: "ok" });
  mockListDocuments.mockResolvedValue({
    items: [doc("d1", "公共文件.txt"), doc("d2", "案件文件.txt")],
    total: 2,
    page: 1,
    page_size: 20,
    pending_count: 0,
  });
});

describe("KnowledgePage path filtering", () => {
  it("never sends the 全部 sentinel to the server", async () => {
    renderWithProviders(<KnowledgePage />);
    // Initial load (no filter) already proves the sentinel is not in the request.
    await waitFor(() => expect(mockListDocuments).toHaveBeenCalled());
    for (const call of mockListDocuments.mock.calls) {
      expect(call[0]?.path_id).toBeUndefined();
    }
  });

  it("sends the real path id after selecting a path, and omits it again for 全部", async () => {
    renderWithProviders(<KnowledgePage />);
    await screen.findByText("案件一");
    mockListDocuments.mockClear();

    // Select 案件一 -> a real id must be sent (not the sentinel).
    fireEvent.click(screen.getByText("案件一"));
    await waitFor(() =>
      expect(
        mockListDocuments.mock.calls.some((c) => c[0]?.path_id === "case-1"),
      ).toBe(true),
    );

    mockListDocuments.mockClear();
    fireEvent.click(screen.getByText("全部"));
    await waitFor(() => expect(mockListDocuments).toHaveBeenCalled());
    // Critically: no request may carry "__all__".
    for (const call of mockListDocuments.mock.calls) {
      expect(call[0]?.path_id).not.toBe("__all__");
    }
  });

  it("clears the selection when the path changes", async () => {
    renderWithProviders(<KnowledgePage />);
    await screen.findByText("案件一");

    // Tick every row via the header checkbox (role-based so it does not depend on
    // the app's AntD class prefix).
    const boxes = screen.getAllByRole("checkbox") as HTMLInputElement[];
    const header = boxes.find((box) => box.closest("th")) ?? boxes[0];
    fireEvent.click(header);
    await waitFor(() => {
      expect(
        screen
          .getAllByRole("checkbox")
          .filter((box) => (box as HTMLInputElement).checked && !box.closest("th"))
          .length,
      ).toBeGreaterThan(0);
    });

    // Switching path must drop that selection: those checkboxes belong to a list
    // that is no longer on screen, and a batch action would hit hidden rows.
    fireEvent.click(screen.getByText("案件一"));
    await waitFor(() => {
      expect(
        screen
          .getAllByRole("checkbox")
          .filter((box) => (box as HTMLInputElement).checked && !box.closest("th")),
      ).toHaveLength(0);
    });
  });
});
