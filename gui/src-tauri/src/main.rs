#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

//! Gui 进程入口：连核的命名管道、转发请求/事件、管窗口与托盘。
//!
//! 分工：界面在 WebView 里；进程能力（管道、托盘、窗口、几何记忆、DPI）在这里。
//! 规范要求「GUI 不读日志文件、不碰文件系统」，所以几何这类**界面自己的**状态
//! 由本进程自己存（不经过核），业务数据一律走 IPC。

mod ipc;
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
    let client = lock(&state.ipc).clone();
    let Some(client) = client else {
        return Ok(json!({ "ok": false, "error": "未连接核心" }));
    };
    let call = tauri::async_runtime::spawn_blocking(move || {
        client.call(&method, params.unwrap_or_else(|| json!({})))
    });
    match call.await {
        Ok(Ok(v)) => Ok(v),
        Ok(Err(e)) => Ok(json!({ "ok": false, "error": e })),
        Err(e) => Ok(json!({ "ok": false, "error": format!("请求任务异常: {e}") })),
    }
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
    if !p.enabled || p.action == "exit" {
        save_geo(&app, &w);
        app.exit(0);
    } else if p.action == "minimize" {
        let _ = w.hide();
    } else if p.ask {
        let _ = app.emit("gui-close-request", ());
    } else {
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
    if remember {
        let mut p = lock(&state.policy);
        p.action = action.clone();
        p.ask = false;
    }
    if action == "minimize" {
        let _ = w.hide();
    } else {
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
        if let Some(img) = load_icon(&app, &icon_file) {
            let _ = tray.set_icon(Some(img));
        }
        let _ = tray.set_tooltip(Some(tip));
    }
}

/// 图标查找顺序：资源目录 → exe 同级 icons/（便携包布局）
fn load_icon(app: &tauri::AppHandle, file: &str) -> Option<tauri::image::Image<'static>> {
    let rel = format!("icons/{file}");
    let mut candidates: Vec<std::path::PathBuf> = Vec::new();
    for base in [
        tauri::path::BaseDirectory::Resource,
        tauri::path::BaseDirectory::Executable,
    ] {
        if let Ok(p) = app.path().resolve(&rel, base) {
            candidates.push(p);
        }
    }
    for p in candidates {
        if let Ok(img) = tauri::image::Image::from_path(&p) {
            return Some(img);
        }
    }
    None
}

// ── 几何记忆（§三 窗口） ─────────────────────────────────────────────────
fn geo_path(app: &tauri::AppHandle) -> Option<std::path::PathBuf> {
    app.path()
        .app_config_dir()
        .ok()
        .map(|d| d.join("window.json"))
}

fn save_geo(app: &tauri::AppHandle, w: &WebviewWindow) {
    let (Ok(pos), Ok(size)) = (w.outer_position(), w.inner_size()) else {
        return;
    };
    let g = Geo {
        x: pos.x,
        y: pos.y,
        w: size.width,
        h: size.height,
        maximized: w.is_maximized().unwrap_or(false),
    };
    if let Some(p) = geo_path(app) {
        if let Some(dir) = p.parent() {
            let _ = std::fs::create_dir_all(dir);
        }
        if let Ok(text) = serde_json::to_string(&g) {
            let _ = std::fs::write(p, text);
        }
    }
}

