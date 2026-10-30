#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

//! Gui 进程入口（骨架）：连接核的命名管道，完成 hello 握手，把结果推给前端。
//!
//! 为什么桥接放在 Rust：WebView 里的 JS 碰不到 Windows 命名管道，而规范要求
//! "GUI 不读日志文件、只用核推送的结构化事件"，所以连接与逐行读取都在 Rust 侧。

use std::io::{BufRead, BufReader, Write};

use tauri::{Emitter, Manager};

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

    tauri::Builder::default()
        .setup(move |app| {
            let handle = app.handle().clone();
            if let Some(pipe_name) = pipe {
                std::thread::spawn(move || {
                    let path = format!(r"\\.\pipe\{}", pipe_name);
                    match std::fs::OpenOptions::new()
                        .read(true)
                        .write(true)
                        .open(&path)
                    {
                        Ok(stream) => {
                            let mut writer = match stream.try_clone() {
                                Ok(w) => w,
                                Err(e) => {
                                    let _ =
                                        handle.emit("ipc-error", format!("复制管道句柄失败: {e}"));
                                    return;
                                }
                            };
                            let req =
                                serde_json::json!({"id": 1, "method": "hello", "token": token});
                            if let Err(e) = writeln!(writer, "{}", req) {
                                let _ = handle.emit("ipc-error", format!("发送握手失败: {e}"));
                                return;
                            }
                            let reader = BufReader::new(stream);
                            let mut lines = reader.lines();
                            // 第一行是握手应答（带 id），之后是无 id 的事件推送
                            if let Some(Ok(first)) = lines.next() {
                                let _ = handle.emit("ipc-hello", first);
                            }
                            for line in lines {
                                match line {
                                    Ok(l) => {
                                        let _ = handle.emit("ipc-event", l);
                                    }
                                    Err(e) => {
                                        let _ =
                                            handle.emit("ipc-error", format!("读取管道失败: {e}"));
                                        break;
                                    }
                                }
                            }
                            let _ = handle.emit("ipc-closed", "核心已断开");
                        }
                        Err(e) => {
                            let _ = handle.emit("ipc-error", format!("连接核心失败: {e}"));
                        }
                    }
                });
            }
            if smoke {
                // 规范 §10.3：冒烟必须真启动并自关，不接受"进程能起来就算过"
                let h = app.handle().clone();
                std::thread::spawn(move || {
                    std::thread::sleep(std::time::Duration::from_secs(2));
                    h.exit(0);
                });
            }
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
