// 运行页/状态条的纯格式化与表格逻辑（无 DOM，便于 vitest）。

/** 进度百分比，0..100 取整；total<=0 时按 0 处理（避免 NaN 宽度）。 */
export function pct(done, total) {
  if (!total || total <= 0) return 0;
  const v = Math.round((done / total) * 100);
  return Math.min(100, Math.max(0, v));
}

/** 毫秒 → H:MM:SS / M:SS */
export function fmtDuration(ms) {
  if (!ms || ms < 0) return "0:00";
  const s = Math.floor(ms / 1000);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const two = (n) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${two(m)}:${two(sec)}` : `${m}:${two(sec)}`;
}

/** 时间戳 → HH:MM:SS（事件流前缀）。 */
export function fmtTs(ts) {
  if (ts === undefined || ts === null || ts === "") return "--:--:--";
  const d = typeof ts === "number" ? new Date(ts < 1e12 ? ts * 1000 : ts) : new Date(ts);
  if (Number.isNaN(d.getTime())) return "--:--:--";
  const two = (n) => String(n).padStart(2, "0");
  return `${two(d.getHours())}:${two(d.getMinutes())}:${two(d.getSeconds())}`;
}

/** 表格排序：数字按数值，其余按本地化字符串；null 永远排最后。 */
export function sortRows(rows, key, dir = "asc") {
  const sign = dir === "desc" ? -1 : 1;
  return [...rows].sort((a, b) => {
    const av = a[key];
    const bv = b[key];
    if (av === null || av === undefined) return 1;
    if (bv === null || bv === undefined) return -1;
    if (typeof av === "number" && typeof bv === "number") return (av - bv) * sign;
    return String(av).localeCompare(String(bv), "zh-CN") * sign;
  });
}

/** 玩家表筛选：搜索（名字/备注）+ 决策 + 在线状态。 */
export function filterRows(rows, { q = "", decision = "", online = "" } = {}) {
  const needle = q.trim().toLocaleLowerCase();
  return rows.filter((r) => {
    if (decision && r.decision !== decision) return false;
    if (online && r.online_state !== online) return false;
    if (!needle) return true;
    return [r.name, r.note].some((v) =>
      String(v ?? "")
        .toLocaleLowerCase()
        .includes(needle),
    );
  });
}

/** 待办队列与失败分组的小工具：按类别聚合并保持出现顺序。 */
export function groupBy(items, keyFn) {
  const map = new Map();
  for (const item of items || []) {
    const k = keyFn(item) || "其他";
    if (!map.has(k)) map.set(k, []);
    map.get(k).push(item);
  }
  return [...map.entries()].map(([key, list]) => ({ key, list }));
}

/** 状态文案表（托盘提示只用状态与进度，§5 禁止出现玩家名）。 */
export function stateText(state) {
  return { idle: "空闲", running: "运行中", paused: "已暂停", failed: "有失败" }[state] || "空闲";
}

export function trayTip(state, done, total) {
  const head = stateText(state);
  return total > 0 ? `${head} · ${done}/${total}` : head;
}
