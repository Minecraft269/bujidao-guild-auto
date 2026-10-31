# -*- coding: utf-8 -*-
"""
vision/navigator.py —— 按键试探与界面导航
==========================================
入口链:`hotbar 键 → 装扮与个人设定 → 公会物品 → 公会系统`。

纪律(`docs/UI识别模式详解.md` §八 / §七):
  * 首选键来自 options.txt 的运行时解析,候选键来自用户配置 —— **不内置任何默认键**;
  * 每次按键后必须截图 + 重新全扫确认,禁止假设按键生效;
  * 已进入设置界面但公会入口动作失败 → 立即报错,不在错误界面继续乱点;
  * 成功后缓存该键,本次会话优先复用。

press / scan_title / sleep / escape 全部注入,本模块零真实设备依赖。
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from guild_auto.infrastructure import logger
from guild_auto.vision.tooltip import TITLE_GUILD, TITLE_SETTINGS, classify_title

if TYPE_CHECKING:  # 仅用于类型注解
    from collections.abc import Callable

DEFAULT_RENDER_MS = 300
"""按键/点击后等待渲染的默认毫秒数(规格要求 ≥300ms)。可由调用方覆盖。"""


class NavigationError(RuntimeError):
    """导航失败。抛出即表示"停在这里,不盲点",由上层安全回退。"""


class Navigator:
    """按键试探 + 界面判定。一个实例只服务一次会话(缓存成功键)。"""

    def __init__(
        self,
        *,
        first_key: str | None,
        candidates: tuple[str, ...] | list[str] = (),
        press: Callable[[str], None],
        scan_title: Callable[[], str],
        sleep: Callable[[float], None] = time.sleep,
        escape: Callable[[], None] | None = None,
        render_ms: int = DEFAULT_RENDER_MS,
        max_misclick: int = 3,
    ):
        if render_ms < 0:
            raise ValueError(f"渲染等待不能为负: {render_ms}")
        if max_misclick < 1:
            raise ValueError(f"误按阈值必须 ≥1,当前: {max_misclick}")
        ordered: list[str] = []
        for key in (first_key, *tuple(candidates)):
            text = str(key or "").strip()
            if text and text not in ordered:
                ordered.append(text)
        self._keys = tuple(ordered)
        self._press = press
        self._scan_title = scan_title
        self._sleep = sleep
        self._escape = escape
        self._render_ms = render_ms
        self._max_misclick = max_misclick
        # 成功后缓存:本次会话优先复用(options.txt 解析结果可能因改键失效)
        self.cached_key: str | None = None

    @property
    def candidate_keys(self) -> tuple[str, ...]:
        """实际尝试顺序:缓存键优先,其次 options.txt 首键,最后配置候选。"""
        if self.cached_key and self.cached_key in self._keys:
            return (self.cached_key,) + tuple(k for k in self._keys if k != self.cached_key)
        return self._keys

    def current_screen(self) -> str:
        """重新全扫当前界面标题并分类 —— 禁止凭记忆断言界面。"""
        screen = classify_title(self._scan_title())
        logger.log_debug(f"vision.navigator: 标题分类结果 screen={screen}")
        return screen

    def _recover(self, screen_before: str, misses: int) -> None:
        """连续误按后按 Esc 回到干净状态,并重新全扫(不带着错误界面继续)。"""
        if self._escape is None:
            logger.log_warning(
                f"vision.navigator: 连续误按需 Esc 恢复,但未注入 escape"
                f"(恢复前 screen={screen_before},误按 {misses} 次),保持现状"
            )
            return
        logger.log_warning(
            f"vision.navigator: 连续误按触发 Esc 恢复"
            f"(恢复前 screen={screen_before},误按次数={misses},阈值={self._max_misclick})"
        )
        self._escape()
        self._sleep(self._render_ms / 1000.0)
        screen_after = self.current_screen()
        logger.log_info(f"vision.navigator: Esc 恢复完成(screen={screen_after})")

    def enter_guild_ui(self, open_guild: Callable[[], None] | None = None) -> str:
        """按键试探进入公会系统界面,返回成功的按键;失败抛 NavigationError。

        open_guild 是"在装扮与个人设定界面里点击公会物品"的动作(由上层注入;
        它自身负责先悬停读 tooltip 再点击)。本方法只做导航与确认。
        """
        keys = self.candidate_keys
        logger.log_info(
            f"vision.navigator: 导航开始(候选键顺序={keys},"
            f"max_misclick={self._max_misclick},render_ms={self._render_ms},"
            f"cached_key={self.cached_key!r})"
        )
        if not keys:
            logger.log_warning("vision.navigator: 未解析到 hotbar 绑定且未配置候选键,导航无法开始")
            raise NavigationError(
                "未解析到 hotbar 绑定且未配置候选键:不提供任何默认键值,请检查 "
                "game_options 指向游戏实际写入的 options.txt,或在配置中补充候选键"
            )

        misses = 0
        screen = ""
        for key in keys:
            used_cache = key == self.cached_key
            logger.log_debug(
                f"vision.navigator: 按键试探 key={key!r} cached={used_cache} misses={misses}"
            )
            self._press(key)
            self._sleep(self._render_ms / 1000.0)
            screen = self.current_screen()  # 每次按键后截图确认
            logger.log_debug(
                f"vision.navigator: 试探结果 key={key!r} screen={screen} "
                f"cached={used_cache} misses={misses}"
            )
            if screen not in (TITLE_SETTINGS, TITLE_GUILD):
                misses += 1
                if misses >= self._max_misclick:
                    self._recover(screen, misses)
                    misses = 0
                continue

            if screen == TITLE_GUILD:
                self.cached_key = key
                logger.log_info(f"vision.navigator: 导航成功(key={key!r},命中缓存={used_cache})")
                return key

            # 已到设置界面:点击公会物品后再全扫确认
            if open_guild is None:
                logger.log_warning(
                    f"vision.navigator: 已进入设置界面但未提供公会入口动作"
                    f"(key={key!r},screen={screen})"
                )
                raise NavigationError("已进入装扮与个人设定,但未提供公会入口动作,无法继续(不盲点)")
            logger.log_debug("vision.navigator: 已到设置界面,执行公会入口动作")
            open_guild()
            screen = self.current_screen()
            if screen != TITLE_GUILD:
                logger.log_warning(
                    f"vision.navigator: 点击公会物品后界面不是公会系统(实际 {screen}),停止导航"
                )
                raise NavigationError(
                    f"点击公会物品后界面不是公会系统(实际: {screen}),停止导航(不盲点)"
                )
            self.cached_key = key
            logger.log_info(
                f"vision.navigator: 导航成功(经设置界面,key={key!r},命中缓存={used_cache})"
            )
            return key

        logger.log_warning(
            f"vision.navigator: 全部候选键失败(候选键={keys},最后 screen={screen!r})"
        )
        raise NavigationError(f"按键试探失败:候选键 {keys} 均未进入装扮与个人设定界面")
