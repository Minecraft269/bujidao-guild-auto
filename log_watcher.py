# -*- coding: utf-8 -*-
"""
log_watcher.py —— 日志钩子模块
===============================
职责:增量监控游戏日志文件(latest.log,类 tail -f),提供
  * 编码自动检测(utf-8 → gbk → gb2312,兼容网易版客户端)
  * 新行读取(含文件轮转处理)
  * "发命令→等待响应块→超时" 原语(块 = 两条分隔线之间)
"""
import logger
import os
import re
import time

# ---------------------------------------------------------------------------
# 编码检测
# ---------------------------------------------------------------------------
_ENCODING_CANDIDATES = ("utf-8", "gbk", "gb2312")


def detect_encoding(path, preferred="auto"):
    """检测日志文件编码。
    - preferred 非 auto 时直接使用(不检测);
    - auto:在文件 头部/1/4/1/2/3/4/尾部 多处采样拼接,依次尝试 utf-8/gbk/gb2312 严格解码,
      第一个成功者胜;全部失败则回退 gbk + errors=replace(尽力解码中文)。
    多处采样防止"开头长段纯 ASCII、中文在后"的文件被误判为 utf-8。
    """
    if preferred != "auto":
        return preferred
    try:
        size = os.path.getsize(path)
        positions = sorted({0, size // 4, size // 2, size * 3 // 4, max(0, size - 65536)})
        chunks = []
        for pos in positions:
            with open(path, "rb") as f:
                f.seek(pos)
                chunks.append(f.read(65536))
        sample = b"".join(chunks)
    except OSError:
        return "gbk"
    for enc in _ENCODING_CANDIDATES:
        try:
            sample.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return "gbk"


# ---------------------------------------------------------------------------
# 日志钩子
# ---------------------------------------------------------------------------
class LogWatcher:
    """增量读取日志文件。用法:
        w = LogWatcher(path)
        w.open()
        lines = w.read_new_lines()
    或配合 wait_for_block 等待命令响应块。
    """

    def __init__(self, path, encoding="auto", poll_interval_ms=300):
        self.path = path
        self.encoding = encoding
        self.poll = poll_interval_ms / 1000.0
        self._fh = None
        self._size = 0
        self._skip_until_ts = 0  # first_command 等新回显时设 = time.time() + 1.0,读到的旧行跳

    # ---- 打开 ----
    def open(self, wait_seconds=60):
        """打开日志文件;若文件不存在则轮询等待(wait_seconds 内)。"""
        deadline = time.time() + wait_seconds
        while not os.path.exists(self.path):
            if time.time() > deadline:
                raise FileNotFoundError(
                    f"游戏日志不存在: {self.path}\n请确认游戏已启动且路径配置正确(--log 或 config.json 的 game_log)。")
            time.sleep(1.0)
        self.encoding = detect_encoding(self.path, self.encoding)
        self._fh = open(self.path, "rb")
        # 跳到末尾:只关心本次运行之后的新日志
        self._fh.seek(0, os.SEEK_END)
        self._size = self._fh.tell()

    def close(self):
        if self._fh:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

    # ---- 读取新行 ----
    def discard_buffer(self):
        """first_command=True 时调用:丢弃"前次命令残留"的日志。
        关闭句柄+重开+seek 到文件末尾,让 wait_for_chat_block 只看到本轮 paste 后的回显。
        """
        size_before = self._size
        try:
            if self._fh is not None:
                self._fh.close()
                self._fh = None
        except Exception:
            pass
        # 重新打开并 seek 到末尾(否则 read_new_lines 默认从 0 读会读到 launch 前所有内容)
        try:
            self._fh = open(self.path, "rb")
            if os.path.getsize(self.path) > 0:
                self._fh.seek(0, 2)  # SEEK_END
                self._size = self._fh.tell()
            else:
                self._size = 0
            logger.log_debug(
                f"[log_watcher] discard_buffer:size {size_before}→{self._size} "
                f"(跳过了 launch 前 {self._size - size_before} 字节 = "
                f"{self._size - size_before}B 文件增长)"
            )
        except Exception as e:
            logger.log_debug(f"discard_buffer 重开异常: {e}")
            self._size = 0

    def _reopen_if_rotated(self):
        """检测轮转(latest.log 被改名/重建):文件不存在或变小 → 重新打开。"""
        if not os.path.exists(self.path):
            self.close()
            return True
        size_now = os.path.getsize(self.path)
        if size_now < self._size:
            # 文件被截断/轮转:关闭句柄(read_new_lines 会统一重新打开并读全部)
            self.close()
            self._size = 0
            return True
        return False

    def read_new_lines(self, block=True):
        """读取自上次以来的新行,返回行列表(自动按当前编码解码,忽略坏字节)。
        block=True:至少读到一行才返回(内部轮询等待)。
        first_command=True 启动时调 reader.discard_buffer() 丢弃 launch 前的
        "前次命令残留"日志,避免本轮第一次试探就读到旧回显。
        """
        out = []
        while True:
            if self._fh is None or self._reopen_if_rotated():
                self._fh = open(self.path, "rb")
                # 轮转/重开后从头读新文件(seek 0):
                #  - 新文件是轮转后刚建的,通常很小,无内存尖峰;
                #  - 若 seek 到末尾会连"重开前已写入的首批新日志"一起跳过(丢行);
                #  - 旧文件场景(_reopen 因句柄失效而非轮转)由 _reopen_if_rotated
                #    的 size 变小检测区分——真正的大文件全量重读已被该分支拦截。
                self._fh.seek(0)
                self._size = 0
            data = self._fh.read()
            self._size += len(data)
            if data:
                text = data.decode(self.encoding, errors="replace")
                new_lines = text.splitlines()
                out.extend(new_lines)
                # 详细日志:打印读到的每条新行(前 120 字符,GBK 乱码用 ? 替代,纯看增量)
                # ——让用户从 latest.log 直接看到游戏实时输出,便于诊断"脚本读到什么"
                for idx, ln in enumerate(new_lines, 1):
                    preview = ln[:120].replace("\r", "")
                    logger.log_debug(
                        f"[log_watcher] READ +{idx}/{len(new_lines)} "
                        f"len={len(ln)} | {preview!r}"
                    )
            if out or not block:
                return out
            time.sleep(self.poll)

    # ---- 等待响应块(消息级) ----
    def wait_for_chat_block(self, timeout=12.0, max_pending_chars=2_000_000, stop_check=None,
                             min_wait_after_start=0.0):
        """等待并收集一个公会面板响应块。
        stop_check: 可调用对象,返回 True 表示用户希望中断等待。
        用户原话"按下暂停键后不暂停":瞬时触发(False→True→False)不应放弃等待,
        仅持续 True 才放弃(去抖 1 个 poll 周期)。

        min_wait_after_start: 启动后至少等这么多秒才允许返回首个块(防止 first_command
        起步阶段读到 discard_buffer 之前的残留——若文件在 discard 后又有少量"启动期"
        日志延迟写入,这段时间会被错认成响应块)。
        """
        pending = []
        start_ts = time.time()
        deadline = start_ts + timeout
        first_eligible_ts = start_ts + min_wait_after_start
        logger.log_debug(f"等待响应块(最长 {timeout:.1f}s,轮询 {self.poll*1000:.0f}ms,首块最早 {min_wait_after_start:.2f}s 后可返回)...")
        while time.time() < deadline:
            if stop_check and stop_check():
                # 去抖:暂停持续至少 1 个 poll 周期才放弃,避免瞬时 True 立即放弃
                # 修复根因:time.sleep(self.poll) 当作 if 左操作数时返回 None,
                # `None or X` 短路求值会跳过 sleep,必须独立调用确保真正 sleep
                # 修复"Ctrl+C 无反应":用 6×0.05s 子循环(每段检查 stop_check)替代单次 sleep,
                # 让 Python 在 sleep 边界有足够机会处理 SIGINT 中断
                stable_ms = 0
                while stable_ms < int(self.poll * 1000):
                    if stop_check and stop_check():
                        logger.log_debug(f"等待被持续暂停中断,返回 None")
                        return None
                    time.sleep(0.05)
                    stable_ms += 50
            for line in self.read_new_lines(block=False):
                pending.append(line)
                logger.trace_code_location("log_watcher.wait_for_chat_block.got_lines", f"new_lines={len(pending)} total")
            messages = split_messages(pending)
            logger.trace_code_location("log_watcher.wait_for_chat_block.messages", f"msgs={len(messages)} pending_lines={len(pending)}")
            # 详细日志:打印当前消息集合的"首行时间戳 + [CHAT]标记"摘要
            # ——让用户直接看到脚本监听到了哪几条消息、各自是否公会面板
            for mi, m in enumerate(messages, 1):
                head_ts = m[0][:60] if m else "<empty>"
                tag = "CHAT" if any("[CHAT]" in ln for ln in m) else "其他"
                logger.log_debug(
                    f"[log_watcher] MSG #{mi}/{len(messages)} [{tag}] "
                    f"{len(m)} 行 | 首行: {head_ts!r}"
                )
            # 最后一条可能不完整(还没等到其结尾),保留在 pending 中
            if messages and time.time() >= first_eligible_ts:
                for msg in messages[:-1]:
                    block = extract_chat_block(msg)
                    if block is not None:
                        logger.log_debug(f"收到完整响应块({len(block)} 行,"
                                         f"等待 {timeout - (deadline - time.time()):.1f}s)")
                        # 详细日志:打印块首/末行(让用户确认提取出的内容确实是面板)
                        block_head = block[0][:100] if block else "<empty>"
                        block_tail = block[-1][:100] if block else "<empty>"
                        logger.log_debug(
                            f"[log_watcher] BLOCK 首行: {block_head!r}\n"
                            f"[log_watcher] BLOCK 末行: {block_tail!r}"
                        )
                        return block
                # 最后一条若完整,尝试提取(可能本身就是完整消息)
                block = extract_chat_block(messages[-1])
                if block is not None:
                    logger.log_debug(f"收到完整响应块({len(block)} 行,"
                                     f"等待 {timeout - (deadline - time.time()):.1f}s)")
                    block_head = block[0][:100] if block else "<empty>"
                    block_tail = block[-1][:100] if block else "<empty>"
                    logger.log_debug(
                        f"[log_watcher] BLOCK 首行: {block_head!r}\n"
                        f"[log_watcher] BLOCK 末行: {block_tail!r}"
                    )
                    return block
                # 若最后一条是"以块起始线开头但尚未闭合"的面板消息(日志写入中),
                # 必须完整保留等待补齐,否则块中间行会永久丢失;
                # 若是普通聊天消息,只保留消息头即可(防止 pending 无限增长)
                if _starts_with_block(messages[-1]):
                    pending = messages[-1]
                    logger.trace_code_location("log_watcher.waiting_panel_incomplete",
                                               f"{len(pending)} 行待补齐")
                else:
                    pending = messages[-1][:1]
            if sum(len(l) for l in pending) > max_pending_chars:
                pending = pending[-200:]
            # 拆短 sleep + 检查 _force_stop(从 main 模块取;未设时视为 False)
            try:
                from main import _force_stop as _fs
            except Exception:
                _fs = False
            stable_ms = 0
            while stable_ms < int(self.poll * 1000):
                if _fs:
                    logger.log_debug("wait_for_chat_block 检测到 _force_stop(Ctrl+C),返回 None")
                    return None
                time.sleep(0.05)
                stable_ms += 50
        logger.log_debug("wait_for_chat_block 循环退出(超时/停止)")
        logger.log_warning(f"等待响应块超时({timeout:.1f}s 无完整面板),返回 None")
        return None

    def read_until(self, timeout=2.0):
        """读取并返回指定时长内到达的所有新行(用于执行命令后收集回显)。"""
        out = []
        deadline = time.time() + timeout
        collected = 0
        try:
            from main import _force_stop as _fs
        except Exception:
            _fs = False
        while time.time() < deadline:
            if _fs:
                logger.log_debug("read_until 检测到 _force_stop,返回已收集")
                break
            new_lines = self.read_new_lines(block=False)
            out.extend(new_lines)
            collected += len(new_lines)
            # 拆短 sleep + 复检 _force_stop(让 Ctrl+C 立即生效)
            stable_ms = 0
            while stable_ms < int(self.poll * 1000) and not _fs:
                time.sleep(0.05)
                stable_ms += 50
        logger.log_debug(f"回显收集完成: {collected} 行(窗口 {timeout:.1f}s)")
        return out


# ---------------------------------------------------------------------------
# 消息切分与块提取
# 真实日志格式(网易版):整个面板是【单条日志消息】——时间戳前缀只出现在
# 消息首行,面板内部换行无前缀;消息结尾分隔线带复制标记 " [C]"。
# 因此按"时间戳开头"把物理行切分为消息,再从消息内提取块。
# ---------------------------------------------------------------------------
# 消息首行模式: [148月2026 21:23:46.135] [Render thread/INFO] [模块/]: 内容
_MSG_HEAD_RE = re.compile(r"^\[\d+[^\]]*\]\s*\[[^\]]+\]")
# 块起始线: "--------------------公会---------------------"(可能带 " [C]")
# 块结尾线: "--------------------------------------------" (可能带 " [C]")


def is_block_start_line(line):
    """起始分隔线:去掉 '公会'(及尾部 [C])后全为 '-'。"""
    s = line.strip()
    if s.endswith("[C]"):
        s = s[:-3].strip()
    return "公会" in s and set(s.replace("公会", "").replace(" ", "")) == {"-"}


def is_block_end_line(line):
    """结束分隔线:全为 '-' 且不含 '公会'(兼容尾部 ' [C]' 标记)。"""
    s = line.strip()
    if s.endswith("[C]"):
        s = s[:-3].strip()
    return s and set(s) == {"-"} and len(s) >= 20


def split_messages(lines):
    """把物理行列表按"时间戳开头"切分为消息列表(无前缀行归入上一条消息)。
    返回 [ [line, ...], ... ]。"""
    messages = []
    for line in lines:
        if _MSG_HEAD_RE.match(line):
            messages.append([line])
        elif messages:
            messages[-1].append(line)
    return messages


def extract_chat_block(msg_lines):
    """从一条消息的物理行中提取公会面板块。

    规则:[CHAT] 之后的内容,若首行是块起始线,收集到块结尾线为止。
    返回块内容行列表(不含首尾分隔线);不匹配返回 None。

    注意:网易版日志中面板整体是【一条物理行】,内部换行以字面 "\\n"(反斜杠+n)
    写入(实测 184 人面板为单行 2233 字符);标准 Java 版为物理多行。两种都兼容:
    先把字面 "\\n" 还原为真实换行,再按行处理。
    """
    text = "\n".join(msg_lines)
    idx = text.rfind("[CHAT]")
    if idx < 0:
        return None
    content = text[idx + len("[CHAT]"):]
    content = content.replace("\\n", "\n")
    lines = [l.strip() for l in content.splitlines()]
    # 去掉头部空行(保留后续空行:解析器的多行名字拼接依赖空行终止)
    while lines and not lines[0]:
        lines.pop(0)
    if not lines or not is_block_start_line(lines[0]):
        return None
    block = []
    for l in lines[1:]:
        if is_block_end_line(l):
            return block
        block.append(l)
    return None  # 消息内未出现结尾线(异常/被截断)


def _starts_with_block(msg_lines):
    """判断消息是否以公会面板起始线开头(用于识别"写入中"的面板消息)。"""
    text = "\n".join(msg_lines)
    idx = text.rfind("[CHAT]")
    if idx < 0:
        return False
    content = text[idx + len("[CHAT]"):].replace("\\n", "\n")
    for l in content.splitlines():
        if not l.strip():
            continue
        return is_block_start_line(l.strip())
    return False
