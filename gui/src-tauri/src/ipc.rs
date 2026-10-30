//! 命名管道客户端 + 请求/应答关联（规范 §2）。
//!
//! 为什么在 Rust 侧：WebView 里的 JS 碰不到 Windows 命名管道。这里只做搬运：
//! 每个请求带自增 id，读线程按 id 把应答交回等待方；无 id 的行是核主动推的
//! 事件，直接交给回调转成前端的 `core-event`。

use std::collections::HashMap;
use std::io::{BufRead, BufReader, Write};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{self, Sender};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::Value;

use crate::protocol::{build_request, classify_line, strip_id, Classified};

const CALL_TIMEOUT: Duration = Duration::from_secs(15);

pub struct Ipc {
    out: Mutex<Option<std::fs::File>>,
    pending: Arc<Mutex<HashMap<u64, Sender<String>>>>,
    next_id: AtomicU64,
    token: String,
}

impl Ipc {
    /// 连接管道并起读线程；`on_event` 收核推送的 `{method, params}`。
    pub fn connect<F>(pipe: &str, token: &str, on_event: F) -> Result<Self, String>
    where
        F: Fn(Value) + Send + 'static,
    {
        let path = format!(r"\\.\pipe\{pipe}");
        let file = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .open(&path)
            .map_err(|e| format!("连接核心失败: {e}"))?;
        // File 不实现 Clone：管道要一份读句柄一份写句柄，用 try_clone 复制 OS 句柄
        let writer = file
            .try_clone()
            .map_err(|e| format!("复制管道句柄失败: {e}"))?;
        let pending: Arc<Mutex<HashMap<u64, Sender<String>>>> =
            Arc::new(Mutex::new(HashMap::new()));
        let pending_for_reader = pending.clone();

        std::thread::spawn(move || {
            for line in BufReader::new(file).lines() {
                let Ok(text) = line else { break };
                match classify_line(&text) {
                    Classified::Reply { id, text } => {
                        let tx = pending_for_reader
                            .lock()
                            .ok()
                            .and_then(|m| m.get(&id).cloned());
                        if let Some(tx) = tx {
                            let _ = tx.send(text);
                        }
                    }
                    Classified::Event(value) => on_event(value),
                    Classified::Skip => {}
                }
            }
        });

        Ok(Self {
            out: Mutex::new(Some(writer)),
            pending,
            next_id: AtomicU64::new(1),
            token: token.to_string(),
        })
    }

    /// 发一条请求并等应答。返回核的原始应答体（含 ok / result / error）。
    pub fn call(&self, method: &str, params: Value) -> Result<Value, String> {
        let id = self.next_id.fetch_add(1, Ordering::SeqCst);
        let (tx, rx) = mpsc::channel();
        self.pending
            .lock()
            .map_err(|_| "pending 锁中毒".to_string())?
            .insert(id, tx);
        let line = build_request(id, method, params, &self.token);
        {
            let mut guard = self.out.lock().map_err(|_| "管道锁中毒".to_string())?;
            let writer = guard.as_mut().ok_or_else(|| "管道未连接".to_string())?;
            writeln!(writer, "{line}").map_err(|e| format!("写管道失败: {e}"))?;
        }
        let text = rx
            .recv_timeout(CALL_TIMEOUT)
            .map_err(|_| format!("等待 {method} 应答超时"))?;
        let v: Value =
            serde_json::from_str(&text).map_err(|e| format!("应答不是合法 JSON: {e}"))?;
        Ok(strip_id(v))
    }
}
