import { describe, it, expect } from "vitest";
import {
  getChannelIconUrl,
  getChannelLetterColor,
  CHANNEL_DEFAULT_ICON_URL,
} from "./channelIcons";

// 内网化部署：渠道图标一律走本地字母头像，URL 查询返回空字符串。
describe("getChannelIconUrl (intranet)", () => {
  it("returns an empty string for known channels", () => {
    expect(getChannelIconUrl("dingtalk")).toBe("");
    expect(getChannelIconUrl("discord")).toBe("");
  });

  it("returns an empty string for unknown channels", () => {
    expect(getChannelIconUrl("unknown_channel")).toBe("");
  });

  it("CHANNEL_DEFAULT_ICON_URL is an empty string (no CDN in intranet)", () => {
    expect(CHANNEL_DEFAULT_ICON_URL).toBe("");
  });
});

describe("getChannelLetterColor", () => {
  it("returns predefined color '#2A63AD' for known channel 'console'", () => {
    expect(getChannelLetterColor("console")).toBe("#2A63AD");
  });

  it("returns predefined color '#5865F2' for known channel 'discord'", () => {
    expect(getChannelLetterColor("discord")).toBe("#5865F2");
  });

  it("returns a color string starting with '#' for unknown channel 'my_custom_bot'", () => {
    const color = getChannelLetterColor("my_custom_bot");
    expect(typeof color).toBe("string");
    expect(color).toMatch(/^#/);
  });

  it("returns the same color on repeated calls for the same unknown channel (deterministic hash)", () => {
    const color1 = getChannelLetterColor("my_custom_bot");
    const color2 = getChannelLetterColor("my_custom_bot");
    expect(color1).toBe(color2);
  });
});
