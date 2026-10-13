import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import PathTreePanel, { ALL_PATHS } from "./PathTreePanel";

/**
 * Editing is the reason the tree has buttons at all: a user has to be able to
 * create the sub-paths that isolate one case's material from another's. These
 * tests pin the wiring (which API each button calls with which arguments) rather
 * than the visual result, because the store already owns the invariants.
 */

const mocks = vi.hoisted(() => ({
  pathTree: vi.fn(),
  create: vi.fn(),
  rename: vi.fn(),
  remove: vi.fn(),
  assignable: vi.fn(),
  bindResource: vi.fn(),
  unbindResource: vi.fn(),
  pathAgents: vi.fn(),
}));

vi.mock("../../api/modules/paths", () => ({
  pathTree: mocks.pathTree,
  pathsApi: {
    create: mocks.create,
    rename: mocks.rename,
    remove: mocks.remove,
    assignable: mocks.assignable,
    bindResource: mocks.bindResource,
    unbindResource: mocks.unbindResource,
    agents: mocks.pathAgents,
  },
  ALL_PATHS: "__all__",
}));

vi.mock("../../api/modules/agents", () => ({
}));

const TREE = {
  tree: [
    {
      id: "root-1",
      name: "默认",
      parent_id: null,
      is_root: true,
      is_public: true,
      document_count: 5,
      table_count: 2,
      agents: ["ag1"],
      children: [
        {
          id: "case-1",
          name: "案件一",
          parent_id: "root-1",
          is_root: false,
          is_public: false,
          document_count: 0,
          table_count: 0,
          agents: [],
          children: [],
        },
      ],
    },
  ],
};

beforeEach(() => {
  vi.clearAllMocks();
  mocks.pathTree.mockResolvedValue(TREE);
  mocks.create.mockResolvedValue({ id: "new-1", name: "x" });
  mocks.rename.mockResolvedValue({ id: "case-1", name: "y" });
  mocks.remove.mockResolvedValue({ deleted: true });
  mocks.assignable.mockResolvedValue({ items: [] });
  mocks.pathAgents.mockResolvedValue({ agents: [] });
});

/** The row that carries the given path name. */
function rowFor(name: string): HTMLElement {
  const label = screen.getByText(name);
  const row = label.closest('[class*="row"]');
  if (!row) throw new Error(`no row for ${name}`);
  return row as HTMLElement;
}

/** Row action buttons are named by their accessible label. */
function actionButton(row: HTMLElement, name: string): HTMLElement {
  const button = row.querySelector(`button[aria-label="${name}"]`);
  if (!button) throw new Error(`no ${name} button in row`);
  return button as HTMLElement;
}

/**
 * Row actions other than "create" live in a dropdown. The menu renders in a
 * portal, so items are looked up on `document` rather than within the row.
 */
async function openRowMenu(row: HTMLElement): Promise<void> {
  fireEvent.click(actionButton(row, "更多操作"));
  await screen.findByRole("menu");
}

/** Click an item in the open row menu by its visible label. */
async function clickMenuItem(label: string): Promise<void> {
  const item = await screen.findByText(label);
  fireEvent.click(item);
}

/**
 * Buttons that must not exist anywhere in a row, dropdown included. The menu
 * items only exist once opened, so absence has to be asserted on a closed menu.
 */
function rowHasNoDirectAction(row: HTMLElement, name: string): boolean {
  return row.querySelector(`button[aria-label="${name}"]`) === null;
}

