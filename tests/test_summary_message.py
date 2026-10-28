# -*- coding: utf-8 -*-
"""
test_summary_message.py —— summary 广播的人数统计与文案渲染
=========================================================
这两个函数原住在 main.py,src-layout 迁移时并入 domain/decisions.py。
迁入后一度零测试(核心层覆盖率因此掉了 10 个点),本文件补上。

锁定的业务规则(继承清单 §3):
  1. 人数必须**实统计**,不能用 records 长度代替 ——
     无操作玩家也算进"升/降/踢人数"会让公告谎报。
  2. 一个玩家的动作列表以 actions[0] 为准:promote×N 仍只算"被升职 1 人"。
  3. 文案模板只能来自配置,缺键抛 KeyError ——
     代码兜底串会悄悄覆盖用户改过的文案。
"""

import json
import unittest

from guild_auto.config import DEFAULT_CONFIG
from guild_auto.domain.decisions import build_summary_message, count_actions


def _cfg(**overrides):
    """深拷贝默认配置,按需覆盖。"""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    cfg.update(overrides)
    return cfg


def _rec(player, actions, weekly=1000):
    return {"player": player, "actions": actions, "weekly_total": weekly}


class TestCountActions(unittest.TestCase):
    def test_empty_records(self):
        self.assertEqual(count_actions([]), {"promote": 0, "demote": 0, "kick": 0})

    def test_counts_by_first_action_only(self):
        """以 actions[0] 为准:promote×3 只算 1 人升职。"""
        recs = [
            _rec("甲", ["promote", "promote", "promote"]),
            _rec("乙", ["demote"]),
            _rec("丙", ["kick"]),
        ]
        self.assertEqual(count_actions(recs), {"promote": 1, "demote": 1, "kick": 1})

    def test_records_without_actions_are_not_counted(self):
        """无操作玩家不得计入任何一类 —— 否则公告人数虚高。"""
        recs = [_rec("甲", []), _rec("乙", None), _rec("丙", ["promote"])]
        self.assertEqual(count_actions(recs), {"promote": 1, "demote": 0, "kick": 0})

    def test_missing_actions_key_is_tolerated(self):
        """record 里没有 actions 键时按"无操作"处理,不抛异常。"""
        self.assertEqual(count_actions([{"player": "甲"}])["promote"], 0)

    def test_unknown_action_is_ignored(self):
        """未知动作名不进任何计数(不会 KeyError 也不会污染已知类别)。"""
        self.assertEqual(
            count_actions([_rec("甲", ["teleport"])]), {"promote": 0, "demote": 0, "kick": 0}
        )

    def test_multiple_players_same_action(self):
        recs = [_rec(f"p{i}", ["kick"]) for i in range(5)]
        self.assertEqual(count_actions(recs)["kick"], 5)


class TestBuildSummaryMessage(unittest.TestCase):
    def test_renders_counts_from_config_template(self):
        """数值来自实统计,不是 len(records)。"""
        cfg = _cfg()
        recs = [_rec("甲", ["promote"]), _rec("乙", ["kick"]), _rec("丙", [])]  # 丙 无操作,不计入
        msg = build_summary_message(recs, cfg)
        # 模板原文由配置决定,这里只断言"无操作玩家没被算进人数"
        self.assertIn("1", msg)
        self.assertNotIn("3", msg, f"无操作玩家被算进人数了: {msg}")

    def test_admin_name_is_substituted(self):
        cfg = _cfg(admin="测试管理员")
        msg = build_summary_message([], cfg)
        self.assertIn("测试管理员", msg, f"admin 未代入模板: {msg}")

    def test_missing_admin_falls_back_to_generic_label(self):
        """admin 为空时用中性称呼,不渲染成 'None'。"""
        cfg = _cfg(admin="")
        msg = build_summary_message([], cfg)
        self.assertNotIn("None", msg, f"admin 为空时渲染出了 None: {msg}")

    def test_missing_summary_template_raises_key_error(self):
        """缺模板必须抛 KeyError,不许有代码兜底串。

        兜底串会悄悄覆盖用户改过的文案,让"文案不得改动"这条红线
        失去唯一真源 —— 所以这里断言的是"抛出",不是"用了默认值"。
        """
        cfg = _cfg()
        cfg["notify_templates"] = {}
        with self.assertRaises(KeyError) as cm:
            build_summary_message([], cfg)
        self.assertIn("summary", str(cm.exception))

    def test_missing_notify_templates_section_raises(self):
        cfg = _cfg()
        del cfg["notify_templates"]
        with self.assertRaises(KeyError):
            build_summary_message([], cfg)

    def test_template_comes_from_config_not_code(self):
        """改配置里的模板,输出必须跟着变 —— 证明没有硬编码。"""
        cfg = _cfg()
        cfg["notify_templates"]["summary"] = "升{promote}降{demote}踢{kick}联系人{admin}"
        msg = build_summary_message([_rec("甲", ["promote"])], cfg)
        self.assertEqual(msg, "升1降0踢0联系人管理员")


if __name__ == "__main__":
    unittest.main(verbosity=2)
