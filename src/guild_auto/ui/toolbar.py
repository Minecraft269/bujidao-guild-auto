# -*- coding: utf-8 -*-
"""
ui/toolbar.py —— 窗口控制按钮（小窗/大窗切换、置顶）
====================================================
只做"按钮外观 + 回调"，不持有窗口状态：状态由 app.py 掌握，
按钮通过 on_toggle_* 回调触发，再由 app 回写图标与高亮。

图标用 Unicode 几何符号而不是图片：
  * 箭头：◱（大窗时显示，点了变小窗）/ ◰（小窗时显示，点了变大窗）
  * 图钉：📍（未置顶）/ 📌（已置顶，高亮）
选 Unicode 的原因：零资源文件、不依赖材质包/DPI，打包体积不变。
"""

import tkinter as tk

ARROW_FROM_BIG = "◱"
ARROW_FROM_SMALL = "◰"
PIN_OFF = "📍"
PIN_ON = "📌"


class Toolbar(tk.Frame):
    """顶部工具栏：左侧标题，右侧两个切换按钮。"""

    def __init__(self, master, on_toggle_mode, on_toggle_top, title="布吉岛公会管理脚本"):
        super().__init__(master)
        self._on_toggle_mode = on_toggle_mode
        self._on_toggle_top = on_toggle_top

        self.title_label = tk.Label(self, text=title, anchor="w")
        self.title_label.pack(side="left", padx=6, pady=2)

        self.pin_btn = tk.Button(self, text=PIN_OFF, width=3, command=self._toggle_top)
        self.pin_btn.pack(side="right", padx=2, pady=2)

        self.mode_btn = tk.Button(self, text=ARROW_FROM_BIG, width=3, command=self._toggle_mode)
        self.mode_btn.pack(side="right", padx=2, pady=2)

    def _toggle_mode(self):
        if self._on_toggle_mode:
            self._on_toggle_mode()

    def _toggle_top(self):
        if self._on_toggle_top:
            self._on_toggle_top()

    def set_compact(self, compact):
        """小窗时箭头指向大窗，大窗时指向小窗。"""
        self.mode_btn.configure(text=ARROW_FROM_SMALL if compact else ARROW_FROM_BIG)

    def set_on_top(self, on_top):
        """置顶时图钉高亮。"""
        self.pin_btn.configure(
            text=PIN_ON if on_top else PIN_OFF,
            relief="sunken" if on_top else "raised",
        )
