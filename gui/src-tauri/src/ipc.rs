//! 命名管道客户端 + 请求/应答关联（规范 §2）。
//!
//! 为什么在 Rust 侧：WebView 里的 JS 碰不到 Windows 命名管道。这里只做搬运：
//! 每个请求带自增 id，读线程按 id 把应答交回等待方；无 id 的行是核主动推的
//! 事件，直接交给回调转成前端的 `core-event`。
//!
//! 可观测性（规范 §6.4）：连接尝试/重试/成功/失败、每行归类、每次 RPC 进出
//! 与耗时、超时告警都必须留痕；管道名内嵌 token，日志一律脱敏。

use std::collections::HashMap;
use std::io::{Read, Write};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{self, Sender};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde_json::Value;

use crate::logger;
use crate::protocol::{build_request, classify_line, strip_id, Classified};

const CALL_TIMEOUT: Duration = Duration::from_secs(15);

/// ERROR_NO_DATA：句柄处于非阻塞模式且暂无数据时的返回值（不是错误）。
const ERROR_NO_DATA: i32 = 232;

/// 管道里**待读字节数**（PeekNamedPipe：不阻塞、不占住文件对象）。
/// None = 管道已断（对端关闭/句柄失效），调用方按 EOF 处理。
///
/// 为什么改成返回字节数（P0-4e 根因）：旧实现只回答「有没有数据」，然后调
/// `BufReader::read_line`。那是个**阻塞**读：只来了半行（还没有 `\n`）时它会一直
/// 挂住同一个文件对象，而 Windows 会把同一文件对象上的同步 I/O 串行化 ——
/// 于是随后所有 `WriteFile` 都排队，表现为「冷启动第一次点任何按钮都要等满 15s」。
/// 现在的做法是只按 Peek 报的字节数读，读多少算多少，绝不发起会挂起的读。
///
/// 这也顺带否掉了 `SetNamedPipeHandleState(PIPE_NOWAIT)`：它在真机实测会被拒
/// （os error 231 = ERROR_PIPE_BUSY），而一旦被拒就退回阻塞读 —— 正是上面那条死路。
/// 既然读已经不会再阻塞，就没必要再设 NOWAIT，那条失败日志也一并消失。
#[cfg(windows)]
fn pipe_avail(file: &std::fs::File) -> Option<u32> {
    use std::os::windows::io::AsRawHandle;

    extern "system" {
        fn PeekNamedPipe(
            handle: *mut core::ffi::c_void,
            buffer: *mut core::ffi::c_void,
            buffer_size: u32,
            bytes_read: *mut u32,
            total_bytes_avail: *mut u32,
            bytes_left: *mut u32,
        ) -> i32;
    }
    let mut avail: u32 = 0;
    let ok = unsafe {
        PeekNamedPipe(
            file.as_raw_handle().cast(),
            std::ptr::null_mut(),
            0,
            std::ptr::null_mut(),
            &mut avail,
            std::ptr::null_mut(),
        )
    } != 0;
    if ok {
        Some(avail)
    } else {
        None // 断开等：交给调用方按 EOF 处理
    }
}

/// 核未就绪时不能一次失败就永久放弃：45 × 200ms ≈ 9s，与前端轮询上限对齐。
const CONNECT_ATTEMPTS: u32 = 45;
const CONNECT_RETRY_INTERVAL: Duration = Duration::from_millis(200);

/// 管道名形如 `bujidao-gui-<token>`，日志里必须把 token 抠掉。
fn masked_pipe(pipe: &str, token: &str) -> String {
    if token.is_empty() {
        pipe.to_string()
    } else {
        pipe.replace(token, "<token>")
    }
}

fn masked_token(token: &str) -> String {
    if token.is_empty() {
        "<空>".to_string()
    } else {
        format!("<已脱敏 len={}>", token.chars().count())
    }
}

pub struct Ipc {
    out: Mutex<Option<std::fs::File>>,
    pending: Arc<Mutex<HashMap<u64, Sender<String>>>>,
    next_id: AtomicU64,
    token: String,
}

