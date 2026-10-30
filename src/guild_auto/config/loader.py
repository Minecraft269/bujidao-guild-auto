# -*- coding: utf-8 -*-
"""
config/loader.py —— 加载 / 保存 / 热重载 / 命令行覆盖 / 控制台输出
=================================================================
只负责"把文件读成 dict、把 dict 写回文件",不含业务校验规则
(校验在 schema.validate_config)。
"""

import json
import os
import sys
import time
import traceback

from guild_auto.config.defaults import CONFIG_COMMENTS, DEFAULT_CONFIG
from guild_auto.config.schema import SCHEMA_VERSION, validate_config


def _log(level, msg):
    """按级别写入日志文件(不打印控制台;logger 未初始化时自动忽略)。

    延迟 import infrastructure.logger:logger 的配置本身来自 config 层,
    模块级 import 会构成循环依赖。与 console() 共用同一日志器,不新造。
    """
    try:
        from guild_auto.infrastructure import logger as _logger_mod
    except Exception as e:  # pragma: no cover - 仅 logger 导入失败时
        sys.stderr.write(f"[config] 日志模块不可用: {e!r}\n")
        return
    getattr(_logger_mod, f"log_{level}", _logger_mod.log_info)(str(msg))


def _trace_file(kind, path, size):
    """写 TRACE_FILE_READ/WRITE 埋点(kind 取 'read'/'write')。"""
    try:
        from guild_auto.infrastructure import logger as _logger_mod
    except Exception as e:  # pragma: no cover - 仅 logger 导入失败时
        sys.stderr.write(f"[config] 日志模块不可用: {e!r}\n")
        return
    getattr(_logger_mod, f"trace_file_{kind}")(path, size=size)


def _elapsed_ms(t0):
    return (time.monotonic() - t0) * 1000.0


def _leaf_paths(node, prefix=""):
    """递归收集叶键的点分路径(用于记录合并时补默认/被丢弃的键名)。"""
    out = []
    if isinstance(node, dict):
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, dict) and value:
                out.extend(_leaf_paths(value, path))
            else:
                out.append(path)
    return out


def _deep_merge(base, override):
    """递归合并:override 中的键覆盖 base,保留 base 中 override 没有的键。"""
    result = dict(base)
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(base[key], value)
        else:
            result[key] = value
    return result


def _dump_jsonc(obj, indent=0, prefix=""):
    """把配置 dict 输出为 JSONC 文本:每个键前插入 "// 注释" 注释行(无引号,行首 //)。
    返回嵌套内容的行串(不含外层大括号);注释行不参与 JSON 结构,加载时按行首剥离。"""
    pad = " " * indent
    out = []
    if isinstance(obj, dict):
        keys = list(obj.keys())
        for i, k in enumerate(keys):
            path = f"{prefix}.{k}" if prefix else k
            comment = CONFIG_COMMENTS.get(path)
            if comment:
                out.append(f"{pad}  // {comment}")
            v = obj[k]
            if isinstance(v, dict):
                out.append(f'{pad}  "{k}": {{')
                out.append(_dump_jsonc(v, indent + 4, path))
                out.append(f"{pad}  }}" + ("," if i < len(keys) - 1 else ""))
            else:
                out.append(
                    f'{pad}  "{k}": {json.dumps(v, ensure_ascii=False)}'
                    + ("," if i < len(keys) - 1 else "")
                )
    return "\n".join(out)


def _strip_comment_keys(node):
    """递归删除 '//' 与 '_' 开头的键(兼容旧版生成的"注释即数据键"格式;
    新版为行首 // 注释行,加载前已剥离,此处过滤对新格式无副作用)。"""
    if isinstance(node, dict):
        return {
            k: _strip_comment_keys(v)
            for k, v in node.items()
            if not (k.startswith("//") or k.startswith("_"))
        }
    if isinstance(node, list):
        return [_strip_comment_keys(v) for v in node]
    return node


