// 规则编辑器纯逻辑（规范 §7）：一套数据两个视图，AND/OR 最多两层。
//
// 引擎（domain/rules.py）只表达「一条规则 = 一组 AND 条件」。OR 与「两层的
// AND-of-OR」在**作者侧**展开成多条引擎规则（DNF 展开），引擎一行不改 ——
// 这是业务规则零改动的落地方式。展开结果在预览里可见，不静默。

export const FIELDS = {
  weekly_total: { label: "周贡献", type: "number", ops: ["<", "<=", ">", ">=", "=="] },
  rank: { label: "当前等级", type: "enum", ops: ["==", "!=", "in", "not_in"] },
  player: { label: "玩家名", type: "text", ops: ["==", "!=", "matches"] },
  joined_at: { label: "加入时间", type: "date", ops: ["before", "after"] },
  last_online: { label: "上次在线", type: "date", ops: ["before", "after"] },
};

export const ACTIONS = ["promote", "demote", "kick", "notify", "ignore"];
export const MAX_EXPAND = 16;

/** 视图模型：{id,name,enabled,priority,stop,match,groups:[{match,conditions:[]}],actions:[]} */
export function newRule(id) {
  return {
    id,
    name: "新规则",
    enabled: true,
    priority: 10,
    stop: false,
    match: "all",
    groups: [{ match: "all", conditions: [{ field: "weekly_total", op: "<", value: 0 }] }],
    actions: [{ type: "notify" }],
  };
}

function groupAlternatives(group) {
  const conds = Array.isArray(group.conditions) ? group.conditions : [];
  // 组内 any = OR：每个条件各自成一条 AND 集；组内 all = 整组一条 AND 集
  return group.match === "any" ? conds.map((c) => [c]) : [conds];
}

/** 视图 → DNF：外层 all 走笛卡尔积（AND of OR），外层 any 走并集（OR of 组）。 */
export function expand(rule) {
  const groups = Array.isArray(rule.groups) ? rule.groups : [];
  if (groups.length === 0) return { error: "至少需要一个条件组" };
  const parts = groups.map(groupAlternatives);
  let combos;
  if (rule.match === "any") {
    combos = parts.flat();
  } else {
    combos = [[]];
    for (const alts of parts) {
      const next = [];
      for (const acc of combos) {
        for (const alt of alts) next.push(acc.concat(alt));
        if (next.length > MAX_EXPAND) break;
      }
      combos = next;
      if (combos.length > MAX_EXPAND) break;
    }
  }
  combos = combos.filter((c) => c.length > 0);
  if (combos.length === 0) return { error: "条件为空：无条件规则会命中所有人" };
  if (combos.length > MAX_EXPAND)
    return { error: `展开后超过 ${MAX_EXPAND} 条组合，请拆分规则或改用 JSON 视图` };
  return { combos };
}

/** 视图 → 引擎规则数组；带 error 时不产出（宁可拒绝也不写坏规则）。 */
export function toEngine(viewRules) {
  const out = [];
  for (const rule of viewRules) {
    if (rule.readonly) continue; // 不可表达的结构原样保留，由调用方带回
    const res = expand(rule);
    if (res.error) return { error: `规则「${rule.name || rule.id}」：${res.error}` };
    const many = res.combos.length > 1;
    res.combos.forEach((conditions, k) => {
      out.push({
        id: many ? `${rule.id}#${k}` : rule.id,
        name: rule.name,
        enabled: rule.enabled !== false,
        priority: Number(rule.priority) || 0,
        stop: !!rule.stop,
        conditions,
        actions: rule.actions,
      });
    });
  }
  return { rules: out };
}

/** 引擎规则 → 视图：同一 base id 的兄弟规则合并回 OR，不可表达的标只读并给原因。 */
export function fromEngine(engineRules) {
  const buckets = new Map();
  for (const rule of engineRules || []) {
    const reason = unexpressible(rule);
    if (reason) {
      buckets.set(`ro:${rule.id}`, {
        id: rule.id,
        name: rule.name || rule.id,
        readonly: true,
        readonlyReason: reason,
        raw: rule,
        match: "all",
        groups: [],
        actions: rule.actions || [],
        enabled: rule.enabled !== false,
        priority: rule.priority ?? 0,
        stop: !!rule.stop,
      });
      continue;
    }
    const base = String(rule.id).split("#")[0];
    if (!buckets.has(base)) buckets.set(base, []);
    buckets.get(base).push(rule);
  }
  const view = [];
  for (const [key, val] of buckets) {
    if (key.startsWith("ro:")) {
      view.push(val);
      continue;
    }
    const first = val[0];
    const sameShape = val.every((r) => sameAs(first, r));
    const shape = (r) => ({
      id: r.id,
      name: r.name || r.id,
      enabled: r.enabled !== false,
      priority: r.priority ?? 0,
      stop: !!r.stop,
      match: "all",
      groups: [{ match: "all", conditions: r.conditions }],
      actions: r.actions || [],
    });
    if (!sameShape) {
      // 形状不同不能合并成一棵 OR 树：逐条保留，各自成一条视图规则并注明未合并。
      // 绝不静默丢弃（§7.3）——丢条件等于悄悄改判定。
      for (const r of val)
        view.push({ ...shape(r), splitReason: `与 ${key} 的同组规则形状不同，未合并` });
      continue;
    }
    view.push({
      id: key,
      name: first.name || key,
      enabled: first.enabled !== false,
      priority: first.priority ?? 0,
      stop: !!first.stop,
      match: "all",
      groups:
        val.length > 1
          ? val.map((r) => ({ match: "all", conditions: r.conditions }))
          : [{ match: "all", conditions: first.conditions }],
      actions: first.actions || [],
      expandedFrom: val.length > 1 ? val.map((r) => r.id) : undefined,
    });
  }
  return view;
}

