# -*- coding: utf-8 -*-
"""
vision/adapters.py —— 真实外部能力适配器(截图 / OCR / 键鼠输入)
================================================================
vision 层本身只依赖注入进来的可调用对象(见 runner/sweep/navigator),本模块
负责把"真实环境"组装成那组可调用对象:

  capture  截图      → pyautogui(PIL 图像)
  ocr      文字识别  → 外部 OCR 命令(优先,显式配置)或 pytesseract
  输入     press/hover/click/escape → pyautogui,并在动作前打开"模拟输入"忽略窗口

**没有 OCR 就明确报错**,绝不允许静默降级回指令模式:用户以为在跑 UI 识别、
实际却按指令模式发聊天命令,是最危险的静默失效。

测试不碰真实屏幕:所有用例注入假适配器;本模块只测"配置齐全时能组装、
配置缺失时抛明确错误"。
"""

from __future__ import annotations

import os
import shlex
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Callable

from guild_auto.infrastructure import logger

EXTERNAL_OCR_TIMEOUT_S = 10.0
"""外部 OCR 命令超时(秒):卡死的识别进程不能拖住整个扫掠。"""

MOUSE_BINDING_PREFIX = "key.mouse."
"""options.txt 的鼠标绑定前缀。鼠标绑定需要点击坐标,当前构建不猜坐标,直接报错。"""


class AdapterError(RuntimeError):
    """外部能力不可用(截图 / OCR / 输入)。继承 RuntimeError 以便 runner 捕获。"""


@dataclass(frozen=True)
class VisionAdapters:
    """一组可注入的真实能力。字段全部是可调用对象,测试可整体替换。"""

    press: Callable[[str], None]
    hover: Callable[[tuple[int, int]], None]
    click: Callable[[tuple[int, int]], None]
    escape: Callable[[], None]
    capture: Callable[[], Any]
    ocr: Callable[[Any], tuple[str, float]]
    sleep: Callable[[float], None]
    screenshot_source: str
    ocr_source: str


def _find_pytesseract():
    """尝试导入 pytesseract;未安装返回 None(不吞其它错误)。"""
    try:
        import pytesseract
    except ImportError as e:
        logger.log_debug(f"vision.adapters: pytesseract 不可用,返回 None(ImportError: {e!r})")
        return None
    return pytesseract


def has_configured_ocr(cfg) -> bool:
    """是否配置了可用的 OCR(外部命令优先,其次 pytesseract)。

    启动阶段用它做快速门禁:UI 模式没有 OCR 就不该开始跑 —— 等到第一次
    截图才发现,用户已经以为脚本在工作了。
    """
    vision_cfg = cfg.get("vision") if isinstance(cfg.get("vision"), dict) else {}
    if str(vision_cfg.get("ocr_command") or "").strip():
        logger.log_info("vision.adapters: OCR 后端选型 external(已配置 ocr_command)")
        return True
    if _find_pytesseract() is not None:
        logger.log_info("vision.adapters: OCR 后端选型 pytesseract")
        return True
    logger.log_warning(
        "vision.adapters: 无可用 OCR 后端(未配置 ocr_command 且 pytesseract 不可用),"
        "UI 模式不会降级为指令模式"
    )
    return False


def _pyautogui():
    """延迟导入 pyautogui:导入失败(无显示环境)转成明确的 AdapterError。"""
    try:
        import pyautogui as module
    except ImportError as e:
        raise AdapterError("未配置输入/截图适配器:pyautogui 不可用") from e
    return module


def _build_capture():
    """截图适配器。当前使用 pyautogui(PIL 图像,可直接交给 pytesseract)。"""
    _pyautogui()  # 提前失败:不要等到第一次扫掠才发现截图不可用

    def _capture():
        logger.log_debug("vision.adapters: 截图开始(screenshot_source=pyautogui)")
        started = time.monotonic()
        try:
            image = _pyautogui().screenshot()
        except AdapterError as e:
            logger.log_warning(f"vision.adapters: 截图不可用(screenshot_source=pyautogui): {e}")
            raise
        except Exception as e:  # 无显示/权限不足/窗口被遮挡时 pyautogui 抛各种异常
            logger.log_warning(f"vision.adapters: 截图失败(screenshot_source=pyautogui): {e!r}")
            raise AdapterError(f"截图失败:{e}") from e
        logger.log_debug(
            f"vision.adapters: 截图返回(耗时 {(time.monotonic() - started) * 1000:.1f}ms,"
            f"尺寸={getattr(image, 'size', None)})"
        )
        return image

    return _capture, "pyautogui"


