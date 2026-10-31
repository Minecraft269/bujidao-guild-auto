#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

//! Gui 进程入口：连核的命名管道、转发请求/事件、管窗口与托盘。
//!
//! 分工：界面在 WebView 里；进程能力（管道、托盘、窗口、几何记忆、DPI）在这里。
//! 规范要求「GUI 不读日志文件、不碰文件系统」，所以几何这类**界面自己的**状态
//! 由本进程自己存（不经过核），业务数据一律走 IPC。
//!
//! 可观测性（规范 §6.1/§6.4）：S0 启动、S4 UI 入口决策、S6 管道与 hello、
//! 每次 RPC、窗口/托盘创建、关闭流程、异常退出全部落 `logs/gui.log`。

mod ipc;
mod logger;
mod protocol;

use std::sync::{Mutex, MutexGuard};
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tauri::menu::{Menu, MenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{Emitter, Manager, WebviewWindow, WindowEvent};

/// 关闭策略的本地镜像：由前端在拿到配置后下发（配置的真源在核侧 config.json）。
#[derive(Clone, Serialize, Deserialize)]
struct ClosePolicy {
    enabled: bool,
    action: String,
    ask: bool,
}

impl Default for ClosePolicy {
    fn default() -> Self {
        Self {
            enabled: true,
            action: "ask".into(),
            ask: true,
        }
    }
}

#[derive(Clone, Copy, Serialize, Deserialize)]
struct Geo {
    x: i32,
    y: i32,
    w: u32,
    h: u32,
    maximized: bool,
}

struct AppState {
    // Arc：core_call 是 async 的，要把客户端句柄移进 spawn_blocking 里用
    ipc: Mutex<Option<std::sync::Arc<ipc::Ipc>>>,
    hello: Mutex<Option<String>>,
    policy: Mutex<ClosePolicy>,
    last_save: Mutex<Instant>,
    tray: Mutex<Option<tauri::tray::TrayIcon>>,
}

fn lock<T>(m: &Mutex<T>) -> MutexGuard<'_, T> {
    // 中毒只意味某处 panic 过；界面状态仍要能读，继续用里面的值
    m.lock().unwrap_or_else(|e| e.into_inner())
}

// ── 脱敏辅助（规范 §四 隐私） ────────────────────────────────────────────
fn masked_token(token: &str) -> String {
    if token.is_empty() {
        "<空>".to_string()
    } else {
        format!("<已脱敏 len={}>", token.chars().count())
    }
}

/// `--pipe` 的值内嵌 token（核侧 pipe_name = 前缀 + token），整串按 token 抠掉。
fn masked_pipe(pipe: &str, token: &str) -> String {
    if token.is_empty() {
        pipe.to_string()
    } else {
        pipe.replace(token, "<token>")
    }
}

fn masked_argv(token: &str) -> String {
    std::env::args()
        .map(|a| {
            if token.is_empty() {
                a
            } else {
                a.replace(token, "<token>")
            }
        })
        .collect::<Vec<_>>()
        .join(" ")
}

/// RPC 参数只记键名与大致体积，不落配置值/路径等可能敏感的原文。
fn params_summary(params: &Value) -> String {
    match params.as_object() {
        Some(obj) => {
            let keys: Vec<&str> = obj.keys().map(|k| k.as_str()).collect();
            format!("params_keys={keys:?} 字节={}", params.to_string().len())
        }
        None => format!("params=非对象 字节={}", params.to_string().len()),
    }
}

