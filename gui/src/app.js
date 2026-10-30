// GUI 主脚本：把核推送的结构化事件渲染成五个页签 + 小窗 + 状态条（§3.3/§3.4/§6/§7）。
//
// 约定：GUI 不读日志文件、不碰文件系统；所有数据来自 IPC（Rust 侧 core_call 转发）。
// 界面文案里的业务值（阈值、文案模板、命令模板）一律来自核下发的配置，本文件不复制。

import { ICONS } from "./icons/icons.js";
import { Ring, categoryVar, dotClass, foldBatch, trayState } from "./lib/events.js";
import {
  filterRows,
  fmtDuration,
  fmtTs,
  groupBy,
  pct,
  sortRows,
  stateText,
  trayTip,
} from "./lib/format.js";
import {
  ACTIONS,
  FIELDS,
  fromEngine,
  newRule,
  samplePlayer,
  sortByPriority,
  toEngine,
  validateView,
} from "./lib/rules.js";

const $ = (id) => document.getElementById(id);
const api = window.__TAURI__;

const DECISION_LABEL = { promote: "提升", demote: "降级", kick: "踢出", none: "无操作" };
const EXEC_LABEL = {
  pending: "待执行",
  running: "执行中",
  done: "已完成",
  failed: "失败",
  skipped: "跳过",
};
const ONLINE_LABEL = { online: "在线", offline: "离线", unknown: "未知" };
const LEVEL_LABEL = { 1: "追踪", 2: "调试", 3: "详细", 4: "信息", 5: "警告", 6: "错误" };

const S = {
  hello: null,
  state: {
    state: "idle",
    stage: "未开始",
    done: 0,
    total: 0,
    player: "",
    elapsed_ms: 0,
    pause_reason: "",
  },
  action: { cmd: "", verify: "", retry: 0, last: "", kind: "" },
  players: [],
  pending: [],
  fails: [],
  reports: {},
  dirs: {},
  sort: { key: "index", dir: "asc" },
  cfg: { fields: [], dirty: new Set(), errors: {} },
  rules: { view: [], jsonMode: false, dirty: false, issues: [] },
  log: { ring: new Ring(), minLevel: 4, selected: null, paused: false, filter: "" },
  theme: "dark",
  topmost: false,
  maximized: false,
  compact: false,
  // 首次运行门禁（§3.5）：配置刚生成时必须先保存才放行其它页签
  firstRun: false,
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 玩家名/阶段名来自游戏文本，进 innerHTML 前必须转义（本地应用也不留注入面）
const esc = (s) =>
  String(s ?? "").replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c],
  );