def _pytesseract_ocr(pytesseract):
    """pytesseract 适配器:按行拼接文字,置信度取非空词的平均值(0~1)。"""

    def _ocr(image):
        try:
            data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
        except (RuntimeError, OSError) as e:
            # 装了 pytesseract 但没装 tesseract 引擎时走这里:必须明确报错
            raise AdapterError(f"pytesseract 调用失败(需安装 tesseract 引擎):{e}") from e

        lines: dict[tuple[int, int, int], list[str]] = {}
        confidences: list[float] = []
        skipped_conf = 0
        for index, raw_word in enumerate(data.get("text") or ()):
            word = str(raw_word or "").strip()
            if not word:
                continue
            key = (
                data["block_num"][index],
                data["par_num"][index],
                data["line_num"][index],
            )
            lines.setdefault(key, []).append(word)
            try:
                value = float(data["conf"][index])
            except (TypeError, ValueError) as e:
                skipped_conf += 1
                logger.log_debug(
                    f"vision.adapters: pytesseract conf 字段解析失败(index={index},"
                    f"原始值={data['conf'][index]!r}),已跳过该词:{e!r}"
                )
                continue
            if value >= 0:
                confidences.append(min(max(value / 100.0, 0.0), 1.0))

        if skipped_conf:
            logger.log_warning(
                f"vision.adapters: pytesseract 有 {skipped_conf} 个词的 conf 字段无法解析,"
                "置信度计算已跳过这些词"
            )

        text = "\n".join(" ".join(words) for _key, words in sorted(lines.items()))
        confidence = sum(confidences) / len(confidences) if confidences else 0.0
        return text, confidence

    return _ocr


def _external_ocr(command: str):
    """外部 OCR 命令适配器:命令 stdout 视为识别文本。

    命令模板用 {image} 代表截图临时文件路径;没写占位符时把路径追加到末尾。
    外部工具不回报置信度,统一记 1.0(表示"无低置信度信息",不触发无意义的二次扫掠)。
    """

    parts = shlex.split(command)
    has_placeholder = any("{image}" in part for part in parts)

    def _ocr(image):
        handle, path = tempfile.mkstemp(prefix="guild-auto-ocr-", suffix=".png")
        os.close(handle)
        try:
            image.save(path, "PNG")
            argv = [part.replace("{image}", path) for part in parts]
            if not has_placeholder:
                argv.append(path)
            logger.log_info(
                f"vision.adapters: 外部 OCR 调用开始(backend=external,"
                f"timeout={EXTERNAL_OCR_TIMEOUT_S}s,图像来自临时文件,命令={argv[0]!r})"
            )
            started = time.monotonic()
            try:
                proc = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=EXTERNAL_OCR_TIMEOUT_S,
                )
            except subprocess.TimeoutExpired as e:
                logger.log_warning(
                    f"vision.adapters: 外部 OCR 超时(已等待 {EXTERNAL_OCR_TIMEOUT_S}s,"
                    f"命令={argv[0]!r}):{e!r}"
                )
                raise AdapterError(f"外部 OCR 命令超时(>{EXTERNAL_OCR_TIMEOUT_S}s):{e}") from e
            except (OSError, subprocess.SubprocessError) as e:
                logger.log_warning(
                    f"vision.adapters: 外部 OCR 命令执行失败(命令={argv[0]!r}):{e!r}"
                )
                raise AdapterError(f"外部 OCR 命令执行失败:{e}") from e
            logger.log_info(
                f"vision.adapters: 外部 OCR 返回(耗时 {(time.monotonic() - started) * 1000:.1f}ms,"
                f"returncode={proc.returncode},stdout={len(proc.stdout or '')} 字)"
            )
            if proc.returncode != 0:
                stderr_tail = (proc.stderr or "").strip()[:200]
                logger.log_error(
                    f"vision.adapters: 外部 OCR 命令退出码 {proc.returncode},"
                    f"stderr 前 200 字:{stderr_tail!r}"
                )
                raise AdapterError(f"外部 OCR 命令退出码 {proc.returncode}:{stderr_tail}")
            return proc.stdout, 1.0
        finally:
            try:
                os.unlink(path)
            except OSError as e:
                # 临时文件清理失败不影响识别结果,但必须留痕
                logger.log_debug(f"vision.adapters: 清理 OCR 临时文件失败: {e!r}")

    return _ocr


