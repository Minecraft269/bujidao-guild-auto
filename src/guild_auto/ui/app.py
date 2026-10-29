# -*- coding: utf-8 -*-
"""
ui/app.py —— 图形配置界面主窗口
================================
职责：
  * 组装 工具栏 + 配置面板 + 底部操作区
  * 窗口化 600x400 ↔ 小窗 200x150 切换（小窗只显示状态与进度）
  * 始终置顶（Windows 用 win32gui.SetWindowPos HWND_TOPMOST；失败降级 -topmost）
  * 保存配置 / 开始运行 / 热重载（仅 UI 模式）
  * 首次启动（配置刚生成）时，未做任何修改不允许切页

不做流程编排：点"开始运行"后交给 main.main()（与命令行同一条路径）。
"""

import tkinter as tk
from tkinter import messagebox

from guild_auto.config import load_config, save_config
from guild_auto.config.ui_binding import diff_against_default
from guild_auto.infrastructure import logger
from guild_auto.ui.config_panel import ConfigPanel
from guild_auto.ui.confirm_dialog import ask_confirm_text
from guild_auto.ui.toolbar import Toolbar

BIG_SIZE = (600, 400)
SMALL_SIZE = (200, 150)


def set_always_on_top(win, on_top):
    """切换始终置顶。Windows 走 win32gui；其余平台退回 Tk 的 -topmost。"""
    try:
        import win32con
        import win32gui

        win.update_idletasks()
        hwnd = int(win.winfo_id())
        top = win32gui.GetParent(hwnd) or hwnd
        flag = win32con.HWND_TOPMOST if on_top else win32con.HWND_NOTOPMOST
        win32gui.SetWindowPos(
            top,
            flag,
            0,
            0,
            0,
            0,
            win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_NOACTIVATE,
        )
        return
    except Exception as e:
        # 置顶是增强项:win32 调用失败就退回 Tk 自带 -topmost(下面那段)。
        # 不得静默 pass —— US-004 要求 catch 分支可检索。
        logger.log_debug(f"win32 SetWindowPos 置顶失败,回退 Tk -topmost: {e!r}")
    try:
        win.attributes("-topmost", bool(on_top))
    except Exception as e:
        logger.log_warning(f"Tk -topmost 也失败,置顶功能不可用: {e!r}")


