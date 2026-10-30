import { describe, expect, it } from "vitest";

import {
  filterRows,
  fmtDuration,
  fmtTs,
  groupBy,
  pct,
  sortRows,
  stateText,
  trayTip,
} from "../src/lib/format.js";

describe("pct", () => {
  it("常规换算取整", () => {
    expect(pct(87, 200)).toBe(44);
    expect(pct(200, 200)).toBe(100);
  });
  it("total 缺失/为 0 时返回 0 而不是 NaN", () => {
    expect(pct(5, 0)).toBe(0);
    expect(pct(5, undefined)).toBe(0);
  });
  it("越界钳制到 0..100", () => {
    expect(pct(300, 200)).toBe(100);
    expect(pct(-1, 200)).toBe(0);
  });
});

describe("fmtDuration", () => {
  it("不足 1 小时只显示 M:SS", () => {
    expect(fmtDuration(0)).toBe("0:00");
    expect(fmtDuration(65_000)).toBe("1:05");
  });
  it("超过 1 小时显示 H:MM:SS", () => {
    expect(fmtDuration(3_723_000)).toBe("1:02:03");
  });
  it("负值与空值归零", () => {
    expect(fmtDuration(-5)).toBe("0:00");
    expect(fmtDuration(null)).toBe("0:00");
  });
});

describe("fmtTs", () => {
  it("秒级时间戳按本地时区格式化", () => {
    const ts = Math.floor(new Date(2026, 9, 30, 18, 42, 7).getTime() / 1000);
    expect(fmtTs(ts)).toBe("18:42:07");
  });
  it("空值与非法值给占位", () => {
    expect(fmtTs("")).toBe("--:--:--");
    expect(fmtTs("not-a-date")).toBe("--:--:--");
  });
});

describe("sortRows / filterRows", () => {
  const rows = [
    { name: "张三", level: 5, decision: "踢出", online_state: "在线", note: "" },
    { name: "李四", level: 9, decision: "无操作", online_state: "离线", note: "名称被屏蔽" },
    { name: "王五", level: null, decision: "降级", online_state: "在线", note: "" },
  ];

  it("按数值列排序且 null 永远在最后", () => {
    expect(sortRows(rows, "level", "asc").map((r) => r.name)).toEqual(["张三", "李四", "王五"]);
    expect(sortRows(rows, "level", "desc").map((r) => r.name)).toEqual(["李四", "张三", "王五"]);
  });

  it("按名字搜索（含备注）", () => {
    expect(filterRows(rows, { q: "李" }).map((r) => r.name)).toEqual(["李四"]);
    expect(filterRows(rows, { q: "屏蔽" }).map((r) => r.name)).toEqual(["李四"]);
  });

  it("按决策与在线状态筛选可叠加", () => {
    expect(filterRows(rows, { decision: "无操作" }).map((r) => r.name)).toEqual(["李四"]);
    expect(filterRows(rows, { online: "在线" }).map((r) => r.name)).toEqual(["张三", "王五"]);
    expect(filterRows(rows, { decision: "无操作", online: "在线" })).toHaveLength(0);
  });

  it("空条件返回全部", () => {
    expect(filterRows(rows, {})).toHaveLength(3);
  });
});

describe("groupBy / 状态文案", () => {
  it("按类别聚合且保持出现顺序", () => {
    const out = groupBy(
      [
        { kind: "查询失败", text: "a" },
        { kind: "执行失败", text: "b" },
        { kind: "查询失败", text: "c" },
      ],
      (x) => x.kind,
    );
    expect(out.map((g) => g.key)).toEqual(["查询失败", "执行失败"]);
    expect(out[0].list).toHaveLength(2);
  });

  it("缺 key 归入『其他』", () => {
    expect(groupBy([{ text: "x" }], (x) => x.kind)[0].key).toBe("其他");
  });

  it("托盘提示只含状态与进度，不含玩家名（§5）", () => {
    expect(trayTip("running", 87, 200)).toBe("运行中 · 87/200");
    expect(trayTip("idle", 0, 0)).toBe("空闲");
    expect(stateText("paused")).toBe("已暂停");
  });
});