def _build_ocr(vision_cfg):
    """组装 OCR 适配器:显式配置的外部命令优先,其次 pytesseract;都没有则报错。"""
    command = str(vision_cfg.get("ocr_command") or "").strip()
    if command:
        logger.log_info("vision.adapters: OCR 适配器组装完成(backend=external,ocr_command=已配置)")
        return _external_ocr(command), "external"
    pytesseract = _find_pytesseract()
    if pytesseract is not None:
        logger.log_info("vision.adapters: OCR 适配器组装完成(backend=pytesseract)")
        return _pytesseract_ocr(pytesseract), "pytesseract"
    logger.log_error(
        "vision.adapters: 无可用 OCR 能力(未配置 ocr_command 且 pytesseract 不可用);"
        "UI 模式不会降级为指令模式"
    )
    raise AdapterError(
        "未配置 OCR 适配器:请安装 pytesseract(需同时安装 tesseract 引擎),"
        "或在 vision.ocr_command 配置外部 OCR 命令。UI 模式不会降级为指令模式"
    )


def _window_input():
    """延迟导入输入原语模块:vision → infrastructure 是允许方向,但延迟导入
    可避免"只做 OCR 相关单测"时被 Windows ctypes 拖累。"""
    try:
        from guild_auto.infrastructure import window_input as module
    except ImportError as e:
        raise AdapterError("未配置输入适配器:infrastructure.window_input 不可用") from e
    return module


def _build_input(on_simulated_input):
    """键鼠输入适配器。每个动作前打开忽略窗口,避免脚本自己的输入触发自动暂停。"""

    def _mark():
        if on_simulated_input is not None:
            on_simulated_input()

    def _press(key):
        text = str(key)
        if text.startswith(MOUSE_BINDING_PREFIX):
            # 不猜"该点哪里":鼠标绑定必须先知道 hotbar 槽位坐标,当前构建没有该坐标
            logger.log_warning(
                f"vision.adapters: 拒绝鼠标绑定(绑定原值={text!r},原因=无 hotbar 槽位坐标)"
            )
            raise AdapterError(
                f"hotbar 绑定 {text!r} 是鼠标按键,需要点击坐标才能使用;"
                "当前构建不支持,请在 options.txt 改用键盘绑定"
            )
        logger.log_debug(
            f"vision.adapters: 动作 action=press key={text!r} "
            f"simulated_input={on_simulated_input is not None}"
        )
        _mark()
        _window_input().press_key(text)

    def _hover(point):
        logger.log_debug(
            f"vision.adapters: 动作 action=hover point={point} "
            f"simulated_input={on_simulated_input is not None}"
        )
        _mark()
        _window_input().move_mouse(point[0], point[1])

    def _click(point):
        logger.log_debug(
            f"vision.adapters: 动作 action=click point={point} "
            f"simulated_input={on_simulated_input is not None}"
        )
        _mark()
        _window_input().click_mouse(point[0], point[1])

    def _escape():
        logger.log_debug(
            f"vision.adapters: 动作 action=escape key='esc' "
            f"simulated_input={on_simulated_input is not None}"
        )
        _mark()
        _window_input().press_key("esc")

    return _press, _hover, _click, _escape


def build_default_adapters(cfg, on_simulated_input=None) -> VisionAdapters:
    """按配置组装真实适配器。

    on_simulated_input 由调用方注入(通常传 runtime.note_simulated_input):
    vision 层不得反向依赖 application 层,所以用回调而不是 import。
    测试直接传假实现,或干脆不用本函数、自行构造 VisionAdapters。
    """
    capture, screenshot_source = _build_capture()
    ocr, ocr_source = _build_ocr(cfg["vision"])
    press, hover, click, escape = _build_input(on_simulated_input)
    return VisionAdapters(
        press=press,
        hover=hover,
        click=click,
        escape=escape,
        capture=capture,
        ocr=ocr,
        sleep=time.sleep,
        screenshot_source=screenshot_source,
        ocr_source=ocr_source,
    )
