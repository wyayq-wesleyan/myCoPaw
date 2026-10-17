import { describe, it, expect } from "vitest";
import {
  collapseInjectedFileContentParts,
  parseInjectedFileContent,
} from "./fileContentEnvelope";

const ZH = [
  "用户上传了文件：订单明细.csv",
  "",
  "--- 文件内容开始 ---",
  "行1",
  "行2",
  "--- 文件内容结束 ---",
].join("\n");

const EN = [
  "User uploaded file: orders.csv",
  "",
  "--- FILE CONTENT START ---",
  "row1",
  "row2",
  "--- FILE CONTENT END ---",
].join("\n");

describe("parseInjectedFileContent", () => {
  it("识别中文封套并取回文件名与正文", () => {
    expect(parseInjectedFileContent(ZH)).toEqual({
      name: "订单明细.csv",
      body: "行1\n行2",
    });
  });

  it("识别英文封套", () => {
    expect(parseInjectedFileContent(EN)).toEqual({
      name: "orders.csv",
      body: "row1\nrow2",
    });
  });

  it("正则元字符的文件名也能正确取出", () => {
    const text = ZH.replace("订单明细.csv", "a+b(c)[1].csv");
    expect(parseInjectedFileContent(text)?.name).toBe("a+b(c)[1].csv");
  });

  it("普通文本不匹配", () => {
    expect(parseInjectedFileContent("就是一句普通的话")).toBeNull();
    expect(parseInjectedFileContent("")).toBeNull();
  });

  it("缺少结束标记时不匹配（宁可漏判也不吞掉用户内容）", () => {
    const broken = [
      "用户上传了文件：x.csv",
      "",
      "--- 文件内容开始 ---",
      "正文但没结束标记",
    ].join("\n");
    expect(parseInjectedFileContent(broken)).toBeNull();
  });

  it("头部行出现但位置不对时不匹配", () => {
    const text = [
      "先说话",
      "用户上传了文件：x.csv",
      "--- 文件内容开始 ---",
      "正文",
      "--- 文件内容结束 ---",
    ].join("\n");
    expect(parseInjectedFileContent(text)).toBeNull();
  });
});

describe("collapseInjectedFileContentParts", () => {
  it("同一条消息里已有文件项时，丢掉注入正文，只留卡片", () => {
    const parts = [
      { type: "file", file_name: "订单明细.csv", file_url: "http://x/f" },
      { type: "text", text: ZH },
    ];

    const out = collapseInjectedFileContentParts(parts);

    expect(out).toHaveLength(1);
    expect(out[0].type).toBe("file");
  });

  it("没有文件项时退化成只有文件名的卡片，而不是摊开正文", () => {
    const out = collapseInjectedFileContentParts([{ type: "text", text: ZH }]);

    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({ type: "file", file_name: "订单明细.csv" });
  });

  it("用户自己写的内容与普通文本块不受影响", () => {
    const parts = [
      { type: "text", text: "帮我看看这个文件" },
      { type: "file", file_name: "订单明细.csv" },
      { type: "text", text: ZH },
      { type: "text", text: "谢谢" },
    ];

    const out = collapseInjectedFileContentParts(parts);

    expect(out.map((p) => p.type)).toEqual(["text", "file", "text"]);
    expect(out[0].text).toBe("帮我看看这个文件");
    expect(out[2].text).toBe("谢谢");
  });

  it("没有注入正文时原样返回", () => {
    const parts = [{ type: "text", text: "hello" }];
    expect(collapseInjectedFileContentParts(parts)).toEqual(parts);
  });
});
