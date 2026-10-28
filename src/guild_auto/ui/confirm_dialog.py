# -*- coding: utf-8 -*-
"""
ui/confirm_dialog.py —— 破坏性操作二次确认弹窗
===============================================
踢出不可逆，必须输入确认词才执行（确认词来自配置 ui.kick_confirm_text）。
判定逻辑保持纯函数，便于单测：check_confirm_text(typed, expected) -> bool

方式可配置：ui.kick_confirm_mode = "dialog"（本弹窗）或 "inline"（主界面确认）。
"""
import tkinter as tk

DEFAULT_CONFIRM_TEXT = "kick-confirm"


def check_confirm_text(typed, expected):
    """输入的确认词是否逐字匹配（去首尾空白，区分大小写）。

    不做模糊匹配：踢出不可逆，宁可让用户重输，也不能因为"差不多"就执行。
    """
    if expected is None:
        expected = DEFAULT_CONFIRM_TEXT
    exp = str(expected).strip()
    return exp != "" and str(typed).strip() == exp


def ask_confirm_text(parent, title, message, expected):
    """模态弹窗：用户输入确认词，返回是否匹配。取消/关闭一律 False。"""
    result = {"ok": False}
    win = tk.Toplevel(parent)
    win.title(title)
    win.transient(parent)
    win.grab_set()

    tk.Label(win, text=message, wraplength=420, justify="left").pack(padx=12, pady=(12, 6))
    tk.Label(win, text=f"请输入确认词：{expected}", fg="#b00020").pack(padx=12, pady=(0, 6))
    entry = tk.Entry(win, width=32)
    entry.pack(padx=12, pady=6)
    entry.focus_set()

    def _ok():
        result["ok"] = check_confirm_text(entry.get(), expected)
        win.destroy()

    def _cancel():
        result["ok"] = False
        win.destroy()

    btns = tk.Frame(win)
    btns.pack(pady=(0, 12))
    tk.Button(btns, text="确认执行", command=_ok).pack(side="left", padx=6)
    tk.Button(btns, text="取消", command=_cancel).pack(side="left", padx=6)
    win.protocol("WM_DELETE_WINDOW", _cancel)
    win.wait_window()
    return result["ok"]
