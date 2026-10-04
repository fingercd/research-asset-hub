//! 数据目录:文档目录下的持久数据区,卸载/升级不触碰。

use std::path::{Path, PathBuf};

pub fn data_root() -> PathBuf {
    let root = if let Ok(override_dir) = std::env::var("HUB_DATA_DIR") {
        if !override_dir.trim().is_empty() {
            PathBuf::from(override_dir)
        } else {
            data_root_default()
        }
    } else {
        data_root_default()
    };
    // 剥 verbatim 前缀:数据目录路径会进入 initdb 的 -D 参数,带前缀同样会导致失败
    crate::runtime::plain(&root)
}

fn data_root_default() -> PathBuf {
    if let Some(preference) = preference_file() {
        if let Ok(value) = std::fs::read_to_string(preference) {
            let selected = PathBuf::from(value.trim().trim_start_matches('\u{feff}'));
            assert!(selected.is_absolute(), "保存的资料库目录必须是绝对路径");
            return selected;
        }
    }
    std::env::var("USERPROFILE")
        .map(|p| PathBuf::from(p).join("Documents"))
        .unwrap_or_else(|_| PathBuf::from("."))
        .join("ResearchAssetHub")
}

fn preference_file() -> Option<PathBuf> {
    std::env::var("LOCALAPPDATA").ok().map(|path| {
        PathBuf::from(path)
            .join("ResearchAssetHub")
            .join("data-directory.txt")
    })
}

/// A selected library is either empty or a complete existing Hub data root.
/// This prevents initialization from clearing another folder's reserved files.
pub fn validate_library(path: &Path, resources: &Path) -> Result<(PathBuf, bool), String> {
    if !path.is_absolute() {
        return Err("请选择本机的绝对目录".into());
    }
    let selected = std::fs::canonicalize(path).map_err(|error| error.to_string())?;
    let selected = crate::runtime::plain(&selected);
    let resources = crate::runtime::plain(
        &std::fs::canonicalize(resources).map_err(|error| error.to_string())?,
    );
    if selected.starts_with(&resources) {
        return Err("资料库应放在用户目录或其他磁盘，不能放在程序安装目录中".into());
    }
    let empty = std::fs::read_dir(&selected)
        .map_err(|error| error.to_string())?
        .next()
        .is_none();
    if empty {
        return Ok((selected, false));
    }
    let version = std::fs::read_to_string(selected.join("postgres-data/PG_VERSION"));
    if version.map(|value| value.trim() == "16").unwrap_or(false)
        && selected.join("config/sources.yaml").is_file()
    {
        return Ok((selected, true));
    }
    Err("请选择空文件夹，或包含完整配置和数据库的已有资料库目录".into())
}

pub fn save_library(path: &Path) -> Result<(), String> {
    let preference = preference_file().ok_or("无法确定当前用户的设置目录")?;
    save_library_at(path, &preference)
}

fn save_library_at(path: &Path, preference: &Path) -> Result<(), String> {
    let parent = preference.parent().expect("preference parent");
    std::fs::create_dir_all(parent).map_err(|error| error.to_string())?;
    let temporary = parent.join(format!(".data-directory-{}.tmp", std::process::id()));
    std::fs::write(&temporary, path.to_string_lossy().as_bytes())
        .map_err(|error| error.to_string())?;
    std::fs::rename(&temporary, preference).map_err(|error| error.to_string())?;
    Ok(())
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
    fn library_choice_accepts_empty_or_existing_and_preserves_unrelated_files() {
        let root = std::env::temp_dir().join(format!("hub-library-choice-{}", std::process::id()));
        let resources = root.join("installed resources");
        let selected = root.join("中文 空目录");
        std::fs::create_dir_all(&resources).unwrap();
        std::fs::create_dir_all(&selected).unwrap();
        assert!(!validate_library(&selected, &resources).unwrap().1);
        let unrelated = selected.join("my-paper.pdf");
        std::fs::write(&unrelated, "user original").unwrap();
        assert!(validate_library(&selected, &resources).is_err());
        assert_eq!(
            std::fs::read_to_string(&unrelated).unwrap(),
            "user original"
        );
        std::fs::create_dir_all(selected.join("postgres-data")).unwrap();
        std::fs::create_dir_all(selected.join("config")).unwrap();
        std::fs::write(selected.join("postgres-data/PG_VERSION"), "16\n").unwrap();
        std::fs::write(selected.join("config/sources.yaml"), "sources: []\n").unwrap();
        assert!(validate_library(&selected, &resources).unwrap().1);
        assert!(validate_library(&resources, &resources).is_err());
        let preference = root.join("settings/data-directory.txt");
        save_library_at(&selected, &preference).unwrap();
        save_library_at(&selected, &preference).unwrap();
        assert_eq!(
            std::fs::read_to_string(&preference).unwrap(),
            selected.to_string_lossy()
        );
        let _ = std::fs::remove_dir_all(root);
    }

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
