fn main() {
    // 前端资源是编译期内嵌的（tauri.conf.json 的 frontendDist = "../src"）。
    // tauri-build 只跟踪 tauri.conf.json 与 capabilities，**不跟踪前端目录**，
    // 于是改完 app.js/styles.css 后 cargo 根本不重新链接 —— 本地构建会悄悄
    // 带着旧界面，看起来「改了没用」。实测：改 app.js 后 cargo build 退出 0，
    // 但 Gui.exe 时间戳与内容都没变。
    println!("cargo:rerun-if-changed=../src");
    tauri_build::build()
}
