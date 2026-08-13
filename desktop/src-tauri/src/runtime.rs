//! 便携运行时路径解析:生产从安装目录 runtime/ 取可执行文件,开发回退到项目 .venv/PATH。

use std::path::{Path, PathBuf};
use std::process::Command;

pub struct RuntimePaths {
    /// Python 可执行文件(启动 api/worker/asset-gateway 与 alembic)。
    pub python: PathBuf,
    /// Python 进程工作目录:内含 apps/、packages/、migrations/ 源码。
    pub python_workdir: PathBuf,
    /// Node 可执行文件(启动 gitnexus)。
    pub node: PathBuf,
    /// gitnexus CLI 脚本(ps1 shim)。
    pub gitnexus_script: PathBuf,
    /// PostgreSQL 便携版 bin 目录(initdb/pg_ctl/pg_isready/psql)。
    pub postgres_bin: PathBuf,
    /// Meilisearch 单二进制。
    pub meilisearch: PathBuf,
}

pub fn resolve(root: &Path) -> RuntimePaths {
    let runtime = root.join("runtime");
    RuntimePaths {
        python: runtime.join("python").join("python.exe"),
        python_workdir: root.join("app"),
        node: runtime.join("node").join("node.exe"),
        gitnexus_script: runtime.join("gitnexus").join("gitnexus.ps1"),
        postgres_bin: runtime.join("postgres").join("bin"),
        meilisearch: runtime.join("meilisearch.exe"),
    }
}

/// 开发回退:便携 runtime 尚未组装(root/runtime 不存在)时使用。
/// python 用项目根 .venv;postgres/meili 由 docker compose 容器提供(见 orchestrator 的
/// uses_containers 分支),此处指向不存在的假路径防止误用;gitnexus 用 PATH 上 npm shim。
pub fn resolve_dev(root: &Path) -> RuntimePaths {
    RuntimePaths {
        python: root.join(".venv").join("Scripts").join("python.exe"),
        python_workdir: root.to_path_buf(),
        node: root.join(".venv").join("Scripts").join("node.exe"),
        gitnexus_script: gitnexus_shim(),
        postgres_bin: root.join("runtime-unavailable").join("postgres").join("bin"),
        meilisearch: root.join("runtime-unavailable").join("meilisearch.exe"),
    }
}

/// 解析 PATH 上 gitnexus 命令的 shim 路径(镜像 scripts/hub.ps1:Get-Command gitnexus
/// 得到 shim 后以 powershell -File 启动)。找不到时回退到命令名(启动阶段会报错)。
fn gitnexus_shim() -> PathBuf {
    let get_command = Command::new("powershell")
        .args([
            "-NoProfile",
            "-Command",
            "(Get-Command gitnexus -ErrorAction SilentlyContinue).Source",
        ])
        .output();
    if let Ok(out) = get_command {
        if out.status.success() {
            if let Some(path) = first_line(&out.stdout) {
                return PathBuf::from(path);
            }
        }
    }
    let where_cmd = Command::new("where").arg("gitnexus").output();
    if let Ok(out) = where_cmd {
        if out.status.success() {
            if let Some(path) = first_line(&out.stdout) {
                return PathBuf::from(path);
            }
        }
    }
    PathBuf::from("gitnexus")
}

fn first_line(bytes: &[u8]) -> Option<String> {
    String::from_utf8_lossy(bytes)
        .lines()
        .map(str::trim)
        .find(|l| !l.is_empty())
        .map(str::to_string)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 全局计数器:rustc 测试默认并行执行,tmp_root 的 root 须逐实例唯一,
    /// 避免并行的 remove_dir_all 删掉其他测试正在使用的目录。
    static TMP_ROOT_SEQ: std::sync::atomic::AtomicU32 = std::sync::atomic::AtomicU32::new(0);

    fn tmp_root() -> std::path::PathBuf {
        let n = TMP_ROOT_SEQ.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        let dir = std::env::temp_dir().join(format!(
            "hub-runtime-test-{}-{n}",
            std::process::id()
        ));
        std::fs::create_dir_all(dir.join("runtime/postgres/bin")).unwrap();
        std::fs::create_dir_all(dir.join("runtime/python")).unwrap();
        std::fs::create_dir_all(dir.join("runtime/node")).unwrap();
        std::fs::write(dir.join("runtime/postgres/bin/initdb.exe"), "").unwrap();
        std::fs::write(dir.join("runtime/python/python.exe"), "").unwrap();
        std::fs::write(dir.join("runtime/node/node.exe"), "").unwrap();
        std::fs::write(dir.join("runtime/meilisearch.exe"), "").unwrap();
        dir
    }

    #[test]
    fn resolve_points_at_installed_runtime() {
        let root = tmp_root();
        let paths = resolve(&root);
        assert_eq!(
            paths.postgres_bin,
            root.join("runtime").join("postgres").join("bin")
        );
        assert_eq!(paths.python, root.join("runtime").join("python").join("python.exe"));
        assert_eq!(paths.node, root.join("runtime").join("node").join("node.exe"));
        assert_eq!(paths.meilisearch, root.join("runtime").join("meilisearch.exe"));
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn python_workdir_is_app_sources() {
        let root = tmp_root();
        let paths = resolve(&root);
        assert_eq!(paths.python_workdir, root.join("app"));
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn resolve_dev_uses_project_venv_and_unavailable_binaries() {
        let root = tmp_root();
        let paths = resolve_dev(&root);
        assert_eq!(paths.python, root.join(".venv").join("Scripts").join("python.exe"));
        assert_eq!(paths.python_workdir, root);
        // 便携二进制指向不存在的假路径,容器模式不会使用
        assert!(!paths.postgres_bin.exists());
        assert!(!paths.meilisearch.exists());
        // gitnexus shim 环境相关,只断言非空(找不到时回退到命令名)
        assert!(!paths.gitnexus_script.as_os_str().is_empty());
        let _ = std::fs::remove_dir_all(&root);
    }
}
