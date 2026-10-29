# -*- coding: utf-8 -*-
"""
test_masked_name.py —— 网易屏蔽词(星号名)专项
============================================
真实事故:一次清人运行后,报告【未执行】3 人,其中 2 人的名字全是星号,
原因写着"查询重试耗尽"。根因不是网络抖动,是**本地校验在发命令之前**
就把网易屏蔽词产物(整串星号)当成了注入攻击。

网易客户端会把敏感词(玩家名/聊天/公会名)统一替换成一整串星号,
长度 3~704 不等,同一玩家不同次渲染长度都不同(`/guild list` 某一整段被抹平
时是 704 星;只遮单个玩家时是 8 星)。这是真实存在的合法 UI 显示。

本文件锚住三条边界,任何人改写解析器都会先在这里撞墙:

  A. 不能误伤:整串纯星号名必须能穿过 validate_player_name,
     否则一个 704 星字符串就能把整个花名册炸掉 —— 那一整段约 78 人
     连查询都轮不到。
  B. 安全边界不能松:带星号又带其它字符的混串(`*x*`/`a*b`)仍必须被拒。
     `*` 是 Windows 通配符,继续留在黑名单里挡 glob 注入;只有
     "整串纯星号"被单独放行。
  C. 破坏性操作不得建在猜测上:查询目标名是星号时**必然抛 ParseError**,
     即使 /guild member 面板里恰好也回显了同样长度的星号也不行。
     "谁"在这个面板上根本不可读,拿它的周贡献去升/降/踢就是猜 ——
     宁可漏一人,不可错踢一人。

三层修复各自的落点都有测试:
  第 1 层 parsers.py(根因)  → TestIsMaskedName / ...PassesValidation /
                              ...StillRejected / ...InGuildList /
                              ...NeverYieldsRecord
  第 2 层 main.py(错误回显)  → TestMainQueryMemberEarlyExit /
                              TestMainAsyncVerifyEarlyExit /
                              TestFailureReasonWiringInMain
  第 3 层 可见性报告         → ...test_..._flows_into_action_report

所有数据均为合成值,不含任何真实玩家名/公会名/群号/本机路径。
"""

import ast
import inspect
import os
import sys
import tempfile
import textwrap
import unittest


from guild_auto.infrastructure import logger  # noqa: E402
from guild_auto.domain.decisions import decide_actions  # noqa: E402
from guild_auto.infrastructure.log_watcher import extract_chat_block, split_messages  # noqa: E402
from guild_auto.domain.parsers import (  # noqa: E402
    MASKED_NAME_MAX_LEN,
    ParseError,
    is_masked_name,
    parse_guild_list,
    parse_guild_member,
    validate_player_name,
)
from guild_auto.domain.reporter import write_action_report  # noqa: E402

for _n in (
    "log_debug",
    "log_info",
    "log_warning",
    "log_error",
    "log_critical",
    "log_trace",
    "log_exception",
):
    if not hasattr(logger, _n):
        setattr(logger, _n, lambda *a, **k: None)

RANK_NAMES = {
    "leader": "会长",
    "vice_leader": "副会长",
    "admin": "管理员",
    "high": "高活跃成员",
    "mid": "中等活跃成员",
    "low": "低活跃成员",
    "entry": "入门成员",
}

THRESHOLDS = {"mid_promote": 30000, "mid_demote": 5000, "entry_promote_1": 3500, "entry_kick": 3500}

# 真实样本里的两种长度:整段名单被抹平 → 704;单个玩家被遮 → 8
MASKED_704 = "*" * 704
MASKED_8 = "*" * 8
MASKED_3 = "***"


def _member_block(player="合成玩家甲", weekly_today=0):
    """构造一个 /guild member 面板块(玩家名行可换成星号)。"""
    return [
        "演示公会",
        player,
        "加入时间: 2026-08-15 10:26:58",
        "上次在线: 加载中(???)",
        "公会经验周贡献:",
        f"    今天           : {weekly_today} 公会经验",
        "    2026-10-09: 0 公会经验",
    ]


def _physical_panel_line(guild_name, groups, total=4, online=3):
    """把面板块拼成网易版的**一条物理行**(内部换行是字面 \\n)。

    真实形态:网易客户端把整个 /guild list 面板写成一条日志行,
    block_lines 的换行是字面 "\\n",必须能走 split_messages → extract_chat_block
    → parse_guild_list 这条真实链路 —— 只喂 list[str] 会把"单行 + 字面 \\n"
    这个真实前提漏掉。
    """
    body = [guild_name, ""]
    for title, members in groups:
        body.append(f"-- {title} --")
        body.append(f" {' ● '.join(members)} ●")
        body.append("")
    body += [f"成员总数: {total}", f"在线成员数: {online}"]
    panel = "\n".join(
        [
            "--------------------公会---------------------",
            *body,
            "-------------------------------------------- [C]",
        ]
    )
    return (
        "[148月2026 21:55:41.046] [Render thread/INFO] "
        "[net.minecraft.client.gui.components.ChatComponent/]: [CHAT] " + panel
    )


