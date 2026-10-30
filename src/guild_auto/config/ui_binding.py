# -*- coding: utf-8 -*-
"""
config/ui_binding.py —— UI 配置绑定与校验（纯逻辑，零 tkinter）
==============================================================
UI 需要的"把配置摊平成表单字段 / 把表单值写回配置 / 校验非法输入"
全部放在这里，**不依赖任何 GUI 库**。好处：
  * 属于 config 层 → 天然纳入核心层覆盖率门禁（≥98%）
  * tkinter 只做"画控件 + 取值/回填"的薄胶水
  * 命令行模式与 UI 模式共用同一套校验（schema.validate_config）

字段模型（FieldSpec）：
    path     配置路径，用 '.' 连接，如 "thresholds.high_demote"
    label    中文标签（取 CONFIG_COMMENTS 首句）
    comment  完整中文说明（作用 + 可填值）
    kind     str | int | float | bool | str_list
    value    当前值
    choices  可选项（仅 str 且配置为枚举时有值）

写回时的类型转换是"严格"的：数字字段收到 "abc" 不会静默变 0，
而是产生一条 error 并把该字段标红——这正是 UI 实时校验的基础。
"""

from __future__ import annotations

import copy

from guild_auto.config.defaults import CONFIG_COMMENTS, DEFAULT_CONFIG


class FieldSpec:
    """一个可编辑配置项的描述。"""

    __slots__ = ("path", "label", "comment", "kind", "value", "choices")

    def __init__(self, path, label, comment, kind, value, choices=None):
        self.path = path
        self.label = label
        self.comment = comment
        self.kind = kind
        self.value = value
        self.choices = choices

    def __repr__(self):
        return f"<Field {self.path} {self.kind}={self.value!r}>"

    def __eq__(self, other):
        return (
            isinstance(other, FieldSpec)
            and self.path == other.path
            and self.kind == other.kind
            and self.value == other.value
        )


# 已知枚举：这些键只能取固定值
_ENUMS = {
    "query_scope": ("all", "high", "mid", "low", "entry"),
    "switches.notify_mode": ("summary", "per_player"),
    "log_encoding": ("auto", "utf-8", "gbk", "gb2312"),
    "blocked_name_policy": ("skip", "query_attempt"),
    "operation_mode": ("command", "ui"),
    "vision.kick_verify": ("grid", "count", "time_window", "any"),
}


def _kind_of(value):
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, list):
        return "str_list"
    return "str"


def _label_of(path):
    """取 CONFIG_COMMENTS 的第一句作为短标签；无注释时退回路径末段。"""
    comment = CONFIG_COMMENTS.get(path)
    if not comment:
        return path.rsplit(".", 1)[-1]
    for sep in ("：", ":", "，", ",", "。", "；", ";"):
        if sep in comment:
            return comment.split(sep, 1)[0].strip()[:24]
    return comment[:24]


# 结构化字段：由专用编辑器（规则管理面板）维护，**不得**当普通文本摊平，
# 否则会像 ["{...}", "..."] 这样被逗号切碎，静默写坏配置。
STRUCTURED_PATHS = ("rules",)


def _flatten(node, prefix=""):
    """递归把配置摊平成 FieldSpec 列表（dict 继续展开，list/标量成为字段）。"""
    out = []
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else key
        if path in STRUCTURED_PATHS:
            continue
        if isinstance(value, dict):
            out.extend(_flatten(value, path))
            continue
        out.append(
            FieldSpec(
                path=path,
                label=_label_of(path),
                comment=CONFIG_COMMENTS.get(path, ""),
                kind=_kind_of(value),
                value=copy.deepcopy(value),
                choices=_ENUMS.get(path),
            )
        )
    return out


def flatten_config(cfg):
    """把配置对象摊平成 UI 字段列表（顺序与 DEFAULT_CONFIG 一致）。"""
    return _flatten(cfg)