fn restore_geo(w: &WebviewWindow) {
    let app = w.app_handle().clone();
    let Some(p) = geo_path(&app) else { return };
    let Ok(text) = std::fs::read_to_string(p) else {
        return;
    };
    let Ok(g) = serde_json::from_str::<Geo>(&text) else {
        return;
    };
    let _ = w.set_size(tauri::PhysicalSize::new(g.w.max(360), g.h.max(150)));
    // 上次位置已在屏幕外（换显示器/拔扩展屏）→ 回落居中
    let on_screen = w.available_monitors().unwrap_or_default().iter().any(|m| {
        let (mx, my) = (m.position().x, m.position().y);
        let (mw, mh) = (m.size().width as i32, m.size().height as i32);
        g.x + g.w as i32 > mx && g.x < mx + mw && g.y + g.h as i32 > my && g.y < my + mh
    });
    if on_screen {
        let _ = w.set_position(tauri::PhysicalPosition::new(g.x, g.y));
    } else {
        let _ = w.center();
    }
    if g.maximized {
        let _ = w.maximize();
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
    let pipe = arg_value("--pipe");
    let token = arg_value("--token").unwrap_or_default();
    let smoke = std::env::args().any(|a| a == "--smoke-test");
    let dpi_check = std::env::args().any(|a| a == "--dpi-check");

    tauri::Builder::default()
        .manage(AppState {
            ipc: Mutex::new(None),
            hello: Mutex::new(None),
            policy: Mutex::new(ClosePolicy::default()),
            last_save: Mutex::new(Instant::now()),
            tray: Mutex::new(None),
        })
        .invoke_handler(tauri::generate_handler![
            core_call,
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
                    if !p.enabled || p.action == "exit" {
                        if let Some(w) = app.get_webview_window("main") {
                            save_geo(&app, &w);
                        }
                        app.exit(0);
                    } else if p.action == "minimize" {
                        let _ = window.hide();
                    } else if p.ask {
                        let _ = app.emit("gui-close-request", ());
                    } else {
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

            if let Some(w) = app.get_webview_window("main") {
                restore_geo(&w);
            }

            // 托盘（§5）：四态图标 + 显示/暂停/继续/退出
            let show = MenuItem::with_id(app, "show", "显示窗口", true, None::<&str>)?;
            let pause = MenuItem::with_id(app, "pause", "暂停", true, None::<&str>)?;
            let resume = MenuItem::with_id(app, "resume", "继续", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show, &pause, &resume, &quit])?;
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
                    "quit" => app.exit(0),
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
            if let Some(img) = load_icon(&handle, "tray-idle.png") {
                builder = builder.icon(img);
            }
            let tray = builder.build(app)?;
            *lock(&app.state::<AppState>().tray) = Some(tray);

            // 管道连接放后台线程：连不上也不该卡住首屏
            if let Some(pipe_name) = pipe {
                let handle_for_events = handle.clone();
                let handle_for_hello = handle.clone();
                std::thread::spawn(move || {
                    let on_event = move |v: Value| {
                        let _ = handle_for_events.emit("core-event", v);
                    };
                    match ipc::Ipc::connect(&pipe_name, &token, on_event) {
                        Ok(client) => {
                            // 握手：把核下发的变体/能力存快照并推给前端
                            match client.call("hello", json!({})) {
                                Ok(reply) => {
                                    let text = reply.to_string();
                                    *lock(&handle_for_hello.state::<AppState>().hello) =
                                        Some(text.clone());
                                    let _ = handle_for_hello.emit("ipc-hello", text);
                                }
                                Err(e) => {
                                    let _ = handle_for_hello
                                        .emit("ipc-error", format!("握手失败: {e}"));
                                }
                            }
                            *lock(&handle_for_hello.state::<AppState>().ipc) =
                                Some(std::sync::Arc::new(client));
                        }
                        Err(e) => {
                            let _ = handle_for_hello.emit("ipc-error", e);
                            let _ = handle_for_hello.emit("ipc-closed", "核心未连接");
                        }
                    }
                });
            }

            if smoke {
                // 规范 §10.3：冒烟必须真启动并自关
                let h = app.handle().clone();
                std::thread::spawn(move || {
                    std::thread::sleep(Duration::from_secs(2));
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
                        println!("scale_factor={sf} physical={size:?} logical={logical:?}");
                    }
                }
                let h = app.handle().clone();
                std::thread::spawn(move || {
                    std::thread::sleep(Duration::from_secs(1));
                    h.exit(0);
                });
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