def _panel_block(guild_name, groups, total=4, online=3):
    """真实形态 → block_lines:走与 test_real_fixtures.py 相同的提取链路。"""
    msgs = split_messages([_physical_panel_line(guild_name, groups, total, online)])
    self_check = [m for m in msgs if m]
    assert len(self_check) == 1, "真实形态必须切成 1 条消息"
    block = extract_chat_block(self_check[0])
    assert block is not None, "未能从真实形态里切出面板"
    return block


class TestIsMaskedName(unittest.TestCase):
    """识别器本身:只认"整串纯星号"。"""

    def test_pure_star_strings_are_masked(self):
        for s in ("*" * 3, "*" * 8, "*" * 68, MASKED_704):
            with self.subTest(length=len(s)):
                self.assertTrue(is_masked_name(s))

    def test_empty_and_none_are_not_masked(self):
        self.assertFalse(is_masked_name(""))
        self.assertFalse(is_masked_name(None))

    def test_mixed_star_names_are_not_masked(self):
        """带星号又带其它字符 → 不是屏蔽词产物,仍走黑名单。"""
        for s in ("*x*", "a*b", " * ", "* *", "x*", "**a"):
            with self.subTest(name=s):
                self.assertFalse(is_masked_name(s))


class TestMaskedNamePassesValidation(unittest.TestCase):
    """A. 屏蔽词名必须能穿过名单校验(否则整个花名册被一条 704 星炸掉)。"""

    def test_704_star_masked_name_accepted(self):
        """整段被遮成 704 星:不能因"超长"被拒。

        真实事故:704 星串触发了 MAX_NAME_LEN 校验,parse_guild_list 抛错
        → main 重发 3 次 /guild list 全炸 → 那一整段约 78 人连 targets
        都不是,连"查一次"都没发生。
        """
        self.assertEqual(validate_player_name(MASKED_704), MASKED_704)

    def test_8_star_masked_name_accepted(self):
        """单个玩家被遮成 8 星:不能因"含非法字符 *"被拒。"""
        self.assertEqual(validate_player_name(MASKED_8), MASKED_8)

    def test_empty_still_rejected(self):
        """空名仍必须拒绝(放行屏蔽名不等于放行空名)。"""
        for bad in ("", None):
            with self.subTest(bad=bad):
                with self.assertRaises(ParseError):
                    validate_player_name(bad)

    def test_overlong_real_name_still_rejected(self):
        """真名字仍守 48 上限 —— 放行的只是"整串纯星号"这一个形态。"""
        with self.assertRaises(ParseError):
            validate_player_name("x" * 49)


class TestMixedStarNamesStillRejected(unittest.TestCase):
    """B. 安全边界不能松:混串仍被黑名单拒绝(glob 注入防线)。"""

    def test_mixed_star_names_rejected(self):
        for bad in ("*x*", "a*b", " * ", "* *"):
            with self.subTest(bad=bad):
                with self.assertRaises(ParseError):
                    validate_player_name(bad)

    def test_other_dangerous_chars_still_rejected(self):
        for bad in ("a/b", "a\\b", "a:b", "a●b", "{player}", "a\nb", "a\tb"):
            with self.subTest(bad=bad):
                with self.assertRaises(ParseError):
                    validate_player_name(bad)

    def test_real_names_with_spaces_unaffected(self):
        """真实样本存在"带空格 名字",放行屏蔽名不得波及它们。"""
        for good in ("暴躁的合成星球跳合成芭蕾", "_合成络绎合成", "演示________演示", "1234000078"):
            with self.subTest(good=good):
                self.assertEqual(validate_player_name(good), good)


