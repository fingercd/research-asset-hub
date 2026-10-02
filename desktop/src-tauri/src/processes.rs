//! 子进程管理:spawn(日志落盘)、taskkill 停进程树、TCP 端口探测。

use std::net::TcpStream;
use std::path::Path;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

/// Windows 下阻止子进程创建控制台窗口。壳是 GUI 子系统进程,自身无控制台;
/// 若不加 CREATE_NO_WINDOW,Windows 会为每个控制台子系统的子进程
/// (python.exe、pg_ctl.exe、initdb.exe、taskkill、powershell 等)各自新建一个
/// 独立控制台窗口,表现为启动时"闪现大量终端"。重定向 stdio 无法阻止窗口
/// 创建,必须设置该标志。非 Windows 为空实现,方便跨平台编译。
#[cfg(windows)]
pub fn no_console(cmd: &mut Command) {
    use std::os::windows::process::CommandExt;
    cmd.creation_flags(0x08000000); // CREATE_NO_WINDOW
}

#[cfg(not(windows))]
pub fn no_console(_cmd: &mut Command) {}

pub struct ChildHandle {
    /// 子进程语义标识(api/worker/…),供诊断日志使用
    #[allow(dead_code)]
    pub name: String,
    child: Mutex<Child>,
    pub pid: u32,
}

pub fn spawn(
    name: &str,
    exe: &Path,
    args: &[&str],
    cwd: &Path,
    log_file: &Path,
) -> std::io::Result<ChildHandle> {
    let log = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(log_file)?;
    let mut command = Command::new(exe);
    no_console(&mut command);
    command
        .args(args)
        .current_dir(cwd)
        .stdout(Stdio::from(log.try_clone()?))
        .stderr(Stdio::from(log));
    let child = command.spawn()?;
    let pid = child.id();
    Ok(ChildHandle {
        name: name.to_string(),
        child: Mutex::new(child),
        pid,
    })
}

pub fn wait_exit(handle: &ChildHandle, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    loop {
        if let Ok(Some(_)) = handle.child.lock().unwrap().try_wait() {
            return true;
        }
        if Instant::now() >= deadline {
            return false;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}

/// 停止整个进程树(taskkill /T /F);随后等待进程退出。
pub fn kill_tree(handle: &ChildHandle) -> std::io::Result<()> {
    let mut taskkill = Command::new("taskkill");
    no_console(&mut taskkill);
    let _ = taskkill
        .args(["/PID", &handle.pid.to_string(), "/T", "/F"])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
    // 兜底:直接杀根进程
    if !wait_exit(handle, Duration::from_secs(3)) {
        let _ = handle.child.lock().unwrap().kill();
    }
    let _ = handle.child.lock().unwrap().wait();
    Ok(())
}

pub fn tcp_probe(port: u16) -> bool {
    TcpStream::connect_timeout(
        &std::net::SocketAddr::from(([127, 0, 0, 1], port)),
        Duration::from_millis(300),
    )
    .is_ok()
}

/// taskkill 单个进程(连同其进程树)。
pub fn kill_pid(pid: u32) -> std::io::Result<()> {
    let mut taskkill = Command::new("taskkill");
    no_console(&mut taskkill);
    let status = taskkill
        .args(["/PID", &pid.to_string(), "/T", "/F"])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()?;
    if status.success() {
        Ok(())
    } else {
        Err(std::io::Error::other(format!(
            "taskkill /PID {pid} 返回失败"
        )))
    }
}

/// 查询端口当前监听进程的 (PID, 命令行):PowerShell Get-NetTCPConnection
/// -LocalPort <port> -State Listen 取 OwningProcess(PID),再经 list_processes
/// 查该 PID 的命令行。供 Preflight 按端口归属识别残留进程——只有实际持有
/// 冲突端口的进程且命令行匹配自有特征时才允许清理,避免误杀同栈但非壳启动
/// 的无关进程。查询失败(无监听归属/权限/解析失败)返回 None,调用方按
/// "端口无 PID 归属"处理:不清理,报错提示用户,宁可不杀不误杀。
pub fn port_owner(port: u16) -> Option<(u32, String)> {
    let mut ps = Command::new("powershell");
    no_console(&mut ps);
    let output = ps
        .args([
            "-NoProfile",
            "-Command",
            &format!(
                "ConvertTo-Json -Compress @(Get-NetTCPConnection -LocalPort {port} -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess)"
            ),
        ])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let value: serde_json::Value = serde_json::from_slice(&output.stdout).ok()?;
    // 同端口 IPv4/IPv6 双监听时返回数组,取首个 OwningProcess(通常同一进程)
    let pid = match value.as_array().and_then(|items| items.first()) {
        Some(item) => item.as_u64()?,
        None => value.as_u64()?,
    };
    let pid = u32::try_from(pid).ok()?;
    let cmd = list_processes()
        .into_iter()
        .find(|(p, _)| *p == pid)
        .map(|(_, cmd)| cmd)
        .unwrap_or_default();
    Some((pid, cmd))
}

/// 查询本机全部进程 (pid, 命令行),Windows 经 PowerShell Get-CimInstance。
/// 供 port_owner 按 PID 取命令行。查询失败返回空列表:调用方按
/// "无非自有进程可清理"处理,不影响报错路径。
pub fn list_processes() -> Vec<(u32, String)> {
    let mut ps = Command::new("powershell");
    no_console(&mut ps);
    let output = match ps
        .args([
            "-NoProfile",
            "-Command",
            "ConvertTo-Json -Compress @(Get-CimInstance Win32_Process | Select-Object ProcessId, CommandLine)",
        ])
        .output()
    {
        Ok(o) if o.status.success() => o,
        _ => return Vec::new(),
    };
    let Ok(value) = serde_json::from_slice::<serde_json::Value>(&output.stdout) else {
        return Vec::new();
    };
    let Some(items) = value.as_array() else {
        return Vec::new();
    };
    items
        .iter()
        .filter_map(|item| {
            let pid = item.get("ProcessId").and_then(|v| v.as_u64()).or_else(|| {
                item.get("ProcessId")
                    .and_then(|v| v.as_str())
                    .and_then(|s| s.parse::<u64>().ok())
            })?;
            let cmd = item
                .get("CommandLine")
                .and_then(|v| v.as_str())
                .unwrap_or_default()
                .to_string();
            Some((pid as u32, cmd))
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn spawn_and_kill_tree() {
        let dir = std::env::temp_dir().join(format!("hub-proc-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let log = dir.join("child.log");
        let handle = spawn(
            "probe",
            Path::new("cmd"),
            &["/C", "ping -n 300 127.0.0.1"],
            &dir,
            &log,
        )
        .unwrap();
        assert!(handle.pid > 0);
        kill_tree(&handle).unwrap();
        assert!(wait_exit(&handle, Duration::from_secs(5)));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn tcp_probe_reports_unused_port() {
        assert!(!tcp_probe(1));
    }
}
