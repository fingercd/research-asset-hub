mod data_dir;
mod env;
mod orchestrator;
mod processes;
mod runtime;
mod single_instance;
mod tray;
mod updater;

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, OnceLock};

use serde::Serialize;
use tauri::{
    webview::{NewWindowResponse, PageLoadEvent},
    Manager, WindowEvent,
};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons};
use tauri_plugin_opener::OpenerExt;

use orchestrator::{Orchestrator, StartupError, StepStatus, CANCEL_DETAIL};

static APP_STARTED: OnceLock<std::time::Instant> = OnceLock::new();

fn log_startup_milestone(label: &str) {
    use std::io::Write;
    let started = APP_STARTED.get_or_init(std::time::Instant::now);
    let logs = data_dir::data_paths().logs;
    let _ = std::fs::create_dir_all(&logs);
    if let Ok(mut file) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(logs.join("desktop-timing.log"))
    {
        let _ = writeln!(
            file,
            "{} {:.3}s {label}",
            std::process::id(),
            started.elapsed().as_secs_f64()
        );
    }
}

/// 当前编排实例与它的取消令牌:start 在 orch 的锁内执行;
/// 退出/重试先经 cancel 无锁置位打断(等待循环 ≤500ms 察觉),再取 orch 锁清理,
/// 避免与进行中的 start 争锁被阻塞(此前最长 120s)。
#[derive(Clone)]
pub struct OrchestratorEntry {
    pub orch: Arc<Mutex<Orchestrator>>,
    pub cancel: Arc<AtomicBool>,
}

pub struct AppState {
    pub background: bool,
    pub force_exit: AtomicBool,
    pub orchestrator: Mutex<Option<OrchestratorEntry>>,
    pub startup_progress: Mutex<Vec<StartupProgress>>,
    pub startup_ready: AtomicBool,
}

