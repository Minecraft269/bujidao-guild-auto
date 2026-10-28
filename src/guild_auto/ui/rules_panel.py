# -*- coding: utf-8 -*-
"""
ui/rules_panel.py —— 自定义规则管理（阶段 4）
=============================================
只做 UI 胶水，判定全部委托 domain/rules.py（同一接口，不另写一套）。

能力（对应验收：UI 可完整管理规则 增删改查/启停/排序/预览）：
  * 列表：id / 名称 / 启用 / 优先级 / 动作摘要
  * 增：插入一条模板规则；删：移除选中
  * 启停：切换 enabled
  * 排序：上移/下移（改变列表顺序 = first_match 的优先顺序）
  * JSON 编辑：选中规则可编辑原文，应用前用 validate_rules 校验并指出字段
  * 实时预览：输入模拟成员数据，显示命中哪条规则、动作、条件明细
"""
import json
import tkinter as tk
from tkinter import messagebox, ttk

from guild_auto.domain.rules import evaluate_rules, validate_rules

TEMPLATE_RULE = {
    "id": "新规则",
    "name": "新规则",
    "enabled": True,
    "priority": 100,
    "stop": True,
    "conditions": [{"field": "weekly_total", "op": "<", "value": 1000}],
    "actions": [{"type": "demote"}],
}


class RulesPanel(tk.Frame):
    """规则列表 + 编辑器 + 预览。"""

    def __init__(self, master, cfg, on_change=None):
        super().__init__(master)
        self.cfg = cfg
        self.on_change = on_change
        self.rules = list(cfg.get("rules") or [])

        toolbar = tk.Frame(self)
        toolbar.pack(fill="x", padx=6, pady=4)
        for text, cmd in (
            ("新增", self.add_rule),
            ("删除", self.delete_rule),
            ("启用/禁用", self.toggle_enabled),
            ("上移", lambda: self.move(-1)),
            ("下移", lambda: self.move(1)),
        ):
            tk.Button(toolbar, text=text, command=cmd).pack(side="left", padx=3)

        cols = ("id", "name", "enabled", "priority", "actions")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", height=8)
        for c, w in (("id", 120), ("name", 140), ("enabled", 60),
                     ("priority", 70), ("actions", 160)):
            self.tree.heading(c, text=c)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=6)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.load_selected())

        tk.Label(self, text="规则 JSON（选中后可编辑，应用前会校验）", anchor="w").pack(
            fill="x", padx=6, pady=(6, 0))
        self.editor = tk.Text(self, height=8, wrap="none")
        self.editor.pack(fill="both", expand=True, padx=6)
        tk.Button(self, text="应用 JSON 修改", command=self.apply_json).pack(
            anchor="w", padx=6, pady=4)

        preview = tk.LabelFrame(self, text="实时预览（模拟一名玩家）")
        preview.pack(fill="x", padx=6, pady=6)
        row = tk.Frame(preview)
        row.pack(fill="x", padx=4, pady=2)
        tk.Label(row, text="玩家名").pack(side="left")
        self.var_player = tk.StringVar(value="示例玩家")
        tk.Entry(row, textvariable=self.var_player, width=14).pack(side="left", padx=4)
        tk.Label(row, text="等级").pack(side="left")
        self.var_rank = tk.StringVar(value="entry")
        ttk.Combobox(row, textvariable=self.var_rank, width=8,
                     values=("entry", "low", "mid", "high")).pack(side="left", padx=4)
        tk.Label(row, text="周贡献").pack(side="left")
        self.var_weekly = tk.StringVar(value="800")
        tk.Entry(row, textvariable=self.var_weekly, width=8).pack(side="left", padx=4)
        tk.Button(row, text="预览", command=self.preview).pack(side="left", padx=6)
        self.var_result = tk.StringVar(value="（未预览）")
        tk.Label(preview, textvariable=self.var_result, anchor="w", justify="left",
                 fg="#004080").pack(fill="x", padx=6, pady=(0, 4))

        self.refresh()

    # ---------------- 列表 ----------------
    def refresh(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        for i, rule in enumerate(self.rules):
            acts = ",".join(a.get("type", "?") for a in (rule.get("actions") or []))
            self.tree.insert("", "end", iid=str(i),
                             values=(rule.get("id", ""), rule.get("name", ""),
                                     "是" if rule.get("enabled", True) else "否",
                                     rule.get("priority", 0), acts))

    def _selected_index(self):
        sel = self.tree.selection()
        if not sel:
            return None
        return int(sel[0])

    def load_selected(self):
        i = self._selected_index()
        if i is None:
            return
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", json.dumps(self.rules[i], ensure_ascii=False, indent=2))

    def _commit(self):
        self.cfg["rules"] = list(self.rules)
        self.refresh()
        if self.on_change:
            self.on_change(self.cfg)

    # ---------------- 增删改 ----------------
    def add_rule(self):
        existing = {r.get("id") for r in self.rules}
        n = 1
        while f"rule{n}" in existing:
            n += 1
        rule = json.loads(json.dumps(TEMPLATE_RULE))
        rule["id"] = rule["name"] = f"rule{n}"
        self.rules.append(rule)
        self._commit()

    def delete_rule(self):
        i = self._selected_index()
        if i is None:
            return
        del self.rules[i]
        self._commit()

    def toggle_enabled(self):
        i = self._selected_index()
        if i is None:
            return
        self.rules[i]["enabled"] = not self.rules[i].get("enabled", True)
        self._commit()

    def move(self, delta):
        i = self._selected_index()
        if i is None:
            return
        j = i + delta
        if not (0 <= j < len(self.rules)):
            return
        self.rules[i], self.rules[j] = self.rules[j], self.rules[i]
        self._commit()
        self.tree.selection_set(str(j))

    def apply_json(self):
        i = self._selected_index()
        if i is None:
            messagebox.showinfo("未选中", "请先在列表中选择一条规则")
            return False
        try:
            rule = json.loads(self.editor.get("1.0", "end"))
        except json.JSONDecodeError as e:
            messagebox.showerror("JSON 语法错误", f"第 {e.lineno} 行: {e.msg}")
            return False
        errs = validate_rules([rule])
        if errs:
            messagebox.showerror(
                "规则校验失败",
                "\n".join(f"· {e['rule_id']} 的 {e['field']}: {e['message']}" for e in errs),
            )
            return False
        self.rules[i] = rule
        self._commit()
        return True

    # ---------------- 预览 ----------------
    def preview(self):
        try:
            weekly = int(self.var_weekly.get())
        except ValueError:
            self.var_result.set("周贡献必须是整数")
            return None
        member = {"player": self.var_player.get(), "rank": self.var_rank.get(),
                  "weekly_total": weekly, "joined_at": None,
                  "last_online": None, "online": None}
        actions, hits, traces = evaluate_rules(
            list(self.rules), member, self.cfg.get("rules_strategy", "first_match"))
        if hits:
            detail = next((t for t in traces if t["rule_id"] == hits[0]), {})
            conds = "; ".join(c["detail"] for c in detail.get("conditions") or [])
            self.var_result.set(f"命中 {hits[0]} → {actions}\n依据: {conds or '(无条件)'}")
        else:
            self.var_result.set("未命中任何规则（将回落到内置阈值规则）")
        return actions, hits
