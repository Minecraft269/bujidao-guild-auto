import { describe, expect, it } from "vitest";

import {
  FOLD_THRESHOLD,
  RING_CAP,
  Ring,
  categoryVar,
  dotClass,
  foldBatch,
  trayState,
} from "../src/lib/events.js";

describe("Ring（§6.2 环形缓冲 2000）", () => {
  it("持续 push 不超过容量，且丢最旧", () => {
    const ring = new Ring(3);
    for (let i = 1; i <= 5; i++) ring.push({ level: 4, text: `e${i}` });
    expect(ring.size).toBe(3);
    expect(ring.items.map((e) => e.text)).toEqual(["e3", "e4", "e5"]);
    expect(ring.dropped).toBe(2);
  });

  it("默认容量是规范值 2000", () => {
    expect(RING_CAP).toBe(2000);
    const ring = new Ring();
    for (let i = 0; i < 2500; i++) ring.push({ level: 4 });
    expect(ring.size).toBe(2000);
    expect(ring.dropped).toBe(500);
  });

  it("visible 按 level 过滤且不改动原缓冲", () => {
    const ring = new Ring(10);
    ring.push({ level: 2, text: "trace" });
    ring.push({ level: 5, text: "warn" });
    expect(ring.visible(4).map((e) => e.text)).toEqual(["warn"]);
    expect(ring.size).toBe(2);
  });

  it("缺 level 的事件按 4（信息）处理", () => {
    const ring = new Ring(5);
    ring.push({ text: "no-level" });
    expect(ring.visible(4)).toHaveLength(1);
  });
});

describe("foldBatch（§6.2 单批 >500 折叠）", () => {
  it("未超阈值原样返回", () => {
    const batch = [{ category: "send", level: 4 }];
    const out = foldBatch(batch);
    expect(out.folded).toBe(false);
    expect(out.items).toBe(batch);
  });

  it("恰好等于阈值不折叠（>500 才折叠）", () => {
    const batch = Array.from({ length: FOLD_THRESHOLD }, () => ({ category: "send", level: 4 }));
    expect(foldBatch(batch).folded).toBe(false);
  });

  it("超阈值按 category+level 分组折叠，保留成员供展开", () => {
    const batch = [];
    for (let i = 0; i < 300; i++) batch.push({ category: "send", level: 4, ts: i });
    for (let i = 0; i < 300; i++) batch.push({ category: "parse", level: 4, ts: 1000 + i });
    const out = foldBatch(batch);
    expect(out.folded).toBe(true);
    expect(out.items).toHaveLength(2);
    const send = out.items.find((x) => x.category === "send");
    expect(send.count).toBe(300);
    expect(send.text).toBe("300 条同类事件");
    expect(send.members).toHaveLength(300);
    expect(send.ts).toBe(299);
    expect(send.firstTs).toBe(0);
  });

  it("非数组输入安全返回", () => {
    expect(foldBatch(undefined)).toEqual({ folded: false, items: [] });
  });
});

describe("类别与状态映射", () => {
  it("六类事件各映射到 token 变量", () => {
    for (const c of ["send", "parse", "async", "pause", "action", "exit"]) {
      expect(categoryVar(c)).toBe(`var(--cat-${c})`);
    }
    expect(categoryVar("unknown")).toBe("var(--text-dim)");
  });

  it("状态点类名", () => {
    expect(dotClass("running")).toBe("ok");
    expect(dotClass("paused")).toBe("warn");
    expect(dotClass("failed")).toBe("danger");
    expect(dotClass("idle")).toBe("idle");
  });

  it("托盘四态：有失败优先（§5）", () => {
    expect(trayState("running", 2)).toBe("failed");
    expect(trayState("running", 0)).toBe("running");
    expect(trayState("paused", 0)).toBe("paused");
    expect(trayState("idle", 0)).toBe("idle");
  });
});
