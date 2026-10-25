# -*- coding: utf-8 -*-
"""
test_schema_edge_branches.py —— 配置校验的异常输入分支
====================================================
src-layout 拆分后,schema.py 里一批"用户把配置写坏"的分支没有测试。
这些分支恰恰是配置校验存在的理由:写坏时必须**报出错误**,
而不是在别处抛 AttributeError / TypeError —— 后者等于校验整个失效。

覆盖:
  * switches 整段写成字符串 → 报 error,不能在遍历处炸
  * 各开关写成字符串 "false"(JSON 里最常见的手改错误)
  * notify_mode 取值非法
  * pause_key 非字符串
  * debug.simulation.contribution_range 形状非法(长度/元素类型/min>max)
  * game_log 指向脚本自己的 logs/latest.log(自读自写死循环)
"""
import json
import os
import unittest

from guild_auto.config import DEFAULT_CONFIG, validate_config


def _cfg():
    return json.loads(json.dumps(DEFAULT_CONFIG))


def _errors(cfg):
    return [m for lvl, m in validate_config(cfg) if lvl == "error"]


class TestSwitchesSection(unittest.TestCase):
    def test_switches_as_string_reports_error_not_crash(self):
        """switches 写成字符串时,校验必须报错而不是抛 AttributeError。

        抛异常等于校验整个失效 —— main 里的 error 分支根本看不到,
        用户只会看到脚本启动就崩。
        """
        cfg = _cfg()
        cfg["switches"] = "全部开启"
        errs = _errors(cfg)
        self.assertTrue(any("switches" in e for e in errs),
                        f"switches 类型错误未被报出: {errs}")

    def test_bool_written_as_string_is_rejected(self):
        """JSON 里 "false" 带引号就永远是真 —— 这是"开关关不掉"的经典原因。"""
        cfg = _cfg()
        cfg["switches"]["kick_enabled"] = "false"
        errs = _errors(cfg)
        self.assertTrue(any("kick_enabled" in e for e in errs),
                        f"字符串 'false' 未被拒绝: {errs}")

    def test_notify_mode_invalid_value_rejected(self):
        cfg = _cfg()
        cfg["switches"]["notify_mode"] = "逐条"
        errs = _errors(cfg)
        self.assertTrue(any("notify_mode" in e for e in errs),
                        f"非法 notify_mode 未被拒绝: {errs}")

    def test_valid_notify_modes_accepted(self):
        for mode in ("summary", "per_player"):
            cfg = _cfg()
            cfg["switches"]["notify_mode"] = mode
            self.assertEqual(_errors(cfg), [], f"{mode} 应被接受")


class TestScalarTypeChecks(unittest.TestCase):
    def test_pause_key_non_string_rejected(self):
        cfg = _cfg()
        cfg["pause_key"] = 117      # 数字键码
        errs = _errors(cfg)
        self.assertTrue(any("pause_key" in e for e in errs),
                        f"pause_key 类型错误未被报出: {errs}")

    def test_kick_confirm_non_bool_rejected(self):
        cfg = _cfg()
        cfg["kick_confirm"] = "yes"
        errs = _errors(cfg)
        self.assertTrue(any("kick_confirm" in e for e in errs),
                        f"kick_confirm 类型错误未被报出: {errs}")

    def test_skip_ranks_unknown_rank_is_warning_not_error(self):
        """未知等级只是 warning(可能是用户新加的自定义等级),不该拦启动。"""
        cfg = _cfg()
        cfg["skip_ranks"] = ["不存在的等级"]
        issues = validate_config(cfg)
        errs = [m for lvl, m in issues if lvl == "error"]
        warns = [m for lvl, m in issues if lvl == "warning"]
        self.assertEqual(errs, [], f"未知等级不该报 error: {errs}")
        self.assertTrue(any("不存在的等级" in w for w in warns),
                        f"未知等级应有 warning: {warns}")


class TestContributionRange(unittest.TestCase):
    """debug.simulation.contribution_range 的三类坏形状。"""

    def _with_range(self, value):
        cfg = _cfg()
        cfg["debug"]["simulation"]["contribution_range"] = value
        return _errors(cfg)

    def test_wrong_length_rejected(self):
        for bad in ([1], [1, 2, 3], "0-50000"):
            errs = self._with_range(bad)
            self.assertTrue(any("contribution_range" in e for e in errs),
                            f"{bad!r} 未被拒绝: {errs}")

    def test_non_numeric_element_rejected(self):
        """元素是字符串时不能去做 > 比较 —— 那会 TypeError 把校验打崩。"""
        errs = self._with_range(["0", "50000"])
        self.assertTrue(any("contribution_range" in e for e in errs),
                        f"字符串元素未被拒绝: {errs}")

    def test_bool_element_rejected(self):
        """bool 是 int 子类,true/false 不该被当成 0/1 通过。"""
        errs = self._with_range([False, True])
        self.assertTrue(any("contribution_range" in e for e in errs),
                        f"布尔元素未被拒绝: {errs}")

    def test_min_greater_than_max_rejected(self):
        errs = self._with_range([50000, 0])
        self.assertTrue(any("contribution_range" in e for e in errs),
                        f"min>max 未被拒绝: {errs}")

    def test_valid_range_accepted(self):
        self.assertEqual(self._with_range([0, 50000]), [])


class TestGameLogSelfReadGuard(unittest.TestCase):
    """game_log 不得指向脚本自己的日志 —— 否则读到自己写的输出,解析器被污染。"""

    def test_pointing_at_own_latest_log_rejected(self):
        """构造"正好是脚本自己写的那个 latest.log"的绝对路径。"""
        from guild_auto.config import schema as schema_mod

        cfg = _cfg()
        log_dir = cfg["log"]["dir"]
        # schema 用的是 os.path.dirname(os.path.abspath(__file__)) 作为脚本目录基准,
        # 这里照同样的算法构造,确保落进精确比对分支。
        script_dir = os.path.dirname(os.path.abspath(schema_mod.__file__))
        cfg["game_log"] = os.path.abspath(os.path.join(script_dir, log_dir, "latest.log"))

        errs = _errors(cfg)
        self.assertTrue(any("game_log" in e for e in errs),
                        f"指向自身 latest.log 未被拒绝: {errs}")

    def test_normal_external_path_accepted(self):
        cfg = _cfg()
        cfg["game_log"] = r"C:\Path\To\Minecraft\logs\latest.log"
        self.assertEqual(_errors(cfg), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