function sameAs(a, b) {
  return (
    JSON.stringify(a.actions) === JSON.stringify(b.actions) &&
    a.enabled === b.enabled &&
    (a.priority ?? 0) === (b.priority ?? 0) &&
    !!a.stop === !!b.stop &&
    (a.name || "") === (b.name || "")
  );
}

/** 为什么这条引擎规则在行编辑里表达不了（只读置灰并显示原因）。 */
export function unexpressible(rule) {
  if (rule.builtin) return "内置阈值规则由配置阈值生成，只能在配置页修改";
  if (!Array.isArray(rule.conditions) || rule.conditions.length === 0)
    return "conditions 为空（引擎会在校验期报错）";
  for (const c of rule.conditions) {
    const spec = FIELDS[c?.field];
    if (!spec) return `未知字段 ${c?.field}，行编辑不支持`;
    if (!spec.ops.includes(c.op)) return `字段 ${c.field} 不支持算子 ${c.op}`;
    if (c.value === undefined) return "条件缺少 value";
  }
  if (!Array.isArray(rule.actions) || rule.actions.length === 0) return "actions 为空";
  return "";
}

/** 视图侧校验（与引擎校验同口径，尽量在发 IPC 前就标红）。 */
export function validateView(viewRules) {
  const issues = [];
  const seen = new Set();
  for (const rule of viewRules) {
    if (rule.readonly) continue;
    if (!rule.id) issues.push({ ruleId: rule.id, message: "缺少 id" });
    else if (seen.has(rule.id)) issues.push({ ruleId: rule.id, message: "id 重复" });
    seen.add(rule.id);
    if (!Number.isInteger(Number(rule.priority)))
      issues.push({ ruleId: rule.id, message: "priority 必须是整数" });
    if (!Array.isArray(rule.actions) || rule.actions.length === 0)
      issues.push({ ruleId: rule.id, message: "actions 必须是非空列表" });
    for (const act of rule.actions || []) {
      if (!ACTIONS.includes(act.type))
        issues.push({ ruleId: rule.id, message: `未知动作 ${act.type}` });
      if (["promote", "demote"].includes(act.type) && act.times !== undefined) {
        if (!Number.isInteger(act.times) || act.times < 1)
          issues.push({ ruleId: rule.id, message: `${act.type}.times 必须是 >=1 的整数` });
      }
    }
    const res = expand(rule);
    if (res.error) issues.push({ ruleId: rule.id, message: res.error });
  }
  return issues;
}

/** 拖拽排序只改数字：按顺序重排优先级（10,20,30…）。 */
export function renumberByOrder(viewRules, idOrder) {
  const byId = new Map(viewRules.map((r) => [r.id, r]));
  let n = 10;
  for (const id of idOrder) {
    const rule = byId.get(id);
    if (rule && !rule.readonly) {
      rule.priority = n;
      n += 10;
    }
  }
  return viewRules;
}

/** 顺序与数字不一致时以数字为准（§7.5），界面用这个结果渲染。 */
export function sortByPriority(viewRules) {
  return [...viewRules].sort((a, b) => (a.priority ?? 0) - (b.priority ?? 0));
}

export function orderMismatch(viewRules, idOrder) {
  const byId = new Map(viewRules.map((r) => [r.id, r]));
  let prev = Number.NEGATIVE_INFINITY;
  for (const id of idOrder) {
    const p = byId.get(id)?.priority;
    if (p === undefined) continue;
    if (p < prev) return true;
    prev = p;
  }
  return false;
}

/** 合成玩家样本（§7.4：预览禁止使用真实玩家名）。 */
export function samplePlayer(ranks = []) {
  return {
    player: "示例玩家",
    rank: ranks.length ? ranks[0] : "",
    weekly_total: 12345,
    joined_at: "2026-01-01",
    last_online: "2026-10-01",
  };
}