// ── IPC 命令 ──────────────────────────────────────────────────────────────
/// 转发一条请求给核。
///
/// 必须是 async + spawn_blocking：管道等待是阻塞的，若在 UI 主线程上等，
/// 事件循环会被卡住，Windows 直接把窗口标成「未响应」（首屏 config 就会撞上）。
#[tauri::command]
async fn core_call(
    state: tauri::State<'_, AppState>,
    method: String,
    params: Option<Value>,
) -> Result<Value, String> {
    let params = params.unwrap_or_else(|| json!({}));
    logger::debug(
        "rpc",
        &format!(
            "[ENTER] core_call method={method} {}",
            params_summary(&params)
        ),
    );
    let started = Instant::now();
    let client = lock(&state.ipc).clone();
    let Some(client) = client else {
        logger::warn("rpc", &format!("IPC 未连接，请求被丢弃: {method}"));
        return Ok(json!({ "ok": false, "error": "未连接核心" }));
    };
    let method_for_call = method.clone();
    let call = tauri::async_runtime::spawn_blocking(move || client.call(&method_for_call, params));
    match call.await {
        Ok(Ok(v)) => {
            let outcome = match v.get("ok").and_then(|b| b.as_bool()) {
                Some(false) => format!(
                    "error: {}",
                    v.get("error")
                        .and_then(|e| e.as_str())
                        .unwrap_or("<无错误文本>")
                ),
                _ => "ok".to_string(),
            };
            logger::debug(
                "rpc",
                &format!(
                    "[EXIT ] core_call method={method} 耗时={}ms 响应={outcome}",
                    started.elapsed().as_millis()
                ),
            );
            Ok(v)
        }
        Ok(Err(e)) => {
            logger::warn(
                "rpc",
                &format!(
                    "[EXIT ] core_call method={method} 耗时={}ms 失败: {e}",
                    started.elapsed().as_millis()
                ),
            );
            Ok(json!({ "ok": false, "error": e }))
        }
        Err(e) => {
            logger::error(
                "rpc",
                &format!(
                    "[EXIT ] core_call method={method} 耗时={}ms 请求任务异常: {e}",
                    started.elapsed().as_millis()
                ),
            );
            Ok(json!({ "ok": false, "error": format!("请求任务异常: {e}") }))
        }
    }
}

/// 前端日志桥：console 之外落到同一个 `logs/gui.log`（规范 §6.4）。
#[tauri::command]
fn gui_log(level: String, msg: String) {
    let lv = match level.as_str() {
        "critical" => 1,
        "error" => 2,
        "warning" | "warn" => 3,
        "info" => 4,
        "debug" => 5,
        "trace" => 6,
        other => {
            logger::warn(
                "frontend",
                &format!("未知前端日志级别 {other}，按 info 处理"),
            );
            4
        }
    };
    logger::log(lv, "frontend", &msg);
}

#[tauri::command]
fn hello_snapshot(state: tauri::State<'_, AppState>) -> Option<String> {
    lock(&state.hello).clone()
}

// ── 窗口命令（自绘标题栏用，§3.1） ──────────────────────────────────────
#[tauri::command]
fn win_minimize(w: WebviewWindow) {
    let _ = w.minimize();
}

#[tauri::command]
fn win_toggle_max(w: WebviewWindow) {
    let _ = if w.is_maximized().unwrap_or(false) {
        w.unmaximize()
    } else {
        w.maximize()
    };
}

#[tauri::command]
fn win_is_maximized(w: WebviewWindow) -> bool {
    w.is_maximized().unwrap_or(false)
}

#[tauri::command]
fn win_start_drag(w: WebviewWindow) {
    let _ = w.start_dragging();
}

#[tauri::command]
fn win_show(w: WebviewWindow) {
    let _ = w.show();
    let _ = w.set_focus();
}

#[tauri::command]
fn win_set_top(w: WebviewWindow, on: bool) {
    let _ = w.set_always_on_top(on);
}

/// 两套标题栏切换：自绘时去掉系统边框，原生时恢复（§3.1）
#[tauri::command]
fn win_set_titlebar(w: WebviewWindow, custom: bool) {
    let _ = w.set_decorations(!custom);
}

/// 小窗 360×150 ↔ 正常 1000×680（§3.2）。用 LogicalSize，150% DPI 下也按逻辑像素算。
#[tauri::command]
fn win_set_compact(state: tauri::State<'_, AppState>, w: WebviewWindow, on: bool) {
    let size = if on {
        tauri::LogicalSize::new(360.0, 150.0)
    } else {
        tauri::LogicalSize::new(1000.0, 680.0)
    };
    let _ = w.set_resizable(!on);
    let _ = w.set_size(tauri::Size::Logical(size));
    *lock(&state.last_save) = Instant::now();
}

#[tauri::command]
fn win_set_close_policy(
    state: tauri::State<'_, AppState>,
    enabled: bool,
    action: String,
    ask: bool,
) {
    *lock(&state.policy) = ClosePolicy {
        enabled,
        action,
        ask,
    };
}

