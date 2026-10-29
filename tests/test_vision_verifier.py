# -*- coding: utf-8 -*-
"""
test_vision_verifier.py —— 操作结果验证
=======================================
验收(docs/UI识别模式详解.md §四 / §十二):
  * 升职/降职按「玩家名 + 等级」匹配聊天回显;
  * 踢出回显不含玩家名 —— **不得单凭「踢出成功」归因**;
    格位消失 / 人数对账 / 时间窗 三种交叉验证各自可判定;
  * 都不成立时标记「执行结果待确认」;
  * 权限不足提示 → 失败并告警。
"""

import unittest

from guild_auto.vision.verifier import (
    UNCONFIRMED,
    ChatLine,
    verify_kick,
    verify_rank_change,
)


def reader(*lines):
    return lambda: list(lines)


class TestRankChange(unittest.TestCase):
    def test_promote_matches_player_and_rank(self):
        result = verify_rank_change(
            player="张三",
            expected_rank="高活跃成员",
            chat_reader=reader("成功设置张三的职位为高活跃成员!"),
        )
        self.assertTrue(result.ok)

    def test_demote_matches_player_and_rank(self):
        result = verify_rank_change(
            player="李四",
            expected_rank="入门成员",
            chat_reader=reader("成功设置李四的职位为入门成员!"),
        )
        self.assertTrue(result.ok)

    def test_other_player_echo_does_not_count(self):
        result = verify_rank_change(
            player="张三",
            expected_rank="高活跃成员",
            chat_reader=reader("成功设置李四的职位为高活跃成员!"),
        )
        self.assertFalse(result.ok)

    def test_wrong_rank_does_not_count(self):
        result = verify_rank_change(
            player="张三",
            expected_rank="高活跃成员",
            chat_reader=reader("成功设置张三的职位为入门成员!"),
        )
        self.assertFalse(result.ok)

    def test_permission_denied_fails_with_alert(self):
        result = verify_rank_change(
            player="张三",
            expected_rank="高活跃成员",
            chat_reader=reader("你只能修改低于自己职位的公会成员职位!"),
        )
        self.assertFalse(result.ok)
        self.assertTrue(result.alert)


class TestKickNeverBlindlyAttributed(unittest.TestCase):
    def test_echo_alone_is_not_enough(self):
        """只有「踢出成功」三个字(无格位/人数/时间窗证据)→ 待确认。"""
        result = verify_kick(method="any", chat_reader=reader("踢出成功"))
        self.assertFalse(result.ok)
        self.assertIn(UNCONFIRMED, result.reason)

    def test_no_evidence_at_all_is_unconfirmed(self):
        result = verify_kick(method="any")
        self.assertFalse(result.ok)
        self.assertIn(UNCONFIRMED, result.reason)

    def test_permission_denied_fails_with_alert(self):
        result = verify_kick(
            method="any", chat_reader=reader("你只能修改低于自己职位的公会成员职位!")
        )
        self.assertFalse(result.ok)
        self.assertTrue(result.alert)


class TestKickCrossChecks(unittest.TestCase):
    def test_grid_disappeared_confirms(self):
        result = verify_kick(method="grid", member_gone=True)
        self.assertTrue(result.ok)

    def test_grid_still_present_does_not_confirm(self):
        result = verify_kick(method="grid", member_gone=False)
        self.assertFalse(result.ok)
        self.assertIn(UNCONFIRMED, result.reason)

    def test_count_decrease_confirms(self):
        result = verify_kick(method="count", count_before=280, count_after=279)
        self.assertTrue(result.ok)

    def test_count_unchanged_does_not_confirm(self):
        result = verify_kick(method="count", count_before=280, count_after=280)
        self.assertFalse(result.ok)

    def test_count_missing_does_not_confirm(self):
        result = verify_kick(method="count", count_before=280)
        self.assertFalse(result.ok)
        self.assertIn(UNCONFIRMED, result.reason)

    def test_time_window_single_echo_confirms(self):
        result = verify_kick(
            method="time_window",
            chat_reader=reader(ChatLine("踢出成功", timestamp=100.5)),
            operation_ts=100.0,
        )
        self.assertTrue(result.ok)

    def test_time_window_echo_before_operation_does_not_confirm(self):
        result = verify_kick(
            method="time_window",
            chat_reader=reader(ChatLine("踢出成功", timestamp=99.0)),
            operation_ts=100.0,
        )
        self.assertFalse(result.ok)

    def test_time_window_ambiguous_echoes_do_not_confirm(self):
        result = verify_kick(
            method="time_window",
            chat_reader=reader(
                ChatLine("踢出成功", timestamp=100.5),
                ChatLine("踢出成功", timestamp=101.0),
            ),
            operation_ts=100.0,
        )
        self.assertFalse(result.ok)
        self.assertIn(UNCONFIRMED, result.reason)

    def test_time_window_without_start_time_is_unconfirmed(self):
        result = verify_kick(
            method="time_window", chat_reader=reader(ChatLine("踢出成功", timestamp=100.5))
        )
        self.assertFalse(result.ok)
        self.assertIn(UNCONFIRMED, result.reason)

    def test_any_confirms_when_any_check_holds(self):
        result = verify_kick(method="any", count_before=10, count_after=9)
        self.assertTrue(result.ok)

    def test_any_does_not_confirm_when_all_checks_fail(self):
        result = verify_kick(
            method="any",
            chat_reader=reader("踢出成功"),
            member_gone=False,
            count_before=10,
            count_after=10,
        )
        self.assertFalse(result.ok)
        self.assertIn(UNCONFIRMED, result.reason)

    def test_invalid_method_rejected(self):
        with self.assertRaises(ValueError):
            verify_kick(method="guess")


if __name__ == "__main__":
    unittest.main(verbosity=2)
