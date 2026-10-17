/**
 * ``SessionItem`` 的操作列（下拉菜单）契约。
 *
 * 抽屉那层的测试把 ``SessionItem`` 整个 mock 掉了，所以「导出到底有没有出现在操作列
 * 里、点下去有没有调到回调」在那里是测不到的。这个文件补上这一段，并把菜单项作为
 * 一个整体锁住：导出要在归档之后、删除之前（删除是危险项，必须留在最后并且隔开）。
 */

import { describe, it, expect, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "../../test/common_setup";
import SessionItem from "./index";

// antd 的 Dropdown 在 jsdom 里走 portal + 异步展开，断言起来很吵；这里直接把
// menu.items 渲染成一排按钮，既确定又能验证到「项本身」和它的 onClick。
//
// SessionItem 同时挂了两个 Dropdown：一个点「更多」按钮展开，一个右键菜单。两者
// 用同一份 items，但只有前者是用户点得到的操作列，所以只给它渲染菜单 —— 否则
// testid 会重复两遍。
vi.mock("antd", async (importOriginal) => {
  const actual = await importOriginal<typeof import("antd")>();
  return {
    ...actual,
    Dropdown: ({ children, menu, trigger }: any) => {
      const isActionColumn = Array.isArray(trigger)
        ? trigger.includes("click")
        : true;
      return (
        <div>
          {children}
          {isActionColumn && (
            <div data-testid="menu">
              {menu.items
                .filter((item: any) => item.type !== "divider")
                .map((item: any) => (
                  <button
                    key={item.key}
                    data-testid={`menu-${item.key}`}
                    data-danger={item.danger ? "1" : "0"}
                    onClick={item.onClick}
                  >
                    {item.label}
                  </button>
                ))}
            </div>
          )}
        </div>
      );
    },
  };
});

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string, fallback: string) => fallback ?? key }),
}));

vi.mock("../../pages/Control/Channels/components", () => ({
  ChannelIcon: () => null,
}));

vi.mock("@agentscope-ai/icons", () => {
  const Stub = () => <span />;
  return {
    SparkMoreLine: Stub,
    SparkDeleteLine: Stub,
    SparkEditLine: Stub,
    SparkMarkLine: Stub,
    SparkMarkFill: Stub,
    SparkAistorageLine: Stub,
    SparkDownloadLine: Stub,
  };
});

function renderItem(overrides: Record<string, unknown> = {}) {
  const handlers = {
    onExport: vi.fn(),
    onDelete: vi.fn(),
    onPin: vi.fn(),
    onArchive: vi.fn(),
    onEdit: vi.fn(),
  };
  renderWithProviders(
    <SessionItem
      variant="drawer"
      sessionId="s1"
      name="Session One"
      {...handlers}
      {...overrides}
    />,
  );
  return handlers;
}

describe("SessionItem action menu", () => {
  it("offers export in the action column", async () => {
    const user = userEvent.setup();
    const handlers = renderItem();

    await user.click(screen.getByTestId("menu-export"));

    expect(handlers.onExport).toHaveBeenCalledWith("s1");
  });

  it("keeps delete last and marked as dangerous", () => {
    renderItem();

    const keys = Array.from(
      document.querySelectorAll('[data-testid^="menu-"]'),
    ).map((el) => el.getAttribute("data-testid")!.replace("menu-", ""));

    expect(keys).toContain("export");
    expect(keys[keys.length - 1]).toBe("delete");
    expect(screen.getByTestId("menu-delete").getAttribute("data-danger")).toBe("1");
    // Export is a benign, non-destructive action.
    expect(screen.getByTestId("menu-export").getAttribute("data-danger")).toBe("0");
  });

  it("still wires the pre-existing actions", async () => {
    const user = userEvent.setup();
    const handlers = renderItem();

    await user.click(screen.getByTestId("menu-pin"));
    await user.click(screen.getByTestId("menu-archive"));
    await user.click(screen.getByTestId("menu-delete"));

    expect(handlers.onPin).toHaveBeenCalledWith("s1");
    expect(handlers.onArchive).toHaveBeenCalledWith("s1");
    expect(handlers.onDelete).toHaveBeenCalledWith("s1");
  });
});
