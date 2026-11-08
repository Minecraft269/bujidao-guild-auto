# -*- coding: utf-8 -*-
"""
log_watcher.py —— 日志钩子模块
===============================
职责:增量监控游戏日志文件(latest.log,类 tail -f),提供
  * 编码自动检测(utf-8 → gbk → gb2312 → gb18030,兼容网易版客户端)
  * 新行读取(含文件轮转处理)
  * "发命令→等待响应块→超时" 原语(块 = 两条分隔线之间)
"""

from guild_auto.infrastructure import logger
import codecs
import collections
import os
import re
import time


def _force_stop():
    """读取 Ctrl+C 标志(唯一所有者在 runtime.py)。

    延迟 import:runtime.py 在**模块级** `from log_watcher import LogWatcher`,
    这里若也写模块级 `from runtime import ...` 就成环。函数内 import 命中
    sys.modules 缓存,代价是一次字典查找,相对调用点自带的 50ms sleep 可忽略。

    导入失败时回退 False(视为"未收到 Ctrl+C")而不是抛异常:解释器关闭阶段
    sys.modules 里的条目会被置为 None,此时轮询线程若抛异常会污染退出路径。
    这是拆分前就有的回退语义,行为保持不变。

    旧实现是 `from main import _force_stop` —— 那是拆分后遗留的指针,
    且会把整个入口模块(含 pynput 全局钩子)拖进这个纯 IO 模块的导入链。
    """
    try:
        from guild_auto.application.runtime import is_force_stop
    except ImportError:
        return False
    return is_force_stop()


# ---------------------------------------------------------------------------
# 编码检测
# ---------------------------------------------------------------------------
_ENCODING_CANDIDATES = ("utf-8", "gbk", "gb2312", "gb18030")
# 全部候选都解不动时的兜底(尽力解中文)。gb18030 特意排在 gbk/gb2312 之后:
# 它是 gbk 的超集(还能解 0x80 这类单字节杂散字节),只在 gbk 族严格解码失败时
# 才出手 —— 真实网易版 latest.log.* 里就有这种字节,用 gbk + errors="replace"
# 解码会在日志里留下 '�'(R-25 的真实样本半边)。
_ENCODING_FALLBACK = "gbk"
# 末行缓冲上界(1 MiB):游戏日志单行实测最长 ~2KB,正常永远到不了这个量级;
# 到不了说明对端一直不写换行,此时丢弃前半段比把内存吃满更安全。
_MAX_TAIL_BYTES = 1 << 20
# 编码采样:单个样本块大小 + "跳过所在半行"的单行上限。日志单行实测最长 ~2KB,
# 64 KiB 足够跨过一行;真遇到不写换行的巨无霸,readline 也在上限处停下,
# 不会把整份文件读进内存。
_SAMPLE_CHUNK = 1 << 16
_MAX_SKIP_LINE = 1 << 16
# 轮转指纹:记录游标前 32 字节,用来发现"原地重写但文件没变小"(R-30)。
_ANCHOR_BYTES = 32
# 编码未定时累计的判定样本上限。纯 ASCII 前缀攒到这么多还没出现非 ASCII 字节,
# 说明这份日志确实是纯 ASCII,按 utf-8 走不会有歧义。
_PROBE_MAX = 1 << 16
# TRACE 原始行回放:每秒最多 5 条完整原文(按秒限流),单条 400 字符内(铁律)
_TRACE_LINES_PER_SEC = 5
_TRACE_LINE_MAX = 400
# ParseError 诊断:回放响应块(上限 40 行)+ 最近上下文行数
_PARSE_CTX_LINES = 5
_PARSE_BLOCK_MAX = 40


def _drop_leading_continuation(buf):
    """丢掉开头的 UTF-8 续字节(0b10xxxxxx):说明这段是从字符中间切下来的。"""
    i = 0
    while i < len(buf) and 0x80 <= buf[i] <= 0xBF:
        i += 1
    return buf[i:]


