import { describe, it, expect } from "vitest";
import { __test__ } from "../sessionApi";

const { contentToRequestParts } = __test__;

/**
 * 重载路径必须折叠后端注入的文件正文。
 *
 * 只测 `collapseInjectedFileContentParts` 本身是不够的：把 sessionApi 里那次调用
 * 删掉，helper 的测试照样通过，而用户会重新看到几万行正文。这条测试盯的是**接线**。
 *
 * 背景：上传文档时后端把解析出的正文注入到那条消息里（`--- 文件内容开始 ---` 包裹），
 * 并且持久化——这样后续轮次模型还看得到文件。但控制台重开会话时会把这条消息原样渲染，
 * 于是整份文件摊在气泡里。实时视图看不到是巧合（渲染的是前端自己发出的那条）。
 */
const INJECTED = [
  "用户上传了文件：订单明细.csv",
  "",
  "--- 文件内容开始 ---",
  "行1",
  "行2",
  "--- 文件内容结束 ---",
].join("\n");

describe("contentToRequestParts 折叠注入的文件正文", () => {
  it("有文件项时只留卡片，不再输出正文文本", () => {
    const parts = contentToRequestParts([
      { type: "file", file_name: "订单明细.csv", file_url: "http://x/f" },
      { type: "text", text: INJECTED },
    ]);

    expect(parts.some((p) => p.type === "file")).toBe(true);
    const text = parts
      .map((p) => (typeof p.text === "string" ? p.text : ""))
      .join("");
    expect(text).not.toContain("行1");
    expect(text).not.toContain("文件内容开始");
  });

  it("没有文件项时输出一张只有文件名的卡片", () => {
    const parts = contentToRequestParts([{ type: "text", text: INJECTED }]);

    expect(parts).toHaveLength(1);
    expect(parts[0]).toMatchObject({ type: "file", file_name: "订单明细.csv" });
  });

  it("用户正常的文字消息不受影响", () => {
    const parts = contentToRequestParts([
      { type: "text", text: "这份订单帮我按客户汇总" },
    ]);

    expect(parts).toHaveLength(1);
    expect(parts[0].text).toBe("这份订单帮我按客户汇总");
  });
});
