//! 便携运行时路径解析:生产从安装目录 runtime/ 取可执行文件,开发回退到项目 .venv。

use std::path::{Path, PathBuf};

pub struct RuntimePaths {
    /// Python 可执行文件(启动 api/worker/asset-gateway 与 alembic)。
    pub python: PathBuf,
    /// Python 进程工作目录:内含 apps/、packages/、migrations/ 源码。
    pub python_workdir: PathBuf,
    /// PostgreSQL 便携版 bin 目录(initdb/pg_ctl/pg_isready/psql)。
    pub postgres_bin: PathBuf,
    /// Meilisearch 单二进制。
    pub meilisearch: PathBuf,
}

/// 剥掉 Windows verbatim 路径前缀(`\\?\`、`\\?\UNC\`、`//?/`、`//?/UNC/`)。
///
/// 壳进程若以 verbatim 路径被启动(current_exe 带 `\\?\`),其派生的运行时路径会
/// 把前缀传入子进程 argv[0](Rust std 写为 `//?/C:/...`)。EDB 版 initdb 无法处理
/// 该形态,同目录探测报 "no data was returned by command ..." 而失败。
/// 所有交给子进程的路径必须经此函数规范化。
#[cfg(windows)]
pub fn plain(path: &Path) -> PathBuf {
    let s = path.as_os_str().to_string_lossy();
    // UNC 变体剥前缀后须补回共享根的双反斜杠(\\?\UNC\server\share → \\server\share)
    if let Some(rest) = s
        .strip_prefix(r"\\?\UNC\")
        .or_else(|| s.strip_prefix("//?/UNC/"))
    {
        return PathBuf::from(format!(r"\\{rest}"));
    }
    let stripped = s
        .strip_prefix(r"\\?\")
        .or_else(|| s.strip_prefix("//?/"))
        .unwrap_or(&s);
    PathBuf::from(stripped.to_string())
}

#[cfg(not(windows))]
pub fn plain(path: &Path) -> PathBuf {
    path.to_path_buf()
}

pub fn resolve(root: &Path) -> RuntimePaths {
    let root = plain(root);
    let runtime = root.join("runtime");
    // User-level updates may share the installed runtime through a junction.
    // Launch the real path so Windows and Python reuse the same executable and
    // module cache paths instead of treating each update as another runtime.
    let runtime = plain(&std::fs::canonicalize(&runtime).unwrap_or(runtime));
    // A local installation can explicitly reuse an already prepared interpreter.
    // Without a valid hint, the distributable still uses its bundled runtime.
    let bundled_python = runtime.join("python").join("python.exe");
    let python = std::fs::read_to_string(root.join("app/config/python-executable.txt"))
        .ok()
        .map(|value| plain(Path::new(value.trim().trim_start_matches('\u{feff}'))))
        .filter(|path| path.is_file())
        .unwrap_or(bundled_python);
    RuntimePaths {
        python,
        python_workdir: root.join("app"),
        postgres_bin: runtime.join("postgres").join("bin"),
        meilisearch: runtime.join("meilisearch.exe"),
    }
}

/// 开发回退:便携 runtime 尚未组装(root/runtime 不存在)时使用。
/// python 用项目根 .venv;postgres/meili 由 docker compose 容器提供(见 orchestrator 的
/// uses_containers 分支),此处指向不存在的假路径防止误用。
pub fn resolve_dev(root: &Path) -> RuntimePaths {
    let root = plain(root);
    RuntimePaths {
        python: root.join(".venv").join("Scripts").join("python.exe"),
        python_workdir: root.to_path_buf(),
        postgres_bin: root
            .join("runtime-unavailable")
            .join("postgres")
            .join("bin"),
        meilisearch: root.join("runtime-unavailable").join("meilisearch.exe"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 全局计数器:rustc 测试默认并行执行,tmp_root 的 root 须逐实例唯一,
    /// 避免并行的 remove_dir_all 删掉其他测试正在使用的目录。
    static TMP_ROOT_SEQ: std::sync::atomic::AtomicU32 = std::sync::atomic::AtomicU32::new(0);

    fn tmp_root() -> std::path::PathBuf {
        let n = TMP_ROOT_SEQ.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        let dir = std::env::temp_dir().join(format!("hub-runtime-test-{}-{n}", std::process::id()));
        std::fs::create_dir_all(dir.join("runtime/postgres/bin")).unwrap();
        std::fs::create_dir_all(dir.join("runtime/python")).unwrap();
        std::fs::write(dir.join("runtime/postgres/bin/initdb.exe"), "").unwrap();
        std::fs::write(dir.join("runtime/python/python.exe"), "").unwrap();
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
        assert_eq!(
            paths.python,
            root.join("runtime").join("python").join("python.exe")
        );
        assert_eq!(
            paths.meilisearch,
            root.join("runtime").join("meilisearch.exe")
        );
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
    fn python_hint_selects_prepared_environment_and_missing_path_uses_bundle() {
        let root = tmp_root();
        let config = root.join("app/config");
        let prepared = root.join("prepared/python.exe");
        std::fs::create_dir_all(&config).unwrap();
        std::fs::create_dir_all(prepared.parent().unwrap()).unwrap();
        std::fs::write(&prepared, "").unwrap();
        let hint = config.join("python-executable.txt");
        std::fs::write(&hint, format!("\u{feff}{}\n", prepared.display())).unwrap();
        assert_eq!(resolve(&root).python, prepared);
        std::fs::write(&hint, root.join("missing.exe").to_string_lossy().as_bytes()).unwrap();
        assert_eq!(
            resolve(&root).python,
            root.join("runtime/python/python.exe")
        );
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn resolve_dev_uses_project_venv_and_unavailable_binaries() {
        let root = tmp_root();
        let paths = resolve_dev(&root);
        assert_eq!(
            paths.python,
            root.join(".venv").join("Scripts").join("python.exe")
        );
        assert_eq!(paths.python_workdir, root);
        // 便携二进制指向不存在的假路径,容器模式不会使用
        assert!(!paths.postgres_bin.exists());
        assert!(!paths.meilisearch.exists());
        let _ = std::fs::remove_dir_all(&root);
    }

    #[cfg(windows)]
    #[test]
    fn plain_strips_verbatim_prefixes() {
        use std::path::PathBuf;
        let expect = PathBuf::from("C:/Program Files/ResearchAssetHub/runtime/postgres/bin");
        assert_eq!(
            plain(&PathBuf::from(
                r"\\?\C:\Program Files\ResearchAssetHub\runtime\postgres\bin"
            )),
            expect
        );
        assert_eq!(
            plain(&PathBuf::from(r"\\?\UNC\server\share\dir")),
            PathBuf::from(r"\\server\share\dir"),
        );
        assert_eq!(
            plain(&PathBuf::from(
                "//?/C:/Program Files/ResearchAssetHub/runtime/postgres/bin"
            )),
            expect
        );
        assert_eq!(
            plain(&PathBuf::from("//?/UNC/server/share/dir")),
            PathBuf::from(r"\\server\share\dir"),
        );
        // 普通路径原样返回
        assert_eq!(plain(&expect), expect);
    }

    #[cfg(windows)]
    #[test]
    fn resolve_strips_verbatim_root() {
        // 壳以 verbatim 路径启动时 root 带 \\?\,resolve 结果必须已剥前缀,
        // 否则派生进程 argv[0] 带 //?/ 前缀(initdb 因此失败)。
        let root = PathBuf::from(r"\\?\").join(tmp_root());
        let paths = resolve(&root);
        assert_eq!(
            paths.postgres_bin,
            plain(&root).join("runtime").join("postgres").join("bin")
        );
        assert!(!paths.postgres_bin.to_string_lossy().contains(r"\\?\"));
        assert!(!paths.postgres_bin.to_string_lossy().contains("//?/"));
        let _ = std::fs::remove_dir_all(&root);
    }
}
