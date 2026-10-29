//! 系统托盘:显示/隐藏窗口、重试、检查更新、退出。退出前调用编排器 shutdown 停全部后台。

use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Manager,
};

pub enum TraySignal {
    /// 菜单「打开主界面」直接处理窗口显示;变体保留作信号通道接口
    #[allow(dead_code)]
    Show,
    Quit,
    Retry,
    CheckUpdate,
}

/// 创建托盘;返回信号通道接收端,由 run() 主循环处理(窗口线程与编排器互斥)。
pub fn create_tray(
    app: &AppHandle,
    retry_available: bool,
) -> tauri::Result<Option<std::sync::mpsc::Receiver<TraySignal>>> {
    let (tx, rx) = std::sync::mpsc::channel();
    let show = MenuItem::with_id(app, "show", "打开主界面", true, None::<&str>)?;
    let retry = MenuItem::with_id(app, "retry", "重试启动", retry_available, None::<&str>)?;
    let update = MenuItem::with_id(app, "check-update", "检查更新…", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&show, &retry, &update, &quit])?;

    let tray = TrayIconBuilder::new()
        .icon(app.default_window_icon().unwrap().clone())
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(move |app, event| match event.id.as_ref() {
            "show" => {
                if let Some(w) = app.get_webview_window("main") {
                    let _ = w.show();
                    let _ = w.set_focus();
                }
            }
            "retry" => {
                let _ = tx.send(TraySignal::Retry);
            }
            "check-update" => {
                let _ = tx.send(TraySignal::CheckUpdate);
            }
            "quit" => {
                let _ = tx.send(TraySignal::Quit);
            }
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                if let Some(w) = tray.app_handle().get_webview_window("main") {
                    let _ = w.show();
                    let _ = w.set_focus();
                }
            }
        })
        .build(app)?;
    // 阻止托盘被 drop(RAII 会销毁图标)
    Box::leak(Box::new(tray));
    Ok(Some(rx))
}