class TestMaskedNameInGuildList(unittest.TestCase):
    """A 的落地点:整段被遮的名单必须能被解析出来,而不是炸掉整个面板。"""

    def test_whole_segment_masked_to_704_stars(self):
        """真实形态:"中等活跃成员"整段被抹成一行 704 星(一个 ● 都没有)。"""
        lines = [
            "演示公会",
            "-- 高活跃成员 --",
            "演示高活跃甲 ● 演示高活跃乙 ●",
            "-- 中等活跃成员 --",
            MASKED_704,
            "-- 低活跃成员 --",
            "演示低活跃甲 ●",
            "成员总数: 4",
            "在线成员数: 2",
        ]
        r = parse_guild_list(lines, RANK_NAMES)
        self.assertEqual(r["groups"]["high"], ["演示高活跃甲", "演示高活跃乙"])
        self.assertEqual(r["groups"]["mid"], [MASKED_704], "整段被遮的名单必须保留为一条星号条目")
        self.assertEqual(r["groups"]["low"], ["演示低活跃甲"])

    def test_single_masked_player_among_real_names(self):
        """真实形态:同一段里只有第 20 个条目是 8 星,前后都是正常 ● 分隔。"""
        lines = [
            "演示公会",
            "-- 中等活跃成员 --",
            "合成中等甲awa ● " + MASKED_8 + " ● 合成中等乙_Mc ●",
            "成员总数: 3",
            "在线成员数: 1",
        ]
        r = parse_guild_list(lines, RANK_NAMES)
        self.assertEqual(r["groups"]["mid"], ["合成中等甲awa", MASKED_8, "合成中等乙_Mc"])
        self.assertEqual(len(r["groups"]["mid"]), 3, "屏蔽条目把相邻的真名字吞掉了")

    def test_masked_segment_does_not_swallow_group_title(self):
        """704 星行不得把下一个分组标题也吞进名字里。"""
        lines = [
            "演示公会",
            "-- 中等活跃成员 --",
            MASKED_704,
            "-- 低活跃成员 --",
            "演示低活跃甲 ●",
            "成员总数: 2",
            "在线成员数: 1",
        ]
        r = parse_guild_list(lines, RANK_NAMES)
        self.assertEqual(r["groups"]["low"], ["演示低活跃甲"])
        self.assertNotIn("--", "".join(r["groups"]["mid"]))