impl Ipc {
    /// 连接管道并起读线程；`on_event` 收核推送的 `{method, params}`。
    ///
    /// `on_eof`：读线程因管道 EOF / 读错误退出时回调一次。核被强杀
    /// （TerminateProcess）时不走 atexit，推不出 `ipc-closed`，这是界面唯一
    /// 能感知「核已经走了」的途径（R-46 的 GUI 半边）。是否据此提示由调用方决定。
    pub fn connect<F, G>(pipe: &str, token: &str, on_event: F, on_eof: G) -> Result<Self, String>
    where
        F: Fn(Value) + Send + 'static,
        G: Fn() + Send + 'static,
    {
        let pipe_log = masked_pipe(pipe, token);
        logger::trace(
            "ipc",
            &format!(
                "[ipc.connect] ENTER pipe={pipe_log} max_attempts={CONNECT_ATTEMPTS} retry_ms={} call_timeout_ms={}",
                CONNECT_RETRY_INTERVAL.as_millis(),
                CALL_TIMEOUT.as_millis()
            ),
        );
        logger::info(
            "ipc",
            &format!(
                "[ENTER] 连接命名管道 pipe={pipe_log} token={} 最多尝试 {CONNECT_ATTEMPTS} 次 间隔={}ms",
                masked_token(token),
                CONNECT_RETRY_INTERVAL.as_millis()
            ),
        );
        let path = format!(r"\\.\pipe\{pipe}");
        let started = Instant::now();
        let mut last_err = String::new();
        let mut opened = None;
        for attempt in 1..=CONNECT_ATTEMPTS {
            logger::debug(
                "ipc",
                &format!("管道连接尝试 {attempt}/{CONNECT_ATTEMPTS} pipe={pipe_log}"),
            );
            match std::fs::OpenOptions::new()
                .read(true)
                .write(true)
                .open(&path)
            {
                Ok(file) => {
                    logger::info(
                        "ipc",
                        &format!(
                            "管道连接成功 attempt={attempt} 耗时={}ms",
                            started.elapsed().as_millis()
                        ),
                    );
                    opened = Some(file);
                    break;
                }
                Err(e) => {
                    last_err = format!("连接核心失败: {e}");
                    if attempt < CONNECT_ATTEMPTS {
                        logger::warn(
                            "ipc",
                            &format!(
                                "管道连接失败 attempt={attempt}/{CONNECT_ATTEMPTS}: {e}; {}ms 后重试",
                                CONNECT_RETRY_INTERVAL.as_millis()
                            ),
                        );
                        std::thread::sleep(CONNECT_RETRY_INTERVAL);
                    } else {
                        logger::warn(
                            "ipc",
                            &format!(
                                "管道连接重试耗尽 attempt={attempt} 总耗时={}ms 最后错误: {e}",
                                started.elapsed().as_millis()
                            ),
                        );
                    }
                }
            }
        }
        let Some(file) = opened else {
            logger::trace(
                "ipc",
                &format!(
                    "[ipc.connect] EXIT ok=false attempts={CONNECT_ATTEMPTS} elapsed_ms={} err={last_err}",
                    started.elapsed().as_millis()
                ),
            );
            logger::warn(
                "ipc",
                &format!(
                    "[EXIT ] 管道连接失败 耗时={}ms",
                    started.elapsed().as_millis()
                ),
            );
            return Err(last_err);
        };
        // File 不实现 Clone：管道要一份读句柄一份写句柄，用 try_clone 复制 OS 句柄
        let writer = file.try_clone().map_err(|e| {
            logger::error("ipc", &format!("复制管道写句柄失败: {e}"));
            format!("复制管道句柄失败: {e}")
        })?;
        let pending: Arc<Mutex<HashMap<u64, Sender<String>>>> =
            Arc::new(Mutex::new(HashMap::new()));
        let pending_for_reader = pending.clone();

        // 具名线程 + panic 全局钩子：读线程死掉时日志能指认是谁
        let reader = std::thread::Builder::new()
            .name("ipc-reader".into())
            .spawn(move || {
                let mut lines_read: u64 = 0;
                let mut events: u64 = 0;
                let mut replies: u64 = 0;
                let mut skips: u64 = 0;
                logger::trace(
                    "ipc",
                    "[ipc.reader_loop] ENTER thread=ipc-reader poll_ms=5",
                );
                // P0-4e：读循环**永不发起阻塞读**。
                // 只按 Peek 报的可读字节数 Read 一次，读多少算多少，剩下的半行留在 buf 里。
                // 旧实现用 BufReader::read_line —— 只来了半行（还没有 \n）时它会挂住同一
                // 文件对象，而 Windows 把同一文件对象的同步 I/O 串行化，随后所有 WriteFile
                // 排队，表现为「冷启动第一次点任何按钮都要等满 15s RPC 超时」。
                let mut reader_file = file;
                let mut buf: Vec<u8> = Vec::new();
                let mut chunk = vec![0u8; 64 * 1024];
                loop {
                    let avail = match pipe_avail(&reader_file) {
                        Some(n) => n,
                        None => {
                            logger::warn(
                                "ipc",
                                &format!(
                                    "读线程退出（管道 EOF/已关闭）已读行数={lines_read} 事件数={events}"
                                ),
                            );
                            break;
                        }
                    };
                    if avail == 0 {
                        std::thread::sleep(Duration::from_millis(5));
                        continue;
                    }
                    let want = (avail as usize).min(chunk.len());
                    let mut gone = false;
                    match reader_file.read(&mut chunk[..want]) {
                        Ok(0) => {
                            logger::warn(
                                "ipc",
                                &format!(
                                    "读线程退出（管道 EOF/已关闭）已读行数={lines_read} 事件数={events}"
                                ),
                            );
                            gone = true;
                        }
                        Ok(n) => buf.extend_from_slice(&chunk[..n]),
                        // 非阻塞模式下"暂无数据"不是错误
                        Err(e) if e.raw_os_error() == Some(ERROR_NO_DATA) => {
                            std::thread::sleep(Duration::from_millis(5));
                            continue;
                        }
                        Err(e) => {
                            logger::warn(
                                "ipc",
                                &format!(
                                    "读线程退出（读取错误）: {e} 已读行数={lines_read} 事件数={events}"
                                ),
                            );
                            gone = true;
                        }
                    }
                    if gone {
                        break;
                    }
                    // 把 buf 里成行的部分逐条处理（半行留在 buf，等下一轮补全）
                    while let Some(pos) = buf.iter().position(|&b| b == b'\n') {
                        let raw: Vec<u8> = buf.drain(..=pos).collect();
                        let text = String::from_utf8_lossy(&raw)
                            .trim_end_matches(['\r', '\n'])
                            .to_string();
                        lines_read += 1;
                        match classify_line(&text) {
                        Classified::Reply { id, text } => {
                            replies += 1;
                            logger::debug(
                                "ipc",
                                &format!("归类=reply id={id} 字节={}", text.len()),
                            );
                            let tx = pending_for_reader
                                .lock()
                                .ok()
                                .and_then(|m| m.get(&id).cloned());
                            if let Some(tx) = tx {
                                let _ = tx.send(text);
                            } else {
                                logger::debug(
                                    "ipc",
                                    &format!("应答 id={id} 无等待方（可能已超时）"),
                                );
                            }
                        }
                        Classified::Event(value) => {
                            let method = value
                                .get("method")
                                .and_then(|m| m.as_str())
                                .unwrap_or("?")
                                .to_string();
                            events += 1;
                            logger::trace(
                                "ipc",
                                &format!("收到事件 method={method} 帧长={} 字节", text.len()),
                            );
                            logger::debug("ipc", &format!("归类=event method={method}"));
                            on_event(value);
                        }
                        Classified::Skip => {
                            skips += 1;
                            logger::debug(
                                "ipc",
                                &format!(
                                    "归类=skip 原因=空行/非 JSON/既无 id 也无 method 字节={}",
                                    text.len()
                                ),
                            );
                        }
                        }
                    }
                }
                // 循环内不逐行打点（量控）：退出时只落一条聚合行
                logger::trace(
                    "ipc",
                    &format!(
                        "[ipc.reader_loop] EXIT thread=ipc-reader lines={lines_read} reply={replies} event={events} skip={skips}"
                    ),
                );
                // 读线程退出 = 管道已断（EOF 或读错误）。核正常退出时会先推 ipc-closed，
                // 那条路已由 on_event 处理；调用方按「是否收到过 ipc-closed」决定要不要
                // 把这次 EOF 报成「未收到正常关闭」。
                on_eof();
            });
        if let Err(e) = reader {
            logger::error("ipc", &format!("读线程创建失败: {e}"));
            return Err(format!("读线程创建失败: {e}"));
        }

        logger::trace(
            "ipc",
            &format!(
                "[ipc.connect] EXIT ok=true elapsed_ms={} reader_thread=ipc-reader",
                started.elapsed().as_millis()
            ),
        );
        Ok(Self {
            out: Mutex::new(Some(writer)),
            pending,
            next_id: AtomicU64::new(1),
            token: token.to_string(),
        })
    }

