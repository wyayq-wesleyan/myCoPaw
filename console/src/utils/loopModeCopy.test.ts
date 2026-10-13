import { describe, expect, it } from "vitest";
import zh from "../locales/zh.json";
import en from "../locales/en.json";
import ja from "../locales/ja.json";
import ru from "../locales/ru.json";
import id from "../locales/id.json";
import ptBR from "../locales/pt-BR.json";
import vi from "../locales/vi.json";

/**
 * The three built-in mode labels are read by customers, not by engineers.
 *
 * Two properties are what make them usable, and both are easy to lose when
 * someone edits the copy later:
 *
 *  1. **Each one carries a concrete example.** "持续推进一个具体且可验证的目标"
 *     does not tell a non-technical reader what will happen; "读完这批合同，把所有
 *     风险条款找出来并附上页码" does. An example is the whole point of the copy.
 *  2. **No internal mechanism words.** "Loop", "受控", "迭代" describe how it is
 *     built, and they were in the shipped text: readers saw jargon that defined
 *     nothing.
 *
 * This test fails on the old wording, so it cannot quietly come back.
 */

// Each locale is an independent partial translation, so the entries are typed
// loosely: asserting "same shape as zh" fails on locales that legitimately omit
// keys (e.g. `appCenter`).
type ModeLocale = {
  loop: {
    modes: Record<string, { name: string; description: string }>;
    selectorTitle: string;
    selectorAria: string;
  };
};

const LOCALES: Array<[string, ModeLocale]> = [
  ["zh", zh as unknown as ModeLocale],
  ["en", en as unknown as ModeLocale],
  ["ja", ja as unknown as ModeLocale],
  ["ru", ru as unknown as ModeLocale],
  ["id", id as unknown as ModeLocale],
  ["pt-BR", ptBR as unknown as ModeLocale],
  ["vi", vi as unknown as ModeLocale],
];

const MODE_IDS = ["default", "goal", "mission"] as const;

// The marker that introduces an example, per language.
const EXAMPLE_MARKERS: Record<string, string[]> = {
  zh: ["例：", "例:", "例如"],
  en: ["e.g.", "for example", "example:"],
  ja: ["例：", "例:", "例えば"],
  ru: ["Пример", "пример", "напр."],
  id: ["Contoh", "contoh", "mis."],
  "pt-BR": ["Ex.:", "ex.:", "Exemplo", "exemplo"],
  vi: ["Ví dụ", "ví dụ", "VD:"],
};

// Words that describe the mechanism rather than the experience.
const JARGON = ["Loop", "loop", "受控", "迭代"];

describe("built-in mode copy is written for customers", () => {
  for (const [lang, locale] of LOCALES) {
    describe(lang, () => {
      for (const modeId of MODE_IDS) {
        it(`gives a concrete example for '${modeId}'`, () => {
          const entry = locale.loop.modes[modeId];
          const description: string = entry.description;
          const markers = EXAMPLE_MARKERS[lang];
          expect(
            markers.some((marker) => description.includes(marker)),
            `「${description}」缺少举例（应含 ${markers.join(" / ")} 之一）——没有例子读者无法判断该选哪个`,
          ).toBe(true);
        });
      }

      it("describes what happens rather than how it is built", () => {
        for (const modeId of MODE_IDS) {
          const description: string = locale.loop.modes[modeId].description;
          for (const word of JARGON) {
            expect(
              description.includes(word),
              `「${description}」含有实现术语「${word}」——客户看不懂机制词`,
            ).toBe(false);
          }
        }
      });
    });
  }

  it("keeps the panel title and aria label free of mechanism words", () => {
    // The panel used to be titled "Loop 模式" — the first thing a customer sees,
    // and a word that explains nothing. The title should ask what they want, not
    // name the machinery.
    for (const [lang, locale] of LOCALES) {
      for (const key of ["selectorTitle", "selectorAria"] as const) {
        const text: string = locale.loop[key];
        for (const word of JARGON) {
          expect(
            text.includes(word),
            `${lang}/${key}「${text}」含有实现术语「${word}」`,
          ).toBe(false);
        }
      }
    }
  });

  it("keeps the three labels short enough for the menu", () => {
    // The dropdown clamps to a couple of small lines, so a mode whose label is
    // far longer than its siblings would be the only one visibly cut off.
    for (const [lang, locale] of LOCALES) {
      for (const modeId of MODE_IDS) {
        const entry = locale.loop.modes[modeId];
        expect(
          entry.name.length,
          `${lang}/${modeId} 名称过长：${entry.name}`,
        ).toBeLessThanOrEqual(12);
        expect(
          entry.description.length,
          `${lang}/${modeId} 说明过长，会被菜单截断：${entry.description}`,
        ).toBeLessThanOrEqual(140);
      }
    }
  });
});
