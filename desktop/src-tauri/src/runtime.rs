//! 便携运行时路径解析:生产从安装目录 runtime/ 取可执行文件,开发回退到项目 .venv/PATH。

use std::path::{Path, PathBuf};

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

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp_root() -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "hub-runtime-test-{}",
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
}
