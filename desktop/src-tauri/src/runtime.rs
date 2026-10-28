//! 显式运行时选择：生产仅随包运行时，开发必须指定源码、运行时及隔离数据目录。

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
    RuntimePaths {
        python: runtime.join("python/python.exe"),
        python_workdir: root.join("app"),
        postgres_bin: runtime.join("postgres").join("bin"),
        meilisearch: runtime.join("meilisearch.exe"),
    }
}

#[derive(Debug, PartialEq, Eq)]
pub struct DevelopmentProfile {
    pub source_root: PathBuf,
    pub runtime_root: PathBuf,
    pub data_dir: PathBuf,
}

/// No environment guessing: development is an explicit launch mode.
pub fn development_profile(
    args: impl IntoIterator<Item = std::ffi::OsString>,
) -> Result<Option<DevelopmentProfile>, String> {
    let mut args = args.into_iter();
    let mut source = None;
    let mut runtime = None;
    let mut data = None;
    while let Some(arg) = args.next() {
        let selected = if arg == "--dev-source-root" {
            &mut source
        } else if arg == "--dev-runtime-root" {
            &mut runtime
        } else if arg == "--data-dir" {
            &mut data
        } else {
            continue;
        };
        if selected.is_some() {
            return Err(format!(
                "{} can only be specified once",
                arg.to_string_lossy()
            ));
        }
        let path = PathBuf::from(
            args.next()
                .ok_or_else(|| format!("{} requires a path", arg.to_string_lossy()))?,
        );
        if !path.is_absolute() {
            return Err(format!(
                "{} requires an absolute path",
                arg.to_string_lossy()
            ));
        }
        *selected = Some(plain(&path));
    }
    match (source, runtime, data) {
        (None, None, _) => Ok(None),
        (Some(source_root), Some(runtime_root), Some(data_dir)) => Ok(Some(DevelopmentProfile {
            source_root,
            runtime_root,
            data_dir,
        })),
        _ => Err(
            "Development requires --dev-source-root, --dev-runtime-root and --data-dir together"
                .into(),
        ),
    }
}

pub fn resolve_dev(profile: &DevelopmentProfile) -> RuntimePaths {
    RuntimePaths {
        python: profile.source_root.join(".venv/Scripts/python.exe"),
        python_workdir: profile.source_root.clone(),
        postgres_bin: profile.runtime_root.join("postgres/bin"),
        meilisearch: profile.runtime_root.join("meilisearch.exe"),
    }
}

impl RuntimePaths {
    /// Validate before any child process or database directory is created.
    pub fn validate(&self) -> Result<(), String> {
        for path in [
            self.python.clone(),
            self.meilisearch.clone(),
            self.postgres_bin.join("initdb.exe"),
            self.postgres_bin.join("pg_ctl.exe"),
            self.postgres_bin.join("pg_isready.exe"),
            self.postgres_bin.join("psql.exe"),
            self.python_workdir.join("apps/desktop_services.py"),
            self.python_workdir.join("apps/worker_start.py"),
            self.python_workdir.join("migrations/alembic.ini"),
        ] {
            if !path.is_file() {
                return Err(format!(
                    "运行时文件缺失：{}。请修复安装或明确指定开发 profile；不会启动备用后端。",
                    path.display()
                ));
            }
        }
        Ok(())
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
    fn installed_runtime_ignores_legacy_python_hint_and_fails_when_incomplete() {
        let root = tmp_root();
        std::fs::create_dir_all(root.join("app/config")).unwrap();
        std::fs::write(
            root.join("app/config/python-executable.txt"),
            "C:/other/python.exe",
        )
        .unwrap();
        let paths = resolve(&root);
        assert_eq!(paths.python, root.join("runtime/python/python.exe"));
        assert!(paths.validate().is_err());
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn complete_installed_runtime_validates_without_source_fallback() {
        let root = tmp_root();
        for name in ["pg_ctl.exe", "pg_isready.exe", "psql.exe"] {
            std::fs::write(root.join("runtime/postgres/bin").join(name), "").unwrap();
        }
        for name in [
            "apps/desktop_services.py",
            "apps/worker_start.py",
            "migrations/alembic.ini",
        ] {
            let file = root.join("app").join(name);
            std::fs::create_dir_all(file.parent().unwrap()).unwrap();
            std::fs::write(file, "").unwrap();
        }
        assert!(resolve(&root).validate().is_ok());
        std::fs::remove_file(root.join("runtime/python/python.exe")).unwrap();
        assert!(resolve(&root)
            .validate()
            .unwrap_err()
            .contains("python.exe"));
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn development_requires_explicit_absolute_roots_and_data_directory() {
        let root = tmp_root();
        let args = |values: &[&str]| {
            values
                .iter()
                .map(std::ffi::OsString::from)
                .collect::<Vec<_>>()
        };
        assert_eq!(development_profile(args(&["--background"])), Ok(None));
        assert!(development_profile(args(&["--dev-source-root", "relative"])).is_err());
        let source = root.to_str().unwrap();
        assert!(development_profile(args(&["--dev-source-root", source])).is_err());
        assert!(development_profile(args(&[
            "--dev-source-root",
            source,
            "--dev-runtime-root",
            source
        ]))
        .is_err());
        let profile = development_profile(args(&[
            "--dev-source-root",
            source,
            "--dev-runtime-root",
            source,
            "--data-dir",
            source,
        ]))
        .unwrap()
        .unwrap();
        let paths = resolve_dev(&profile);
        assert_eq!(paths.python, root.join(".venv/Scripts/python.exe"));
        assert_eq!(paths.postgres_bin, root.join("postgres/bin"));
        assert_eq!(paths.meilisearch, root.join("meilisearch.exe"));
        let _ = std::fs::remove_dir_all(root);
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