def _aligned_window(f, pos):
    """从 pos 读一块样本,返回 (bytes, 头部是否被切在字符中间)。

    R-25 根因:旧实现直接 `seek(pos); read(65536)`,pos 落在 UTF-8 多字节字符
    中间时样本首字节是续字节 → 合法 UTF-8 日志严格解码失败 → 落到 gbk;而 gbk
    几乎能吃下任意字节,于是"成功"并按乱码返回。(核对表实测:100 个纯 UTF-8
    文件 71 个误判 gbk;样本 ≥1500 字节时 10/10。)

    UTF-8 与 GBK 都是 ASCII 兼容编码,`\\n` 绝不会出现在多字节序列内部,所以
    "从行首开始、到行尾结束"的样本天然是字符对齐的。只有对端写超长单行
    (readline 撞到上限)时头部才真的被切开,此时才允许解码端容忍头部位移。
    """
    f.seek(pos)
    head_cut = False
    if pos:
        head = f.readline(_MAX_SKIP_LINE)  # 先丢掉所在的半行
        head_cut = not head.endswith(b"\n")
    buf = f.read(_SAMPLE_CHUNK)
    cut = buf.rfind(b"\n")
    if cut >= 0:
        return buf[: cut + 1], False  # 截到行尾:两端都对齐,不容忍任何位移
    return buf, head_cut  # 超长单行:头部可能被切,尾部可能欠一个字符