/// 点关闭按钮：按策略决定隐藏/退出/询问（§5）
#[tauri::command]
fn win_close(app: tauri::AppHandle, w: WebviewWindow, state: tauri::State<'_, AppState>) {
    let p = lock(&state.policy).clone();
    logger::info(
        "close",
        &format!(
            "关闭请求 来源=按钮 policy enabled={} action={} ask={}",
            p.enabled, p.action, p.ask
        ),
    );
    // Python 侧合法值是 ask / minimize / exit（config 的 ui.tray.close_action）。
    // 以前只排除 exit|minimize，于是 ask 被当成"未知值"刷 WARNING。
    if p.enabled && !matches!(p.action.as_str(), "ask" | "minimize" | "exit") {
        logger::error(
            "close",
            &format!(
                "非法 close_action={}，按 ask 处理（合法值：ask/minimize/exit）",
                p.action
            ),
        );
    }
    if !p.enabled || p.action == "exit" {
        let reason = if !p.enabled {
            "policy.enabled=false"
        } else {
            "policy.action=exit"
        };
        logger::info("close", &format!("关闭动作=退出 原因={reason}"));
        save_geo(&app, &w);
        app.exit(0);
    } else if p.action == "minimize" {
        logger::info("close", "关闭动作=隐藏(minimize)");
        let _ = w.hide();
    } else if p.ask {
        logger::info("close", "关闭动作=询问前端(gui-close-request)");
        let _ = app.emit("gui-close-request", ());
    } else {
        logger::info("close", "关闭动作=隐藏(默认分支)");
        let _ = w.hide();
    }
}

/// 首窗询问的结果（§5）：记住选择后由前端写回配置
#[tauri::command]
fn win_close_decision(
    app: tauri::AppHandle,
    w: WebviewWindow,
    state: tauri::State<'_, AppState>,
    action: String,
    remember: bool,
) {
    logger::info(
        "close",
        &format!("关闭询问结果 来源=用户选择 action={action} remember={remember}"),
    );
    if remember {
        let mut p = lock(&state.policy);
        p.action = action.clone();
        p.ask = false;
    }
    if action == "minimize" {
        logger::info("close", "关闭动作=隐藏(用户选择 minimize)");
        let _ = w.hide();
    } else {
        logger::info("close", "关闭动作=退出(用户选择 exit)");
        save_geo(&app, &w);
        app.exit(0);
    }
}

/// 托盘四态（§5）：失败态带计数角标；提示只显示状态与进度，不含玩家名
#[tauri::command]
fn win_set_tray_state(
    app: tauri::AppHandle,
    app_state: tauri::State<'_, AppState>,
    state: String,
    fails: u32,
    tip: String,
) {
    let icon_file = if state == "failed" && fails > 0 {
        format!("tray-failed-{}.png", fails.min(9))
    } else {
        format!("tray-{state}.png")
    };
    if let Some(tray) = lock(&app_state.tray).as_ref() {
        match load_icon(&app, &icon_file) {
            Some(img) => {
                if let Err(e) = tray.set_icon(Some(img)) {
                    logger::warn("tray", &format!("设置托盘图标失败 file={icon_file}: {e}"));
                }
            }
            // 绝不静默：托盘透明是用户直接可见的缺陷，必须在日志里说清
            None => logger::warn(
                "tray",
                &format!(
                    "托盘图标不可用 file={icon_file}（内置/资源/同级目录都失败），保留上一个图标"
                ),
            ),
        }
        let _ = tray.set_tooltip(Some(tip));
    } else {
        logger::debug("tray", "托盘尚未创建，忽略状态更新");
    }
}

/// 图标查找顺序：**编进二进制** → 资源目录 → exe 同级 icons/（便携包布局）。
/// 内置优先：成品目录里没有 icons/，纯按路径找必然失败 —— 实测就是托盘全透明，
/// 且调用点 `let _ = set_icon(...)` 把错误吞了，日志里只剩一条 DEBUG。
fn builtin_icon(file: &str) -> Option<&'static [u8]> {
    Some(match file {
        "tray-idle.png" => include_bytes!("../icons/tray-idle.png"),
        "tray-running.png" => include_bytes!("../icons/tray-running.png"),
        "tray-paused.png" => include_bytes!("../icons/tray-paused.png"),
        "tray-failed.png" => include_bytes!("../icons/tray-failed.png"),
        "tray-failed-1.png" => include_bytes!("../icons/tray-failed-1.png"),
        "tray-failed-2.png" => include_bytes!("../icons/tray-failed-2.png"),
        "tray-failed-3.png" => include_bytes!("../icons/tray-failed-3.png"),
        "tray-failed-4.png" => include_bytes!("../icons/tray-failed-4.png"),
        "tray-failed-5.png" => include_bytes!("../icons/tray-failed-5.png"),
        "tray-failed-6.png" => include_bytes!("../icons/tray-failed-6.png"),
        "tray-failed-7.png" => include_bytes!("../icons/tray-failed-7.png"),
        "tray-failed-8.png" => include_bytes!("../icons/tray-failed-8.png"),
        "tray-failed-9.png" => include_bytes!("../icons/tray-failed-9.png"),
        _ => return None,
    })
}