def _strip_jsonc(text):
    """剥离 JSONC 注释:同时支持行注释 '//' 与块注释 '/* */'。

    必须做字符串感知(逐字符 + 引号状态机)而非正则替换:
    配置里全是路径与中文文案(如 "C:\\Path\\To\\Minecraft\\logs\\latest.log"),
    正则会把字符串字面量内部的 '//' 当成注释起点,把整个配置截断。
    行注释的换行保留,便于 JSON 报错时对得上原始行号。
    """
    out = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        ch = text[i]
        if in_str:
            if ch == "\\":  # 转义符(路径里的反斜杠)连同下一字符一起放行
                out.append(ch)
                if i + 1 < n:
                    out.append(text[i + 1])
                    i += 2
                    continue
            elif ch == '"':
                in_str = False
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n:
            nxt = text[i + 1]
            if nxt == "/":  # 行注释:跳到换行(保留换行符本身)
                j = text.find("\n", i)
                if j == -1:
                    break
                i = j
                continue
            if nxt == "*":  # 块注释:跳到 '*/';未闭合时容忍到文件末尾
                j = text.find("*/", i + 2)
                if j == -1:
                    break
                i = j + 2
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _load_jsonc_text(path):
    """读取 JSONC 配置文件:剥离 '//' 与 '/* */' 注释后按 JSON 解析。

    用 utf-8-sig 读取:顺带吃掉 BOM —— 用户在记事本里编辑存盘常带 BOM,
    而带 BOM 的文本交给 json.loads 会直接抛 JSONDecodeError。"""
    with open(path, "r", encoding="utf-8-sig") as f:
        text = f.read()
    _trace_file("read", os.path.abspath(path), len(text.encode("utf-8")))
    return json.loads(_strip_jsonc(text))


def ensure_default_config(path):
    """若配置文件不存在,写入默认配置(JSONC 格式)。
    生成的文件每个配置键前有一行 "// 中文注释"(无引号,含作用与取值范围),便于直接阅读;
    加载时自动剥离注释行,不影响 JSON 解析。"""
    t0 = time.monotonic()
    abs_path = os.path.abspath(path)
    exists = os.path.exists(path)
    _log("info", f"[S1] ensure_default_config ENTER | path={abs_path} | exists={exists}")
    if exists:
        _log("info", "[S1] 首次运行=false(配置文件已存在,不覆盖)")
        _log(
            "info",
            f"[S1] ensure_default_config EXIT | 耗时={_elapsed_ms(t0):.2f}ms",
        )
        return False
    _log("info", "[S1] 首次运行=true(配置文件不存在,写入默认配置)")
    header = (
        "// 布吉岛公会管理脚本配置文件。每个配置键前有 '//' 中文注释行,说明其作用与取值范围;"
        "详细说明见 docs/《配置说明.md》。修改后保存即可,重启脚本生效。"
    )
    text = "{\n" + _dump_jsonc(DEFAULT_CONFIG, 2) + "\n}"
    content = header + "\n" + text + "\n"
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        # 此刻 logger 可能尚未初始化(logger 的目录来自配置):console 保证控制台可见
        console(f"配置写入失败: {abs_path} | {e!r}", level="error")
        _log(
            "error",
            f"[S1] ensure_default_config 写入失败: {abs_path} | {e!r}\n{traceback.format_exc()}",
        )
        _log(
            "info",
            f"[S1] ensure_default_config EXIT(失败) | 耗时={_elapsed_ms(t0):.2f}ms",
        )
        raise
    size = len(content.encode("utf-8"))
    _trace_file("write", abs_path, size)
    _log(
        "info",
        f"[S1] ensure_default_config EXIT | 已写入 {size} 字节 | 耗时={_elapsed_ms(t0):.2f}ms",
    )
    return True


def load_config(path):
    """读取配置文件,缺失键与默认配置深合并(向前兼容旧配置文件)。
    支持 JSONC(行首 // 注释)与标准 JSON 两种格式。"""
    t0 = time.monotonic()
    abs_path = os.path.abspath(path)
    exists = os.path.exists(path)
    _log("info", f"[S2] load_config ENTER | path={abs_path} | exists={exists}")
    if not exists:
        # 路径写错时"看起来加载成功"却全用默认值,必须留下可检索的降级痕迹
        console(f"配置/加载: 配置路径不存在，已回退默认配置: {abs_path}", level="warning")
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        _log(
            "info",
            f"[S2] load_config EXIT(默认配置) | 查询范围={cfg.get('query_scope')} "
            f"| 操作模式={cfg.get('operation_mode')} | 耗时={_elapsed_ms(t0):.2f}ms",
        )
        return cfg
    try:
        parsed = _load_jsonc_text(path)
    except json.JSONDecodeError as e:
        excerpt = ""
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                lines = f.read().splitlines()
            if 0 < e.lineno <= len(lines):
                excerpt = lines[e.lineno - 1].strip()[:120]
        except OSError as exc2:
            excerpt = f"<摘录读取失败: {exc2!r}>"
            _log("warning", f"[S2] load_config 读取错误行摘录失败: {exc2!r}")
        console(
            f"配置/加载失败: {abs_path} 第 {e.lineno} 行第 {e.colno} 列: {e.msg} | 摘录: {excerpt}",
            level="error",
        )
        _log("error", f"[S2] load_config 解析失败完整 traceback:\n{traceback.format_exc()}")
        raise
    user_cfg = _strip_comment_keys(parsed)
    merged = _deep_merge(DEFAULT_CONFIG, user_cfg)
    added = sorted(set(_leaf_paths(merged)) - set(_leaf_paths(user_cfg)))
    discarded = sorted(set(_leaf_paths(parsed)) - set(_leaf_paths(user_cfg)))
    _log(
        "debug",
        f"[S2] load_config 合并默认值 | 补默认键({len(added)})={added} "
        f"| 丢弃键({len(discarded)})={discarded}",
    )
    _log(
        "info",
        f"[S2] load_config EXIT | 键数量={len(merged)} | config_schema_version={SCHEMA_VERSION} "
        f"| 耗时={_elapsed_ms(t0):.2f}ms",
    )
    return merged