class TestMaskedNameNeverYieldsRecord(unittest.TestCase):
    """C. 破坏性操作不得建在猜测上:查询目标名是星号 → 必然 ParseError。"""

    def test_masked_expected_id_with_normal_panel(self):
        with self.assertRaises(ParseError):
            parse_guild_member(_member_block("合成玩家甲"), expected_id=MASKED_8)

    def test_masked_expected_id_with_704(self):
        with self.assertRaises(ParseError):
            parse_guild_member(_member_block("合成玩家甲"), expected_id=MASKED_704)

    def test_masked_expected_id_with_reject_echo(self):
        """真实回显:查询被服务端拒绝,面板只有"该玩家不在公会中!"一行。"""
        with self.assertRaises(ParseError):
            parse_guild_member(["该玩家不在公会中!"], expected_id=MASKED_8)

    def test_masked_expected_id_when_panel_row_also_masked(self):
        """最危险的一条:面板里玩家名行也恰好被遮成**同样长度**的星号。

        此时"expected_id 出现在面板里"的存在性检查会误判为"面板证实了此人存在",
        进而返回一个带周贡献的记录 → 上游 decide_actions 可能对它下达
        降职/踢出指令。网易的星号长度随渲染上下文浮动,同长度并非不可能。
        这里必须抛错,宁可漏一人,不可错踢一人。
        """
        panel = _member_block(player=MASKED_8, weekly_today=0)
        with self.assertRaises(ParseError):
            parse_guild_member(panel, expected_id=MASKED_8)

    def test_normal_player_with_masked_name_row_still_parses(self):
        """反向护栏:真实名查询,面板玩家名行被遮成 8 星 —— 面板本身可读,
        不应因为面板被遮就拒绝(存在的星号串与真实名无关,存在性检查本就
        不会命中真实名,仍按"未找到目标玩家"失败)。"""
        with self.assertRaises(ParseError):
            parse_guild_member(_member_block(player=MASKED_8), expected_id="合成玩家甲")

    def test_masked_player_cannot_reach_kick_decision(self):
        """端到端护栏:屏蔽名玩家永远拿不到可判定的记录。

        直接锁死"星号名 → decide_actions → kick"这条链:解析层抛错后,
        主流程拿不到 record,判定根本不会被调用。
        """
        panel = _member_block(player=MASKED_8, weekly_today=0)
        with self.assertRaises(ParseError):
            parse_guild_member(panel, expected_id=MASKED_8)
        # 若哪天解析层被改松了,这一行就让 kick 落在这个不可读的名字上
        # (阈值用真实默认值:entry < 3500 → 踢出)
        actions, _ = decide_actions("entry", 0, THRESHOLDS)
        self.assertEqual(actions, ["kick"], "test 自身的阈值假设变了?这条只是把危险链显式化")

    def test_masked_name_has_length_cap(self):
        """屏蔽名放行,但**不是无上限**。

        704 星(网易把一整段名单抹平的真实产物)必须放行 ——
        否则整个公会名单解析崩溃;但任意长的星号串会被一路带进
        命令行参数与日志文件。上限由 MASKED_NAME_MAX_LEN 约束。

        真实观测到的最长屏蔽名是 704 字符,所以上限远大于它,
        只拦"非真实面板产物"那种滥用(如从伪造日志喂进 200KB 星号)。
        """
        for n, should_pass in (
            (8, True),
            (704, True),
            (MASKED_NAME_MAX_LEN, True),
            (MASKED_NAME_MAX_LEN + 1, False),
            (200_000, False),
        ):
            with self.subTest(length=n):
                if should_pass:
                    validate_player_name("*" * n)  # 不抛即通过
                else:
                    with self.assertRaises(ParseError):
                        validate_player_name("*" * n)

    def test_masked_name_704_still_parses_guild_list(self):
        """回归护栏:704 星整段不得让整个公会名单解析崩溃。

        这条锁死"真实形态必须放行"这一侧。只看上一条会让人误以为
        收紧上限无代价 —— 收得太紧(让星号撞 48 上限)会让一次真实
        运行里约 78 人名字从未进入 targets。
        """
        lines = [
            "演示公会",
            "-- 高活跃成员 --",
            "正常玩家甲 ● 正常玩家乙 ●",
            "-- 中等活跃成员 --",
            "*" * 704,
            "-- 低活跃成员 --",
            "低活玩家丙 ●",
        ]
        RANK = {
            "leader": "会长",
            "vice_leader": "副会长",
            "admin": "管理员",
            "high": "高活跃成员",
            "mid": "中等活跃成员",
            "low": "低活跃成员",
            "entry": "入门成员",
        }
        r = parse_guild_list(lines, RANK)
        self.assertEqual(r["groups"]["high"], ["正常玩家甲", "正常玩家乙"])
        self.assertEqual(r["groups"]["low"], ["低活玩家丙"])

    def test_blank_line_stops_name_merge(self):
        """空行必须终止折行拼接 —— 否则星号串会吞掉后面的真实玩家名。

        独立审查发现:parse_guild_list 原来把空行全部丢掉,而
        _join_truncated_names 的契约是"空行终止续行"。契约失效后,
        一段被网易抹平的星号串(不以 ● 结尾)会把紧随的真实玩家名
        一并吞掉,拼出既超长又含 * 的名字 → **整张公会名单**解析崩溃。

        修复:空行保留为 None 哨兵传给 _join_truncated_names。

        两侧都测:既不能让星号吞掉真名,也不能让空行本身产出空条目。
        """
        RANK = {
            "leader": "会长",
            "vice_leader": "副会长",
            "admin": "管理员",
            "high": "高活跃成员",
            "mid": "中等活跃成员",
            "low": "低活跃成员",
            "entry": "入门成员",
        }
        for masked_len in (8, 704):
            with self.subTest(masked_len=masked_len):
                lines = [
                    "演示公会",
                    "-- 高活跃成员 --",
                    "正常玩家甲 ●",
                    "-- 中等活跃成员 --",
                    "*" * masked_len,
                    "",  # ← 关键:空行必须在这里断开
                    "真实玩家乙 ●",
                    "-- 低活跃成员 --",
                    "低活玩家丙 ●",
                ]
                r = parse_guild_list(lines, RANK)
                mid = r["groups"]["mid"]
                # 1) 真名必须作为**独立条目**存在(没被吞进星号串)
                self.assertIn("真实玩家乙", mid, f"真实玩家名被星号串吞掉了: {mid}")
                # 2) 不得出现"星号串 + 真名"的拼接产物
                for name in mid:
                    if name != "真实玩家乙":
                        self.assertNotIn("真实玩家乙", name, f"拼接产物泄漏: {name[:40]}...")
                # 3) 其余组的玩家不受影响
                self.assertEqual(r["groups"]["high"], ["正常玩家甲"])
                self.assertEqual(r["groups"]["low"], ["低活玩家丙"])

    def test_blank_line_does_not_create_empty_entry(self):
        """反向护栏:空行本身不得产出空玩家条目。"""
        RANK = {"high": "高活跃成员", "mid": "中等活跃成员"}
        r = parse_guild_list(
            ["演示公会", "-- 高活跃成员 --", "", "玩家甲 ●", "", "-- 中等活跃成员 --", "玩家乙 ●"],
            RANK,
        )
        self.assertEqual(r["groups"]["high"], ["玩家甲"])
        self.assertEqual(r["groups"]["mid"], ["玩家乙"])


