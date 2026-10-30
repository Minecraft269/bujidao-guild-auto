//! GUI 侧日志落盘（规范 §6.4：与核日志同目录，便于按时间对读）。
//!
//! 为什么不引 `log`/`tauri-plugin-log`：Cargo.toml 不在本层改动范围，且这点
//! 需求用几十行 std 就够。级别沿用规范 §一 的 1~6（数值越大越详细），
//! 通过环境变量 `BUJIDAO_GUI_LOG_LEVEL` 调，默认 6（GUI 只写自己的 gui.log）。
//!
//! 日志行格式：`[+12.345s][INFO][ipc] 消息`，与核日志的相对启动时间风格一致。

use std::io::Write as _;
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

const LEVEL_NAMES: [&str; 6] = ["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "TRACE"];

static SINK: OnceLock<Mutex<Option<std::fs::File>>> = OnceLock::new();
static START: OnceLock<Instant> = OnceLock::new();
static MIN_LEVEL: OnceLock<u8> = OnceLock::new();

/// 打开 `logs/gui.log`（相对 cwd，与核的 `logs/` 相同）。失败时不阻断进程，
/// 降级到 stderr 并继续运行（桌面版 stderr 不可见，但绝不能因此起不来）。
pub fn init() {
    START.get_or_init(Instant::now);
    let level = std::env::var("BUJIDAO_GUI_LOG_LEVEL")
        .ok()
        .and_then(|v| v.trim().parse::<u8>().ok())
        .filter(|v| (1..=6).contains(v))
        .unwrap_or(6);
    let _ = MIN_LEVEL.set(level);

    let dir = std::path::Path::new("logs");
    let opened = std::fs::create_dir_all(dir).and_then(|_| {
        std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(dir.join("gui.log"))
    });
    match opened {
        Ok(file) => {
            let _ = SINK.set(Mutex::new(Some(file)));
            trace(
                "log",
                &format!("GUI 日志已打开 path=logs/gui.log level={level}"),
            );
        }
        Err(e) => {
            let _ = SINK.set(Mutex::new(None));
            eprintln!("[GUI-LOG] 无法打开 logs/gui.log: {e}");
        }
    }
}

pub fn critical(category: &str, msg: &str) {
    log(1, category, msg);
}
pub fn error(category: &str, msg: &str) {
    log(2, category, msg);
}
pub fn warn(category: &str, msg: &str) {
    log(3, category, msg);
}
pub fn info(category: &str, msg: &str) {
    log(4, category, msg);
}
pub fn debug(category: &str, msg: &str) {
    log(5, category, msg);
}
pub fn trace(category: &str, msg: &str) {
    log(6, category, msg);
}

pub fn log(level: u8, category: &str, msg: &str) {
    if SINK.get().is_none() {
        init();
    }
    let min = *MIN_LEVEL.get().unwrap_or(&6);
    if level > min {
        return;
    }
    let elapsed = START.get().map(|s| s.elapsed()).unwrap_or(Duration::ZERO);
    let name = LEVEL_NAMES[(level.clamp(1, 6) - 1) as usize];
    let line = format!(
        "[+{:.3}s][{}][{}] {}\n",
        elapsed.as_secs_f64(),
        name,
        category,
        msg
    );

    let Some(sink) = SINK.get() else {
        eprint!("{line}");
        return;
    };
    match sink.lock() {
        Ok(mut guard) => match guard.as_mut() {
            Some(file) => {
                // 每行 flush：崩溃/被终止时最后几行必须已在磁盘上
                let _ = file.write_all(line.as_bytes());
                let _ = file.flush();
            }
            None => eprint!("{line}"),
        },
        Err(_) => eprint!("{line}"),
    }
}
