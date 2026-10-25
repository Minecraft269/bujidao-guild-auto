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

from guild_auto.config.defaults import CONFIG_COMMENTS, DEFAULT_CONFIG
from guild_auto.config.schema import validate_config

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
                out.append(f'{pad}  "{k}": {json.dumps(v, ensure_ascii=False)}'
                            + ("," if i < len(keys) - 1 else ""))
    return "\n".join(out)


def _strip_comment_keys(node):
    """递归删除 '//' 与 '_' 开头的键(兼容旧版生成的"注释即数据键"格式;
    新版为行首 // 注释行,加载前已剥离,此处过滤对新格式无副作用)。"""
    if isinstance(node, dict):
        return {k: _strip_comment_keys(v) for k, v in node.items()
                if not (k.startswith("//") or k.startswith("_"))}
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
            if ch == "\\":                      # 转义符(路径里的反斜杠)连同下一字符一起放行
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
            if nxt == "/":                     # 行注释:跳到换行(保留换行符本身)
                j = text.find("\n", i)
                if j == -1:
                    break
                i = j
                continue
            if nxt == "*":                     # 块注释:跳到 '*/';未闭合时容忍到文件末尾
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
    return json.loads(_strip_jsonc(text))


def ensure_default_config(path):
    """若配置文件不存在,写入默认配置(JSONC 格式)。
    生成的文件每个配置键前有一行 "// 中文注释"(无引号,含作用与取值范围),便于直接阅读;
    加载时自动剥离注释行,不影响 JSON 解析。"""
    if os.path.exists(path):
        return False
    header = ("// 布吉岛公会管理脚本配置文件。每个配置键前有 '//' 中文注释行,说明其作用与取值范围;"
                "详细说明见同目录《配置说明.md》。修改后保存即可,重启脚本生效。")
    text = "{\n" + _dump_jsonc(DEFAULT_CONFIG, 2) + "\n}"
    with open(path, "w", encoding="utf-8") as f:
        f.write(header + "\n" + text + "\n")
    return True


def load_config(path):
    """读取配置文件,缺失键与默认配置深合并(向前兼容旧配置文件)。
    支持 JSONC(行首 // 注释)与标准 JSON 两种格式。"""
    if not os.path.exists(path):
        return json.loads(json.dumps(DEFAULT_CONFIG))
    user_cfg = _strip_comment_keys(_load_jsonc_text(path))
    merged = _deep_merge(DEFAULT_CONFIG, user_cfg)
    return merged


def save_config(path, cfg):
    """保存配置(UTF-8、缩进 2、保留中文)。"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")



def apply_cli_overrides(cfg, args):
    """命令行参数覆盖配置(--admin/--log/--options/--config 已在 main 处理路径,这里覆盖其余)。"""
    if getattr(args, "admin", None):
        cfg["admin"] = args.admin
    if getattr(args, "log", None):
        cfg["game_log"] = args.log
    if getattr(args, "options", None):
        cfg["game_options"] = args.options
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
    except Exception:
        pass  # 日志系统不可用时不影响控制台输出