class TestMaskedShortAndPartialNames(unittest.TestCase):
    """第 1 层的其余真实形态:短星号 / 部分被遮 / 公会名被遮 / 合法含 * 名。

    真实日志里同一批数据混着四种形态:整段 704 星、单条 8 星、只把名字
    **某个词**遮成 3 星(如"正常名***尾巴")、以及名字本来就合法含 * 的玩家。
    前三种是网易屏蔽词产物,最后一种是真实玩家名 —— 边界必须画在
    "整串纯星号"上,不能把合法含 * 的玩家一起误判。
    """

    def test_short_three_star_mask_treated_as_masked(self):
        """真实形态:只遮单个玩家时是 3 星(最短见过 3 个)。"""
        self.assertTrue(is_masked_name(MASKED_3))
        self.assertEqual(validate_player_name(MASKED_3), MASKED_3)

    def test_partial_mask_is_not_masked_and_is_rejected(self):
        """部分名字被替换(如「正常名***尾巴」)→ 不是"整串纯星号",
        仍走黑名单被拒。这类名字发出去也查不到人,必须让查询失败走
        人工确认,而不是拿它当目标名发命令。"""
        for partial in ("正常名***尾巴", "***尾巴", "正***名", "甲**乙**丙"):
            with self.subTest(name=partial):
                self.assertFalse(is_masked_name(partial))
                with self.assertRaises(ParseError):
                    validate_player_name(partial)

    def test_partial_mask_in_guild_list_fails_loudly(self):
        """部分名字被替换(如「正常名***尾巴」)→ 整个名单解析失败(响亮)。

        这种名字不是"整串纯星号",所以不走 is_masked_name 放行,而是撞
        `*` 黑名单被拒。已知取舍:一条部分屏蔽条目会让**整张** /guild list
        抛 ParseError(main 会重发 3 次),而不是让其它玩家静默通过。
        选择"响亮失败"而非"悄悄跳过该条",是因为 glob 注入防线是安全边界
        (Windows 通配符),为可用性放松它会把 `*x*` 这类注入名一起放行。
        响亮失败至少会被运维看见;静默漏人才是事故。
        """
        lines = [
            "演示公会",
            "-- 入门成员 --",
            "演示入门甲 ● 正常名***尾巴 ● 演示入门乙 ●",
            "成员总数: 3",
            "在线成员数: 1",
        ]
        with self.assertRaises(ParseError) as ctx:
            parse_guild_list(lines, RANK_NAMES)
        self.assertIn(
            "非法字符", str(ctx.exception), f"失败原因必须指向黑名单校验,实际: {ctx.exception}"
        )

    def test_partial_mask_never_yields_record_even_with_good_panel(self):
        """部分屏蔽名即使面板回显完美,也拿不到记录 → 不可能被踢。

        query_member 会把面板交给 parse_guild_member(expected_id=该名),
        而 expected_id 先过 validate_player_name —— 混串星号在这里被拒,
        所以无论面板长什么样,记录都拿不到,判定根本不会被调用。
        """
        with self.assertRaises(ParseError):
            parse_guild_member(_member_block("合成玩家甲"), expected_id="正常名***尾巴")

    def test_partial_mask_not_early_exited_in_query_member(self):
        """部分屏蔽名**不**走 main 的 is_masked_name 早退(边界认知)。

        早退只对"整串纯星号"生效;混串名仍然走完整 3 次重试再由
        parse_guild_member 拒掉。这条锁住"早退条件没有被无意放宽成
        所有含 * 的名字"—— 放宽就等于把 glob 注入防线也让给可用性。
        """
        main = _import_main()
        sent = []

        def fake_send_and_capture(watcher, human, hwnd, cmd, timeout, **kw):
            sent.append(cmd)
            return None, False

        orig = main.send_and_capture
        main.send_and_capture = fake_send_and_capture
        self.addCleanup(lambda: setattr(main, "send_and_capture", orig))

        info = main.query_member(_MIN_CFG, _StubWatcher(), _StubHuman(), None, "正常名***尾巴")
        self.assertIsNone(info)
        self.assertEqual(
            len(sent),
            _MIN_CFG["timeouts"]["max_retries"],
            f"混串名应走满 3 次重试,实际 {len(sent)} 次",
        )

    def test_guild_name_itself_masked_does_not_crash_panel(self):
        """真实形态:公会名行被遮成 *** —— 面板仍要能解析出成员。"""
        lines = [
            MASKED_3,
            "-- 入门成员 --",
            "演示入门甲 ●",
            "成员总数: 1",
            "在线成员数: 1",
        ]
        r = parse_guild_list(lines, RANK_NAMES)
        self.assertEqual(r["guild_name"], MASKED_3)
        self.assertEqual(r["groups"]["entry"], ["演示入门甲"])

    def test_legit_name_containing_star_is_not_treated_as_masked(self):
        """反向护栏:名字本来就合法含 * 的玩家不能被误判成"被屏蔽"。"""
        for legit in ("Star*Player", "名*字", "a*b"):
            with self.subTest(name=legit):
                self.assertFalse(is_masked_name(legit))
                with self.assertRaises(ParseError):
                    validate_player_name(legit)

    def test_masked_player_cannot_reach_demote_decision(self):
        """同 test_masked_player_cannot_reach_kick_decision,锚 mid → demote。"""
        panel = _member_block(player=MASKED_8, weekly_today=0)
        with self.assertRaises(ParseError):
            parse_guild_member(panel, expected_id=MASKED_8)
        actions, _ = decide_actions("mid", 0, THRESHOLDS)
        self.assertEqual(actions, ["demote"], "test 自身的阈值假设变了?这条只是把危险链显式化")