def _sample_chunks(path):
    """在 头部/1/4/1/2/3/4/尾部 多处取样,防"开头长段纯 ASCII、中文在后"被误判 utf-8。"""
    size = os.path.getsize(path)
    positions = sorted({0, size // 4, size // 2, size * 3 // 4, max(0, size - _SAMPLE_CHUNK)})
    with open(path, "rb") as f:
        return [_aligned_window(f, pos) for pos in positions]


def _decodes_clean(chunk, enc, head_cut=False):
    """单块能否被 enc 干净解码。

    * 尾部:增量解码器 final=False —— 半个字符留在解码器缓冲里,不算错
      (对端正写到一半时就是这样,不能据此判死刑)。
    * 头部:只有 head_cut(超长单行被迫从字符中间开始)才容忍位移 —— utf-8 丢掉
      开头续字节,gbk 族最多丢 1~3 个前缀字节。**文件开头(pos=0)或行对齐的窗口
      一律不容忍**:否则 `b"\\x80\\x80"` 这种真坏字节会被当成干净的 utf-8。
    每次新建解码器 —— 复用会把上一块的尾巴和下一块的头拼成假字符。
    """
    if not head_cut:
        heads = (chunk,)
    elif enc == "utf-8":
        heads = (chunk, _drop_leading_continuation(chunk))
    else:
        heads = (chunk, chunk[1:], chunk[2:], chunk[3:])
    for head in heads:
        try:
            codecs.getincrementaldecoder(enc)().decode(head)
            return True
        except UnicodeDecodeError:
            continue
    return False


def _pick_encoding(chunks):
    """第一个能吃下**所有**样本块的编码;空样本返回 None(无依据,交调用方决定)。"""
    chunks = [c for c in chunks if c[0]]
    if not chunks:
        return None
    for enc in _ENCODING_CANDIDATES:
        if all(_decodes_clean(chunk, enc, head_cut) for chunk, head_cut in chunks):
            return enc
    return _ENCODING_FALLBACK


def detect_encoding(path, preferred="auto"):
    """检测日志文件编码。
    - preferred 非 auto 时直接使用(不检测);
    - auto:在文件 头部/1/4/1/2/3/4/尾部 多处按行对齐取样,依次尝试
      utf-8/gbk/gb2312/gb18030 严格解码,第一个全部样本都能解的胜;
      空样本返回 utf-8 仅作占位 —— LogWatcher 不会据此锁定编码(见 R-26),
      否则空日志起步后写入的 GBK 内容会全部乱码;
      全部失败回退 gbk + errors=replace(尽力解码中文)。
    """
    if preferred != "auto":
        return preferred
    try:
        chunks = _sample_chunks(path)
    except OSError:
        return _ENCODING_FALLBACK
    return _pick_encoding(chunks) or "utf-8"


def _file_id(path):
    """文件身份 (dev, ino)。rename 出来的新文件在这里必然不同(见 R-30)。"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


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
        # 用户/配置显式给定的编码;只有 "auto" 才允许运行期重新判定(R-26)
        self._preferred = encoding
        self.poll = poll_interval_ms / 1000.0
        self._fh = None
        self._size = 0
        # 末尾未完成行的原始字节缓冲。read_new_lines 只产出"已见到换行"的完整行:
        # 否则游戏分两次写入同一物理行时,会把一行拆成两条;更糟的是多字节字符
        # 被切在两次读取之间,前半截已经 decode 成替换符,丢字无法挽回。
        self._tail = b""
        # 编码未定时(空日志起步)累计的判定样本,见 R-26
        self._probe = b""
        # 轮转指纹:文件身份 + 游标前 _ANCHOR_BYTES 字节,见 R-30
        self._file_id = None
        self._anchor = b""
        self._skip_until_ts = 0  # first_command 等新回显时设 = time.time() + 1.0,读到的旧行跳
        # ---- TRACE 诊断状态(只写日志,不参与任何判定) ----
        # 最近读到的原始行(ParseError 时回放上下文);按秒限流的桶与丢弃计数。
        self._recent_lines = collections.deque(maxlen=200)
        self._trace_sec = 0
        self._trace_sec_count = 0
        self._trace_dropped = 0
        self._last_block = None  # 最近一次返回的响应块(供 report_parse_error 回放)

    # ---- 打开 ----
    def open(self, wait_seconds=60):
        """打开日志文件;若文件不存在则轮询等待(wait_seconds 内)。"""
        t0 = time.time()
        logger.log_info(
            f"S8 日志等待打开 ENTER | path={self.path} | wait_seconds={wait_seconds:.1f}"
        )
        deadline = t0 + wait_seconds
        while not os.path.exists(self.path):
            if time.time() > deadline:
                waited_s = time.time() - t0
                logger.log_trace(
                    f"[TRACE][log_watcher.open] timeout=true waited_s={waited_s:.1f}"
                    f" path={logger.sanitize_text(self.path)}"
                )
                logger.log_warning(
                    f"S8 等待游戏日志超时 | waited_s={waited_s:.1f} | path={self.path}"
                    f" | rotation_reopen=False"
                )
                raise FileNotFoundError(
                    f"游戏日志不存在: {self.path}\n请确认游戏已启动且路径配置正确(--log 或 config.json 的 game_log)。"
                )
            time.sleep(1.0)
        self.encoding = detect_encoding(self.path, self._preferred)
        self._fh = open(self.path, "rb")
        self._tail = b""
        self._probe = b""
        self._recent_lines.clear()
        self._last_block = None
        # 跳到末尾:只关心本次运行之后的新日志
        self._fh.seek(0, os.SEEK_END)
        self._size = self._fh.tell()
        self._file_id = _file_id(self.path)
        self._anchor = self._read_anchor()
        if self._size == 0:
            # 空文件没有任何编码依据:detect_encoding 返回的 utf-8 只是占位,
            # 不能据此锁定(R-26)——否则之后写入的 GBK 内容会全部乱码。
            self.encoding = "auto"
        logger.trace_file_read(logger.sanitize_text(self.path), size=self._size)
        logger.log_trace(
            f"[TRACE][log_watcher.open] wait_s={wait_seconds:.1f}"
            f" encoding={self.encoding} file_size={self._size}"
            f" elapsed_ms={int((time.time() - t0) * 1000)} mode=append_tail"
        )
        logger.log_info(
            f"S8 日志打开完成 | elapsed_s={time.time() - t0:.2f}"
            f" | encoding={self.encoding} | file_size={self._size}"
        )

    def close(self):
        if self._fh:
            try:
                self._fh.close()
            except OSError as e:
                # 关闭失败不该阻断 close() 的语义,但必须可检索(US-004)
                logger.log_debug(f"关闭日志文件句柄失败: {e!r}")
            self._fh = None

    def _read_anchor(self):
        """读游标前的 _ANCHOR_BYTES 字节,作为"文件没被原地重写"的指纹(R-30)。

        必须严格只读游标**之前**的字节:多读一个字节就会把"游标之后新追加的
        内容"算进指纹,下一轮必然误判成原地重写。
        """
        if not self._size:
            return b""
        start = max(0, self._size - _ANCHOR_BYTES)
        try:
            with open(self.path, "rb") as f:
                f.seek(start)
                return f.read(self._size - start)
        except OSError:
            return b""

    def _open_existing(self, wait_seconds=30.0, deadline=None, stop_check=None):
        """打开日志文件,轮转窗口期(文件暂缺)等待后重试。

        返回 True 表示已打开(self._fh 可用);False 表示等待结束仍未出现 ——
        此时 read_new_lines 返回已读到的行,不抛异常。

        为什么需要:轮转时旧 latest.log 被删、新的还没建,裸 open() 会抛
        FileNotFoundError。read_new_lines 是 while True 轮询循环,
        遇到瞬时文件缺失本该重试而不是崩掉(独立审查指出)。

        R-27:调用方带了 deadline(timeout)/stop_check 时必须让它们生效 ——
        旧实现固定死等 30s,`read_until(timeout=2)` 实测要 30.3s 才返回。
        """
        hard_deadline = time.time() + wait_seconds
        if deadline is not None:
            hard_deadline = min(hard_deadline, deadline)
        while True:
            try:
                self._fh = open(self.path, "rb")
                self._file_id = _file_id(self.path)
                return True
            except FileNotFoundError:
                if (
                    time.time() >= hard_deadline
                    or _force_stop()
                    or (stop_check is not None and stop_check())
                ):
                    logger.log_warning(
                        f"日志文件 {wait_seconds:.0f} 秒内未重新出现(可能仍在轮转): {self.path}"
                    )
                    self._fh = None
                    return False
                time.sleep(0.2)

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
        except Exception as e:
            # 重开前关闭旧句柄失败:下面会重新 open,这里只留痕
            logger.log_debug(f"轮转重开前关闭旧句柄失败: {e!r}")
        # 重新打开并 seek 到末尾(否则 read_new_lines 默认从 0 读会读到 launch 前所有内容)
        try:
            self._fh = open(self.path, "rb")
            # discard 是"从现在开始只看新内容"的语义,缓冲里若还留着旧的半行,
            # 会被拼到 discard 之后的首批新日志前面,造成串行污染。
            self._tail = b""
            self._probe = b""
            if os.path.getsize(self.path) > 0:
                self._fh.seek(0, 2)  # SEEK_END
                self._size = self._fh.tell()
            else:
                self._size = 0
            # 游标搬了,轮转指纹要跟着搬,否则下一轮会被误判成"原地重写"
            self._file_id = _file_id(self.path)
            self._anchor = self._read_anchor()
            logger.log_debug(
                f"[log_watcher] discard_buffer:size {size_before}→{self._size} "
                f"(跳过了 launch 前 {self._size - size_before} 字节 = "
                f"{self._size - size_before}B 文件增长)"
            )
        except Exception as e:
            logger.log_debug(f"discard_buffer 重开异常: {e}")
            self._size = 0

    def _reopen_if_rotated(self):
        """检测轮转(latest.log 被改名/删除/原地重写):需要重开时返回 True。"""
        if not os.path.exists(self.path):
            self.close()
            return True
        try:
            size_now = os.path.getsize(self.path)
        except OSError:
            return False
        if _file_id(self.path) != self._file_id:
            # 换了 inode = 被 rename 成新文件:哪怕比旧文件大也是新内容(R-30)
            self.close()
            self._size = 0
            return True
        if size_now < self._size:
            # 文件被截断/轮转:关闭句柄(read_new_lines 会统一重新打开并读全部)
            self.close()
            self._size = 0
            return True
        if self._size and self._anchor and self._read_anchor() != self._anchor:
            # 大小没变小,但游标前的字节变了 = 原地重写(R-30):继续按旧偏移读
            # 会把新文件首行切开,丢半行。
            self.close()
            self._size = 0
            return True
        return False

    def read_new_lines(self, block=True, deadline=None, stop_check=None):
        """读取自上次以来的新行,返回行列表(自动按当前编码解码,忽略坏字节)。
        block=True:至少读到一行才返回(内部轮询等待)。
        first_command=True 启动时调 reader.discard_buffer() 丢弃 launch 前的
        "前次命令残留"日志,避免本轮第一次试探就读到旧回显。

        deadline/stop_check:轮转窗口内等文件重现时,必须让调用方的 timeout
        与暂停标志生效(R-27);不传则沿用 _open_existing 的默认上限。
        """
        out = []
        while True:
            if self._fh is None or self._reopen_if_rotated():
                # 轮转窗口期:旧 latest.log 已删、新的还没建 -> open 会抛
                # FileNotFoundError。这是**瞬时状态**,轮询循环本就该重试,
                # 不该把异常抛给调用方(独立审查指出)。等待后重试,
                # 直到文件重新出现、调用方 deadline 到点或用户请求中断。
                if not self._open_existing(deadline=deadline, stop_check=stop_check):
                    return out
                # 轮转/重开后从头读新文件(seek 0):
                #  - 新文件是轮转后刚建的,通常很小,无内存尖峰;
                #  - 若 seek 到末尾会连"重开前已写入的首批新日志"一起跳过(丢行);
                #  - 旧文件场景(_reopen 因句柄失效而非轮转)由 _reopen_if_rotated
                #    的 size 变小检测区分——真正的大文件全量重读已被该分支拦截。
                self._fh.seek(0)
                self._size = 0
                self._tail = b""
                self._probe = b""
                if self.encoding != self._preferred:
                    # 新文件是新内容,编码可以重新判(R-26);显式配置的编码不动
                    self.encoding = "auto"
            data = self._fh.read()
            self._size += len(data)
            if self._tail or data:
                buf = self._tail + data
                # 只消费到最后一个换行符:末尾未完成的行(可能还含被切断的多字节字符)
                # 原样留到下次读 —— 否则同一行会被拆成两条,多字节字符前半截还会
                # 被 decode 成替换符而永久丢字。
                cut = buf.rfind(b"\n")
                if cut < 0:
                    cut = buf.rfind(b"\r")
                if cut < 0:
                    data, tail = b"", buf
                else:
                    data, tail = buf[: cut + 1], buf[cut + 1 :]
                if len(tail) > _MAX_TAIL_BYTES:
                    # 极端情况(游戏一直不 flush 行尾):给缓冲一个上界,超出丢前半段并留痕。
                    # 没有换行的内容本来也无法被 split_messages 正确切分。
                    logger.log_warning(
                        f"日志末行缓冲超过 {_MAX_TAIL_BYTES} 字节仍无换行,已截断丢弃前半段"
                    )
                    # 截断点可能落在多字节字符中间:丢掉开头续字节,否则下次
                    # decode 会凭空吐一个 '�'
                    tail = _drop_leading_continuation(tail[-_MAX_TAIL_BYTES:])
                self._tail = tail
                # buf 的末尾就是当前文件游标:留下它的尾巴当"没被原地重写"的指纹
                self._anchor = buf[-_ANCHOR_BYTES:]
            if data:
                text = self._decode_complete_lines(data)
                new_lines = text.splitlines()
                out.extend(new_lines)
                self._recent_lines.extend(new_lines)
                # TRACE:每行完整原文(脱敏,单条 400 字符内),按秒限流;
                # 无论是否触发限流,每次调用补一条含 items=N 的聚合行。
                if new_lines:
                    emitted = 0
                    if logger.current_level() >= 6:
                        now_sec = int(time.time())
                        if now_sec != self._trace_sec:
                            self._trace_sec = now_sec
                            self._trace_sec_count = 0
                        for idx, ln in enumerate(new_lines, 1):
                            if self._trace_sec_count >= _TRACE_LINES_PER_SEC:
                                self._trace_dropped += 1
                                continue
                            self._trace_sec_count += 1
                            emitted += 1
                            logger.log_trace(
                                f"[TRACE][log_watcher.read_new_lines] idx={idx}/{len(new_lines)}"
                                f" len={len(ln)}"
                                f" raw={logger.sanitize_text(ln, _TRACE_LINE_MAX)!r}"
                            )
                    logger.log_trace(
                        f"[TRACE][log_watcher.read_new_lines] items={len(new_lines)}"
                        f" emitted={emitted} dropped={self._trace_dropped}"
                        f" encoding={self.encoding}"
                    )
                # 详细日志:打印读到的每条新行(前 120 字符,GBK 乱码用 ? 替代,纯看增量)
                # ——让用户从 latest.log 直接看到游戏实时输出,便于诊断"脚本读到什么"
                for idx, ln in enumerate(new_lines, 1):
                    preview = ln[:120].replace("\r", "")
                    logger.log_debug(
                        f"[log_watcher] READ +{idx}/{len(new_lines)} len={len(ln)} | {preview!r}"
                    )
            if out or not block:
                return out
            time.sleep(self.poll)

    def _decode_complete_lines(self, data):
        """解码一段"只含完整行"的原始字节(data 必然以换行结尾,字符天然对齐)。

        编码未锁定(encoding == "auto",只可能来自"空日志起步")时:纯 ASCII 内容
        在 utf-8/gbk/gb18030 下解出来完全一致,先按 ascii 放出,同时把样本累计进
        _probe;第一个非 ASCII 字节出现时再用累计样本判定。这就是 R-26 的修法 ——
        旧实现在空文件上直接把 utf-8 当成结论锁定,之后写入的 GBK 内容全变 '�'。
        """
        if self.encoding == "auto":
            if data.isascii():
                if len(self._probe) < _PROBE_MAX:
                    # 截到 _PROBE_MAX 可能切在字符中间,丢掉续字节
                    self._probe = _drop_leading_continuation((self._probe + data)[-_PROBE_MAX:])
                return data.decode("ascii")
            enc = _pick_encoding([(self._probe + data, False)])
            if enc:
                logger.log_info(f"日志编码在读到首个非 ASCII 内容后锁定: {enc}")
                self.encoding = enc
            self._probe = b""
        return data.decode(self.encoding, errors="replace")

    # ---- 等待响应块(消息级) ----
    def wait_for_chat_block(
        self, timeout=12.0, max_pending_chars=2_000_000, stop_check=None, min_wait_after_start=0.0
    ):
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
        logger.trace_call(
            "log_watcher.wait_for_chat_block",
            kwargs={"timeout": timeout, "poll_ms": int(self.poll * 1000)},
        )
        logger.log_trace(
            f"[TRACE][log_watcher.wait_for_chat_block] blocking=true"
            f" timeout_s={timeout:.1f} poll_ms={int(self.poll * 1000)}"
            f" min_wait_s={min_wait_after_start:.2f} stop_check={stop_check is not None}"
        )
        logger.log_debug(
            f"等待响应块(最长 {timeout:.1f}s,轮询 {self.poll * 1000:.0f}ms,首块最早 {min_wait_after_start:.2f}s 后可返回)..."
        )
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
                        logger.trace_return(
                            "log_watcher.wait_for_chat_block",
                            {"reason": "pause", "block_lines": 0},
                        )
                        return None
                    time.sleep(0.05)
                    stable_ms += 50
            for line in self.read_new_lines(block=False, deadline=deadline, stop_check=stop_check):
                pending.append(line)
                logger.trace_code_location(
                    "log_watcher.wait_for_chat_block.got_lines", f"new_lines={len(pending)} total"
                )
            messages = split_messages(pending)
            logger.trace_code_location(
                "log_watcher.wait_for_chat_block.messages",
                f"msgs={len(messages)} pending_lines={len(pending)}",
            )
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
                        logger.log_debug(
                            f"收到完整响应块({len(block)} 行,"
                            f"等待 {timeout - (deadline - time.time()):.1f}s)"
                        )
                        # 详细日志:打印块首/末行(让用户确认提取出的内容确实是面板)
                        block_head = block[0][:100] if block else "<empty>"
                        block_tail = block[-1][:100] if block else "<empty>"
                        logger.log_debug(
                            f"[log_watcher] BLOCK 首行: {block_head!r}\n"
                            f"[log_watcher] BLOCK 末行: {block_tail!r}"
                        )
                        self._last_block = list(block)
                        logger.trace_return(
                            "log_watcher.wait_for_chat_block",
                            {"reason": "block", "block_lines": len(block)},
                        )
                        return block
                # 最后一条若完整,尝试提取(可能本身就是完整消息)
                block = extract_chat_block(messages[-1])
                if block is not None:
                    logger.log_debug(
                        f"收到完整响应块({len(block)} 行,"
                        f"等待 {timeout - (deadline - time.time()):.1f}s)"
                    )
                    block_head = block[0][:100] if block else "<empty>"
                    block_tail = block[-1][:100] if block else "<empty>"
                    logger.log_debug(
                        f"[log_watcher] BLOCK 首行: {block_head!r}\n"
                        f"[log_watcher] BLOCK 末行: {block_tail!r}"
                    )
                    self._last_block = list(block)
                    logger.trace_return(
                        "log_watcher.wait_for_chat_block",
                        {"reason": "block", "block_lines": len(block)},
                    )
                    return block
                # 若最后一条是"以块起始线开头但尚未闭合"的面板消息(日志写入中),
                # 必须完整保留等待补齐,否则块中间行会永久丢失;
                # 若是普通聊天消息,只保留消息头即可(防止 pending 无限增长)
                if _starts_with_block(messages[-1]):
                    pending = messages[-1]
                    logger.trace_code_location(
                        "log_watcher.waiting_panel_incomplete", f"{len(pending)} 行待补齐"
                    )
                else:
                    pending = messages[-1][:1]
            if sum(len(l) for l in pending) > max_pending_chars:
                pending = pending[-200:]
            # 拆短 sleep + 检查 Ctrl+C 标志(见模块头 _force_stop 助手的说明)
            # ⚠ 必须在循环内每轮重读:入口读一次的话,轮询期间按下的 Ctrl+C 要等
            # 本轮 sleep 结束才生效 —— 实测就是"Ctrl+C 要按好几次才响应"。
            stable_ms = 0
            while stable_ms < int(self.poll * 1000):
                if _force_stop():
                    logger.log_debug("wait_for_chat_block 检测到 _force_stop(Ctrl+C),返回 None")
                    logger.trace_return(
                        "log_watcher.wait_for_chat_block",
                        {"reason": "force_stop", "block_lines": 0},
                    )
                    return None
                time.sleep(0.05)
                stable_ms += 50
        logger.log_debug("wait_for_chat_block 循环退出(超时/停止)")
        logger.log_warning(f"等待响应块超时({timeout:.1f}s 无完整面板),返回 None")
        logger.trace_return(
            "log_watcher.wait_for_chat_block",
            {"reason": "timeout", "block_lines": 0, "timeout_s": timeout},
        )
        return None

    def read_until(self, timeout=2.0):
        """读取并返回指定时长内到达的所有新行(用于执行命令后收集回显)。"""
        out = []
        deadline = time.time() + timeout
        collected = 0
        logger.trace_call("log_watcher.read_until", kwargs={"timeout": timeout})
        while time.time() < deadline:
            if _force_stop():
                logger.log_debug("read_until 检测到 _force_stop,返回已收集")
                break
            new_lines = self.read_new_lines(block=False, deadline=deadline)
            out.extend(new_lines)
            collected += len(new_lines)
            # 拆短 sleep + 复检 _force_stop(让 Ctrl+C 立即生效)
            stable_ms = 0
            while stable_ms < int(self.poll * 1000) and not _force_stop():
                time.sleep(0.05)
                stable_ms += 50
        logger.log_debug(f"回显收集完成: {collected} 行(窗口 {timeout:.1f}s)")
        logger.trace_return(
            "log_watcher.read_until",
            {"lines": collected, "window_s": timeout, "bytes_hint": sum(len(x) for x in out)},
        )
        return out

    def report_parse_error(self, exc, block=None, context=None):
        """解析失败(ParseError)诊断:把响应块 + 最近上下文行**无条件写 ERROR**。

        设计目的:ParseError 由上层 application/domain 抛出,默认级别(4)下原本
        只剩一句中文原因,看不到"到底是哪段日志解析失败"。调用方只需在
        `except ParseError` 分支里多调一次本方法,不改变任何返回值/控制流。

        block: 本次解析的响应块(缺省用最近一次 wait_for_chat_block 返回的块);
        context: 回放最近多少条原始日志行(缺省 _PARSE_CTX_LINES)。
        """
        ctx_n = _PARSE_CTX_LINES if context is None else max(0, int(context))
        block_lines = list(block) if block is not None else list(self._last_block or [])
        recent = list(self._recent_lines)[-ctx_n:] if ctx_n else []
        logger.log_error(
            f"[TRACE][log_watcher.report_parse_error] parse_error={type(exc).__name__}"
            f" reason={logger.sanitize_text(exc, 200)}"
            f" block_lines={len(block_lines)} context_lines={len(recent)}"
        )
        for i, ln in enumerate(block_lines[:_PARSE_BLOCK_MAX], 1):
            logger.log_error(
                f"[TRACE][log_watcher.report_parse_error] block[{i}/{len(block_lines)}]"
                f" {logger.sanitize_text(ln, _TRACE_LINE_MAX)}"
            )
        for i, ln in enumerate(recent, 1):
            logger.log_error(
                f"[TRACE][log_watcher.report_parse_error] ctx[{i}/{len(recent)}]"
                f" {logger.sanitize_text(ln, _TRACE_LINE_MAX)}"
            )


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
    content = text[idx + len("[CHAT]") :]
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
    content = text[idx + len("[CHAT]") :].replace("\\n", "\n")
    for l in content.splitlines():
        if not l.strip():
            continue
        return is_block_start_line(l.strip())
    return False
