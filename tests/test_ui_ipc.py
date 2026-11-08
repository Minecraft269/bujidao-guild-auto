# -*- coding: utf-8 -*-
"""test_ui_ipc.py —— 核侧命名管道 JSON-RPC 的协议与真机往返。

两层证据：
  1. handle_line 纯协议层：握手载荷、token 校验、未知方法、坏 JSON；
  2. 真实管道往返：起 IpcServer 线程 + 用 Python 的 open() 当客户端，
     断言 hello 应答字段齐全 —— 这一步不依赖 GUI，能独立证明握手链路通。
"""

import json
import os
import threading
import unittest

from guild_auto.ui.ipc import IpcError, IpcServer


def _handlers(seen=None):
    def hello(params):
        if seen is not None:
            seen.append(params)
        return {
            "variant": "Lite-Async-Gui-Vision",
            "capabilities": ["core", "async", "gui", "vision"],
            "plugin_version": "1.0.0",
            "config_schema_version": 1,
        }

    return {"hello": hello}


class TestProtocol(unittest.TestCase):
    def test_hello_returns_variant_and_capabilities(self):
        srv = IpcServer(_handlers())
        reply = srv.handle_line(json.dumps({"id": 1, "method": "hello", "token": srv.token}))
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["id"], 1)
        self.assertEqual(reply["result"]["variant"], "Lite-Async-Gui-Vision")
        self.assertEqual(reply["result"]["capabilities"], ["core", "async", "gui", "vision"])
        self.assertIn("config_schema_version", reply["result"])

    def test_wrong_token_is_rejected_and_stops(self):
        srv = IpcServer(_handlers())
        reply = srv.handle_line(json.dumps({"id": 2, "method": "hello", "token": "deadbeef"}))
        self.assertFalse(reply["ok"])
        self.assertIn("token", reply["error"])
        self.assertTrue(srv._stop.is_set(), "token 不对必须停止服务，不能继续响应")

    def test_unknown_method_reports_name(self):
        srv = IpcServer(_handlers())
        reply = srv.handle_line(json.dumps({"id": 3, "method": "nope", "token": srv.token}))
        self.assertFalse(reply["ok"])
        self.assertIn("nope", reply["error"])

    def test_invalid_json_does_not_crash(self):
        srv = IpcServer(_handlers())
        reply = srv.handle_line("{not json")
        self.assertFalse(reply["ok"])
        self.assertIn("JSON", reply["error"])

    def test_handler_exception_text_is_passed_through(self):
        def boom(_params):
            raise ValueError("配置校验失败: 字段 ui.theme 取值非法")

        srv = IpcServer({"save_config": boom})
        reply = srv.handle_line(json.dumps({"id": 4, "method": "save_config", "token": srv.token}))
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["error"], "配置校验失败: 字段 ui.theme 取值非法")


class TestQuit(unittest.TestCase):
    """quit 分支（R-45）：GUI 托盘「退出」→ 核自己收尾。

    P0-4 自述靠仓库外脚本 + 实机取证，仓库内零覆盖 —— 这里补三种
    GUI 侧真正会碰到的结果：正常退出 / 核已退出（回调不可用或收尾失败）/ 管道已断。
    """

    def test_quit_normal_calls_on_quit_and_answers_bye(self):
        called = []
        srv = IpcServer(_handlers())
        srv.on_quit = lambda: called.append("quit")
        reply = srv.handle_line(json.dumps({"id": 9, "method": "quit", "token": srv.token}))
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["id"], 9)
        self.assertEqual(reply["result"], {"bye": True})
        self.assertEqual(called, ["quit"], "quit 必须真的触发核侧收尾回调")

    def test_quit_when_core_already_gone_answers_ok_false(self):
        # (a) 收尾回调抛异常：核已进入退出流程 / 收尾失败 → 不能回 ok=true
        def boom():
            raise RuntimeError("核已在退出流程中")

        srv = IpcServer(_handlers())
        srv.on_quit = boom
        reply = srv.handle_line(json.dumps({"id": 10, "method": "quit", "token": srv.token}))
        self.assertFalse(reply["ok"])
        self.assertIn("核退出失败", reply["error"])

        # (b) 回调没注册（旧核 / 核已退出，入口来不及挂）：落到未知方法，同样不能假装成功
        old = IpcServer(_handlers())
        reply2 = old.handle_line(json.dumps({"id": 11, "method": "quit", "token": old.token}))
        self.assertFalse(reply2["ok"])
        self.assertIn("未知方法", reply2["error"])

    def test_quit_reply_over_broken_pipe_raises(self):
        # 核已收尾，但 GUI 那边先没了：写应答必须抛错，不能静默丢
        srv = IpcServer(_handlers())
        srv.on_quit = lambda: None
        reply = srv.handle_line(json.dumps({"id": 12, "method": "quit", "token": srv.token}))
        self.assertTrue(reply["ok"])
        # 从未连上（_handle=None）与句柄已失效（管道断了）都必须抛错，不能静默丢
        with self.assertRaises(IpcError):
            srv._write_line(reply)
        if os.name == "nt":
            srv._handle = 0  # 已断开的句柄：WriteFile 失败 → IpcError
            with self.assertRaises(IpcError):
                srv._write_line(reply)


@unittest.skipUnless(os.name == "nt", "命名管道仅 Windows")
class TestLivePipeRoundTrip(unittest.TestCase):
    """真实管道：服务端线程 + 客户端 write/read 一行，证明 Rust 侧按行读取能对接。"""

    def test_hello_over_real_named_pipe(self):
        seen = []
        srv = IpcServer(_handlers(seen))
        srv.create()
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            with open(srv.path, "r+b", buffering=0) as pipe:
                req = json.dumps({"id": 1, "method": "hello", "token": srv.token}) + "\n"
                pipe.write(req.encode("utf-8"))
                raw = b""
                while b"\n" not in raw:
                    chunk = pipe.read(4096)
                    if not chunk:
                        break
                    raw += chunk
            reply = json.loads(raw.decode("utf-8").split("\n")[0])
            self.assertTrue(reply["ok"], reply)
            self.assertEqual(reply["result"]["variant"], "Lite-Async-Gui-Vision")
            self.assertEqual(len(seen), 1, "handle 必须被调用一次")
        finally:
            srv.stop()
            t.join(timeout=2.0)

    def test_pipe_name_carries_one_time_token(self):
        a, b = IpcServer(_handlers()), IpcServer(_handlers())
        self.assertNotEqual(a.pipe_name, b.pipe_name, "每次启动必须换 token，禁止固定管道名")
        self.assertTrue(a.pipe_name.startswith("bujidao-"))
        self.assertEqual(len(a.token), 16, "8 字节 → 16 个 hex 字符")


if __name__ == "__main__":
    unittest.main(verbosity=2)
