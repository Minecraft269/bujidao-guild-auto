# -*- coding: utf-8 -*-
"""binding.py —— 把核的能力绑成 IPC 方法表（GUI设计规范 §2.2）。

Python 侧只剩两个界面文件：ipc.py（管道收发）与 binding.py（方法与数据）。
界面本体是独立的 tauri 进程（gui/），本模块不 import 任何 GUI 库。

分工边界：
- 配置/规则的**真源**在这里（config.json），GUI 只做编辑与展示；
- 运行控制映射到 application.runtime 的既有暂停/停止机制，不新造一套状态；
- 报告/日志目录由外面传进来（main.py 知道真实路径），本模块不猜路径。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

from guild_auto import capabilities as capabilities_mod
from guild_auto.config import load_config, save_config, validate_config
from guild_auto.config import ui_binding
from guild_auto.domain import rules as rules_mod
from guild_auto.ui.ipc import IpcServer

START_TIMEOUT = 24 * 3600.0
UPDATE_TIMEOUT = 8.0
GUI_EXE_NAMES = ("布吉岛公会管理脚本-Gui.exe", "bujidao-guild-gui.exe")

# 热重载分级（§3.5）：这些键改了必须重启才生效，其余保存即生效
RESTART_REQUIRED_PATHS = ("log.dir", "ui.enabled")

# 当前会话的桥：main.py 启动 GUI 后登记，流程各阶段用它推状态（未启用 GUI 时为 None）
_ACTIVE: GuiBridge | None = None


def set_active(bridge: GuiBridge | None) -> None:
    global _ACTIVE
    _ACTIVE = bridge


def active() -> GuiBridge | None:
    return _ACTIVE


def gui_executable() -> Path | None:
    """定位 Gui.exe：环境变量优先，其次 exe 同级（发布布局），最后仓库构建产物（开发）。"""
    override = os.environ.get("BUJIDAO_GUI")
    if override:
        return Path(override)
    repo = Path(__file__).resolve().parents[3]
    roots = [
        Path(sys.executable).resolve().parent,
        repo / "gui" / "src-tauri" / "target" / "release",
    ]
    for root in roots:
        for name in GUI_EXE_NAMES:
            candidate = root / name
            if candidate.exists():
                return candidate
    return None


class GuiBridge:
    """一个 GUI 会话：起管道、拉进程、应答请求、推送状态。"""

    def __init__(
        self,
        cfg: dict[str, Any],
        config_path: str | Path,
        *,
        report_dir: str | Path | None = None,
        log_dir: str | Path | None = None,
        controller: Any | None = None,
        first_run: bool = False,
        restart_impl: Callable[[], None] | None = None,
    ):
        self.cfg = cfg
        self.config_path = Path(config_path)
        self.first_run = bool(first_run)
        # 重启实现可注入：测试里绝不能真的 execv（那会把测试进程替换掉）
        self._restart_impl = restart_impl or _default_restart_impl
        self.report_dir = Path(report_dir) if report_dir else None
        self.log_dir = Path(log_dir) if log_dir else None
        self.controller = controller
        self.server = IpcServer(self._build_handlers())
        self._started = threading.Event()
        self._exit = threading.Event()
        self._proc: subprocess.Popen[bytes] | None = None
        self._state: dict[str, Any] = {
            "state": "idle",
            "stage": "未开始",
            "done": 0,
            "total": 0,
            "player": "",
            "elapsed_ms": 0,
            "pause_reason": "",
        }
        self._action: dict[str, Any] = {"cmd": "", "verify": "", "retry": 0, "last": "", "kind": ""}
        self._players: list[dict[str, Any]] = []
        self._pending: list[dict[str, Any]] = []
        self._fails: list[dict[str, Any]] = []
        self._reports: dict[str, Any] = {}
        self._confirm: dict[str, tuple[threading.Event, dict[str, Any]]] = {}
        self._t0: float | None = None

    # ── 生命周期 ────────────────────────────────────────────────────────
    def launch(self, *, timeout: float = START_TIMEOUT) -> bool:
        """起管道 + 拉 Gui，等用户点「开始」。True=开始，False=关窗/超时。"""
        exe = gui_executable()
        if exe is None:
            raise FileNotFoundError(
                "找不到 Gui.exe。请先构建：cargo build --release（见 docs/GUI设计规范.md §10）"
            )
        self.server.create()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        console_safe(f"核侧已建管道: {self.server.path}")
        self._proc = subprocess.Popen(
            [str(exe), "--pipe", self.server.pipe_name, "--token", self.server.token]
        )
        console_safe(f"Gui 已启动 pid={self._proc.pid}")
        if not self._started.wait(timeout):
            self.stop()
            return False
        self._t0 = time.time()
        self._state.update({"state": "running", "stage": "准备中"})
        self.push_state()
        return True

    def stop(self) -> None:
        self._exit.set()
        self.server.stop()
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except OSError:
                pass

    # ── 推送 ────────────────────────────────────────────────────────────
    def push_state(self, **changes: Any) -> None:
        self._state.update(changes)
        if self._t0 is not None:
            self._state["elapsed_ms"] = int((time.time() - self._t0) * 1000)
        self.server.send_event("state", dict(self._state))

    def push_action(self, **changes: Any) -> None:
        self._action.update(changes)
        self.server.send_event("action", dict(self._action))

    def push_players(self, items: list[dict[str, Any]], *, replace: bool = False) -> None:
        if replace:
            self._players = list(items)
        else:
            self._players.extend(items)
        self.server.send_event("players", {"items": items, "replace": replace})

    def push_pending(self, items: list[dict[str, Any]]) -> None:
        self._pending = list(items)
        self.server.send_event("pending", {"items": self._pending})

    def push_failures(self, items: list[dict[str, Any]]) -> None:
        self._fails = list(items)
        self.server.send_event("failures", {"items": self._fails})

    def push_reports(self, reports: dict[str, Any]) -> None:
        self._reports = dict(reports)
        self.server.send_event("reports", dict(self._reports))

    def event(
        self,
        text: str,
        *,
        level: int = 4,
        category: str = "",
        player: str | None = None,
        chain: list[dict[str, Any]] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "level": level,
            "category": category,
            "text": text,
            "ts": int(time.time()),
        }
        if player:
            payload["player"] = player
        if chain:
            payload["chain"] = chain
        self.server.send_event("event", payload)

    def ask_confirm(self, kind: str, **fields: Any) -> str:
        """向 GUI 发模态确认并等答复。GUI 断开时按『停止』处理，绝不挂死流程。"""
        token = f"{kind}-{int(time.time() * 1000)}"
        done = threading.Event()
        box: dict[str, Any] = {}
        self._confirm[token] = (done, box)
        self.server.send_event("confirm", {"id": token, "kind": kind, **fields})
        done.wait()
        self._confirm.pop(token, None)
        return str(box.get("choice") or "stop")

    def ask_stage(self, prompt: str, default: bool = False, context: dict | None = None) -> bool:
        """阶段确认（§3.4）：走 GUI 模态；GUI 模式下不存在的 input() 分支不再被触达。"""
        ctx = context or {}
        choice = self.ask_confirm(
            "stage",
            stage=ctx.get("stage", "执行确认"),
            target=ctx.get("target", 0),
            actions=ctx.get("actions", 0),
            prompt=prompt,
            default=bool(default),
        )
        return choice in ("resume", "continue", "yes")

    def ask_kick_text(self, prompt: str, context: dict | None = None) -> str:
        """kick 二次确认（§3.4）：模态里已逐字校验确认词，这里只回原文。

        返回原文而不是布尔值，是为了让 stages 里 `ans != expected` 的判定语义
        与控制台模式完全一致（逐字匹配，绝不布尔化）。
        """
        ctx = context or {}
        players = ctx.get("players") or []
        first = players[0] if players else {}
        choice = self.ask_confirm(
            "kick",
            players=players,
            player=first.get("player", ""),
            level=first.get("level", ""),
            weekly_total=first.get("weekly_total", ""),
            count=len(players),
            confirm_text=ctx.get("confirm_text", ""),
            prompt=prompt,
        )
        return str(ctx.get("confirm_text", "")) if choice == "kick" else ""

    # ── 方法表 ──────────────────────────────────────────────────────────
    def _build_handlers(self) -> dict[str, Callable[[dict[str, Any]], Any]]:
        return {
            "hello": lambda _p: self._hello(),
            "config": lambda _p: self._config_payload(),
            "save_config": self._save_config,
            "rules": lambda _p: {"rules": self._rules_with_builtin()},
            "save_rules": self._save_rules,
            "preview_rule": self._preview_rule,
            "state": lambda _p: dict(self._state),
            "start": self._on_start,
            "pause": lambda _p: self._on_pause(),
            "resume": lambda _p: self._on_resume(),
            "stop": lambda _p: self._on_stop(),
            "open_reports": lambda _p: self._open_dir(self.report_dir),
            "open_logs": lambda _p: self._open_dir(self.log_dir),
            "window": lambda p: {"ok": True, "echo": p},
            "check_update": lambda _p: self._check_update(),
            "confirm_reply": self._on_confirm_reply,
            "restart_app": self._restart_app,
            "capture_key": self._capture_key,
        }

    def _capture_key(self, params: dict[str, Any]) -> dict[str, Any]:
        """键位捕获（§3.6）：钩子起不来就抛错让界面保住原值；冲突只提示不阻止。"""
        from guild_auto.ui import keybind  # noqa: PLC0415

        path = str(params.get("path") or "")
        try:
            result = keybind.capture()
        except keybind.KeyCaptureError as e:
            raise ValueError(str(e)) from e
        key = result.get("key")
        conflict = self._find_key_conflict(key, path) if key else None
        return {**result, "path": path, "conflict": conflict}

    def _find_key_conflict(self, key: str, path: str) -> str:
        """同名键位只提示不阻止：真的冲突时用户自己决定。"""
        for spec in ui_binding.flatten_config(self.cfg):
            if spec.path == path:
                continue
            if str(spec.value).strip().lower() == key:
                return f"与「{spec.path}」绑定相同"
        return ""

    def _hello(self) -> dict[str, Any]:
        from guild_auto import __version__  # noqa: PLC0415
        from guild_auto.config.schema import SCHEMA_VERSION  # noqa: PLC0415

        meta = capabilities_mod.variant_meta()
        return {
            "variant": meta["build_variant"],
            "capabilities": list(meta["capabilities"]),
            "plugin_version": __version__,
            "config_schema_version": SCHEMA_VERSION,
            # 首次运行（配置是刚生成的）→ 前端锁在配置页并要求先保存（§3.5）
            "first_run": self.first_run,
        }

    def _config_payload(self) -> dict[str, Any]:
        readonly = ui_binding.readonly_values()
        fields = []
        for spec in ui_binding.flatten_config(self.cfg):
            item = {
                "path": spec.path,
                "label": spec.label,
                "comment": spec.comment,
                "kind": spec.kind,
                "value": spec.value,
                "choices": list(spec.choices or ()),
                "readonly": spec.path in readonly,
                # 热重载分级（§3.5）：标了 needs_restart 的保存后仍要重启才生效
                "needs_restart": spec.path in RESTART_REQUIRED_PATHS,
            }
            if item["readonly"]:
                item["value"] = readonly[spec.path]
            fields.append(item)
        # build_variant 不在 config.json 里（由构建期决定），但要作为只读项显示在配置页
        present = {f["path"] for f in fields}
        for path, value in readonly.items():
            if path not in present:
                fields.append(
                    {
                        "path": path,
                        "label": "构建变体",
                        "comment": "由构建期能力开关决定，只读；改它需要换一个构建产物。",
                        "kind": "str",
                        "value": value,
                        "choices": [],
                        "readonly": True,
                        "needs_restart": False,
                    }
                )
        return {"fields": fields, "readonly": readonly}

    def _save_config(self, params: dict[str, Any]) -> dict[str, Any]:
        values = params.get("values") or {}
        if not isinstance(values, dict):
            raise ValueError("values 必须是对象")
        new_cfg, errors = ui_binding.apply_edits(self.cfg, values)
        if errors:
            raise ValueError("；".join(f"{e['path']}: {e['message']}" for e in errors))
        problems = validate_config(new_cfg)
        if problems:
            raise ValueError("；".join(f"{level}: {msg}" for level, msg in problems))
        save_config(self.config_path, new_cfg)
        self.cfg = load_config(self.config_path)
        # 告诉界面哪些改动要重启才生效（§3.5），由界面提示「有 N 项需重启生效」
        restart = sorted(p for p in values if p in RESTART_REQUIRED_PATHS)
        payload = self._config_payload()
        payload["restart_required"] = restart
        return payload

    def _restart_app(self, _params: dict[str, Any]) -> dict[str, Any]:
        """立即重启：先回包，再在后台替换本进程（§3.5 的「立即重启」入口）。"""
        threading.Thread(target=self._do_restart, daemon=True).start()
        return {}

    def _do_restart(self) -> None:
        time.sleep(0.3)  # 让应答先写回界面
        self.stop()
        try:
            self._restart_impl()
        except OSError as e:  # 重启失败必须让用户知道，而不是静默留在旧进程
            console_safe(f"重启失败: {e}")

    def _rules_with_builtin(self) -> list[dict[str, Any]]:
        builtin = rules_mod.builtin_default_rule(self.cfg)
        # 内置规则的 _thresholds 是运行时引用，不进 IPC（前端只读展示）
        return [builtin, *(self.cfg.get("rules") or [])]

    def _save_rules(self, params: dict[str, Any]) -> dict[str, Any]:
        rules = params.get("rules")
        if not isinstance(rules, list):
            raise ValueError("rules 必须是列表")
        custom = [r for r in rules if not (isinstance(r, dict) and r.get("builtin"))]
        problems = rules_mod.validate_rules(custom)
        if problems:
            text = "；".join(f"{p['rule_id']} {p['field']}: {p['message']}" for p in problems)
            raise ValueError(text)
        self.cfg["rules"] = custom
        save_config(self.config_path, self.cfg)
        return {"rules": self._rules_with_builtin()}

    def _preview_rule(self, params: dict[str, Any]) -> dict[str, Any]:
        rules = params.get("rules")
        sample = params.get("sample")
        if not isinstance(rules, list) or not isinstance(sample, dict):
            raise ValueError("preview_rule 需要 rules 与 sample")
        # 预览与生产同一个求值函数：阶段 3 判定走的就是 evaluate_rules
        actions, hits, traces = rules_mod.evaluate_rules(rules, sample)
        return {"actions": actions, "hit_rule_ids": hits, "traces": traces}

    def _on_start(self, _params: dict[str, Any]) -> dict[str, Any]:
        self._started.set()
        return {}

    def _on_pause(self) -> dict[str, Any]:
        if self.controller is not None:
            self.controller.pause("手动")
        self.push_state(state="paused", pause_reason="手动")
        return {}

    def _on_resume(self) -> dict[str, Any]:
        if self.controller is not None:
            self.controller.resume()
        self.push_state(state="running", pause_reason="")
        return {}

    def _on_stop(self) -> dict[str, Any]:
        if self.controller is not None:
            self.controller.stop()
        self.push_state(state="idle", stage="已停止")
        return {}

    def _on_confirm_reply(self, params: dict[str, Any]) -> dict[str, Any]:
        token = str(params.get("id") or "")
        entry = self._confirm.get(token)
        if entry is None:
            raise ValueError("确认已失效")
        done, box = entry
        box["choice"] = str(params.get("choice") or "")
        done.set()
        return {}

    def _open_dir(self, path: Path | None) -> dict[str, Any]:
        if path is None or not Path(path).exists():
            raise ValueError("目录不存在")
        os.startfile(str(path))
        return {}

    def _check_update(self) -> dict[str, Any]:
        """按 update.mode 查清单。无签名清单直接拒绝（§8），不给『忽略』选项。"""
        url = str(self.cfg["update"]["manifest_url"])
        if not url:
            raise ValueError("未配置 update.manifest_url")
        try:
            with urllib.request.urlopen(url, timeout=UPDATE_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # 网络/解析失败都如实回报，不假装有更新
            raise ValueError(f"检查更新失败: {e}") from e
        # 清单按拼接名分列，这里必须用 variant_meta 的 build_variant 键
        variant = capabilities_mod.variant_meta()["build_variant"]
        entry = (data.get("variants") or {}).get(variant) or {}
        if not entry.get("signature"):
            raise ValueError("清单缺少签名，按规范直接拒绝")
        return {"status": "有新版本", "version": data.get("version"), "entry": entry}


def _default_restart_impl() -> None:
    """真重启：用同一个解释器/可执行文件替换当前进程（argv 原样带过去）。"""
    os.execv(sys.executable, [sys.executable, *sys.argv])


def console_safe(msg: str) -> None:
    """控制台输出：打包成无控制台时 sys.stdout 可能是 None。"""
    stream = sys.stdout
    if stream is not None:
        print(msg, flush=True)


class RuntimeController:
    """把 GUI 的暂停/继续/停止映射到 application.runtime 的既有机制。

    不新造状态机：runtime 里已经有 _paused / _force_stop 与配套的 wait_if_paused，
    控制台模式的暂停键走的也是同一套，所以 GUI 与按键控制的语义天然一致。
    """

    def __init__(self, runtime_module: Any | None = None) -> None:
        if runtime_module is None:
            from guild_auto.application import runtime as runtime_module  # noqa: PLC0415
        self._rt: Any = runtime_module

    def pause(self, reason: str = "") -> None:  # noqa: ARG002 - 原因由界面自己显示
        self._rt.set_paused(True)

    def resume(self) -> None:
        self._rt.set_paused(False)
        self._rt.note_resume()

    def stop(self) -> None:
        self._rt.set_force_stop(True)
        self._rt.set_paused(False)  # 解除等待，让流程真的能退出


def pending_rows(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """待办队列行（§3.3）：把 pending_actions.json 的条目整理成界面字段。"""
    rows: list[dict[str, Any]] = []
    for it in items or []:
        acts = it.get("actions")
        rows.append(
            {
                "player": str(it.get("player") or ""),
                "action": ",".join(str(a) for a in acts) if isinstance(acts, list) else str(acts or ""),
                "reason": str(it.get("reason") or it.get("error") or ""),
                "enqueued_at": str(it.get("enqueued_at") or it.get("ts") or ""),
                "retries": int(it.get("retries") or 0),
            }
        )
    return rows


def rows_from_records(
    records: list[dict[str, Any]],
    failures: list[dict[str, Any]] | None = None,
    decisions: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """把查询结果整理成玩家表行（§3.3）。屏蔽词玩家原样显示并在备注标注不可查。"""
    from guild_auto.domain.parsers import is_masked_name  # noqa: PLC0415

    decisions = decisions or {}
    rows: list[dict[str, Any]] = []
    for i, rec in enumerate(records, start=1):
        player = str(rec.get("player") or "")
        masked = is_masked_name(player)
        rows.append(
            {
                "index": i,
                "name": player,
                "level": rec.get("rank"),
                "weekly_total": rec.get("weekly_total"),
                "weekly_detail": rec.get("daily") or [],
                "last_online": rec.get("last_online"),
                "online_state": "unknown",
                "decision": decisions.get(player, "none"),
                "exec_state": "pending",
                "note": "名称被屏蔽，指令不可查" if masked else "",
                "masked": masked,
            }
        )
    for fail in failures or []:
        player = str(fail.get("player") or "")
        rows.append(
            {
                "index": len(rows) + 1,
                "name": player,
                "level": fail.get("rank"),
                "weekly_total": None,
                "weekly_detail": [],
                "last_online": None,
                "online_state": "unknown",
                "decision": "none",
                "exec_state": "failed",
                "note": str(fail.get("reason") or "查询失败"),
                "masked": is_masked_name(player),
            }
        )
    return rows
