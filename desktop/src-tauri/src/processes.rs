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
    // An exited child no longer owns its PID; never taskkill a recycled PID.
    if handle.child.lock().unwrap().try_wait()?.is_some() {
        return Ok(());
    }
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

/// Query only IPv4 listeners reachable through 127.0.0.1. No subprocess,
/// command-line enumeration, cached ownership, or alternate lookup backend.
#[cfg(windows)]
pub fn port_owner(port: u16) -> Option<(u32, String)> {
    use windows::Win32::Foundation::{ERROR_INSUFFICIENT_BUFFER, NO_ERROR};
    use windows::Win32::NetworkManagement::IpHelper::{
        GetExtendedTcpTable, MIB_TCPROW_OWNER_PID, MIB_TCPTABLE_OWNER_PID,
        TCP_TABLE_OWNER_PID_LISTENER,
    };
    use windows::Win32::Networking::WinSock::AF_INET;

    let mut size = 0u32;
    // Microsoft requires a size query, then an aligned caller-owned buffer.
    // https://learn.microsoft.com/windows/win32/api/iphlpapi/nf-iphlpapi-getextendedtcptable
    let status = unsafe {
        GetExtendedTcpTable(
            None,
            &mut size,
            false,
            AF_INET.0 as u32,
            TCP_TABLE_OWNER_PID_LISTENER,
            0,
        )
    };
    if status != ERROR_INSUFFICIENT_BUFFER.0 {
        return None;
    }
    for _ in 0..3 {
        // The table and rows consist of DWORD fields: u32 supplies their alignment.
        let mut buffer = vec![0u32; (size as usize).div_ceil(std::mem::size_of::<u32>())];
        let allocated = buffer.len() * std::mem::size_of::<u32>();
        let status = unsafe {
            GetExtendedTcpTable(
                Some(buffer.as_mut_ptr().cast()),
                &mut size,
                false,
                AF_INET.0 as u32,
                TCP_TABLE_OWNER_PID_LISTENER,
                0,
            )
        };
        if status == ERROR_INSUFFICIENT_BUFFER.0 {
            continue;
        }
        if status != NO_ERROR.0 || size as usize > allocated {
            return None;
        }
        let offset = std::mem::offset_of!(MIB_TCPTABLE_OWNER_PID, table);
        let count = *buffer.first()? as usize;
        let end =
            offset.checked_add(count.checked_mul(std::mem::size_of::<MIB_TCPROW_OWNER_PID>())?)?;
        if end > size as usize {
            return None;
        }
        // Bounds use the SDK layout, including padding; the API succeeded and
        // initialized these rows. The buffer remains alive throughout selection.
        let rows = unsafe {
            std::slice::from_raw_parts(
                buffer
                    .as_ptr()
                    .cast::<u8>()
                    .add(offset)
                    .cast::<MIB_TCPROW_OWNER_PID>(),
                count,
            )
        };
        return loopback_listener_owner(rows, port).map(|pid| (pid, String::new()));
    }
    None
}

#[cfg(windows)]
fn loopback_listener_owner(
    rows: &[windows::Win32::NetworkManagement::IpHelper::MIB_TCPROW_OWNER_PID],
    port: u16,
) -> Option<u32> {
    use windows::Win32::NetworkManagement::IpHelper::MIB_TCP_STATE_LISTEN;
    let loopback = u32::from_ne_bytes([127, 0, 0, 1]);
    let mut owner = None;
    for row in rows {
        if row.dwState != MIB_TCP_STATE_LISTEN.0 as u32
            || (row.dwLocalAddr != loopback && row.dwLocalAddr != 0)
            || u16::from_be(row.dwLocalPort as u16) != port
        {
            continue;
        }
        if row.dwOwningPid == 0 || owner.is_some_and(|pid| pid != row.dwOwningPid) {
            return None; // Conflicting bindings do not prove a unique owner.
        }
        owner = Some(row.dwOwningPid);
    }
    owner
}

#[cfg(not(windows))]
pub fn port_owner(_port: u16) -> Option<(u32, String)> {
    None // This desktop supervisor has no ownership backend on other platforms.
}

#[cfg(test)]
mod tests {
    use super::*;

    #[cfg(windows)]
    #[test]
    fn listener_rows_require_correct_address_state_network_port_and_unique_pid() {
        use windows::Win32::NetworkManagement::IpHelper::{
            MIB_TCPROW_OWNER_PID, MIB_TCP_STATE_LISTEN,
        };
        let row = MIB_TCPROW_OWNER_PID {
            dwState: MIB_TCP_STATE_LISTEN.0 as u32,
            dwLocalAddr: u32::from_ne_bytes([127, 0, 0, 1]),
            dwLocalPort: 5432u16.to_be() as u32,
            dwOwningPid: 42,
            ..Default::default()
        };
        assert_eq!(loopback_listener_owner(&[row], 5432), Some(42));
        assert_eq!(loopback_listener_owner(&[row], 7700), None);
        assert_eq!(
            loopback_listener_owner(
                &[MIB_TCPROW_OWNER_PID {
                    dwLocalAddr: 0,
                    ..row
                }],
                5432
            ),
            Some(42)
        );
        assert_eq!(
            loopback_listener_owner(
                &[MIB_TCPROW_OWNER_PID {
                    dwLocalAddr: u32::from_ne_bytes([192, 168, 0, 2]),
                    ..row
                }],
                5432
            ),
            None
        );
        assert_eq!(
            loopback_listener_owner(&[MIB_TCPROW_OWNER_PID { dwState: 5, ..row }], 5432),
            None
        );
        assert_eq!(loopback_listener_owner(&[row, row], 5432), Some(42));
        assert_eq!(
            loopback_listener_owner(
                &[
                    row,
                    MIB_TCPROW_OWNER_PID {
                        dwOwningPid: 43,
                        ..row
                    }
                ],
                5432
            ),
            None
        );
        assert_eq!(
            loopback_listener_owner(
                &[MIB_TCPROW_OWNER_PID {
                    dwOwningPid: 0,
                    ..row
                }],
                5432
            ),
            None
        );
    }

    /// Explicit read-only benchmark against an already-running desktop backend.
    #[test]
    #[ignore = "requires existing services on 5432/7700; does not start or stop them"]
    fn benchmark_existing_service_owner_lookup() {
        let mut elapsed = Vec::new();
        for port in [5432, 7700] {
            for sample in 0..3 {
                let started = Instant::now();
                let owner = port_owner(port).expect("existing IPv4 service owner");
                let ms = started.elapsed().as_secs_f64() * 1000.0;
                elapsed.push(ms);
                println!(
                    "port={port} sample={sample} pid={} elapsed_ms={ms:.3}",
                    owner.0
                );
            }
        }
        elapsed.sort_by(f64::total_cmp);
        println!(
            "lookup_count={} median_ms={:.3} min_ms={:.3} max_ms={:.3}",
            elapsed.len(),
            (elapsed[2] + elapsed[3]) / 2.0,
            elapsed[0],
            elapsed[5]
        );
    }

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
