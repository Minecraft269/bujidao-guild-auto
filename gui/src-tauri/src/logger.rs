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
static MIN_LEVEL: OnceLock<Mutex<u8>> = OnceLock::new();

/// 运行时改级别：GUI 启动时只知道 env，真正的 `log.level` 要等 hello 下发配置。
/// 之前这里写死默认 6（TRACE 全开），与 config 的 4 不一致。
pub fn set_level(level: u8) {
    if !(1..=6).contains(&level) {
        return;
    }
    let cell = MIN_LEVEL.get_or_init(|| Mutex::new(level));
    if let Ok(mut guard) = cell.lock() {
        *guard = level;
    }
}

fn min_level() -> u8 {
    MIN_LEVEL
        .get()
        .and_then(|cell| cell.lock().ok().map(|g| *g))
        .unwrap_or(4)
}

/// 打开 `logs/gui.log`（相对 cwd，与核的 `logs/` 相同）。失败时不阻断进程，
/// 降级到 stderr 并继续运行（桌面版 stderr 不可见，但绝不能因此起不来）。
pub fn init() {
    START.get_or_init(Instant::now);
    // env 只作覆盖；没设时默认 4（信息级），与 config 的默认一致 ——
    // 之前默认 6 会把 TRACE 全打进 gui.log，和核的 log.level 对不上。
    let level = std::env::var("BUJIDAO_GUI_LOG_LEVEL")
        .ok()
        .and_then(|v| v.trim().parse::<u8>().ok())
        .filter(|v| (1..=6).contains(v))
        .unwrap_or(4);
    let _ = MIN_LEVEL.set(Mutex::new(level));

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
    // 先判级别再 init：被过滤掉的行不该触发建目录/开文件
    // （否则刚启动时的 TRACE 会把 logs/ 提前建出来，测试里也会污染仓库）。
    let min = min_level();
    if level > min {
        return;
    }
    if SINK.get().is_none() {
        init();
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

#[cfg(test)]
mod tests {
    use super::*;

    /// set_level/min_level 是进程级全局，测试默认并行执行会互相覆盖 ——
    /// 实测 CI 上 `log_above_threshold_returns_early` 就因此失败过。
    static LEVEL_LOCK: Mutex<()> = Mutex::new(());

    fn lock_levels() -> std::sync::MutexGuard<'static, ()> {
        LEVEL_LOCK.lock().unwrap_or_else(|e| e.into_inner())
    }

    #[test]
    fn level_names_cover_six_levels() {
        // 6 级语义是规范 §一 的约定，名字与顺序不能漂
        assert_eq!(LEVEL_NAMES.len(), 6);
        assert_eq!(LEVEL_NAMES[0], "CRITICAL");
        assert_eq!(LEVEL_NAMES[5], "TRACE");
    }

    #[test]
    fn set_level_ignores_out_of_range_and_applies_valid() {
        let _guard = lock_levels();
        // 越界值必须被忽略（否则会把日志全关或全开）
        set_level(0);
        set_level(9);
        // 合法值生效
        set_level(2);
        assert_eq!(min_level(), 2);
        set_level(5);
        assert_eq!(min_level(), 5);
    }

    #[test]
    fn log_above_threshold_returns_early() {
        let _guard = lock_levels();
        // 级别高于阈值时直接返回，不触碰 sink（这条路径最容易写出"以为没过但写了"）
        set_level(1);
        trace("test", "不该被写出");
        assert_eq!(min_level(), 1);
        set_level(4);
    }
}