class TestMaskedNameInRealShapedPanel(unittest.TestCase):
    """704 星形态必须走**真实单物理行**链路,而不是只喂 list[str]。

    网易版面板是"一条物理行 + 内部字面 \\n",真实事故里那条 704 星
    就是从这种形态里提取出来的。只测 list[str] 会漏掉
    "提取 → 解析"整条链上的回归。
    """

    def test_704_star_segment_survives_real_shaped_extraction(self):
        block = _panel_block(
            "演示公会",
            [
                ("高活跃成员", ["演示高活跃甲", "演示高活跃乙"]),
                ("中等活跃成员", [MASKED_704]),
                ("入门成员", ["演示入门甲"]),
            ],
            total=4,
            online=3,
        )
        r = parse_guild_list(block, RANK_NAMES)
        self.assertEqual(r["groups"]["high"], ["演示高活跃甲", "演示高活跃乙"])
        self.assertEqual(
            r["groups"]["mid"], [MASKED_704], "真实形态下整段被遮的名单必须保留为一条星号条目"
        )
        self.assertEqual(r["groups"]["entry"], ["演示入门甲"])
        self.assertEqual(r["total"], 4)
        self.assertEqual(r["online"], 3)

    def test_partial_mask_in_real_shaped_panel_fails_loudly(self):
        """真实链路下的混串星号 → 响亮失败(glob 防线是安全边界,不让步)。"""
        # 部分替换的条目与 704 星条目同批出现:704 星那条能被 is_masked_name
        # 识别,部分替换那条撞黑名单 → 整张表抛错
        with self.assertRaises(ParseError):
            parse_guild_list(
                _panel_block(
                    "演示公会",
                    [
                        ("高活跃成员", ["演示高活跃甲", "演示高活跃乙"]),
                        ("中等活跃成员", ["正常名***尾巴", MASKED_704]),
                    ],
                    total=3,
                    online=2,
                ),
                RANK_NAMES,
            )

    def test_normal_players_unaffected_by_masked_segment_in_real_panel(self):
        """混合面板:被屏蔽条目不能让同一批里的正常玩家消失。"""
        block = _panel_block(
            "演示公会",
            [
                ("中等活跃成员", ["演示中等甲", MASKED_8, "演示中等乙"]),
                ("低活跃成员", ["演示低活跃甲", "演示低活跃乙"]),
            ],
            total=5,
            online=2,
        )
        r = parse_guild_list(block, RANK_NAMES)
        self.assertEqual(r["groups"]["mid"], ["演示中等甲", MASKED_8, "演示中等乙"])
        self.assertEqual(r["groups"]["low"], ["演示低活跃甲", "演示低活跃乙"])
        # 整张表仍有正常玩家可查(main 的阶段2 targets 非空)
        normal = [p for g in r["groups"].values() for p in g if not is_masked_name(p)]
        self.assertIn("演示中等甲", normal)
        self.assertIn("演示低活跃乙", normal)

    def test_block_start_and_end_lines_stripped(self):
        """守卫 fixture 本身:分隔线/复制标记必须已被剥掉,否则测的不是真实链。"""
        block = _panel_block("演示公会", [("入门成员", [MASKED_3])], total=1, online=1)
        joined = "\n".join(block)
        self.assertNotIn("--------------------", joined)
        self.assertNotIn("[C]", joined)
        self.assertEqual(block[0], "演示公会")


def _import_main():
    """导入真实 main.py(不 import 会跳过本组测试 —— main 依赖 pynput/pyautogui)。"""
    try:
        from guild_auto import main

        return main
    except Exception as e:  # pragma: no cover - 无 GUI 依赖时跳过
        raise unittest.SkipTest(f"无法导入 main.py(缺 GUI 依赖?): {e}")


class _StubHuman:
    """最小 HumanInput 替身:query_member 只用到 wait_between_commands。"""

    def wait_between_commands(self):
        pass


class _StubWatcher:
    """最小 LogWatcher 替身:query_member 只用到 discard_buffer。"""

    def discard_buffer(self):
        pass


_MIN_CFG = {
    "commands": {"guild_list": "/guild list", "guild_member": "/guild member {player}"},
    "timeouts": {"max_retries": 3, "response_wait": 0.1, "try_wait": 0.1, "try_times": 1},
    "switches": {"verbose_log": False},
    "rank_names": {"entry": "入门成员"},
    "guild_name": "",
    "skip_ranks": [],
}


