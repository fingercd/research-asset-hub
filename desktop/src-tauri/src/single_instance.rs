//! 单实例保护:入口命名互斥体 + 唤起已有实例窗口。
//!
//! 互斥体在任何 Builder/setup 之前生效。第二次启动只按参数决定是否
//! 唤起已有窗口，不触碰后台。无法确认互斥体所有权时失败，而非放行。

use std::sync::OnceLock;

/// 互斥体句柄原始值(HANDLE 包裹裸指针,非 Send/Sync,存 isize)。
/// 进程生命周期内持有,释放即失去单实例保护。
static MUTEX_HANDLE: OnceLock<isize> = OnceLock::new();

fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(std::iter::once(0)).collect()
}

/// 返回 true = 本实例是主实例(互斥体归我);false = 已有实例在运行,
/// show_existing 为 true 时尝试唤起其窗口,调用方应立即退出且不得启动编排逻辑。
pub fn acquire(show_existing: bool) -> Result<bool, String> {
    use windows::core::PCWSTR;
    use windows::Win32::Foundation::{CloseHandle, GetLastError, ERROR_ALREADY_EXISTS};
    use windows::Win32::System::Threading::CreateMutexW;

    let name = wide("ResearchAssetHub-SingleInstance");
    unsafe {
        let handle = match CreateMutexW(None, true, PCWSTR(name.as_ptr())) {
            Ok(h) => h,
            Err(error) => return Err(error.to_string()),
        };
        if GetLastError() == ERROR_ALREADY_EXISTS {
            let _ = CloseHandle(handle);
            if show_existing {
                show_existing_window();
            }
            return Ok(false);
        }
        let _ = MUTEX_HANDLE.set(handle.0 as isize);
        Ok(true)
    }
}

/// 枚举顶层窗口,唤起标题以「研究资产库」开头的窗口(主实例的窗口,
/// X 关闭后处于隐藏态,ShowWindow 重新显示并置前)。
fn show_existing_window() {
    use windows::core::BOOL;
    use windows::Win32::Foundation::{HWND, LPARAM};
    use windows::Win32::UI::WindowsAndMessaging::{
        EnumWindows, GetWindowTextLengthW, GetWindowTextW, SetForegroundWindow, ShowWindow, SW_SHOW,
    };

    const TITLE_PREFIX: &str = "研究资产库";

    unsafe extern "system" fn enum_proc(hwnd: HWND, _lparam: LPARAM) -> BOOL {
        let len = GetWindowTextLengthW(hwnd);
        if len == 0 {
            return BOOL(1);
        }
        let mut buf = vec![0u16; (len + 1) as usize];
        let copied = GetWindowTextW(hwnd, &mut buf);
        let title = String::from_utf16_lossy(&buf[..copied as usize]);
        if title.starts_with(TITLE_PREFIX) {
            let _ = ShowWindow(hwnd, SW_SHOW);
            let _ = SetForegroundWindow(hwnd);
            BOOL(0) // 已找到,停止枚举
        } else {
            BOOL(1)
        }
    }

    unsafe {
        let _ = EnumWindows(Some(enum_proc), LPARAM(0));
    }
}