class App(tk.Tk):
    """主窗口。"""

    def __init__(self, config_path, first_run=False, on_run=None):
        super().__init__()
        self.config_path = config_path
        self.first_run = bool(first_run)
        self.on_run = on_run
        # 先加载配置，再据此决定初始窗口形态（start_mode / always_on_top）
        self.cfg = load_config(config_path)
        ui_cfg = self.cfg.get("ui", {})
        self.compact = str(ui_cfg.get("start_mode", "normal")) == "compact"
        self.always_on_top = bool(ui_cfg.get("always_on_top", False))
        self._dirty = False
        self.run_requested = False

        self.title("布吉岛公会管理脚本")
        self.geometry("%dx%d" % (SMALL_SIZE if self.compact else BIG_SIZE))

        self.toolbar = Toolbar(self, self.toggle_mode, self.toggle_on_top)
        self.toolbar.pack(fill="x")

        self.status = tk.Label(self, text="就绪", anchor="w", fg="#006600")
        self.status.pack(fill="x", padx=8)

        self.body = tk.Frame(self)
        self.body.pack(fill="both", expand=True)
        self.panel = ConfigPanel(self.body, self.cfg, self._on_saved)
        self.panel.pack(fill="both", expand=True)

        bottom = tk.Frame(self)
        bottom.pack(fill="x", pady=4)
        tk.Button(bottom, text="保存配置", command=self.save).pack(side="left", padx=6)
        tk.Button(bottom, text="重新加载（热重载）", command=self.hot_reload).pack(
            side="left", padx=6
        )
        tk.Button(bottom, text="规则管理", command=self.open_rules).pack(side="left", padx=6)
        tk.Button(bottom, text="开始运行", command=self.start_run).pack(side="right", padx=6)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._apply_mode()
        if self.always_on_top:
            set_always_on_top(self, True)

    # ---------------- 窗口形态 ----------------
    def toggle_mode(self):
        """在大窗与小窗之间切换；首次运行未修改配置时禁止切页。"""
        if self.first_run and not self._dirty:
            messagebox.showwarning(
                "请先修改配置",
                "首次启动检测到配置刚生成：请至少修改一项并保存后再切换界面。",
            )
            return
        self.compact = not self.compact
        self._apply_mode()

    def _apply_mode(self):
        self.geometry("%dx%d" % (SMALL_SIZE if self.compact else BIG_SIZE))
        self.toolbar.set_compact(self.compact)
        if self.compact:
            self.body.pack_forget()
            self.status.configure(text="小窗模式：只显示状态")
        else:
            self.body.pack(fill="both", expand=True)
        self.toolbar.set_on_top(self.always_on_top)

    def toggle_on_top(self):
        self.always_on_top = not self.always_on_top
        set_always_on_top(self, self.always_on_top)
        self.toolbar.set_on_top(self.always_on_top)
        self.status.configure(text="已置顶" if self.always_on_top else "取消置顶")

    # ---------------- 配置 ----------------
    def _on_saved(self, new_cfg):
        self.cfg = new_cfg
        self._dirty = True
        self.status.configure(text="配置已保存")

    def save(self):
        if not self.panel.save():
            self.status.configure(text="配置校验失败，请修正标红项")
            return False
        try:
            save_config(self.config_path, self.cfg)
        except OSError as e:
            messagebox.showerror("保存失败", str(e))
            return False
        changed = diff_against_default(self.cfg)
        self.status.configure(
            text=(
                "配置已保存，请重新启动脚本"
                if self.first_run
                else f"配置已保存（{len(changed)} 项与默认不同）"
            )
        )
        return True

    def hot_reload(self):
        """热重载仅在 UI 模式可用：从磁盘重新读取并重建表单。"""
        try:
            self.cfg = load_config(self.config_path)
        except Exception as e:
            messagebox.showerror("热重载失败", str(e))
            return False
        self.panel.cfg = self.cfg
        self.panel._build()
        self.status.configure(text="已热重载配置")
        return True

    def open_rules(self):
        """打开规则管理窗口（阶段 4）。修改即写回 cfg，保存时一并落盘。"""
        from guild_auto.ui.rules_panel import RulesPanel

        win = tk.Toplevel(self)
        win.title("规则管理 — 布吉岛公会管理脚本")
        win.transient(self)
        panel = RulesPanel(win, self.cfg, on_change=self._on_rules_changed)
        panel.pack(fill="both", expand=True)
        tk.Button(win, text="关闭", command=win.destroy).pack(pady=6)
        return panel

    def _on_rules_changed(self, cfg):
        self.cfg = cfg
        self._dirty = True
        self.status.configure(text="规则已修改（记得点『保存配置』落盘）")

    # ---------------- 运行 ----------------
    def start_run(self):
        """保存配置并请求进入主流程。

        UI 不自己跑流程（避免在 Tk 事件循环里再起线程）：
        记录 run_requested 并关窗，run_ui 据此返回 True，main() 继续控制台主流程。
        """
        if not self.save():
            return False
        self.run_requested = True
        if self.on_run:
            self.on_run(self.cfg)
        self.destroy()
        return True

    def confirm_kick(self, player):
        """踢出二次确认：方式由配置决定（dialog 弹窗 / inline 主界面）。"""
        ui_cfg = self.cfg["ui"]  # 缺键直接抛 KeyError（禁止硬编码兜底）
        mode = str(ui_cfg["kick_confirm_mode"])
        expected = ui_cfg["kick_confirm_text"]
        message = f"即将踢出玩家 {player}（不可逆）"
        if mode == "inline":
            return messagebox.askyesno("确认踢出", message)
        return ask_confirm_text(self, "确认踢出", message, expected)

    def _on_close(self):
        self.destroy()


def run_ui(config_path, first_run=False, on_run=None):
    """UI 入口。返回 True 表示用户点了「开始运行」，main() 应继续主流程。"""
    app = App(config_path, first_run=first_run, on_run=on_run)
    app.mainloop()
    return bool(getattr(app, "run_requested", False))