fn load_icon(app: &tauri::AppHandle, file: &str) -> Option<tauri::image::Image<'static>> {
    if let Some(bytes) = builtin_icon(file) {
        match tauri::image::Image::from_bytes(bytes) {
            Ok(img) => {
                logger::debug("tray", &format!("图标加载成功 file={file} 来源=内置"));
                return Some(img);
            }
            Err(e) => logger::warn(
                "tray",
                &format!("内置图标解码失败 file={file}: {e}（继续尝试外部路径）"),
            ),
        }
    }
    let rel = format!("icons/{file}");
    let mut candidates: Vec<std::path::PathBuf> = Vec::new();
    for base in [
        tauri::path::BaseDirectory::Resource,
        tauri::path::BaseDirectory::Executable,
    ] {
        match app.path().resolve(&rel, base) {
            Ok(p) => candidates.push(p),
            Err(e) => logger::debug(
                "tray",
                &format!("图标候选路径解析失败 file={file} base={base:?}: {e}"),
            ),
        }
    }
    for p in &candidates {
        match tauri::image::Image::from_path(p) {
            Ok(img) => {
                logger::debug(
                    "tray",
                    &format!("图标加载成功 file={file} path={}", p.display()),
                );
                return Some(img);
            }
            Err(e) => logger::debug(
                "tray",
                &format!(
                    "图标候选加载失败 file={file} path={} 错误: {e}",
                    p.display()
                ),
            ),
        }
    }
    logger::warn(
        "tray",
        &format!(
            "托盘图标加载失败 file={file} 候选路径={:?}",
            candidates
                .iter()
                .map(|p| p.display().to_string())
                .collect::<Vec<_>>()
        ),
    );
    None
}

// ── 几何记忆（§三 窗口） ─────────────────────────────────────────────────
fn geo_path(app: &tauri::AppHandle) -> Option<std::path::PathBuf> {
    match app.path().app_config_dir() {
        Ok(d) => {
            let p = d.join("window.json");
            logger::trace("geo", &format!("几何文件路径={}", p.display()));
            Some(p)
        }
        Err(e) => {
            logger::warn("geo", &format!("无法获取配置目录，几何记忆不可用: {e}"));
            None
        }
    }
}

fn save_geo(app: &tauri::AppHandle, w: &WebviewWindow) {
    let pos = match w.outer_position() {
        Ok(p) => p,
        Err(e) => {
            logger::warn("geo", &format!("几何保存失败：读取窗口位置失败: {e}"));
            return;
        }
    };
    let size = match w.inner_size() {
        Ok(s) => s,
        Err(e) => {
            logger::warn("geo", &format!("几何保存失败：读取窗口尺寸失败: {e}"));
            return;
        }
    };
    let g = Geo {
        x: pos.x,
        y: pos.y,
        w: size.width,
        h: size.height,
        maximized: w.is_maximized().unwrap_or(false),
    };
    let Some(p) = geo_path(app) else { return };
    if let Some(dir) = p.parent() {
        if let Err(e) = std::fs::create_dir_all(dir) {
            logger::warn(
                "geo",
                &format!("几何保存失败：创建目录 {} 出错: {e}", dir.display()),
            );
        }
    }
    let text = match serde_json::to_string(&g) {
        Ok(t) => t,
        Err(e) => {
            logger::warn("geo", &format!("几何保存失败：序列化出错: {e}"));
            return;
        }
    };
    match std::fs::write(&p, text) {
        Ok(_) => logger::debug(
            "geo",
            &format!(
                "几何保存成功 path={} x={} y={} w={} h={} maximized={}",
                p.display(),
                g.x,
                g.y,
                g.w,
                g.h,
                g.maximized
            ),
        ),
        Err(e) => logger::warn(
            "geo",
            &format!("几何保存失败 path={} 错误: {e}", p.display()),
        ),
    }
}

