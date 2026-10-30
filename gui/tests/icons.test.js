import { describe, expect, it } from "vitest";

import { ICONS } from "../src/icons/icons.js";

// 规范 §4.4：全部图标用内联 SVG，禁 emoji 与字符字形；24px 画布、1.5px 描边、currentColor。
const NAMES = ["compact", "expand", "maximize", "restore", "pin", "pinOff", "minimize", "close"];

describe("图标（§4.4）", () => {
  it("六个图标齐全，全部是内联 SVG", () => {
    for (const name of NAMES) {
      const svg = ICONS[name];
      expect(svg, name).toBeTruthy();
      expect(svg.startsWith("<svg"), name).toBe(true);
      expect(svg).toContain('viewBox="0 0 24 24"');
      expect(svg).toContain('stroke-width="1.5"');
      expect(svg).toContain("currentColor");
    }
  });

  it("不含 emoji 与字符字形", () => {
    const forbidden = ["⤡", "⤢", "📌", "✕", "─", "●", "▣"];
    for (const name of NAMES) {
      for (const ch of forbidden) {
        expect(ICONS[name].includes(ch), `${name} 含 ${ch}`).toBe(false);
      }
    }
    // 图标本体应是纯 path/rect，不允许出现非 ASCII（emoji/字形都会命中）
    for (const name of NAMES) {
      expect(/^[\x20-\x7E\s]+$/.test(ICONS[name]), `${name} 含非 ASCII`).toBe(true);
    }
  });

  it("置顶两种状态可区分：实心 vs 描边", () => {
    expect(ICONS.pin).toContain('fill="currentColor"');
    expect(ICONS.pinOff).not.toContain('fill="currentColor"');
  });

  it("stroke 不随缩放变细（non-scaling-stroke）", () => {
    for (const name of NAMES) expect(ICONS[name]).toContain("non-scaling-stroke");
  });
});