fn background_requested(args: impl IntoIterator<Item = std::ffi::OsString>) -> bool {
    args.into_iter().any(|arg| arg == "--background")
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct StartupProgress {
    pub step: String,
    pub status: String, // "pending" | "done" | "failed"
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
}

fn remember_startup_progress(history: &mut Vec<StartupProgress>, progress: &StartupProgress) {
    if progress.step == "预检环境" && progress.status == "pending" {
        history.clear();
    }
    history.push(progress.clone());
}

fn progress_script(progress: &StartupProgress) -> tauri::Result<String> {
    let json = serde_json::to_string(progress).map_err(|e| tauri::Error::Anyhow(e.into()))?;
    Ok(format!(
        "window.__hubProgress && window.__hubProgress({json});"
    ))
}

/// 把 Rust 侧 startup-progress 事件桥接到加载页的 window.__hubProgress。
/// 同时缓存本轮事件，加载页初始化前发出的事件会在 page load 后顺序补发。
pub fn emit_startup_progress(
    app: &tauri::AppHandle,
    progress: &StartupProgress,
) -> tauri::Result<()> {
    let script = progress_script(progress)?;
    let state = app.state::<AppState>();
    let mut history = state.startup_progress.lock().unwrap();
    remember_startup_progress(&mut history, progress);
    if let Some(window) = app.get_webview_window("main") {
        window.eval(&script)?;
    }
    Ok(())
}

/// 退出前清理:先经取消令牌无锁置位(打断进行中的 start,等待循环 ≤500ms 察觉),
/// 再取编排锁 shutdown 停掉全部后台进程。Option 保留不动,RunEvent::Exit 兜底
/// 可再次调用,shutdown 幂等。
fn shutdown_orchestrator(state: &AppState) {
    let entry = {
        let guard = state.orchestrator.lock().unwrap();
        if let Some(e) = guard.as_ref() {
            e.cancel.store(true, Ordering::SeqCst);
        }
        guard.clone()
    };
    if let Some(e) = entry {
        e.orch.lock().unwrap().shutdown();
    }
}

/// 在编排线程执行一次完整启动;成功则导航到主界面,失败则弹窗。
/// 重试路径:开头取走上一个实例——先经令牌无锁置位取消(打断进行中的 start),
/// 再取锁 shutdown 收尾(已结束的实例立即清理残留,进行中的 ≤~1s 自行返回后清理),
/// 避免端口占用误报。
fn run_startup(app: tauri::AppHandle, show_ui: bool) {
    let started = std::time::Instant::now();
    app.state::<AppState>()
        .startup_ready
        .store(false, Ordering::SeqCst);
    let old = {
        let state = app.state::<AppState>();
        let mut guard = state.orchestrator.lock().unwrap();
        guard.take()
    };
    if let Some(old) = old {
        // 先经令牌无锁置位取消(打断进行中的 start),再取锁 shutdown 收尾
        // (已结束的实例立即清理残留,进行中的 ≤~1s 自行返回后清理)。
        // shutdown 在 state 锁外执行,不阻塞退出路径。
        old.cancel.store(true, Ordering::SeqCst);
        old.orch.lock().unwrap().shutdown();
    }

    // resource_dir 在 Windows 直接返回 exe 目录,可能带 verbatim 前缀(\\?\);
    // resolve 内部会剥前缀,但开发回退分支同样须防:统一在此先规范化。
    let resource = runtime::plain(&app.path().resource_dir().unwrap_or_default());
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
    // The bundled interpreter is the active application environment. Inherit this
    // in child shells too, so local CMD AutoRun hooks do not activate Conda on
    // Python's platform/version probes (several seconds per service on Windows).
    if let Some(python_dir) = paths.python.parent() {
        std::env::set_var("VIRTUAL_ENV", python_dir);
    }
    // meili master key 落盘目录:数据根下 secrets/(与 logs/postgres-data 同级)
    let secrets_dir = data_dir::data_root().join("secrets");
    let app_config_dir = paths.python_workdir.join("config");
    let _ = env::prepare(&ports, &secrets_dir, &app_config_dir);

    let app_for_events = app.clone();
    let orchestrator = Orchestrator::new(paths, ports, move |event| {
        use std::io::Write;
        if let Ok(mut log) = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(data_dir::data_paths().logs.join("startup-timing.log"))
        {
            let _ = writeln!(
                log,
                "{:.3}s {:?} {:?}",
                started.elapsed().as_secs_f64(),
                event.step,
                event.status
            );
        }
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

    let arc = Arc::new(Mutex::new(orchestrator));
    {
        // 换入新实例;cancel 令牌与 orch 锁并排存储,退出/重试可无锁打断 start。
        // 此时 start 尚未开始,拿 orch 锁取令牌立即可得。
        let state = app.state::<AppState>();
        let cancel = arc.lock().unwrap().cancel_token();
        *state.orchestrator.lock().unwrap() = Some(OrchestratorEntry {
            orch: arc.clone(),
            cancel,
        });
    }

    // start 在编排锁内、AppState 锁外执行:退出路径不再被其全程阻塞。
    let result = arc.lock().unwrap().start();

    // start 结束(成功/失败/取消)后保留 Option 不动:成功实例持后台进程句柄、
    // 失败实例可能残留半启动子进程,退出路径依赖 Option 完成 shutdown 才能无残留;
    // 不写回 Option 也就不会覆盖重试换入的更新实例。
    match result {
        Ok(()) => {
            app.state::<AppState>()
                .startup_ready
                .store(true, Ordering::SeqCst);
            log_startup_milestone("services-ready");
            // 编排完成即就绪,导航到主界面并显示窗口。
            // 窗口以 create: false 构建(就绪前不露空窗),首次启动的正常路径
            // 此前从不调用 show(),窗口永远隐藏——用户看到的只有闪现的终端与
            // 托盘图标,表现为"完全无法打开"。就绪后必须显式 show + 聚焦。
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.eval("location.replace('index.html');");
                if show_ui {
                    let _ = window.show();
                    let _ = window.set_focus();
                }
            }
        }
        Err(StartupError::StepFailed { step, detail }) => {
            // 用户主动取消(退出/重试打断)不是失败,不弹错误框
            if detail == CANCEL_DETAIL {
                return;
            }
            let message = format!("{}失败\n\n{}", step.as_str(), detail);
            if show_ui {
                app.dialog()
                    .message(message)
                    .title("研究资产库启动失败")
                    .blocking_show();
            }
        }
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    log_startup_milestone("process-entry");
    let background = background_requested(std::env::args_os().skip(1));
    // 单实例保护必须在任何 Builder/setup 之前:第二个实例若进入 setup,
    // 编排器预检会把已有实例的后台服务当作"残留"清理并弹报错(实测)。
    // 此处互斥体拦截:已有实例则唤起其窗口后立即退出。
    #[cfg(windows)]
    if !single_instance::acquire(!background) {
        std::process::exit(0);
    }
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .manage(AppState {
            background,
            force_exit: AtomicBool::new(false),
            orchestrator: Mutex::new(None),
            startup_progress: Mutex::new(Vec::new()),
            startup_ready: AtomicBool::new(false),
        })
        .on_page_load(|webview, payload| {
            if webview.label() == "main" && payload.event() == PageLoadEvent::Finished && !payload.url().path().ends_with("/loading.html") {
                log_startup_milestone("frontend-loaded");
            }
            if webview.label() != "main"
                || payload.event() != PageLoadEvent::Finished
                || !payload.url().path().ends_with("/loading.html")
            {
                return;
            }
            // 与 emit 共用锁，保证补发的旧事件不会排到新的实时事件之后。
            let state = webview.app_handle().state::<AppState>();
            if state.startup_ready.load(Ordering::SeqCst) {
                let _ = webview.eval("location.replace('index.html');");
                return;
            }
            let history = state.startup_progress.lock().unwrap();
            for progress in history.iter() {
                if let Ok(script) = progress_script(progress) {
                    let _ = webview.eval(&script);
                }
            }
        })
        .setup(|app| {
            log_startup_milestone("setup-enter");
            // Backend imports and database startup overlap WebView creation.
            // The loading-page callback replays progress or redirects when ready.
            let handle = app.handle().clone();
            let show_ui = !app.state::<AppState>().background;
            std::thread::spawn(move || run_startup(handle, show_ui));
            // 桌面壳注入 API 基址;前端 resolveApiBase 优先读它(Task 2)。
            // 用 initialization script 而非 setup 内一次性 eval:初始化脚本在每次
            // 文档加载(包括 location.replace('index.html') 导航)时重新执行,
            // 一次性 eval 注入的值会随导航被清空,导致主界面 API 全部打向
            // tauri://localhost 404。窗口改为 create: false + from_config 手动创建,
            // 以便在构建时附加初始化脚本(conf 的 WindowConfig 无 initializationScript 字段)。
            let api_port = orchestrator::Ports::from_env().api;
            let api_base_init_script =
                format!("window.__HUB_API_BASE__ = 'http://127.0.0.1:{api_port}/api/v1';");
            let window_config = app.config().app.windows.first().expect("main window config");
            let app_for_links = app.handle().clone();
            let _main_window = tauri::WebviewWindowBuilder::from_config(app.handle(), window_config)
                .expect("main window config must be valid")
                .visible(!app.state::<AppState>().background)
                .initialization_script(&api_base_init_script)
                .on_new_window(move |url, _features| {
                    // Wry 默认拦截 window.open；opener 插件只自动接管 a[target=_blank]。
                    // PDF/HTML 快捷打开使用程序调用，交给系统默认浏览器处理。
                    if matches!(url.scheme(), "http" | "https") {
                        let app = app_for_links.clone();
                        std::thread::spawn(move || {
                            if let Err(error) = app.opener().open_url(url.as_str(), None::<&str>) {
                                app.dialog()
                                    .message(format!("无法在默认浏览器中打开链接：{error}"))
                                    .title("打开链接失败")
                                    .blocking_show();
                            }
                        });
                    }
                    NewWindowResponse::Deny
                })
                .build()
                .expect("main window must build");
            log_startup_milestone("webview-created");

            let rx = tray::create_tray(app.handle(), true)
                .expect("tray creation must succeed")
                .expect("channel must be present");
            log_startup_milestone("tray-created");

            let handle = app.handle().clone();
            std::thread::spawn(move || {
                // 托盘信号循环(独立线程,避免阻塞事件循环)
                while let Ok(signal) = rx.recv() {
                    match signal {
                        tray::TraySignal::Show => {}
                        tray::TraySignal::CheckUpdate => {
                            let current = handle.package_info().version.to_string();
                            let dir = updater::default_updates_dir();
                            // 更新源目录不存在时先建好,首次点「检查更新」即可直接放入
                            // manifest.json 与安装包再重试;创建失败由下方 NoManifest 文案兜底。
                            let _ = std::fs::create_dir_all(&dir);
                            match updater::check(&current, &dir) {
                                Ok(result) => {
                                    if !result.update_available {
                                        handle
                                            .dialog()
                                            .message(format!("当前版本 {current} 已是最新。"))
                                            .title("检查更新")
                                            .blocking_show();
                                        continue;
                                    }
                                    let manifest =
                                        result.manifest.expect("available implies manifest");
                                    let notes = manifest.notes.clone().unwrap_or_default();
                                    // 确认框展示安装包文件名并提示来源信任边界:
                                    // 更新源目录位于本机,任意本地程序均可放置文件。
                                    let ok = handle
                                        .dialog()
                                        .message(format!(
                                            "发现新版本 {}{}\n\n即将运行安装包:{}\n\n请确认该文件来源可信(更新源目录位于本机,任意本地程序可放置文件)。\n\n立即下载并安装?",
                                            manifest.version,
                                            if notes.is_empty() {
                                                String::new()
                                            } else {
                                                format!("\n\n更新说明:{notes}")
                                            },
                                            manifest.installer,
                                        ))
                                        .title("检查更新")
                                        .buttons(MessageDialogButtons::OkCancelCustom(
                                            "立即安装".into(),
                                            "取消".into(),
                                        ))
                                        .blocking_show();
                                    if !ok {
                                        continue;
                                    }
                                    match updater::launch_installer(&dir, &manifest) {
                                        Ok(()) => {
                                            handle
                                                .dialog()
                                                .message("安装包已启动,安装完成后请重启软件。")
                                                .title("检查更新")
                                                .blocking_show();
                                        }
                                        Err(e) => {
                                            handle
                                                .dialog()
                                                .message(format!("更新失败:{e}"))
                                                .title("检查更新")
                                                .blocking_show();
                                        }
                                    }
                                }
                                Err(updater::UpdateError::NoManifest) => {
                                    handle
                                        .dialog()
                                        .message(format!(
                                            "尚未配置更新源。将新版安装包与 manifest.json 一起放入更新目录后重试:\n{}",
                                            dir.display()
                                        ))
                                        .title("检查更新")
                                        .blocking_show();
                                }
                                Err(e) => {
                                    handle
                                        .dialog()
                                        .message(format!(
                                            "更新源不可用:{e}\n\n默认更新源:{}\n(将新版安装包与 manifest.json 放入该目录即可)",
                                            dir.display()
                                        ))
                                        .title("检查更新")
                                        .blocking_show();
                                }
                            }
                        }
                        tray::TraySignal::Retry => run_startup(handle.clone(), true),
                        tray::TraySignal::Quit => {
                            let state = handle.state::<AppState>();
                            state.force_exit.store(true, Ordering::SeqCst);
                            // 显式清理:先无锁取消(打断进行中的 start,≤~1s 自行返回),
                            // 再 shutdown 停全部后台;RunEvent::Exit 兜底再幂等执行一次。
                            shutdown_orchestrator(&state);
                            if let Some(w) = handle.get_webview_window("main") {
                                let _ = w.destroy();
                            }
                            handle.exit(0);
                        }
                    }
                }
            });

            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                let state = window.state::<AppState>();
                if state.force_exit.load(Ordering::SeqCst) {
                    // 先无锁取消再 shutdown,不阻塞 UI 线程等待进行中的 start
                    shutdown_orchestrator(&state);
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
            // RunEvent::Exit 兜底:任何退出路径都保证后台进程被清理(shutdown 幂等)
            if let tauri::RunEvent::Exit = event {
                let state = app.state::<AppState>();
                shutdown_orchestrator(&state);
            }
        });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn background_flag_requires_exact_argument() {
        let args = |values: &[&str]| {
            values
                .iter()
                .map(std::ffi::OsString::from)
                .collect::<Vec<_>>()
        };
        assert!(background_requested(args(&["--background"])));
        assert!(!background_requested(args(&[])));
        assert!(!background_requested(args(&["--background=false"])));
    }

    #[test]
    fn startup_progress_replay_keeps_each_stage_and_resets_on_retry() {
        let event = |step: &str, status: &str| StartupProgress {
            step: step.into(),
            status: status.into(),
            detail: None,
        };
        let mut history = Vec::new();
        for progress in [
            event("预检环境", "pending"),
            event("预检环境", "done"),
            event("启动数据库", "pending"),
        ] {
            remember_startup_progress(&mut history, &progress);
        }
        assert_eq!(history.len(), 3, "加载后应补发此前已完成的阶段");
        assert_eq!(history[1], event("预检环境", "done"));

        remember_startup_progress(&mut history, &event("启动数据库", "failed"));
        remember_startup_progress(&mut history, &event("预检环境", "pending"));
        assert_eq!(history, vec![event("预检环境", "pending")]);
    }
}
