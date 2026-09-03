import { describe, it, expect } from "vitest";
import { providerIcon } from "./providerIcon";

// 内网化部署：providerIcon 一律返回空字符串，由调用方（ProviderIcon 组件）
// 渲染本地字母头像，不再引用任何 CDN 图片。
describe("providerIcon (intranet)", () => {
  it("returns an empty string for known providers", () => {
    expect(providerIcon("openai")).toBe("");
    expect(providerIcon("anthropic")).toBe("");
    expect(providerIcon("zhipu-cn")).toBe("");
  });

  it("returns an empty string for aliases and unknown providers", () => {
    expect(providerIcon("kimi-cn")).toBe(providerIcon("kimi-intl"));
    expect(providerIcon("unknown-provider")).toBe("");
    expect(providerIcon("")).toBe("");
  });

  it("never returns a remote url for any provider", () => {
    const providers = [
      "modelscope",
      "aliyun-codingplan",
      "deepseek",
      "gemini",
      "azure-openai",
      "anthropic",
      "ollama",
      "minimax-cn",
      "openai",
      "dashscope",
      "lmstudio",
      "siliconflow-cn",
      "qwenpaw-local",
      "zhipu-cn",
      "openrouter",
      "opencode",
      "kilo",
      "github-models",
      "volcengine-cn",
      "mimo-tokenplan",
    ];
    for (const p of providers) {
      expect(providerIcon(p)).toBe("");
    }
  });
});
