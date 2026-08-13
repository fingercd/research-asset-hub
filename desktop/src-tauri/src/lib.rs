mod data_dir;
mod env;
mod orchestrator;
mod processes;
mod runtime;
mod tray;

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;

use serde::Serialize;
use tauri::{Manager, WindowEvent};
use tauri_plugin_dialog::DialogExt;

use orchestrator::{Orchestrator, StartupError, StepStatus};

pub struct AppState {
    pub force_exit: AtomicBool,
    pub orchestrator: Mutex<Option<Orchestrator>>,
}

#[derive(Clone, Serialize)]
pub struct StartupProgress {
    pub step: String,
    pub status: String, // "pending" | "done" | "failed"
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
}

/// 把 Rust 侧 startup-progress 事件桥接到加载页的 window.__hubProgress。
/// 通过 eval 调用而非让加载页引 @tauri-apps/api,保持前端零依赖。
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

/// 在编排线程执行一次完整启动;成功则导航到主界面,失败则弹窗。
/// 重试路径:开头先 shutdown 掉上一次半启动的子进程,避免端口占用误报。
fn run_startup(app: tauri::AppHandle) {
    {
        let state = app.state::<AppState>();
        let mut guard = state.orchestrator.lock().unwrap();
        if let Some(orch) = guard.as_mut() {
            orch.shutdown();
        }
        *guard = None;
    }

    let resource = app.path().resource_dir().unwrap_or_default();
    let mut paths = runtime::resolve(&resource);
    let mut uses_containers = false;
    if !paths.python.exists() {
        // 开发回退:便携 runtime 未组装,用项目根 .venv + docker compose 容器
        // (CARGO_MANIFEST_DIR = desktop/src-tauri,上两级即项目根)
        let manifest = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
        let project_root = manifest
            .parent()
            .and_then(|p| p.parent())
            .unwrap_or(manifest);
        paths = runtime::resolve_dev(project_root);
        uses_containers = true;
    }

    let ports = orchestrator::Ports::from_env();
    let _ = env::prepare(&ports);

    let app_for_events = app.clone();
    let orchestrator = Orchestrator::new(paths, ports, move |event| {
        let _ = emit_startup_progress(
            &app_for_events,
            &StartupProgress {
                step: event.step.as_str().to_string(),
                status: match event.status {
                    StepStatus::Pending => "pending",
                    StepStatus::Done => "done",
                    StepStatus::Failed => "failed",
                }
                .to_string(),
                detail: event.detail.clone(),
            },
        );
    });
    let orchestrator = if uses_containers {
        orchestrator.with_containers()
    } else {
        orchestrator
    };

    let state = app.state::<AppState>();
    *state.orchestrator.lock().unwrap() = Some(orchestrator);

    let result = {
        let mut guard = state.orchestrator.lock().unwrap();
        guard.as_mut().expect("just inserted").start()
    };

    match result {
        Ok(()) => {
            // 编排完成即就绪,导航到主界面
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.eval("location.replace('index.html');");
            }
        }
        Err(StartupError::StepFailed { step, detail }) => {
            let message = format!("{}失败\n\n{}", step.as_str(), detail);
            app.dialog()
                .message(message)
                .title("研究资产库启动失败")
                .blocking_show();
        }
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.show();
                let _ = w.set_focus();
            }
        }))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .manage(AppState {
            force_exit: AtomicBool::new(false),
            orchestrator: Mutex::new(None),
        })
        .setup(|app| {
            // 桌面壳注入 API 基址;前端 resolveApiBase 优先读它(Task 2)
            let api_port = orchestrator::Ports::from_env().api;
            let window = app
                .get_webview_window("main")
                .expect("main window must exist");
            let _ = window.eval(format!(
                "window.__HUB_API_BASE__ = 'http://127.0.0.1:{api_port}/api/v1';"
            ));

            let rx = tray::create_tray(app.handle(), true)
                .expect("tray creation must succeed")
                .expect("channel must be present");

            let handle = app.handle().clone();
            std::thread::spawn(move || {
                // 托盘信号循环(独立线程,避免阻塞事件循环)
                while let Ok(signal) = rx.recv() {
                    match signal {
                        tray::TraySignal::Show => {}
                        tray::TraySignal::Retry => run_startup(handle.clone()),
                        tray::TraySignal::Quit => {
                            let state = handle.state::<AppState>();
                            state.force_exit.store(true, Ordering::SeqCst);
                            if let Some(w) = handle.get_webview_window("main") {
                                let _ = w.destroy();
                            }
                            handle.exit(0);
                        }
                    }
                }
            });

            let handle = app.handle().clone();
            std::thread::spawn(move || run_startup(handle));
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                let state = window.state::<AppState>();
                if state.force_exit.load(Ordering::SeqCst) {
                    let mut guard = state.orchestrator.lock().unwrap();
                    if let Some(orch) = guard.as_mut() {
                        orch.shutdown();
                    }
                    // 允许窗口真正关闭,进程随之退出
                    return;
                }
                // 点 X = 隐藏到托盘
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app, event| {
            // RunEvent::Exit 兜底:任何退出路径都保证后台进程被清理
            if let tauri::RunEvent::Exit = event {
                let state = app.state::<AppState>();
                let mut guard = state.orchestrator.lock().unwrap();
                if let Some(orch) = guard.as_mut() {
                    orch.shutdown();
                }
            }
        });
}