def save_config(path, cfg):
    """保存配置(UTF-8、缩进 2、保留中文)。

    写成 JSONC(每个键上方一行 // 注释)，与 ensure_default_config 一致 ——
    否则用户用 UI 保存一次后，config.json 就再也看不到"每项怎么填"的说明，
    违反"每个配置键都必须有描述"的初衷。
    """
    t0 = time.monotonic()
    abs_path = os.path.abspath(path)
    _log("info", f"[S2] save_config ENTER | path={abs_path}")
    content = "{" + "\n" + _dump_jsonc(cfg, 0) + "\n" + "}" + "\n"
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        # UI 保存失败时用户会以为已保存:控制台 + 日志双重留痕,并明确"未更新"
        console(f"配置保存失败，磁盘配置未更新: {abs_path} | {e!r}", level="error")
        _log(
            "error",
            f"[S2] save_config 失败，磁盘配置未更新: {abs_path} | {e!r}\n{traceback.format_exc()}",
        )
        raise
    size = len(content.encode("utf-8"))
    _trace_file("write", abs_path, size)
    _log(
        "info",
        f"[S2] save_config EXIT | path={abs_path} | bytes={size} | 耗时={_elapsed_ms(t0):.2f}ms",
    )


def apply_cli_overrides(cfg, args):
    """命令行参数覆盖配置(--admin/--log/--options/--config 已在 main 处理路径,这里覆盖其余)。"""
    overridden = []
    if getattr(args, "admin", None):
        cfg["admin"] = args.admin
        overridden.append("admin")
    if getattr(args, "log", None):
        cfg["game_log"] = args.log
        overridden.append("game_log")
    if getattr(args, "options", None):
        cfg["game_options"] = args.options
        overridden.append("game_options")
    if overridden:
        # 只记键名(admin 名等值不落日志),便于解释"配置与 config.json 不一致"
        _log(
            "info",
            f"配置/命令行覆盖: {', '.join(overridden)} (值来自命令行，优先于 config.json)",
        )
    else:
        _log("debug", "配置/命令行覆盖: 无命令行覆盖")
    return cfg


def console(msg, level="info"):
    """控制台输出(兼容 GBK 控制台,避免编码崩溃);同时按级别写入日志文件。

    level: "critical"/"error"/"warning"/"info"/"debug"/"trace"(默认 info)。
    logger 未初始化或已禁用时仅打印控制台,不写文件。

    ⚠ import 必须走完整包路径。src-layout 迁移后残留的 `import logger` 会
    抛 ImportError 并被下面的 except 吞掉 —— 表现为"控制台照常输出、
    但日志文件里一行都没有",是最难察觉的一类静默失效。
    延迟 import 是为了避免 config ← infrastructure.logger 的模块级循环依赖
    (logger 的默认值也读配置)。
    """
    try:
        print(msg)
    except UnicodeEncodeError:
        sys.stdout.buffer.write((msg + "\n").encode("utf-8", errors="replace"))
    try:
        from guild_auto.infrastructure import logger as _logger_mod

        getattr(_logger_mod, f"log_{level}", _logger_mod.log_info)(str(msg))
    except Exception as e:
        # 走到这里说明 logger 自身不可用(导入失败或初始化异常)。
        # 不能再调 logger(会递归/再抛),直接写 stderr 保证可检索。
        try:
            sys.stderr.write(f"[console] 日志系统不可用,仅输出到控制台: {e!r}\n")
        except Exception:
            return  # stderr 也写不了(如已关闭):此时无任何可用的输出通道