fn restore_geo(w: &WebviewWindow) {
    let app = w.app_handle().clone();
    let Some(p) = geo_path(&app) else { return };
    let text = match std::fs::read_to_string(&p) {
        Ok(t) => t,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            logger::debug(
                "geo",
                &format!("几何文件不存在，使用默认位置 path={}", p.display()),
            );
            return;
        }
        Err(e) => {
            logger::warn(
                "geo",
                &format!("几何恢复失败：读取文件失败 path={} 错误: {e}", p.display()),
            );
            return;
        }
    };
    let g = match serde_json::from_str::<Geo>(&text) {
        Ok(g) => g,
        Err(e) => {
            logger::warn(
                "geo",
                &format!(
                    "几何恢复失败：解析 JSON 失败 path={} 错误: {e}",
                    p.display()
                ),
            );
            return;
        }
    };
    if let Err(e) = w.set_size(tauri::PhysicalSize::new(g.w.max(360), g.h.max(150))) {
        logger::warn("geo", &format!("几何恢复：设置窗口尺寸失败: {e}"));
    }
    // 上次位置已在屏幕外（换显示器/拔扩展屏）→ 回落居中
    let monitors = match w.available_monitors() {
        Ok(m) => m,
        Err(e) => {
            logger::warn("geo", &format!("几何恢复：枚举显示器失败，按居中处理: {e}"));
            Vec::new()
        }
    };
    let on_screen = monitors.iter().any(|m| {
        let (mx, my) = (m.position().x, m.position().y);
        let (mw, mh) = (m.size().width as i32, m.size().height as i32);
        g.x + g.w as i32 > mx && g.x < mx + mw && g.y + g.h as i32 > my && g.y < my + mh
    });
    if on_screen {
        match w.set_position(tauri::PhysicalPosition::new(g.x, g.y)) {
            Ok(_) => logger::info(
                "geo",
                &format!(
                    "几何恢复命中 path={} 位置=({},{}) 尺寸={}x{} maximized={}",
                    p.display(),
                    g.x,
                    g.y,
                    g.w,
                    g.h,
                    g.maximized
                ),
            ),
            Err(e) => logger::warn("geo", &format!("几何恢复：设置窗口位置失败: {e}")),
        }
    } else {
        match w.center() {
            Ok(_) => logger::info(
                "geo",
                &format!(
                    "几何恢复：上次位置不在任何显示器内，回落居中 path={}",
                    p.display()
                ),
            ),
            Err(e) => logger::warn("geo", &format!("几何恢复：居中失败: {e}")),
        }
    }
    if g.maximized {
        if let Err(e) = w.maximize() {
            logger::warn("geo", &format!("几何恢复：最大化失败: {e}"));
        }
    }
}

fn arg_value(name: &str) -> Option<String> {
    let mut args = std::env::args().skip(1);
    while let Some(a) = args.next() {
        if a == name {
            return args.next();
        }
    }
    None
}

