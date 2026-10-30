import { describe, expect, it } from "vitest";

import {
  MAX_EXPAND,
  expand,
  fromEngine,
  newRule,
  orderMismatch,
  renumberByOrder,
  samplePlayer,
  sortByPriority,
  toEngine,
  unexpressible,
  validateView,
} from "../src/lib/rules.js";

const cond = (field, op, value) => ({ field, op, value });

function viewRule(over = {}) {
  return {
    id: "r1",
    name: "测试规则",
    enabled: true,
    priority: 10,
    stop: false,
    match: "all",
    groups: [{ match: "all", conditions: [cond("weekly_total", "<", 1000)] }],
    actions: [{ type: "notify" }],
    ...over,
  };
}

describe("expand（AND/OR 最多两层的 DNF 展开）", () => {
  it("单组 AND 只有一条组合", () => {
    const out = expand(viewRule());
    expect(out.combos).toHaveLength(1);
    expect(out.combos[0][0].field).toBe("weekly_total");
  });

  it("组内 OR（一层）展开成 N 条组合", () => {
    const rule = viewRule({
      groups: [
        {
          match: "any",
          conditions: [cond("weekly_total", "<", 1000), cond("rank", "==", "member")],
        },
      ],
    });
    const out = expand(rule);
    expect(out.combos).toHaveLength(2);
    expect(out.combos.map((c) => c[0].field)).toEqual(["weekly_total", "rank"]);
  });

  it("AND of OR（两层）走笛卡尔积", () => {
    const rule = viewRule({
      groups: [
        {
          match: "any",
          conditions: [cond("weekly_total", "<", 1000), cond("weekly_total", "<", 500)],
        },
        { match: "any", conditions: [cond("rank", "==", "member"), cond("rank", "==", "elder")] },
      ],
    });
    const out = expand(rule);
    expect(out.combos).toHaveLength(4);
    for (const combo of out.combos) expect(combo).toHaveLength(2);
  });

  it("外层 OR 是组的并集，不互乘", () => {
    const rule = viewRule({
      match: "any",
      groups: [
        { match: "all", conditions: [cond("weekly_total", "<", 1000)] },
        { match: "all", conditions: [cond("rank", "==", "member")] },
      ],
    });
    expect(expand(rule).combos).toHaveLength(2);
  });

  it("空条件报错而不是产出『命中所有人』的规则", () => {
    expect(expand(viewRule({ groups: [] })).error).toMatch("至少需要一个条件组");
    expect(expand(viewRule({ groups: [{ match: "all", conditions: [] }] })).error).toMatch(
      "条件为空",
    );
  });

  it("超过展开上限时报错，不静默截断", () => {
    const conds = (n) => Array.from({ length: n }, (_, i) => cond("weekly_total", "<", i + 1));
    const rule = viewRule({
      groups: [
        { match: "any", conditions: conds(5) },
        { match: "any", conditions: conds(5) },
      ],
    });
    // 5×5=25 > 16
    expect(expand(rule).error).toContain(String(MAX_EXPAND));
  });
});

describe("toEngine / fromEngine 双向", () => {
  it("OR 展开成多条规则，id 带 #k 且其余字段继承", () => {
    const rule = viewRule({
      groups: [
        { match: "any", conditions: [cond("weekly_total", "<", 1), cond("weekly_total", "<", 2)] },
      ],
    });
    const { rules } = toEngine([rule]);
    expect(rules.map((r) => r.id)).toEqual(["r1#0", "r1#1"]);
    expect(rules.every((r) => r.priority === 10 && r.name === "测试规则")).toBe(true);
  });

  it("单条组合不加后缀", () => {
    expect(toEngine([viewRule()]).rules[0].id).toBe("r1");
  });

  it("展开结果能被读回成 OR 视图（往返不丢条件）", () => {
    const rule = viewRule({
      groups: [
        {
          match: "any",
          conditions: [cond("weekly_total", "<", 1000), cond("rank", "==", "member")],
        },
      ],
    });
    const back = fromEngine(toEngine([rule]).rules);
    expect(back).toHaveLength(1);
    expect(back[0].groups).toHaveLength(2);
    expect(back[0].expandedFrom).toEqual(["r1#0", "r1#1"]);
  });

  it("形状不同的兄弟规则不强行合并，但两条都保留并注明原因（§7.3 不静默丢弃）", () => {
    const a = {
      id: "x#0",
      name: "n",
      enabled: true,
      priority: 10,
      stop: false,
      conditions: [cond("rank", "==", "a")],
      actions: [{ type: "kick" }],
    };
    const b = { ...a, id: "x#1", actions: [{ type: "notify" }] };
    const view = fromEngine([a, b]);
    expect(view).toHaveLength(2);
    expect(view.map((r) => r.actions[0].type)).toEqual(["kick", "notify"]);
    expect(view.every((r) => r.expandedFrom === undefined && r.splitReason)).toBe(true);
  });

  it("只读规则不参与 toEngine 写回", () => {
    const ro = { id: "b", readonly: true, raw: { builtin: true }, groups: [], actions: [] };
    expect(toEngine([ro]).rules).toEqual([]);
  });
});