def _coerce(field, raw):
    """把 UI 输入的原样值转成配置要求的类型。失败抛 ValueError(中文原因)。"""
    kind = field.kind
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        text = str(raw).strip().lower()
        if text in ("true", "1", "yes", "y", "是", "开", "开启"):
            return True
        if text in ("false", "0", "no", "n", "否", "关", "关闭", ""):
            return False
        raise ValueError(f"{field.path} 应为布尔值(true/false),当前: {raw!r}")

    if kind == "int":
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            raise ValueError(f"{field.path} 应为整数,当前: {raw!r}") from None

    if kind == "float":
        try:
            return float(str(raw).strip())
        except (TypeError, ValueError):
            raise ValueError(f"{field.path} 应为数字,当前: {raw!r}") from None

    if kind == "str_list":
        if isinstance(raw, list):
            items = [str(x) for x in raw]
        else:
            text = str(raw).strip()
            if not text:
                return []
            items = None
            for sep in (chr(10), "，", ","):
                if sep in text:
                    items = [x.strip() for x in text.split(sep) if x.strip()]
                    break
            if items is None:
                items = [text]

        # 数值列表（如 debug.simulation.contribution_range = [0, 50000]）：
        # 渲染成文本再写回会变成 ["0", "50000"]，整体校验会拒。
        # 若原始值为数值列表，则逐项转回数值；转不动就报错（不静默变 0）。
        original = field.value
        if (
            isinstance(original, list)
            and original
            and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in original)
        ):
            converted = []
            for x in items:
                try:
                    converted.append(int(str(x).strip()))
                except (TypeError, ValueError):
                    try:
                        converted.append(float(str(x).strip()))
                    except (TypeError, ValueError):
                        raise ValueError(f"{field.path} 的元素应为数字,当前: {x!r}") from None
            return converted
        return items

    text = str(raw)
    if field.choices is not None and text not in field.choices:
        raise ValueError(f"{field.path} 取值必须是 {field.choices} 之一,当前: {text!r}")
    return text


def _set_path(cfg, path, value):
    """按点路径写入（中间层不存在则创建 dict）。"""
    parts = path.split(".")
    node = cfg
    for p in parts[:-1]:
        # 路径来自 flatten_config(cfg)，中间层必然存在；
        # apply_edits 只接受已知字段，不会构造新段。
        node = node[p]
    node[parts[-1]] = value


# 只读展示字段（GUI设计规范 §2.2 的 config.readonly[]）：只给 GUI 看，不参与写回。
READONLY_PATHS = ("build_variant",)


def readonly_values() -> dict[str, str]:
    """GUI 的只读展示字段 → 值。唯一来源是 capabilities.variant_meta()。"""
    from guild_auto.capabilities import variant_meta

    meta = variant_meta()
    return {"build_variant": str(meta["build_variant"])}


def apply_edits(cfg, edits):
    """把 UI 提交的 {path: raw_value} 写回配置的**副本**。

    返回 (new_cfg, errors)：
      errors 形如 [{"path": "thresholds.high_demote", "message": "应为整数"}]
    只有 errors 为空时才应把 new_cfg 落盘。
    不就地修改传入的 cfg（UI 需要保留"上一次保存的版本"以便回滚）。
    """
    new_cfg = copy.deepcopy(cfg)
    fields = {f.path: f for f in flatten_config(cfg)}
    errors = []

    for path, raw in edits.items():
        if path in READONLY_PATHS:
            # 只读字段由核与构建常量决定，写回会被后续读取覆盖，静默接受等于骗 GUI
            errors.append({"path": path, "message": f"只读字段不可写回: {path}"})
            continue
        field = fields.get(path)
        if field is None:
            errors.append({"path": path, "message": f"未知配置项: {path}"})
            continue
        try:
            _set_path(new_cfg, path, _coerce(field, raw))
        except ValueError as e:
            errors.append({"path": path, "message": str(e)})

    if errors:
        return new_cfg, errors

    # 结构化字段保持原值（编辑它们的是专用面板，不经表单）
    for _p in STRUCTURED_PATHS:
        if _p in cfg:
            new_cfg[_p] = copy.deepcopy(cfg[_p])

    # 写回后再跑一次整体校验（跨字段一致性：必填、min<=max、路径存在性等）
    from guild_auto.config.schema import validate_config

    for level, msg in validate_config(new_cfg):
        if level == "error":
            errors.append({"path": "", "message": msg})

    # 规则语法：规则面板之外的表单保存也要拦住非法规则
    if new_cfg.get("rules_enabled"):
        from guild_auto.domain.rules import validate_rules

        for rule_err in validate_rules(new_cfg.get("rules")):
            errors.append(
                {
                    "path": "",
                    "message": (
                        f"规则 {rule_err['rule_id']} 的 {rule_err['field']}: {rule_err['message']}"
                    ),
                }
            )
    return new_cfg, errors


def field_by_path(cfg, path):
    """按路径取字段描述（UI 回填单项时用）。"""
    for f in flatten_config(cfg):
        if f.path == path:
            return f
    return None


def render_field_value(field):
    """把字段值渲染成 UI 输入框里的文本（str_dict 用 k=v 逐行）。"""
    value = field.value
    if field.kind == "bool":
        return "true" if value else "false"
    if field.kind == "str_list":
        return ", ".join(str(x) for x in value)
    if field.kind == "str_dict":
        return "\n".join(f"{k}={v}" for k, v in value.items())
    return str(value)


def diff_against_default(cfg):
    """与 DEFAULT_CONFIG 相比哪些字段被改过（首次运行要求"必须有修改才放行"）。"""
    changed = []
    for f in flatten_config(cfg):
        base = field_by_path(DEFAULT_CONFIG, f.path)
        if base is None or base.value != f.value:
            changed.append(f.path)
    return changed