fn main() {
    logger::init();
    // windows_subsystem="windows" 下 stderr 不可见：panic 必须落文件，含调用栈
    std::panic::set_hook(Box::new(|info| {
        let thread = std::thread::current();
        let thread_name = thread.name().unwrap_or("<未命名线程>");
        let payload = if let Some(s) = info.payload().downcast_ref::<&str>() {
            (*s).to_string()
        } else if let Some(s) = info.payload().downcast_ref::<String>() {
            s.clone()
        } else {
            "<非字符串 panic>".to_string()
        };
        let location = info
            .location()
            .map(|l| format!("{}:{}:{}", l.file(), l.line(), l.column()))
            .unwrap_or_else(|| "<未知位置>".to_string());
        let backtrace = std::backtrace::Backtrace::force_capture();
        logger::critical(
            "panic",
            &format!("线程 {thread_name} panic: {payload} 位置={location}\n{backtrace}"),
        );
    }));

    let pipe = arg_value("--pipe");
    let token = arg_value("--token").unwrap_or_default();
    let smoke = std::env::args().any(|a| a == "--smoke-test");
    let dpi_check = std::env::args().any(|a| a == "--dpi-check");

    // S0 进程启动（§6.1）
    let cwd = std::env::current_dir()
        .map(|p| p.display().to_string())
        .unwrap_or_else(|e| format!("<无法获取: {e}>"));
    let exe = std::env::current_exe()
        .map(|p| p.display().to_string())
        .unwrap_or_else(|e| format!("<无法获取: {e}>"));
    logger::info(
        "startup",
        &format!(
            "[S0][ENTER] 进程启动 argv={} cwd={} exe={} version={} variant={} capabilities=待握手",
            masked_argv(&token),
            cwd,
            exe,
            env!("CARGO_PKG_VERSION"),
            if cfg!(debug_assertions) {
                "debug"
            } else {
                "release"
            }
        ),
    );
    // S4 UI 入口决策（§6.5）
    logger::info(
        "startup",
        &format!(
            "[S4] UI 入口决策 pipe={} token={} smoke_test={smoke} dpi_check={dpi_check} 最终走向={}",
            pipe.as_deref()
                .map(|p| masked_pipe(p, &token))
                .unwrap_or_else(|| "<未传 --pipe>".to_string()),
            masked_token(&token),
            if pipe.is_some() {
                "连接核心"
            } else {
                "仅窗口（未传 --pipe，不连核）"
            }
        ),
    );

    let run_result = tauri::Builder::default()
        .manage(AppState {
            ipc: Mutex::new(None),
            hello: Mutex::new(None),
            policy: Mutex::new(ClosePolicy::default()),
            last_save: Mutex::new(Instant::now()),
            tray: Mutex::new(None),
        })
        .invoke_handler(tauri::generate_handler![
            core_call,
            gui_log,
            hello_snapshot,
            win_minimize,
            win_toggle_max,
            win_is_maximized,
            win_start_drag,
            win_show,
            win_set_top,
            win_set_titlebar,
            win_set_compact,
            win_set_close_policy,
            win_close,
            win_close_decision,
            win_set_tray_state
        ])
        .on_window_event(|window, event| {
            let app = window.app_handle().clone();
            let state = app.state::<AppState>();
            match event {
                WindowEvent::CloseRequested { api, .. } => {
                    // 托盘策略接管关闭：运行中不得直接退出（§5）
                    api.prevent_close();
                    let p = lock(&state.policy).clone();
                    logger::info(
                        "close",
                        &format!(
                            "关闭请求 来源=系统 CloseRequested policy enabled={} action={} ask={}",
                            p.enabled, p.action, p.ask
                        ),
                    );
                    if p.enabled && !matches!(p.action.as_str(), "ask" | "minimize" | "exit") {
                        logger::warn(
                            "close",
                            &format!(
                                "非法 close_action={}，按 ask 处理（合法值：ask/minimize/exit）",
                                p.action
                            ),
                        );
                    }
                    if !p.enabled || p.action == "exit" {
                        let reason = if !p.enabled {
                            "policy.enabled=false"
                        } else {
                            "policy.action=exit"
                        };
                        logger::info("close", &format!("关闭动作=退出 原因={reason}"));
                        if let Some(w) = app.get_webview_window("main") {
                            save_geo(&app, &w);
                        }
                        app.exit(0);
                    } else if p.action == "minimize" {
                        logger::info("close", "关闭动作=隐藏(minimize)");
                        let _ = window.hide();
                    } else if p.ask {
                        logger::info("close", "关闭动作=询问前端(gui-close-request)");
                        let _ = app.emit("gui-close-request", ());
                    } else {
                        logger::info("close", "关闭动作=隐藏(默认分支)");
                        let _ = window.hide();
                    }
                }
                WindowEvent::Moved(_) | WindowEvent::Resized(_) => {
                    // 节流：连续拖动时最多每 800ms 落盘一次
                    let mut last = lock(&state.last_save);
                    if last.elapsed() > Duration::from_millis(800) {
                        *last = Instant::now();
                        drop(last);
                        if let Some(w) = app.get_webview_window("main") {
                            save_geo(&app, &w);
                        }
                    }
                }
                _ => {}
            }
        })
        .setup(move |app| {
            let handle = app.handle().clone();

            match app.get_webview_window("main") {
                Some(w) => {
                    logger::info("window", "[ENTER] 主窗口就绪 label=main，开始恢复几何");
                    restore_geo(&w);
                    logger::info("window", "[EXIT ] 主窗口几何恢复完成");
                }
                None => logger::warn(
                    "window",
                    "未找到 main 窗口（tauri.conf.json 未创建？），跳过几何恢复",
                ),
            }

            // 托盘（§5）：四态图标 + 显示/暂停/继续/退出
            logger::info("tray", "[ENTER] 托盘创建开始");
            let show = MenuItem::with_id(app, "show", "显示窗口", true, None::<&str>).map_err(
                |e| {
                    logger::error("tray", &format!("托盘菜单项 show 创建失败: {e}"));
                    e
                },
            )?;
            let pause = MenuItem::with_id(app, "pause", "暂停", true, None::<&str>).map_err(
                |e| {
                    logger::error("tray", &format!("托盘菜单项 pause 创建失败: {e}"));
                    e
                },
            )?;
            let resume = MenuItem::with_id(app, "resume", "继续", true, None::<&str>).map_err(
                |e| {
                    logger::error("tray", &format!("托盘菜单项 resume 创建失败: {e}"));
                    e
                },
            )?;
            let quit = MenuItem::with_id(app, "quit", "退出", true, None::<&str>).map_err(
                |e| {
                    logger::error("tray", &format!("托盘菜单项 quit 创建失败: {e}"));
                    e
                },
            )?;
            let menu = Menu::with_items(app, &[&show, &pause, &resume, &quit]).map_err(|e| {
                logger::error("tray", &format!("托盘菜单创建失败: {e}"));
                e
            })?;
            let mut builder = TrayIconBuilder::with_id("main")
                .menu(&menu)
                .tooltip("布吉岛公会管理脚本")
                .on_menu_event(|app, event| match event.id().as_ref() {
                    "show" => {
                        if let Some(w) = app.get_webview_window("main") {
                            let _ = w.show();
                            let _ = w.set_focus();
                        }
                    }
                    // 退出是真退出，不走 close_action（§5）
                    "quit" => {
                        logger::info("close", "关闭动作=退出 来源=托盘 quit");
                        app.exit(0);
                    }
                    other => {
                        let _ = app.emit("tray-command", json!({ "action": other }));
                    }
                })
                .on_tray_icon_event(|tray, event| {
                    if let tauri::tray::TrayIconEvent::DoubleClick { .. } = event {
                        let app = tray.app_handle();
                        if let Some(w) = app.get_webview_window("main") {
                            let _ = w.show();
                            let _ = w.set_focus();
                        }
                    }
                });
            match load_icon(&handle, "tray-idle.png") {
                Some(img) => builder = builder.icon(img),
                // 透明托盘的元凶就是这里曾经静默跳过
                None => logger::warn("tray", "托盘初始图标不可用，托盘会显示为空（见上一条失败原因）"),
            }
            let tray = builder.build(app).map_err(|e| {
                logger::error("tray", &format!("托盘创建失败（build 返回 Err）: {e}"));
                e
            })?;
            *lock(&app.state::<AppState>().tray) = Some(tray);
            logger::info("tray", "[EXIT ] 托盘创建完成");

            // 管道连接放后台线程：连不上也不该卡住首屏
            if let Some(pipe_name) = pipe {
                let handle_for_events = handle.clone();
                let handle_for_hello = handle.clone();
                let token_for_connect = token.clone();
                let spawn = std::thread::Builder::new()
                    .name("ipc-handshake".into())
                    .spawn(move || {
                        logger::info(
                            "ipc",
                            &format!(
                                "[S6][ENTER] 握手线程启动 pipe={}",
                                masked_pipe(&pipe_name, &token_for_connect)
                            ),
                        );
                        let on_event = move |v: Value| {
                            let _ = handle_for_events.emit("core-event", v);
                        };
                        match ipc::Ipc::connect(&pipe_name, &token_for_connect, on_event) {
                            Ok(client) => {
                                // 握手：把核下发的变体/能力存快照并推给前端
                                let hello_started = Instant::now();
                                logger::info("ipc", "[ENTER] hello 发送");
                                // 实测约 1/4 的启动（打包态更高）会出现「管道连接成功、
                                // 但 hello 字节没到核」：核的读循环仍在 ReadFile 上等，
                                // 此时在同一连接上重发一次就能到，比重新建连便宜得多。
                                let mut hello_reply = client.call("hello", json!({}));
                                if hello_reply.is_err() {
                                    let why = hello_reply.as_ref().err().cloned().unwrap_or_default();
                                    logger::warn(
                                        "ipc",
                                        &format!("hello 首次失败，在同一连接上重发: {why}"),
                                    );
                                    hello_reply = client.call("hello", json!({}));
                                }
                                match hello_reply {
                                    Ok(reply) => {
                                        let elapsed = hello_started.elapsed().as_millis();
                                        let ok =
                                            reply.get("ok").and_then(|b| b.as_bool()).unwrap_or(true);
                                        if ok {
                                            let result = reply.get("result").unwrap_or(&reply);
                                            let variant = result
                                                .get("variant")
                                                .and_then(|v| v.as_str())
                                                .unwrap_or("?");
                                            let caps = result
                                                .get("capabilities")
                                                .and_then(|v| v.as_array())
                                                .map(|a| {
                                                    a.iter()
                                                        .filter_map(|x| x.as_str())
                                                        .collect::<Vec<_>>()
                                                        .join(",")
                                                })
                                                .unwrap_or_default();
                                            let first_run = result
                                                .get("first_run")
                                                .and_then(|v| v.as_bool())
                                                .unwrap_or(false);
                                            // GUI 日志级别以配置为准（env 只作覆盖）：
                                            // 启动时只知道 env，真正的 log.level 要等这里。
                                            if let Some(level) = result
                                                .get("log_level")
                                                .and_then(|v| v.as_u64())
                                            {
                                                logger::set_level(level as u8);
                                                logger::info(
                                                    "log",
                                                    &format!("日志级别已按配置调整 level={level}"),
                                                );
                                            }
                                            logger::info(
                                                "ipc",
                                                &format!(
                                                    "[EXIT ] hello 往返耗时={elapsed}ms 结果=ok variant={variant} capabilities=[{caps}] first_run={first_run}"
                                                ),
                                            );
                                        } else {
                                            logger::error(
                                                "ipc",
                                                &format!(
                                                    "[EXIT ] hello 往返耗时={elapsed}ms 结果=error {}",
                                                    reply
                                                        .get("error")
                                                        .and_then(|e| e.as_str())
                                                        .unwrap_or("<无错误文本>")
                                                ),
                                            );
                                        }
                                        let text = reply.to_string();
                                        *lock(&handle_for_hello.state::<AppState>().hello) =
                                            Some(text.clone());
                                        let _ = handle_for_hello.emit("ipc-hello", text);
                                    }
                                    Err(e) => {
                                        logger::error(
                                            "ipc",
                                            &format!(
                                                "[EXIT ] hello 往返耗时={}ms 结果=error 错误: {e}",
                                                hello_started.elapsed().as_millis()
                                            ),
                                        );
                                        let _ = handle_for_hello
                                            .emit("ipc-error", format!("握手失败: {e}"));
                                    }
                                }
                                *lock(&handle_for_hello.state::<AppState>().ipc) =
                                    Some(std::sync::Arc::new(client));
                                logger::info("ipc", "[S6][EXIT ] 握手线程完成，核心已就绪");
                            }
                            Err(e) => {
                                logger::error(
                                    "ipc",
                                    &format!("[S6][EXIT ] 连接核心失败（重试耗尽）: {e}"),
                                );
                                let _ = handle_for_hello.emit("ipc-error", e);
                                let _ = handle_for_hello.emit("ipc-closed", "核心未连接");
                            }
                        }
                    });
                if let Err(e) = spawn {
                    logger::error("ipc", &format!("握手线程创建失败: {e}"));
                }
            } else {
                logger::info("ipc", "未传 --pipe：跳过连接核心（仅窗口模式）");
            }

            if smoke {
                // 规范 §10.3：冒烟必须真启动并自关
                let h = app.handle().clone();
                std::thread::spawn(move || {
                    std::thread::sleep(Duration::from_secs(2));
                    logger::info("startup", "smoke-test 到时，自动退出");
                    h.exit(0);
                });
            }

            if dpi_check {
                // DPI 诊断：逻辑尺寸应恒为 1000×680，物理尺寸随缩放因子变化
                if let Some(w) = app.get_webview_window("main") {
                    let sf = w.scale_factor().unwrap_or(1.0);
                    if let Ok(size) = w.inner_size() {
                        let logical = (
                            (size.width as f64 / sf).round() as u32,
                            (size.height as f64 / sf).round() as u32,
                        );
                        logger::info(
                            "startup",
                            &format!(
                                "dpi-check scale_factor={sf} physical={size:?} logical={logical:?}"
                            ),
                        );
                        println!("scale_factor={sf} physical={size:?} logical={logical:?}");
                    }
                }
                let h = app.handle().clone();
                std::thread::spawn(move || {
                    std::thread::sleep(Duration::from_secs(1));
                    logger::info("startup", "dpi-check 到时，自动退出");
                    h.exit(0);
                });
            }
            Ok(())
        })
        .run(tauri::generate_context!());

    match run_result {
        Ok(()) => logger::info("main", "事件循环正常退出"),
        Err(e) => {
            logger::critical("main", &format!("致命：tauri 运行失败/进程异常退出: {e:?}"));
            std::process::exit(1);
        }
    }
}