describe("PathTreePanel editing", () => {
  it("creates a child path under the row whose button was clicked", async () => {
    render(<PathTreePanel value={ALL_PATHS} onChange={vi.fn()} />);
    await screen.findByText("案件一");

    // Create under 案件一, not under the root: the parent must be the clicked row,
    // which is what makes arbitrarily deep case trees possible.
    fireEvent.click(actionButton(rowFor("案件一"), "新建子路径"));
    const input = await screen.findByPlaceholderText(/案件一/);
    fireEvent.change(input, { target: { value: "上证材料" } });
    fireEvent.click(screen.getByRole("button", { name: "创 建" }));

    await waitFor(() =>
      expect(mocks.create).toHaveBeenCalledWith("上证材料", "case-1"),
    );
  });

  it("refuses a blank name instead of creating an unnamed path", async () => {
    render(<PathTreePanel value={ALL_PATHS} onChange={vi.fn()} />);
    await screen.findByText("案件一");

    fireEvent.click(actionButton(rowFor("案件一"), "新建子路径"));
    const input = await screen.findByPlaceholderText(/案件一/);
    fireEvent.change(input, { target: { value: "   " } });
    fireEvent.click(screen.getByRole("button", { name: "创 建" }));

    await waitFor(() => expect(mocks.create).not.toHaveBeenCalled());
  });

  it("renames a path from its own row", async () => {
    render(<PathTreePanel value={ALL_PATHS} onChange={vi.fn()} />);
    await screen.findByText("案件一");

    await openRowMenu(rowFor("案件一"));
    await clickMenuItem("重命名");
    const input = await screen.findByDisplayValue("案件一");
    fireEvent.change(input, { target: { value: "案件甲" } });
    fireEvent.click(screen.getByRole("button", { name: "保 存" }));

    await waitFor(() =>
      expect(mocks.rename).toHaveBeenCalledWith("case-1", "案件甲"),
    );
  });

  it("leaves only the create action inline so names keep their width", async () => {
    render(<PathTreePanel value={ALL_PATHS} onChange={vi.fn()} />);
    await screen.findByText("案件一");

    const row = rowFor("案件一");
    // Five 24px buttons in a 226px row left the name 0px wide, so the tree only
    // showed names while hovering. Everything but "create" must move into the menu.
    expect(actionButton(row, "新建子路径")).toBeTruthy();
    expect(actionButton(row, "更多操作")).toBeTruthy();
    for (const name of ["重命名", "删除", "管理此路径下的内容"]) {
      expect(rowHasNoDirectAction(row, name)).toBe(true);
    }
  });

  it("does not offer rename or delete on the root path", async () => {
    render(<PathTreePanel value={ALL_PATHS} onChange={vi.fn()} />);
    await screen.findByText("案件一");

    await openRowMenu(rowFor("公共区（未归类）"));
    // The root is the account's public area and is created by the migration, so
    // it must never be renamable or removable from the UI.
    expect(screen.queryByText("重命名")).toBeNull();
    expect(screen.queryByText("删除此路径")).toBeNull();
    expect(actionButton(rowFor("公共区（未归类）"), "新建子路径")).toBeTruthy();
  });

  it("deletes only after the confirmation is accepted", async () => {
    render(<PathTreePanel value={ALL_PATHS} onChange={vi.fn()} />);
    await screen.findByText("案件一");

    await openRowMenu(rowFor("案件一"));
    await clickMenuItem("删除此路径");
    fireEvent.click(await screen.findByRole("button", { name: "删 除" }));

    await waitFor(() => expect(mocks.remove).toHaveBeenCalledWith("case-1"));
  });

  it("surfaces the store's refusal verbatim when a path is not empty", async () => {
    const reason = "路径下还有内容（文档、数据表、子路径或已挂载的智能体），请先移走再删除。";
    mocks.remove.mockRejectedValue(new Error(reason));
    render(<PathTreePanel value={ALL_PATHS} onChange={vi.fn()} />);
    await screen.findByText("案件一");

    await openRowMenu(rowFor("案件一"));
    await clickMenuItem("删除此路径");
    fireEvent.click(await screen.findByRole("button", { name: "删 除" }));

    // The message explains what to do, so it must not be replaced by a generic
    // "操作失败".
    expect(await screen.findByText(reason)).toBeTruthy();
  });

  it("hides every edit action when readOnly", async () => {
    render(<PathTreePanel value={ALL_PATHS} onChange={vi.fn()} readOnly />);
    await screen.findByText("案件一");

    const row = rowFor("案件一");
    for (const name of ["新建子路径", "更多操作"]) {
      expect(rowHasNoDirectAction(row, name)).toBe(true);
    }
  });
});

describe("PathTreePanel no longer manages path contents", () => {
  it("offers no contents action on any row", async () => {
    // Filing is done from the right-hand list (select rows → 转移到路径). The tree
    // used to expose its own dialog, which duplicated that flow with different
    // semantics and confused people about which one was authoritative.
    render(<PathTreePanel value={ALL_PATHS} onChange={vi.fn()} />);
    await screen.findByText("案件一");

    await openRowMenu(rowFor("案件一"));
    expect(screen.queryByText("管理此路径下的内容")).toBeNull();
  });

  it("keeps only rename / delete in the row menu", async () => {
    render(<PathTreePanel value={ALL_PATHS} onChange={vi.fn()} />);
    await screen.findByText("案件一");

    await openRowMenu(rowFor("案件一"));
    const menu = await screen.findByRole("menu");
    const items = Array.from(menu.querySelectorAll('[role="menuitem"]'))
      .map((i) => (i.textContent || "").trim())
      .filter(Boolean);
    expect(items).toEqual(["重命名", "删除此路径"]);
  });
});