class TestMainQueryMemberEarlyExit(unittest.TestCase):
    """第 2 层:query_member 必须在**发命令之前**就判定屏蔽名不可用。

    真实事故:屏蔽名玩家发了 3 次 /guild member,每次都被服务端拒,
    报告写"查询重试耗尽",看起来像网络抖动。修法是早退,不发命令。
    """

    def setUp(self):
        self.main = _import_main()
        self.cmds = []
        self.logs = []

        def fake_send_and_capture(watcher, human, hwnd, cmd, timeout, **kw):
            self.cmds.append(cmd)
            return None, False  # 永远超时:正常玩家应发满 max_retries 次

        # 猴子补丁:不 key 真模块状态,tearDown 显式还原
        self._orig = self.main.send_and_capture
        self.main.send_and_capture = fake_send_and_capture
        self.addCleanup(self._restore)
        # logger 换成记录器:test_logger_levels_mock 会把真实 handler 指向
        # 已删除的临时目录,直接用真实 logger 会 FileNotFoundError(测试顺序污染)
        self._orig_log = logger.log_error
        logger.log_error = lambda msg, *a, **k: self.logs.append(str(msg))
        self.addCleanup(lambda: setattr(logger, "log_error", self._orig_log))

    def _restore(self):
        self.main.send_and_capture = self._orig

    def test_masked_pid_sends_zero_commands(self):
        info = self.main.query_member(_MIN_CFG, _StubWatcher(), _StubHuman(), None, MASKED_8)
        self.assertIsNone(info, "屏蔽名查询必须返回 None(不可用)")
        self.assertEqual(self.cmds, [], f"屏蔽名查询不该发任何命令,却发了 {self.cmds}")
        # 早退必须留痕:否则运维在日志里看不到为什么跳过
        self.assertTrue(self.logs, "屏蔽名跳过时没有写任何错误日志")
        self.assertIn("屏蔽词", self.logs[0])

    def test_704_star_pid_sends_zero_commands(self):
        info = self.main.query_member(_MIN_CFG, _StubWatcher(), _StubHuman(), None, MASKED_704)
        self.assertIsNone(info)
        self.assertEqual(self.cmds, [])

    def test_partial_mask_pid_sends_three_times(self):
        """部分星号名**不**走早退:早退只覆盖"整串纯星号"。

        边界认知:混串名仍然走完整 3 次重试(每次都被 parse_guild_member 拒),
        最后仍返回 None。这条与 test_masked_pid_sends_zero_commands 合起来,
        锁住早退条件没被无意放宽成"所有含 * 的名字"—— 放宽就等于把
        glob 注入防线也让给可用性。
        """
        info = self.main.query_member(_MIN_CFG, _StubWatcher(), _StubHuman(), None, "正常名***尾巴")
        self.assertIsNone(info)
        self.assertEqual(
            len(self.cmds),
            _MIN_CFG["timeouts"]["max_retries"],
            f"混串名应走满 3 次重试,实际 {len(self.cmds)} 次",
        )

    def test_normal_pid_still_sends_three_times(self):
        """回归护栏:正常玩家的 3 次重试时序未被早退改变。"""
        info = self.main.query_member(_MIN_CFG, _StubWatcher(), _StubHuman(), None, "合成玩家甲")
        self.assertIsNone(info)
        self.assertEqual(
            len(self.cmds),
            _MIN_CFG["timeouts"]["max_retries"],
            f"正常玩家应发满 3 次,实际 {len(self.cmds)} 次",
        )
        self.assertTrue(
            all("合成玩家甲" in c for c in self.cmds), f"发出的命令不是查这个玩家: {self.cmds}"
        )


