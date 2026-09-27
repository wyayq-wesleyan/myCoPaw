import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import PathTreePanel, { ALL_PATHS } from "./PathTreePanel";

// The tree is a *filter control* when filtering is possible and a *read-only
// overview* when it is not. The distinction matters because acting on a click it
// cannot honour fires a request guaranteed to fail, and the page surfaces that as
// an error toast on every click — which is exactly what happened when the
// knowledge service was unconfigured and the tree was still clickable.
vi.mock("../../api/modules/paths", () => ({
  pathTree: async () => ({
    tree: [
      {
        id: "root-1",
        name: "默认",
        parent_id: null,
        is_root: true,
        is_public: true,
        children: [],
        document_count: 3,
        table_count: 1,
        agents: ["ag1"],
      },
    ],
  }),
  ALL_PATHS: "__all__",
}));

describe("PathTreePanel", () => {
  it("calls onChange when a row is clicked", async () => {
    const onChange = vi.fn();
    render(<PathTreePanel value={ALL_PATHS} onChange={onChange} />);
    const row = await screen.findByText("公共区（未归类）");
    fireEvent.click(row);
    expect(onChange).toHaveBeenCalledWith("root-1");
  });

  it("does not call onChange when disabled", async () => {
    const onChange = vi.fn();
    render(<PathTreePanel value={ALL_PATHS} onChange={onChange} disabled />);
    const row = await screen.findByText("公共区（未归类）");
    fireEvent.click(row);
    expect(onChange).not.toHaveBeenCalled();
  });

  it("does not expose a button role when disabled", async () => {
    render(<PathTreePanel value={ALL_PATHS} onChange={vi.fn()} disabled />);
    const row = await screen.findByText("公共区（未归类）");
    // A read-only tree must not advertise itself as interactive.
    expect(row.closest('[role="button"]')).toBeNull();
  });
});
