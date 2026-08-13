use serde::Serialize;
use tauri::{Emitter, Manager, WindowEvent};

mod data_dir;
mod env;
mod orchestrator;
mod processes;
mod runtime;

pub struct AppState {
    pub force_exit: std::sync::atomic::AtomicBool,
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            let _ = app
                .get_webview_window("main")
                .expect("main window must exist")
                .show()
                .and_then(|_| {
                    app.get_webview_window("main")
                        .expect("main window must exist")
                        .set_focus()
                });
        }))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .manage(AppState {
            force_exit: std::sync::atomic::AtomicBool::new(false),
        })
        .setup(|app| {
            // 静态测试事件:验证 Rust→加载页进度桥(编排器在 Task 9 接入后替换为真实进度)
            emit_startup_progress(
                app.handle(),
                &StartupProgress {
                    step: "预检环境".into(),
                    status: "pending".into(),
                    detail: None,
                },
            )?;
            let window = app
                .get_webview_window("main")
                .expect("main window must exist");
            // 桌面壳注入 API 基址;前端 resolveApiBase 优先读它(Task 2)
            let _ = window.eval(
                "window.__HUB_API_BASE__ = 'http://127.0.0.1:8767/api/v1';",
            );
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                let state = window.state::<AppState>();
                if !state.force_exit.load(std::sync::atomic::Ordering::SeqCst) {
                    // 点 X = 隐藏到托盘(托盘在 Task 10 接入;在此之前 X 只隐藏,可从任务栏图标恢复)
                    api.prevent_close();
                    let _ = window.hide();
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running the desktop shell");
}

#[derive(Clone, Serialize)]
pub struct StartupProgress {
    pub step: String,
    pub status: String, // "pending" | "done" | "failed"
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
}

/// 把 Rust 侧 startup-progress 事件桥接到加载页的 window.__hubProgress。
/// 通过 evaluate_script 调用而非让加载页引 @tauri-apps/api,保持前端零依赖。
pub fn emit_startup_progress(
    app: &tauri::AppHandle,
    progress: &StartupProgress,
) -> tauri::Result<()> {
    let json = serde_json::to_string(progress)
        .map_err(|e| tauri::Error::Anyhow(e.into()))?;
    let script = format!(
        "window.__hubProgress && window.__hubProgress({json});"
    );
    if let Some(window) = app.get_webview_window("main") {
        window.eval(&script)?;
    }
    Ok(())
}