// ── IPC ────────────────────────────────────────────────────────────────
async function rpc(method, params = {}) {
  if (!api?.core?.invoke) return { ok: false, error: "Tauri API 不可用（静态预览）" };
  try {
    return await api.core.invoke("core_call", { method, params });
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

function applyEnvelope(payload) {
  if (!payload || typeof payload !== "object") return;
  const { method, params } = payload;
  if (!method) return;
  const handlers = {
    state: applyState,
    action: applyAction,
    players: applyPlayers,
    pending: applyPending,
    failures: applyFails,
    config: applyConfig,
    config_errors: applyConfigErrors,
    rules: applyRules,
    reports: applyReports,
    confirm: openConfirmFromCore,
  };
  if (handlers[method]) handlers[method](params);
  else if (method === "event") pushEvents([params]);
  else if (method === "events") pushEvents(params.items || []);
}

// ── 运行页 ─────────────────────────────────────────────────────────────
function applyState(p) {
  Object.assign(S.state, p || {});
  renderState();
}

function applyAction(p) {
  Object.assign(S.action, p || {});
  $("act-cmd").textContent = S.action.cmd || "—";
  $("act-verify").textContent = S.action.verify || "—";
  $("act-retry").textContent = String(S.action.retry ?? 0);
  $("act-last").textContent = S.action.last || "—";
  $("act-target-kind").textContent = S.action.kind || "—";
}

function applyPlayers(p) {
  const items = p?.items ?? [];
  S.players = p?.replace === false ? [...S.players, ...items] : items;
  renderPlayers();
}

function applyPending(p) {
  S.pending = p?.items ?? [];
  renderPending();
}

function applyFails(p) {
  S.fails = p?.items ?? [];
  renderFails();
}

function applyReports(p) {
  S.reports = p || {};
  S.dirs = p?.dirs || S.dirs;
  renderReports();
}

function applyConfig(p) {
  S.cfg.fields = p?.fields ?? [];
  S.cfg.dirty.clear();
  S.cfg.errors = {};
  renderConfig();
  applyUiPrefs();
}

function applyConfigErrors(p) {
  S.cfg.errors = p?.fields ?? {};
  renderConfig();
}

function applyUiPrefs() {
  const get = (path) => S.cfg.fields.find((f) => f.path === path)?.value;
  const titlebar = get("ui.custom_titlebar");
  if (titlebar !== undefined) {
    // 自绘 = 去掉系统边框；原生 = 恢复边框 + 32px 工具条（§3.1）
    document.body.dataset.titlebar = titlebar ? "custom" : "native";
    void winCmd("win_set_titlebar", { custom: !!titlebar });
  }
  const theme = get("ui.theme");
  if (theme) setTheme(theme);
  // 关闭策略交给 Rust 决定（运行中不得直接退出，§5）
  const trayEnabled = get("ui.tray.enabled");
  const action = get("ui.tray.close_action");
  const ask = get("ui.tray.ask_on_close");
  if (trayEnabled !== undefined || action !== undefined) {
    void winCmd("win_set_close_policy", {
      enabled: trayEnabled !== false,
      action: action || "ask",
      ask: ask !== false,
    });
  }
}

function renderState() {
  const s = S.state;
  const cls = dotClass(s.state);
  $("st-dot").className = `dot ${cls}`;
  $("c-dot").className = `dot ${cls}`;
  $("st-state").textContent = stateText(s.state);
  $("c-state").textContent = stateText(s.state);
  $("st-stage").textContent = s.stage || "未开始";
  $("c-stage").textContent = s.stage || "未开始";
  const prog = `${s.done ?? 0}/${s.total ?? 0}`;
  $("st-progress").textContent = prog;
  $("c-progress").textContent = prog;
  const p = pct(s.done, s.total);
  // 进度条用 transform 驱动（见 styles.css），不写 width，避免触发布局
  $("st-bar").style.transform = `scaleX(${p / 100})`;
  $("c-bar").style.transform = `scaleX(${p / 100})`;
  $("st-elapsed").textContent = fmtDuration(s.elapsed_ms);
  $("st-player").textContent = s.player || "—";
  $("c-player").textContent = `当前玩家：${s.player || "—"}`;
  $("st-pause-reason").textContent = s.pause_reason || "—";
  $("sb-tray").textContent =
    `托盘：${trayTip(trayState(s.state, S.fails.length), s.done, s.total)}`;
  $("btn-start").disabled = s.state === "running" || s.state === "paused";
  $("btn-pause").disabled = s.state !== "running";
  $("btn-resume").disabled = s.state !== "paused";
  $("btn-stop").disabled = s.state === "idle";
  syncTray();
}

function renderPlayers() {
  const rows = filterRows(S.players, {
    q: $("player-search").value,
    decision: $("player-decision").value,
    online: $("player-online").value,
  });
  const sorted = sortRows(rows, S.sort.key, S.sort.dir);
  const body = $("player-body");
  body.replaceChildren();
  for (const r of sorted) {
    const tr = document.createElement("tr");
    tr.append(
      cell(r.index ?? ""),
      cell(r.name, r.masked ? "名称被屏蔽，指令不可查" : undefined),
      cell(r.level ?? "—"),
      cell(r.weekly_total ?? "—"),
      cell(Array.isArray(r.weekly_detail) ? r.weekly_detail.join(" / ") : (r.weekly_detail ?? "—")),
      cell(r.last_online ?? "—"),
      cell(ONLINE_LABEL[r.online_state] || "未知"),
      cell(DECISION_LABEL[r.decision] || "无操作"),
      cell(EXEC_LABEL[r.exec_state] || "—"),
      cell(r.note || "", undefined, true),
    );
    body.append(tr);
  }
  $("player-count").textContent = `${sorted.length} / ${S.players.length} 人`;
  $("player-empty").hidden = sorted.length > 0;
}

function cell(text, title, wrap = false) {
  const td = document.createElement("td");
  td.textContent = String(text ?? "");
  if (title) td.title = title;
  if (wrap) td.className = "wrap";
  return td;
}

function textDiv(text, cls) {
  const d = document.createElement("div");
  if (cls) d.className = cls;
  d.textContent = String(text ?? "");
  return d;
}

function renderPending() {
  const box = $("pending-list");
  box.replaceChildren();
  $("pending-count").textContent = `${S.pending.length} 条`;
  if (S.pending.length === 0) box.append(textDiv("没有待办。", "muted"));
  for (const it of S.pending) {
    const row = document.createElement("div");
    row.className = "item";
    row.append(
      textDiv(it.player || "—"),
      textDiv(`${it.action || ""}：${it.reason || ""}`, "why"),
      textDiv(`${fmtTs(it.enqueued_at)} · 重试 ${it.retries ?? 0}`),
    );
    box.append(row);
  }
  $("sb-pending").textContent = `待办 ${S.pending.length}`;
}

function renderFails() {
  const box = $("fail-list");
  box.replaceChildren();
  $("fail-count").textContent = `${S.fails.length} 条`;
  if (S.fails.length === 0) box.append(textDiv("没有失败或跳过。", "muted"));
  for (const g of groupBy(S.fails, (x) => x.kind)) {
    box.append(textDiv(`${g.key}（${g.list.length}）`, "group-title"));
    for (const it of g.list) {
      const row = document.createElement("div");
      row.className = "item";
      row.append(textDiv(it.player || "—"), textDiv(it.reason || "", "why"), textDiv(""));
      box.append(row);
    }
  }
  $("sb-fail").textContent = `失败 ${S.fails.length}`;
  renderState();
}

function renderReports() {
  $("rep-1").textContent = S.reports.basic || "—";
  $("rep-2").textContent = S.reports.full || "—";
  $("rep-3").textContent = S.reports.summary || "—";
}

// ── 事件流（§6） ───────────────────────────────────────────────────────
function pushEvents(items) {
  for (const ev of items) S.log.ring.push(ev);
  renderEvents();
  const last = items[items.length - 1];
  if (last) $("c-event").textContent = `[${fmtTs(last.ts)}] ${last.text || ""}`;
}

function renderEvents() {
  const box = $("events");
  const all = S.log.ring.visible(S.log.minLevel);
  const needle = S.log.filter.trim();
  const list = needle ? all.filter((e) => String(e.text || "").includes(needle)) : all;
  $("log-count").textContent =
    `${list.length} 条（缓冲 ${S.log.ring.size}/2000，丢弃 ${S.log.ring.dropped}）`;
  const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 4;
  box.replaceChildren();
  for (const ev of list.slice(-800)) {
    const row = document.createElement("div");
    row.className = `ev${ev.player ? " selectable" : ""}${ev.folded ? " folded" : ""}`;
    if (ev.folded) {
      row.title = "折叠的同类事件，点开查看原文";
      row.onclick = () => {
        $("event-detail").textContent = (ev.members || [])
          .map((m) => `${fmtTs(m.ts)} ${m.text}`)
          .join("\n");
        $("event-detail-title").textContent = `${ev.count} 条同类事件原文`;
      };
    } else if (ev.player) {
      row.onclick = () => selectEvent(ev, row);
    }
    const cat = textDiv(ev.category || "", "cat");
    cat.style.setProperty("--cat-color", categoryVar(ev.category));
    row.append(textDiv(fmtTs(ev.ts), "ts"), cat, textDiv(ev.text || "", "txt"));
    box.append(row);
  }
  if (!S.log.paused && atBottom) box.scrollTop = box.scrollHeight;
}

function selectEvent(ev, row) {
  for (const el of $("events").querySelectorAll('.ev[aria-selected="true"]'))
    el.removeAttribute("aria-selected");
  row.setAttribute("aria-selected", "true");
  S.log.selected = ev;
  $("event-detail-title").textContent = `${ev.player} · ${fmtTs(ev.ts)}`;
  const chain = ev.chain || ev.trace || [];
  $("event-detail").textContent = chain.length
    ? chain.map((c, i) => `${i + 1}. ${c.detail || JSON.stringify(c)}`).join("\n")
    : "该事件未带判定链。";
}

// ── 配置页 ─────────────────────────────────────────────────────────────
function renderConfig() {
  const box = $("cfg-list");
  const needle = $("cfg-search").value.trim().toLowerCase();
  box.replaceChildren();
  const groups = groupBy(
    S.cfg.fields.filter((f) => {
      if (!needle) return true;
      return [f.path, f.label, f.comment].some((v) =>
        String(v ?? "")
          .toLowerCase()
          .includes(needle),
      );
    }),
    (f) => String(f.path).split(".")[0],
  );
  for (const g of groups) {
    const det = document.createElement("details");
    det.className = "cfg-group";
    det.open = true;
    const sum = document.createElement("summary");
    sum.textContent = `${g.key}（${g.list.length} 项）`;
    det.append(sum);
    for (const f of g.list) det.append(configRow(f));
    box.append(det);
  }
  $("sb-dirty").textContent = S.cfg.dirty.size ? `未保存 ${S.cfg.dirty.size} 项` : "";
}

function configRow(f) {
  const row = document.createElement("div");
  row.className = "cfg-row";
  const err = S.cfg.errors[f.path];
  if (err) row.classList.add("invalid");
  const note = textDiv(err || f.comment || "", err ? "err" : "comment");
  if (f.needs_restart) {
    // §3.5 热重载分级：这些键保存后仍需重启
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = "需重启";
    note.append(" ", chip);
  }
  row.append(textDiv(f.path, "name"), controlFor(f), note);
  return row;
}

function controlFor(f) {
  let el;
  if (f.kind === "bool") {
    el = document.createElement("input");
    el.type = "checkbox";
    el.checked = !!f.value;
    el.onchange = () => markDirty(f.path, el.checked);
  } else if (f.kind === "enum" && Array.isArray(f.choices) && f.choices.length) {
    el = document.createElement("select");
    for (const c of f.choices) {
      const o = document.createElement("option");
      o.value = String(c);
      o.textContent = String(c);
      el.append(o);
    }
    el.value = String(f.value);
    el.onchange = () => markDirty(f.path, el.value);
  } else if (f.kind === "json" || f.kind === "list") {
    el = document.createElement("textarea");
    el.value = typeof f.value === "string" ? f.value : JSON.stringify(f.value, null, 2);
    el.onchange = () => markDirty(f.path, el.value);
  } else {
    el = document.createElement("input");
    el.type = "text";
    el.value = f.value === undefined || f.value === null ? "" : String(f.value);
    el.onchange = () => markDirty(f.path, el.value);
  }
  el.disabled = !!f.readonly;
  el.dataset.path = f.path;
  if (f.readonly) el.title = "只读字段，由构建期决定";
  // 键位类字段给一个「绑定」按钮（§3.6）：点一下捕获实体按键
  if (!f.readonly && el.tagName === "INPUT" && el.type === "text" && /key/i.test(f.path)) {
    const wrap = document.createElement("span");
    wrap.style.display = "inline-flex";
    wrap.style.gap = "var(--sp-1)";
    wrap.style.width = "100%";
    const btn = document.createElement("button");
    btn.className = "ctl";
    btn.textContent = "绑定";
    btn.onclick = () => captureKey(f, el, btn);
    wrap.append(el, btn);
    return wrap;
  }
  return el;
}

/** 捕获一次按键并写回（§3.6）：Esc 取消、10 秒超时、冲突只提示。 */
async function captureKey(f, input, btn) {
  btn.disabled = true;
  btn.textContent = "请按键…";
  const r = await rpc("capture_key", { path: f.path });
  btn.disabled = false;
  btn.textContent = "绑定";
  if (!r.ok) {
    // 钩子起不来：保持原值并显式报错
    $("sb-dirty").textContent = `绑定失败：${r.error}`;
    return;
  }
  const res = r.result || {};
  if (!res.ok) {
    $("sb-dirty").textContent =
      res.reason === "timeout" ? "绑定超时（10 秒内没有按键）" : "已取消绑定";
    return;
  }
  input.value = res.key;
  markDirty(f.path, res.key);
  $("sb-dirty").textContent = res.conflict
    ? `已绑定 ${res.display}（${res.conflict}）`
    : `已绑定 ${res.display}`;
}

const pendingEdits = new Map();

function markDirty(path, value) {
  pendingEdits.set(path, value);
  S.cfg.dirty.add(path);
  $("sb-dirty").textContent = `未保存 ${S.cfg.dirty.size} 项`;
}

async function saveConfig() {
  if (pendingEdits.size === 0) return;
  const r = await rpc("save_config", { values: Object.fromEntries(pendingEdits) });
  if (r.ok) {
    pendingEdits.clear();
    S.cfg.dirty.clear();
    $("cfg-save").textContent = "保存";
    const restart = r.result?.restart_required ?? [];
    // §3.5：有需重启项就给出明确提示与「立即重启」入口，不假装已生效
    $("sb-restart").hidden = restart.length === 0;
    $("sb-dirty").textContent = restart.length ? `有 ${restart.length} 项需重启生效` : "";
    if (S.firstRun) {
      S.firstRun = false;
      unlockTabs();
      await openModal({
        title: "配置已保存",
        body: "配置已保存，请重新启动脚本以使其生效。",
        actions: [{ label: "知道了", value: true, primary: true }],
      });
    }
    await rpc("config", {});
  } else {
    $("cfg-save").textContent = "保存失败";
    $("cfg-save").title = r.error || "";
  }
}

// ── 规则页（§7） ───────────────────────────────────────────────────────
function applyRules(p) {
  S.rules.view = fromEngine(p?.rules ?? []);
  S.rules.dirty = false;
  if (S.rules.jsonMode) syncJsonFromView();
  renderRules();
}

function renderRules() {
  const issues = validateView(S.rules.view);
  S.rules.issues = issues;
  $("rule-issues").textContent = issues
    .map((i) => `· ${i.ruleId || "(无 id)"}：${i.message}`)
    .join("\n");
  const box = $("rule-list");
  box.replaceChildren();
  for (const rule of sortByPriority(S.rules.view)) box.append(ruleCard(rule));
  box.hidden = S.rules.jsonMode;
  $("rule-json").hidden = !S.rules.jsonMode;
  $("rule-view-toggle").textContent = S.rules.jsonMode ? "切到行编辑视图" : "切到 JSON 视图";
}

function ruleCard(rule) {
  const el = document.createElement("div");
  el.className = "rule";
  el.dataset.ruleId = rule.id;
  el.dataset.readonly = String(!!rule.readonly);

  const head = document.createElement("div");
  head.className = "rule-head";
  if (rule.readonly) {
    head.append(
      textDiv(`🔒 ${rule.name || rule.id}`, "grow-x"),
      textDiv(rule.readonlyReason, "muted"),
    );
  } else {
    const en = document.createElement("input");
    en.type = "checkbox";
    en.checked = rule.enabled !== false;
    en.title = "启用/禁用";
    en.onchange = () => {
      rule.enabled = en.checked;
      touchRules();
    };
    const name = document.createElement("input");
    name.type = "text";
    name.className = "grow-x";
    name.value = rule.name || "";
    name.oninput = () => {
      rule.name = name.value;
      touchRules();
    };
    const prio = document.createElement("input");
    prio.type = "number";
    prio.value = String(rule.priority ?? 0);
    prio.title = "优先级（数字小的先命中）";
    prio.oninput = () => {
      rule.priority = Number(prio.value);
      touchRules();
    };
    const stop = document.createElement("input");
    stop.type = "checkbox";
    stop.checked = !!rule.stop;
    stop.title = "命中后停止后续规则";
    stop.onchange = () => {
      rule.stop = stop.checked;
      touchRules();
    };
    const del = document.createElement("button");
    del.className = "ctl danger";
    del.textContent = "删除";
    del.onclick = () => removeRule(rule.id);
    head.append(
      en,
      name,
      textDiv("优先级", "muted"),
      prio,
      stop,
      textDiv("命中即停", "muted"),
      del,
    );
  }
  el.append(head);
  if (rule.readonly) return el;

  const groups = document.createElement("div");
  groups.className = "groups";
  (rule.groups || []).forEach((group, gi) => groups.append(groupRow(rule, group, gi)));
  const outer = document.createElement("select");
  outer.innerHTML =
    '<option value="all">全部条件组都要满足（AND）</option><option value="any">任一条件组满足（OR）</option>';
  outer.value = rule.match || "all";
  outer.onchange = () => {
    rule.match = outer.value;
    touchRules();
  };
  const addGroup = document.createElement("button");
  addGroup.className = "ctl";
  addGroup.textContent = "+ 条件组";
  addGroup.onclick = () => {
    rule.groups.push({ match: "all", conditions: [{ field: "weekly_total", op: "<", value: 0 }] });
    touchRules();
  };
  const outerRow = document.createElement("div");
  outerRow.className = "group";
  outerRow.append(outer, addGroup);
  groups.append(outerRow);
  el.append(groups);

  const chips = document.createElement("div");
  chips.className = "chips";
  for (const act of rule.actions || []) chips.append(actionChip(rule, act));
  const addAct = document.createElement("select");
  addAct.innerHTML = `<option value="">+ 动作</option>${ACTIONS.map(
    (a) => `<option value="${a}">${a}</option>`,
  ).join("")}`;
  addAct.onchange = () => {
    if (!addAct.value) return;
    const act = { type: addAct.value };
    if (addAct.value === "promote" || addAct.value === "demote") act.times = 1;
    rule.actions.push(act);
    touchRules();
  };
  chips.append(addAct);
  el.append(chips);

  if (rule.splitReason) el.append(textDiv(rule.splitReason, "muted"));
  if (rule.expandedFrom)
    el.append(textDiv(`由 ${rule.expandedFrom.join(" / ")} 合并显示`, "muted"));
  return el;
}

function groupRow(rule, group, gi) {
  const row = document.createElement("div");
  row.className = "group";
  const match = document.createElement("select");
  match.innerHTML = '<option value="all">且（AND）</option><option value="any">或（OR）</option>';
  match.value = group.match || "all";
  match.onchange = () => {
    group.match = match.value;
    touchRules();
  };
  row.append(match);
  (group.conditions || []).forEach((cond, ci) => row.append(condRow(group, cond, ci)));
  const add = document.createElement("button");
  add.className = "ctl";
  add.textContent = "+ 条件";
  add.onclick = () => {
    group.conditions.push({ field: "weekly_total", op: "<", value: 0 });
    touchRules();
  };
  const rm = document.createElement("button");
  rm.className = "ctl danger";
  rm.textContent = "删组";
  rm.onclick = () => {
    rule.groups.splice(gi, 1);
    touchRules();
  };
  row.append(add, rm);
  return row;
}

function condRow(group, cond, ci) {
  const wrap = document.createElement("span");
  wrap.className = "cond";
  const field = document.createElement("select");
  field.innerHTML = Object.entries(FIELDS)
    .map(([k, v]) => `<option value="${k}">${v.label}</option>`)
    .join("");
  field.value = cond.field;
  const op = document.createElement("select");
  const fillOps = () => {
    const ops = FIELDS[field.value]?.ops || [];
    op.innerHTML = ops.map((o) => `<option value="${o}">${o}</option>`).join("");
    if (!ops.includes(cond.op)) cond.op = ops[0];
    op.value = cond.op;
  };
  fillOps();
  field.onchange = () => {
    cond.field = field.value;
    cond.op = FIELDS[field.value].ops[0];
    touchRules();
  };
  op.onchange = () => {
    cond.op = op.value;
    touchRules();
  };
  const value = document.createElement("input");
  value.type = "text";
  value.value = Array.isArray(cond.value) ? cond.value.join(",") : String(cond.value ?? "");
  value.placeholder = FIELDS[cond.field]?.type === "date" ? "YYYY-MM-DD" : "值";
  value.oninput = () => {
    const type = FIELDS[cond.field]?.type;
    if (type === "number") cond.value = Number(value.value);
    else if (cond.op === "in" || cond.op === "not_in")
      cond.value = value.value
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
    else cond.value = value.value;
    touchRules();
  };
  const rm = document.createElement("button");
  rm.className = "ctl";
  rm.textContent = "×";
  rm.title = "删条件";
  rm.onclick = () => {
    group.conditions.splice(ci, 1);
    touchRules();
  };
  wrap.append(field, op, value, rm);
  return wrap;
}

function actionChip(rule, act) {
  const chip = document.createElement("span");
  chip.className = "chip";
  const rm = document.createElement("button");
  rm.className = "ctl";
  rm.textContent = "×";
  rm.onclick = () => {
    const i = rule.actions.indexOf(act);
    if (i >= 0) rule.actions.splice(i, 1);
    touchRules();
  };
  chip.append(document.createTextNode(`${act.type} `));
  if (act.type === "promote" || act.type === "demote") {
    const times = document.createElement("input");
    times.type = "number";
    times.value = String(act.times ?? 1);
    times.style.width = "48px";
    times.oninput = () => {
      act.times = Number(times.value);
      touchRules();
    };
    chip.append(times);
  }
  chip.append(rm);
  return chip;
}

function removeRule(id) {
  S.rules.view = S.rules.view.filter((r) => r.id !== id);
  touchRules();
}

function touchRules() {
  S.rules.dirty = true;
  renderRules();
  $("sb-dirty").textContent = "未保存（规则）";
  if (S.rules.jsonMode) syncJsonFromView();
}

function syncJsonFromView() {
  const res = toEngine(S.rules.view);
  $("rule-json").value = JSON.stringify(res.error ? { error: res.error } : res.rules, null, 2);
  if (res.error) $("rule-issues").textContent = res.error;
}

function toggleRuleView() {
  if (!S.rules.jsonMode) {
    syncJsonFromView();
    S.rules.jsonMode = true;
  } else {
    let parsed;
    try {
      parsed = JSON.parse($("rule-json").value);
    } catch (e) {
      $("rule-issues").textContent = `JSON 解析失败：${e.message}`;
      return;
    }
    if (!Array.isArray(parsed)) {
      $("rule-issues").textContent = "JSON 必须是一个规则数组。";
      return;
    }
    S.rules.view = fromEngine(parsed);
    S.rules.dirty = true;
    S.rules.jsonMode = false;
  }
  renderRules();
}

async function saveRules() {
  const res = toEngine(S.rules.view);
  if (res.error) {
    $("rule-issues").textContent = res.error;
    return;
  }
  const r = await rpc("save_rules", { rules: res.rules });
  if (r.ok) {
    S.rules.dirty = false;
    $("rule-issues").textContent = "已保存。";
    await rpc("rules", {});
  } else {
    $("rule-issues").textContent = `保存失败：${r.error || "核侧校验未通过"}`;
  }
}

async function previewRule() {
  const res = toEngine(S.rules.view);
  if (res.error) {
    $("rule-issues").textContent = res.error;
    return;
  }
  const ranks = S.cfg.fields.find((f) => f.path === "rank_names")?.value;
  const sample = samplePlayer(ranks && typeof ranks === "object" ? Object.keys(ranks) : []);
  const r = await rpc("preview_rule", { rules: res.rules, sample });
  $("rule-preview-box").hidden = false;
  $("rule-preview-out").textContent = JSON.stringify(r.ok ? r.result : { error: r.error }, null, 2);
}

// ── 主题 / 标题栏 / 窗口 ───────────────────────────────────────────────
function setTheme(mode) {
  let resolved = mode;
  if (mode === "auto") {
    resolved =
      (window.matchMedia?.("(prefers-color-scheme: dark)").matches ?? true) ? "dark" : "light";
  }
  if (resolved !== "dark" && resolved !== "light") resolved = "dark";
  S.theme = resolved;
  document.documentElement.dataset.theme = resolved;
}

async function winCmd(name, args = {}) {
  if (!api?.core?.invoke) return;
  try {
    await api.core.invoke(name, args);
  } catch (e) {
    console.warn("窗口命令失败", name, e);
  }
}

/** 让「最大化/还原」按钮的图标与窗口真实状态一致（§3.1）。 */
async function syncMaxIcon() {
  if (!api?.core?.invoke) return;
  try {
    const max = await api.core.invoke("win_is_maximized");
    S.maximized = !!max;
    $("btn-max").innerHTML = S.maximized ? ICONS.restore : ICONS.maximize;
    $("btn-max").title = S.maximized ? "还原" : "最大化";
  } catch {
    // 静态预览或非 tauri 环境：保持默认图标即可
  }
}

async function toggleCompact() {
  S.compact = !S.compact;
  document.body.dataset.mode = S.compact ? "compact" : "normal";
  // 图标随模式切换方向（§3.2）：小窗时显示「还原成大窗」的图标
  $("btn-compact").innerHTML = S.compact ? ICONS.expand : ICONS.compact;
  $("btn-compact2").innerHTML = `${S.compact ? ICONS.expand : ICONS.compact}小窗`;
  await winCmd("win_set_compact", { on: S.compact });
  await rpc("window", { mode: S.compact ? "compact" : "normal", on_top: S.topmost });
}

async function toggleTop() {
  S.topmost = !S.topmost;
  // 置顶用实心图钉，未置顶用描边图钉（§4.4）
  const icon = S.topmost ? ICONS.pin : ICONS.pinOff;
  $("btn-top").innerHTML = icon;
  $("btn-top2").innerHTML = `${icon}置顶`;
  $("btn-top").classList.toggle("active", S.topmost);
  $("btn-top2").classList.toggle("active", S.topmost);
  await winCmd("win_set_top", { on: S.topmost });
  await rpc("window", { mode: S.compact ? "compact" : "normal", on_top: S.topmost });
}

async function syncTray() {
  const ts = trayState(S.state.state, S.fails.length);
  await winCmd("win_set_tray_state", {
    state: ts,
    fails: S.fails.length,
    tip: trayTip(ts, S.state.done, S.state.total),
  });
}

// ── 模态（§3.4） ───────────────────────────────────────────────────────
function openModal({ title, body, actions, input, hint }) {
  return new Promise((resolve) => {
    const layer = $("modal-layer");
    const modal = $("modal");
    modal.replaceChildren();
    const h = document.createElement("h3");
    h.textContent = title;
    const b = document.createElement("div");
    b.className = "modal-body";
    b.innerHTML = body;
    modal.append(h, b);

    let inputEl = null;
    if (input) {
      inputEl = document.createElement("input");
      inputEl.className = "ctl";
      inputEl.type = "text";
      inputEl.placeholder = input.placeholder || "";
      inputEl.style.width = "100%";
      b.append(inputEl);
      if (hint) b.append(textDiv(hint, "muted"));
    }

    const row = document.createElement("div");
    row.className = "modal-actions";
    const close = (value) => {
      layer.dataset.open = "false";
      resolve(value);
    };
    for (const a of actions) {
      const btn = document.createElement("button");
      btn.className = `ctl${a.primary ? " primary" : ""}${a.danger ? " danger" : ""}`;
      btn.textContent = a.label;
      btn.onclick = () => {
        if (a.requiresInput && inputEl && inputEl.value !== a.requiresInput) {
          inputEl.style.borderColor = "var(--danger)";
          return;
        }
        close(a.value);
      };
      row.append(btn);
    }
    modal.append(row);
    layer.dataset.open = "true";
    inputEl?.focus();
  });
}

async function openConfirmFromCore(p) {
  if (p.kind === "stage") {
    const choice = await openModal({
      title: "阶段确认",
      body: `即将进入「${esc(p.stage)}」：目标 ${esc(p.target)} 人，预计 ${esc(p.actions)} 个动作。`,
      actions: [
        { label: "继续", value: "resume", primary: true },
        { label: "暂停", value: "pause" },
        { label: "停止", value: "stop", danger: true },
      ],
    });
    await rpc("confirm_reply", { id: p.id, choice });
  } else if (p.kind === "kick") {
    const choice = await openModal({
      title: "踢出二次确认",
      body: `玩家 <b>${esc(p.player)}</b>　等级 ${esc(p.level)}　周贡献 ${esc(p.weekly_total)}`,
      input: { placeholder: `请输入确认词：${p.confirm_text}` },
      hint: "确认词来自配置 ui.kick_confirm_text；输入不符不会提交。",
      actions: [
        { label: "确认踢出", value: "kick", danger: true, requiresInput: p.confirm_text },
        { label: "取消", value: "cancel" },
      ],
    });
    await rpc("confirm_reply", { id: p.id, choice });
  }
}

async function startupCountdown() {
  const layer = $("modal-layer");
  const modal = $("modal");
  layer.dataset.open = "true";
  for (let n = 3; n > 0; n--) {
    modal.replaceChildren();
    const h = document.createElement("h3");
    h.textContent = "即将开始";
    const b = document.createElement("div");
    b.className = "modal-body";
    b.innerHTML = `<div class="countdown">${n}</div><p class="muted">请不要操作键盘和鼠标。</p>`;
    modal.append(h, b);
    await sleep(1000);
  }
  layer.dataset.open = "false";
}

function confirmStop() {
  return openModal({
    title: "停止运行",
    body: `还有 <b>${S.pending.length}</b> 个任务未完成验证，停止将丢弃。确定停止？`,
    actions: [
      { label: "停止", value: true, danger: true },
      { label: "取消", value: false, primary: true },
    ],
  });
}

async function askClosePolicy() {
  const layer = $("modal-layer");
  const modal = $("modal");
  modal.replaceChildren();
  const h = document.createElement("h3");
  h.textContent = "关闭时希望怎么做？";
  const b = document.createElement("div");
  b.className = "modal-body";
  const remember = document.createElement("input");
  remember.type = "checkbox";
  const label = document.createElement("label");
  label.append(
    remember,
    document.createTextNode(" 不再询问（可在配置页 ui.tray.ask_on_close 改回）"),
  );
  b.append(label);
  const row = document.createElement("div");
  row.className = "modal-actions";
  const go = async (action) => {
    layer.dataset.open = "false";
    await winCmd("win_close_decision", { action, remember: remember.checked });
    if (remember.checked) {
      // 「不再询问」必须落盘到配置，否则重启后还会再问（§5）
      await rpc("save_config", {
        values: {
          "ui.tray.close_action": action === "minimize" ? "minimize" : "exit",
          "ui.tray.ask_on_close": false,
        },
      });
    }
  };
  const min = document.createElement("button");
  min.className = "ctl primary";
  min.textContent = "最小化到托盘";
  min.onclick = () => go("minimize");
  const exit = document.createElement("button");
  exit.className = "ctl danger";
  exit.textContent = "直接退出";
  exit.onclick = () => go("exit");
  row.append(min, exit);
  modal.append(h, b, row);
  layer.dataset.open = "true";
}

// ── 引导 ───────────────────────────────────────────────────────────────
function setHandshake(payload) {
  let msg;
  try {
    msg = typeof payload === "string" ? JSON.parse(payload) : payload;
  } catch {
    $("sb-conn").textContent = "握手应答不是合法 JSON";
    return;
  }
  if (!msg || msg.ok === false) {
    $("sb-conn").textContent = `握手失败：${msg?.error || "未知原因"}`;
    return;
  }
  const r = msg.result || msg;
  S.hello = r;
  $("variant-label").textContent = r.variant ? `· ${r.variant}` : "";
  $("about-version").textContent = r.plugin_version || "—";
  $("about-variant").textContent = r.variant || "—";
  $("about-caps").textContent = (r.capabilities || []).join(", ") || "—";
  $("about-schema").textContent = r.config_schema_version ?? "—";
  $("sb-conn").textContent = "已连接核心";
  // 首次运行：配置是刚生成的 → 锁在配置页，保存后才放行（§3.5）
  S.firstRun = !!r.first_run;
  if (S.firstRun) lockTabsToConfig();
}

function handleTray(p) {
  const action = p?.action;
  if (action === "pause") void rpc("pause", {});
  else if (action === "resume") void rpc("resume", {});
  else if (action === "show") void winCmd("win_show", {});
  else if (action === "compact") void toggleCompact();
}

async function boot() {
  if (!api?.core?.invoke) {
    // 静态预览（无 Tauri）：照常渲染成可截图的状态，但不假装已连接
    $("sb-conn").textContent = "静态预览（无 Tauri API）";
    renderState();
    renderPlayers();
    renderPending();
    renderFails();
    renderReports();
    renderEvents();
    renderConfig();
    renderRules();
    return;
  }

  await api.event.listen("core-event", (e) => applyEnvelope(e.payload));
  await api.event.listen("ipc-hello", (e) => setHandshake(e.payload));
  await api.event.listen("ipc-error", (e) => {
    $("sb-conn").textContent = `连接异常：${e.payload}`;
  });
  await api.event.listen("ipc-closed", () => {
    $("sb-conn").textContent = "核心已断开";
    $("st-dot").className = "dot danger";
  });
  await api.event.listen("gui-close-request", () => void askClosePolicy());
  await api.event.listen("tray-command", (e) => handleTray(e.payload));

  // 前端可能先于核就绪：订阅后轮询握手快照，直到拿到应答
  for (let i = 0; i < 60; i++) {
    const snap = await api.core.invoke("hello_snapshot").catch(() => null);
    if (snap) {
      setHandshake(snap);
      break;
    }
    await sleep(150);
  }

  await rpc("config", {});
  await rpc("rules", {});
  await rpc("state", {});
  renderState();
}

// ── 页签与首次运行硬门禁（§3.5） ─────────────────────────────────────
function switchTab(name) {
  for (const t of document.querySelectorAll(".tab")) {
    if (t.disabled && t.dataset.tab !== name) return; // 门禁期间不许切走
    t.setAttribute("aria-selected", String(t.dataset.tab === name));
  }
  for (const p of document.querySelectorAll(".page"))
    p.dataset.active = String(p.dataset.page === name);
  document.body.dataset.page = name;
}

function lockTabsToConfig() {
  for (const t of document.querySelectorAll(".tab")) {
    if (t.dataset.tab !== "config") {
      t.disabled = true;
      t.title = "首次运行：请先保存配置";
    }
  }
  switchTab("config");
  $("sb-dirty").textContent = "首次运行：请先核对并保存配置";
}

function unlockTabs() {
  for (const t of document.querySelectorAll(".tab")) {
    t.disabled = false;
    t.title = "";
  }
  $("sb-dirty").textContent = "";
}

// ── 事件绑定 ───────────────────────────────────────────────────────────
function bind() {
  // 图标全部来自 icons.js 的内联 SVG（§4.4：禁 emoji 与字符字形）
  $("btn-compact").innerHTML = ICONS.compact;
  $("btn-top").innerHTML = ICONS.pinOff;
  $("btn-min").innerHTML = ICONS.minimize;
  $("btn-max").innerHTML = ICONS.maximize;
  $("btn-close").innerHTML = ICONS.close;
  $("btn-compact2").innerHTML = `${ICONS.compact}小窗`;
  $("btn-top2").innerHTML = `${ICONS.pinOff}置顶`;
  for (const tab of document.querySelectorAll(".tab")) {
    tab.onclick = () => switchTab(tab.dataset.tab);
  }
  $("sb-restart").onclick = () => rpc("restart_app", {});

  $("btn-min").onclick = () => winCmd("win_minimize");
  // 最大化/还原：图标随窗口真实状态切换（§3.1），窗口尺寸变化时再同步一次
  $("btn-max").onclick = async () => {
    await winCmd("win_toggle_max");
    await syncMaxIcon();
  };
  window.addEventListener("resize", () => void syncMaxIcon());
  $("btn-close").onclick = () => winCmd("win_close");
  $("drag-region").ondblclick = async () => {
    await winCmd("win_toggle_max");
    await syncMaxIcon();
  };
  $("drag-region").onmousedown = (e) => {
    if (e.button === 0) void winCmd("win_start_drag");
  };
  $("btn-compact").onclick = toggleCompact;
  $("btn-compact2").onclick = toggleCompact;
  $("btn-top").onclick = toggleTop;
  $("btn-top2").onclick = toggleTop;
  $("compact").ondblclick = toggleCompact;

  $("btn-start").onclick = async () => {
    await startupCountdown();
    await rpc("start", {});
  };
  $("btn-pause").onclick = () => rpc("pause", {});
  $("btn-resume").onclick = () => rpc("resume", {});
  $("btn-stop").onclick = async () => {
    if (await confirmStop()) await rpc("stop", {});
  };
  $("btn-open-reports").onclick = () => rpc("open_reports", {});
  $("btn-log-open").onclick = () => rpc("open_reports", {});
  $("btn-open-logs").onclick = () => rpc("open_logs", {});

  $("player-search").oninput = renderPlayers;
  $("player-decision").onchange = renderPlayers;
  $("player-online").onchange = renderPlayers;
  for (const th of document.querySelectorAll("#player-table th")) {
    th.onclick = () => {
      const key = th.dataset.key;
      S.sort = { key, dir: S.sort.key === key && S.sort.dir === "asc" ? "desc" : "asc" };
      for (const t of document.querySelectorAll("#player-table th")) t.removeAttribute("data-sort");
      th.dataset.sort = S.sort.dir;
      renderPlayers();
    };
  }

  $("cfg-search").oninput = renderConfig;
  $("cfg-save").onclick = saveConfig;
  $("cfg-revert").onclick = () => {
    pendingEdits.clear();
    S.cfg.dirty.clear();
    renderConfig();
  };

  $("rule-add").onclick = () => {
    S.rules.view.push(newRule(`rule-${Date.now()}`));
    touchRules();
  };
  $("rule-view-toggle").onclick = toggleRuleView;
  $("rule-preview").onclick = previewRule;
  $("rule-save").onclick = saveRules;

  $("log-level").onchange = () => {
    S.log.minLevel = Number($("log-level").value);
    $("sb-level").textContent = `日志级别：${LEVEL_LABEL[S.log.minLevel] || "信息"}`;
    renderEvents();
  };
  $("log-search").oninput = () => {
    S.log.filter = $("log-search").value;
    renderEvents();
  };
  $("btn-log-pause").onclick = () => {
    S.log.paused = !S.log.paused;
    $("btn-log-pause").textContent = S.log.paused ? "恢复滚动" : "暂停滚动";
  };
  $("btn-log-copy").onclick = async () => {
    const ev = S.log.selected;
    if (!ev) return;
    try {
      await navigator.clipboard.writeText(`${fmtTs(ev.ts)} [${ev.category || ""}] ${ev.text}`);
      $("btn-log-copy").textContent = "已复制";
      setTimeout(() => {
        $("btn-log-copy").textContent = "复制本条";
      }, 1200);
    } catch {
      $("btn-log-copy").textContent = "复制失败";
    }
  };

  $("btn-check-update").onclick = async () => {
    $("about-update").textContent = "正在检查…";
    const r = await rpc("check_update", {});
    $("about-update").textContent = r.ok
      ? `最新版本 ${r.result?.version ?? "未知"}（${r.result?.status ?? ""}）`
      : `检查失败：${r.error}`;
  };

  window.matchMedia?.("(prefers-color-scheme: dark)").addEventListener?.("change", () => {
    const mode = S.cfg.fields.find((f) => f.path === "ui.theme")?.value;
    if (mode === "auto") setTheme("auto");
  });
}

bind();
void boot();