describe("unexpressible（只读置灰必须给原因）", () => {
  it("内置规则给配置页指引", () => {
    expect(unexpressible({ builtin: true, id: "b" })).toContain("配置页");
  });
  it("未知字段/非法算子/空 conditions 都给原因", () => {
    expect(
      unexpressible({ id: "x", conditions: [cond("foo", "==", 1)], actions: [{ type: "notify" }] }),
    ).toContain("未知字段");
    expect(
      unexpressible({ id: "x", conditions: [cond("rank", "<", 1)], actions: [{ type: "notify" }] }),
    ).toContain("不支持算子");
    expect(unexpressible({ id: "x", conditions: [], actions: [{ type: "notify" }] })).toContain(
      "conditions 为空",
    );
  });
  it("合法规则没有原因", () => {
    expect(
      unexpressible({
        id: "x",
        conditions: [cond("rank", "==", "a")],
        actions: [{ type: "kick" }],
      }),
    ).toBe("");
  });
});

describe("validateView", () => {
  it("重复 id 被抓到", () => {
    const issues = validateView([viewRule(), viewRule()]);
    expect(issues.some((i) => i.message === "id 重复")).toBe(true);
  });
  it("空 actions 被抓到", () => {
    const issues = validateView([viewRule({ actions: [] })]);
    expect(issues.some((i) => i.message.includes("actions"))).toBe(true);
  });
  it("未知动作被抓到", () => {
    const issues = validateView([viewRule({ actions: [{ type: "banish" }] })]);
    expect(issues.some((i) => i.message.includes("未知动作"))).toBe(true);
  });
  it("合法规则零 issue", () => {
    expect(validateView([viewRule()])).toEqual([]);
  });
});

describe("排序与优先级", () => {
  it("拖拽后按顺序回写优先级数字（10,20,30…）", () => {
    const a = viewRule({ id: "a", priority: 10 });
    const b = viewRule({ id: "b", priority: 20 });
    renumberByOrder([a, b], ["b", "a"]);
    expect(b.priority).toBe(10);
    expect(a.priority).toBe(20);
  });
  it("只读规则不被改数字", () => {
    const ro = { id: "ro", readonly: true, priority: 5 };
    renumberByOrder([ro], ["ro"]);
    expect(ro.priority).toBe(5);
  });
  it("数字为准：sortByPriority 按数字排", () => {
    const out = sortByPriority([
      viewRule({ id: "a", priority: 20 }),
      viewRule({ id: "b", priority: 10 }),
    ]);
    expect(out.map((r) => r.id)).toEqual(["b", "a"]);
  });
  it("顺序与数字不一致能被检测出来（界面据此提示）", () => {
    const rules = [viewRule({ id: "a", priority: 20 }), viewRule({ id: "b", priority: 10 })];
    expect(orderMismatch(rules, ["a", "b"])).toBe(true);
    expect(orderMismatch(rules, ["b", "a"])).toBe(false);
  });
});

describe("预览样本", () => {
  it("使用合成玩家名，绝不出现真实名字（§7.4）", () => {
    const sample = samplePlayer(["member", "elder"]);
    expect(sample.player).toBe("示例玩家");
    expect(sample.rank).toBe("member");
    expect(Object.keys(sample).sort()).toEqual(
      ["joined_at", "last_online", "player", "rank", "weekly_total"].sort(),
    );
  });
  it("newRule 产出的规则本身合法", () => {
    expect(validateView([newRule("n1")])).toEqual([]);
  });
});

describe("不可表达规则的读入", () => {
  it("内置规则读成只读条目并给出原因，不参与写回", () => {
    const view = fromEngine([
      { id: "builtin", builtin: true, conditions: [], actions: [{ type: "notify" }] },
    ]);
    expect(view).toHaveLength(1);
    expect(view[0].readonly).toBe(true);
    expect(view[0].readonlyReason).toContain("配置页");
    expect(view[0].raw.builtin).toBe(true);
    expect(toEngine(view).rules).toEqual([]);
  });

  it("缺 actions 的规则也判为不可表达", () => {
    expect(
      unexpressible({ id: "x", conditions: [cond("rank", "==", "a")], actions: [] }),
    ).toContain("actions 为空");
  });

  it("同 base id 但 priority 不同：不合并，两条都在", () => {
    const a = {
      id: "y#0",
      name: "n",
      enabled: true,
      priority: 10,
      stop: false,
      conditions: [cond("rank", "==", "a")],
      actions: [{ type: "kick" }],
    };
    const b = { ...a, id: "y#1", priority: 20 };
    const view = fromEngine([a, b]);
    expect(view).toHaveLength(2);
    expect(view.map((r) => r.priority)).toEqual([10, 20]);
  });

  it("组条件不是数组时按空组处理（不抛异常）", () => {
    expect(expand(viewRule({ groups: [{ match: "all" }] })).error).toMatch("条件为空");
  });
});