    /// 发一条请求并等应答。返回核的原始应答体（含 ok / result / error）。
    pub fn call(&self, method: &str, params: Value) -> Result<Value, String> {
        self.call_with_timeout(method, params, CALL_TIMEOUT)
    }

    /// 指定超时的版本：hello 要用**更短**的超时，才能赶在核的首帧看门狗
    /// 断开连接之前重发（否则重发时连接已死，兜底形同虚设）。
    pub fn call_with_timeout(
        &self,
        method: &str,
        params: Value,
        timeout: Duration,
    ) -> Result<Value, String> {
        let id = self.next_id.fetch_add(1, Ordering::SeqCst);
        logger::debug(
            "ipc",
            &format!(
                "[ENTER] RPC method={method} id={id} 超时={}ms",
                timeout.as_millis()
            ),
        );
        let started = Instant::now();
        logger::trace(
            "ipc",
            &format!(
                "[ipc.call_with_timeout] ENTER method={method} id={id} timeout_ms={}",
                timeout.as_millis()
            ),
        );
        let (tx, rx) = mpsc::channel();
        self.pending
            .lock()
            .map_err(|_| {
                logger::error(
                    "ipc",
                    &format!("RPC method={method} id={id} pending 锁中毒"),
                );
                "pending 锁中毒".to_string()
            })?
            .insert(id, tx);
        let line = build_request(id, method, params, &self.token);
        {
            // 写入段留痕：实测「RPC 只有 ENTER、核侧收不到」时，卡点就在这几十行里。
            logger::trace("ipc", &format!("等待写锁 id={id} 待写字节={}", line.len()));
            let mut guard = self.out.lock().map_err(|_| {
                logger::error("ipc", &format!("RPC method={method} id={id} 管道锁中毒"));
                "管道锁中毒".to_string()
            })?;
            logger::trace("ipc", &format!("已获写锁 id={id}"));
            let writer = guard.as_mut().ok_or_else(|| {
                logger::warn(
                    "ipc",
                    &format!("RPC method={method} id={id} 失败: 管道未连接"),
                );
                logger::trace(
                    "ipc",
                    &format!(
                        "[ipc.call_with_timeout] EXIT method={method} id={id} outcome=pipe_down elapsed_ms={}",
                        started.elapsed().as_millis()
                    ),
                );
                "管道未连接".to_string()
            })?;
            if let Err(e) = writeln!(writer, "{line}") {
                logger::warn(
                    "ipc",
                    &format!("RPC method={method} id={id} 写管道失败: {e}"),
                );
                logger::trace(
                    "ipc",
                    &format!(
                        "[ipc.call_with_timeout] EXIT method={method} id={id} outcome=write_error elapsed_ms={} err={e}",
                        started.elapsed().as_millis()
                    ),
                );
                if let Ok(mut m) = self.pending.lock() {
                    m.remove(&id);
                }
                return Err(format!("写管道失败: {e}"));
            }
            logger::trace("ipc", &format!("写入完成 id={id} 字节={}", line.len() + 1));
        }
        match rx.recv_timeout(timeout) {
            Ok(text) => {
                let elapsed = started.elapsed().as_millis();
                match serde_json::from_str::<Value>(&text) {
                    Ok(v) => {
                        let outcome = match v.get("ok").and_then(|b| b.as_bool()) {
                            Some(true) => "ok".to_string(),
                            Some(false) => format!(
                                "error: {}",
                                v.get("error")
                                    .and_then(|e| e.as_str())
                                    .unwrap_or("<无错误文本>")
                            ),
                            None => "无 ok 字段".to_string(),
                        };
                        logger::debug(
                            "ipc",
                            &format!(
                                "[EXIT ] RPC method={method} id={id} 耗时={elapsed}ms 响应={outcome}"
                            ),
                        );
                        logger::trace(
                            "ipc",
                            &format!(
                                "[ipc.call_with_timeout] EXIT method={method} id={id} outcome={outcome} elapsed_ms={elapsed} bytes={}",
                                text.len()
                            ),
                        );
                        Ok(strip_id(v))
                    }
                    Err(e) => {
                        logger::warn(
                            "ipc",
                            &format!(
                                "RPC method={method} id={id} 耗时={elapsed}ms 应答不是合法 JSON: {e}"
                            ),
                        );
                        logger::trace(
                            "ipc",
                            &format!(
                                "[ipc.call_with_timeout] EXIT method={method} id={id} outcome=bad_json elapsed_ms={elapsed} err={e}"
                            ),
                        );
                        Err(format!("应答不是合法 JSON: {e}"))
                    }
                }
            }
            Err(e) => {
                let elapsed = started.elapsed().as_millis();
                logger::warn(
                    "ipc",
                    &format!(
                        "RPC 超时 method={method} id={id} 已等待={elapsed}ms 超时值={}ms 原因={e}",
                        timeout.as_millis()
                    ),
                );
                logger::trace(
                    "ipc",
                    &format!(
                        "[ipc.call_with_timeout] EXIT method={method} id={id} outcome=timeout waited_ms={elapsed} timeout_ms={}",
                        timeout.as_millis()
                    ),
                );
                Err(format!("等待 {method} 应答超时"))
            }
        }
    }
}
