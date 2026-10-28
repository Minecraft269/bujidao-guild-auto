# -*- coding: utf-8 -*-
"""
ui/config_panel.py —— 配置面板（全配置项可编辑）
================================================
字段来自 config/ui_binding.flatten_config()，因此**新增配置键自动出现在 UI**，
不需要改这个文件（零硬编码的延伸）。

每个字段：标签（CONFIG_COMMENTS 首句）+ 输入框 + 说明（完整注释，灰色小字）
校验：保存时收集 {path: 文本} → apply_edits → 有 error 的字段标红；
      零 error 才真正落盘。
"""
import tkinter as tk
from tkinter import messagebox

from guild_auto.config.ui_binding import apply_edits, flatten_config, render_field_value


class ConfigPanel(tk.Frame):
    """可滚动的配置表单。"""

    def __init__(self, master, cfg, on_save):
        super().__init__(master)
        self.cfg = cfg
        self.on_save = on_save
        self.vars = {}
        self.widgets = {}

        canvas = tk.Canvas(self, borderwidth=0, highlightthickness=0)
        scroll = tk.Scrollbar(self, orient="vertical", command=canvas.yview)
        self.inner = tk.Frame(canvas)
        self.inner.bind(
            "<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.create_window((0, 0), window=self.inner, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self._build()

    def _build(self):
        for child in self.inner.winfo_children():
            child.destroy()
        self.vars.clear()
        self.widgets.clear()

        section = None
        for f in flatten_config(self.cfg):
            sec = f.path.rsplit(".", 1)[0] if "." in f.path else ""
            if sec != section:
                section = sec
                if sec:
                    tk.Label(self.inner, text=f"── {sec} ──", anchor="w",
                             font=("", 9, "bold")).pack(fill="x", padx=6, pady=(10, 2))

            row = tk.Frame(self.inner)
            row.pack(fill="x", padx=8, pady=1)
            tk.Label(row, text=f.label, width=18, anchor="w").pack(side="left")

            var = tk.StringVar(value=render_field_value(f))
            entry = tk.Entry(row, textvariable=var, width=48)
            entry.pack(side="left", fill="x", expand=True)
            self.vars[f.path] = var
            self.widgets[f.path] = entry

            if f.comment:
                tk.Label(self.inner, text=f.comment, anchor="w", justify="left",
                         fg="#666666", font=("", 8), wraplength=560).pack(
                    fill="x", padx=(30, 8))

    def collect(self):
        """收集当前表单值（未修改的项也一并带上，apply_edits 幂等）。"""
        return {path: var.get() for path, var in self.vars.items()}

    def mark_errors(self, errors):
        """把有错误的字段标红；无错误的恢复默认背景。"""
        for entry in self.widgets.values():
            entry.configure(background="white")
        for err in errors:
            path = err.get("path") or ""
            for p, entry in self.widgets.items():
                if path and (p == path or p.startswith(path + ".")):
                    entry.configure(background="#ffd6d6")

    def save(self):
        """应用编辑：成功返回 True 并交给 on_save；失败弹窗并返回 False。"""
        edits = self.collect()
        new_cfg, errors = apply_edits(self.cfg, edits)
        self.mark_errors(errors)
        if errors:
            msg = "\n".join(f"· {e['message']}" for e in errors[:12])
            messagebox.showerror("配置校验失败", msg)
            return False
        self.cfg = new_cfg
        if self.on_save:
            self.on_save(new_cfg)
        return True
