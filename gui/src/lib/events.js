// 事件流纯逻辑（规范 §6）：环形缓冲 2000、单批 >500 折叠、分级过滤。
// 与 DOM 无关，vitest 直接测。

export const RING_CAP = 2000;
export const FOLD_THRESHOLD = 500;

/** 固定容量环形缓冲；满了丢最旧（§6.2）。 */
export class Ring {
  constructor(cap = RING_CAP) {
    this.cap = cap;
    this.items = [];
    this.dropped = 0;
  }

  push(item) {
    this.items.push(item);
    if (this.items.length > this.cap) {
      this.dropped += this.items.length - this.cap;
      this.items.splice(0, this.items.length - this.cap);
    }
    return this;
  }

  get size() {
    return this.items.length;
  }

  /** 按 level 过滤后的副本（§6.3：默认只看 4 级以上）。 */
  visible(minLevel) {
    return this.items.filter((e) => (e.level ?? 4) >= minLevel);
  }

  clear() {
    this.dropped = 0;
    this.items.length = 0;
  }
}

/**
 * 一批事件超阈值时折叠成摘要（§6.2）：按 category+level 分组，
 * 每组一行「N 条同类事件」，保留首末时间戳，原文放 members 供展开。
 */
export function foldBatch(batch, threshold = FOLD_THRESHOLD) {
  if (!Array.isArray(batch)) return { folded: false, items: [] };
  if (batch.length <= threshold) return { folded: false, items: batch };
  const groups = new Map();
  for (const ev of batch) {
    const key = `${ev.category ?? ""}|${ev.level ?? 4}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(ev);
  }
  const items = [];
  for (const [key, members] of groups) {
    const [category] = key.split("|");
    items.push({
      folded: true,
      count: members.length,
      category,
      level: members[0].level ?? 4,
      text: `${members.length} 条同类事件`,
      ts: members[members.length - 1].ts,
      firstTs: members[0].ts,
      members,
    });
  }
  return { folded: true, items };
}

/** 按类别取 token 变量名（样式只认变量，不认色值）。 */
export function categoryVar(category) {
  const known = ["send", "parse", "async", "pause", "action", "exit"];
  return known.includes(category) ? `var(--cat-${category})` : "var(--text-dim)";
}

/** 状态 → 状态点类名（空闲/运行中/已暂停/失败）。 */
export function dotClass(state) {
  if (state === "running") return "ok";
  if (state === "paused") return "warn";
  if (state === "failed") return "danger";
  return "idle";
}

/** 状态 → 托盘四态之一（§5）：有失败优先。 */
export function trayState(state, failCount) {
  if (failCount > 0) return "failed";
  if (state === "running") return "running";
  if (state === "paused") return "paused";
  return "idle";
}
