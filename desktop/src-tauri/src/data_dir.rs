//! 数据目录:文档目录下的持久数据区,卸载/升级不触碰。

use std::path::{Path, PathBuf};

pub fn data_root() -> PathBuf {
    if let Ok(override_dir) = std::env::var("HUB_DATA_DIR") {
        if !override_dir.trim().is_empty() {
            return PathBuf::from(override_dir);
        }
    }
    let docs = std::env::var("USERPROFILE")
        .map(|p| PathBuf::from(p).join("Documents"))
        .unwrap_or_else(|_| PathBuf::from("."));
    docs.join("ResearchAssetHub")
}

pub struct DataPaths {
    pub root: PathBuf,
    pub postgres: PathBuf,
    pub meili: PathBuf,
    pub logs: PathBuf,
    pub backups: PathBuf,
    pub updates: PathBuf,
}

pub fn data_paths() -> DataPaths {
    let root = data_root();
    DataPaths {
        postgres: root.join("postgres-data"),
        meili: root.join("meili-data"),
        logs: root.join("logs"),
        backups: root.join("backups"),
        updates: root.join("updates"),
        root,
    }
}

pub fn ensure_dirs(paths: &DataPaths) -> std::io::Result<()> {
    for dir in [
        &paths.postgres,
        &paths.meili,
        &paths.logs,
        &paths.backups,
        &paths.updates,
    ] {
        std::fs::create_dir_all(dir)?;
    }
    Ok(())
}

pub fn postgres_initialized(data: &Path) -> bool {
    data.join("PG_VERSION").is_file()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn postgres_initialized_detects_data_dir() {
        let tmp = std::env::temp_dir().join(format!("hub-pg-{}", std::process::id()));
        assert!(!postgres_initialized(&tmp));
        std::fs::create_dir_all(&tmp).unwrap();
        std::fs::write(tmp.join("PG_VERSION"), "16\n").unwrap();
        assert!(postgres_initialized(&tmp));
        let _ = std::fs::remove_dir_all(&tmp);
    }

    #[test]
    fn data_paths_nest_under_root() {
        let root = data_root();
        let paths = data_paths();
        assert!(paths.postgres.starts_with(&root));
        assert!(paths.updates.starts_with(&root));
    }
}