class TestFailureReasonWiringInMain(unittest.TestCase):
    """第 2 层的报告分流:屏蔽名报真实原因,正常玩家仍报"查询重试耗尽"。

    直接读 main.main 的源码结构 —— 这条分流在 700 行的编排函数内部,
    跑不了端到端(要窗口+日志),但它必须是可发现的:任何人把
    `查询重试耗尽` 挪回去,这条测试就红。
    """

    @classmethod
    def setUpClass(cls):
        cls.main = _import_main()
        cls.src = textwrap.dedent(inspect.getsource(cls.main.main))
        cls.tree = ast.parse(cls.src)

    def _branch_texts(self):
        """收集 main() 源码里所有含'屏蔽词'的字符串字面量。"""
        return [
            n.value
            for n in ast.walk(self.tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and "屏蔽词" in n.value
        ]

    def test_failure_reason_branches_on_masked_name(self):
        texts = self._branch_texts()
        self.assertTrue(texts, "main() 里找不到'屏蔽词'分支(报告分流被删了?)")
        joined = "\n".join(texts)
        self.assertIn("需人工确认", joined, "屏蔽名失败原因必须写明'需人工确认'")
        # 分流必须是 IfExp:屏蔽名 reason + 正常玩家 reason 同时存在,
        # 而不是把"查询重试耗尽"替换掉(替换会让正常玩家失去原有文案)
        self.assertTrue(
            any(
                isinstance(n, ast.IfExp) or isinstance(n, ast.If)
                for n in ast.walk(self.tree)
                if "屏蔽词" in ast.dump(n)
            ),
            "屏蔽词 reason 必须与'查询重试耗尽'构成条件分支",
        )
        normal_reasons = [
            n.value
            for n in ast.walk(self.tree)
            if isinstance(n, ast.Constant)
            and isinstance(n.value, str)
            and "查询重试耗尽" in n.value
        ]
        self.assertTrue(normal_reasons, "main() 里正常玩家的'查询重试耗尽'分支被删了")

    def test_masked_name_failure_reason_flows_into_action_report(self):
        """可见性回归:该 reason 必须显示在报告【未执行】区。

        真实事故里报告只写"查询重试耗尽",运维看不出是网易过滤还是网络抖动。
        main.py 把 failures 展开成 action_records(补 actions=[] 与 error=reason),
        reporter 只负责原样印出 —— 这条按 main 的同一种展开方式构造输入,
        把 failures → report 的最后一环也锚住。
        """
        failures = [
            {
                "player": MASKED_8,
                "rank": "entry",
                "reason": "查询失败: 玩家名被网易屏蔽词过滤(星号),无法确认身份,需人工确认",
            }
        ]
        # 与 main.py 报告前对 failures 的展开方式一致
        action_records = [
            dict(f, new_rank=f.get("rank"), actions=[], error=f.get("reason") or "查询失败")
            for f in failures
        ]
        with tempfile.TemporaryDirectory() as d:
            p = write_action_report(
                d, "演示公会", action_records, {"contact_text": "联系人{admin}", "admin": "管理员X"}
            )
            content = open(p, encoding="utf-8").read()
        self.assertIn("【未执行】", content, f"失败原因没进报告【未执行】区:\n{content}")
        self.assertIn("屏蔽词", content)
        self.assertIn("需人工确认", content)
        self.assertIn(MASKED_8, content)
        # 屏蔽名玩家不参与判定,公告里不得出现它的"踢出"计数
        self.assertNotIn("被踢出（1人）", content)
        self.assertIn("被踢出（0人）", content)


class TestDuplicatedPanelCopies(unittest.TestCase):
    """一条 [CHAT] 消息里同一面板被重复写入多次 → 只能按最后一份解析。

    真实事故(验收 25"全员名册零遗漏、对账差值 0"):网易版把 /guild list 面板在
    同一条消息里写了 3 份,前两份的整行名字被遮成星号;合并解析时那行被当成额外
    成员,名册 201 > 面板声明 200,报告直接判"对账差 1,报告缺陷"。
    """

    def test_three_copies_yield_one_roster_with_zero_difference(self):
        """真实的"重复三份"形态:前两份整行名字被遮成星号、第三份才是真名。

        注意不能三份逐字相同 —— 那样 dict.fromkeys 去重后本来就是 3 人,
        这条用例就抓不到回归(独立复查指出)。必须让前两份的内容**不同**,
        合并解析才会多出幻影成员(5 人 vs 声明 3 人)。
        """
        masked = "*" * 70
        masked_block = _panel_block(
            "演示公会", [("会长", [masked]), ("中等活跃成员", [masked])], total=3, online=1
        )
        real_block = _panel_block(
            "演示公会", [("会长", ["甲"]), ("中等活跃成员", ["乙", "丙"])], total=3, online=1
        )
        tripled = masked_block + masked_block + real_block
        r = parse_guild_list(tripled, RANK_NAMES)
        names = [n for v in r["groups"].values() for n in v]
        self.assertEqual(r["total"], 3)
        self.assertEqual(len(names), 3, f"重复面板不得产生幻影成员(实际 {len(names)} 个): {names}")
        self.assertEqual(sorted(names), ["丙", "乙", "甲"])

    def test_unregistered_group_title_keeps_panel_totals(self):
        """未登记职位标题下的成员无法归类,但「成员总数/在线成员数」是面板级元数据,
        不能连着丢掉 —— 丢了对账永远算不出差值,报告会伪装成"无数据"。"""
        lines = [
            "演示公会",
            "",
            "-- 会长 --",
            "甲 ●",
            "",
            "-- 未知职位 --",
            "乙 ●",
            "",
            "成员总数: 9",
            "在线成员数: 2",
        ]
        r = parse_guild_list(lines, RANK_NAMES)
        self.assertEqual(r["groups"]["leader"], ["甲"])
        self.assertEqual(r["total"], 9, "未登记职位不得连带丢掉成员总数")
        self.assertEqual(r["online"], 2, "未登记职位不得连带丢掉在线成员数")


if __name__ == "__main__":
    unittest.main(verbosity=2)
