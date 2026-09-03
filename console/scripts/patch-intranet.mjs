/**
 * patch-intranet.mjs — 内网化补丁（构建前运行）
 *
 * 将 node_modules 中硬编码的外网资源（alicdn CDN 图片/iconfont）替换为
 * 内联 data-URI，确保离线环境 UI 完整。幂等：已打补丁的文件自动跳过。
 *
 * 由 package.json 的 prebuild 钩子自动执行；node_modules 重装后需重跑
 * （prebuild 已保证每次构建前都会执行）。
 */
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const MARKER = "/* intranet-patched */";

/** 彩色文档图标 data-URI（参数：首字母文字、背景色） */
function docIconSvg(text, color) {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 40"><rect x="6" y="3" width="28" height="34" rx="4" fill="${color}"/><text x="20" y="26" font-family="sans-serif" font-size="12" font-weight="bold" fill="#fff" text-anchor="middle">${text}</text></svg>`;
  return `data:image/svg+xml,${encodeURIComponent(svg)}`;
}

/** 空状态插画 data-URI（内联灰盒子） */
const emptyIllustration = `data:image/svg+xml,${encodeURIComponent(
  `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 320"><rect x="80" y="90" width="160" height="120" rx="10" fill="#f0f1f5"/><rect x="100" y="115" width="80" height="8" rx="4" fill="#d8dbe3"/><rect x="100" y="135" width="120" height="8" rx="4" fill="#e3e6ee"/><rect x="100" y="155" width="100" height="8" rx="4" fill="#e3e6ee"/><circle cx="235" cy="105" r="26" fill="#e8ebf2"/><path d="M225 105l7 7 13-14" stroke="#c3c9d6" stroke-width="4" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
)}`;

/** 通用 logo 占位 data-URI */
const logoPlaceholder = `data:image/svg+xml,${encodeURIComponent(
  `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200"><circle cx="100" cy="100" r="88" fill="#e6f4ff"/><path d="M60 108l26 26 54-58" stroke="#1677ff" stroke-width="14" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
)}`;

/** 加载动画占位（静态圆环） */
const spinnerPlaceholder = `data:image/svg+xml,${encodeURIComponent(
  `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 56 56"><circle cx="28" cy="28" r="22" stroke="#e5e7eb" stroke-width="6" fill="none"/><path d="M28 6a22 22 0 0 1 22 22" stroke="#1677ff" stroke-width="6" fill="none" stroke-linecap="round"/></svg>`,
)}`;

const patches = [
  {
    // 文件卡片图标（用户可见度最高）：doc/xls/pdf/ppt/zip/视频/音频/图片/md/默认
    file: join(root, "node_modules/@agentscope-ai/chat/lib/Attachments/FileList/FileListCard.js"),
    replacements: [
      [/https:\/\/gw\.alicdn\.com\/imgextra\/i1\/O1CN01cVtZXI23tPVhiZoPJ_[^"']*/g, docIconSvg("XLS", "#22b35e")],
      [/https:\/\/gw\.alicdn\.com\/imgextra\/i1\/O1CN01uDnnuz1XMNEjgFMul_[^"']*/g, docIconSvg("IMG", "#8c8c8c")],
      [/https:\/\/gw\.alicdn\.com\/imgextra\/i1\/O1CN01PaXli01DDPAO68fsI_[^"']*/g, docIconSvg("MD", "#8c8c8c")],
      [/https:\/\/gw\.alicdn\.com\/imgextra\/i3\/O1CN01mB5PzD27fuIWK661W_[^"']*/g, docIconSvg("PDF", "#ff4d4f")],
      [/https:\/\/gw\.alicdn\.com\/imgextra\/i3\/O1CN01a8j7Jv1nW1QyFme7k_[^"']*/g, docIconSvg("PPT", "#ff6e31")],
      [/https:\/\/gw\.alicdn\.com\/imgextra\/i1\/O1CN01XaNi8P1UkhQXoQdUL_[^"']*/g, docIconSvg("DOC", "#1677ff")],
      [/https:\/\/gw\.alicdn\.com\/imgextra\/i1\/O1CN01K7jgEj1sywWTkPSGY_[^"']*/g, docIconSvg("ZIP", "#fab714")],
      [/https:\/\/gw\.alicdn\.com\/imgextra\/i2\/O1CN01zTTe0q1Xg4GkZgJol_[^"']*/g, docIconSvg("VID", "#ff4d4f")],
      [/https:\/\/gw\.alicdn\.com\/imgextra\/i2\/O1CN01qOBdXG1UpHO6f3Vvc_[^"']*/g, docIconSvg("AUD", "#8c8c8c")],
    ],
  },
  {
    // 空状态插画
    file: join(root, "node_modules/@agentscope-ai/design/lib/components/commonComponents/Empty/index.js"),
    replacements: [
      [/https:\/\/gw\.alicdn\.com\/[^"']*/g, emptyIllustration],
      [/https:\/\/img\.alicdn\.com\/[^"']*/g, emptyIllustration],
    ],
  },
  {
    // iconfont 远程脚本：内网禁用（Spark 图标为内联 SVG，不依赖它）
    file: join(root, "node_modules/@agentscope-ai/design/lib/components/commonComponents/ConfigProvider/index.js"),
    replacements: [
      [/https:\/\/at\.alicdn\.com\/[^"']*/g, ""],
    ],
  },
  {
    // 会话页 logo + 启动面板默认图
    file: join(root, "node_modules/@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/core/Sessions/index.js"),
    replacements: [[/https:\/\/img\.alicdn\.com\/[^"']*/g, logoPlaceholder]],
  },
  {
    file: join(root, "node_modules/@agentscope-ai/chat/lib/AgentScopeRuntimeWebUI/starter/OptionsPanel/defaultConfig.js"),
    replacements: [[/https:\/\/img\.alicdn\.com\/[^"']*/g, logoPlaceholder]],
  },
  {
    // 深度思考/图片生成加载动画
    file: join(root, "node_modules/@agentscope-ai/chat/lib/Accordion/DeepThinking.js"),
    replacements: [[/https:\/\/img\.alicdn\.com\/[^"']*/g, spinnerPlaceholder]],
  },
  {
    file: join(root, "node_modules/@agentscope-ai/chat/lib/ImageGenerator/index.js"),
    replacements: [[/https:\/\/img\.alicdn\.com\/[^"']*/g, spinnerPlaceholder]],
  },
  {
    // 欢迎提示小图标
    file: join(root, "node_modules/@agentscope-ai/chat/lib/WelcomePrompts/index.js"),
    replacements: [[/https:\/\/img\.alicdn\.com\/[^"']*/g, logoPlaceholder]],
  },
];

let changed = 0;
let skipped = 0;
let missing = 0;

for (const { file, replacements } of patches) {
  let content;
  try {
    content = readFileSync(file, "utf8");
  } catch {
    console.warn(`[patch-intranet] 跳过（不存在）: ${file}`);
    missing++;
    continue;
  }
  if (content.includes(MARKER)) {
    skipped++;
    continue;
  }
  let count = 0;
  for (const [pattern, replacement] of replacements) {
    content = content.replace(pattern, () => {
      count++;
      return replacement;
    });
  }
  if (count > 0) {
    writeFileSync(file, `${content}\n${MARKER.replace("*/", `${count} url(s) localized */`)}\n`);
    changed++;
  }
}

console.log(
  `[patch-intranet] 完成: ${changed} 个文件已内网化, ${skipped} 个已打补丁, ${missing} 个不存在`,
);
