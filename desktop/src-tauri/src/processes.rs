//! 子进程管理:spawn(日志落盘)、taskkill 停进程树、TCP 端口探测。

use std::net::TcpStream;
use std::path::Path;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

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
    let _ = Command::new("taskkill")
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
