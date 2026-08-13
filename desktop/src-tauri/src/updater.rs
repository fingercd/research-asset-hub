//! 检查更新:更新源目录(默认 Documents\ResearchAssetHub\updates)内的 manifest.json
//! 格式: {"version": "0.2.0", "installer": "Hub-Setup-0.2.0.exe", "notes": "..."}
//! installer 与 manifest 同目录;有新版则运行安装器(NSIS 静默)。
//!
//! 后续项:安装包 Authenticode 签名校验本期未实现(个人单机工具,更新源为
//! 本地目录,确认框已提示用户核对文件名与来源可信);如更新源迁至网络,
//! 须在 launch_installer 前校验签名链再执行。

use std::path::{Path, PathBuf};
use std::process::Command;

use semver::Version;
use serde::Deserialize;

use crate::data_dir::data_paths;

#[derive(Debug, Deserialize)]
pub struct UpdateManifest {
    pub version: Version,
    pub installer: String,
    #[serde(default)]
    pub notes: Option<String>,
}

pub struct UpdateCheck {
    pub update_available: bool,
    pub manifest: Option<UpdateManifest>,
}

#[derive(Debug)]
pub enum UpdateError {
    NoManifest,
    InvalidManifest(String),
    NotFound,
    LaunchFailed(String),
}

impl std::fmt::Display for UpdateError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            UpdateError::NoManifest => write!(f, "更新源中未找到 manifest.json"),
            UpdateError::InvalidManifest(e) => write!(f, "manifest 无效: {e}"),
            UpdateError::NotFound => write!(f, "安装包文件不存在"),
            UpdateError::LaunchFailed(e) => write!(f, "安装器启动失败: {e}"),
        }
    }
}

impl std::error::Error for UpdateError {}

pub fn default_updates_dir() -> PathBuf {
    data_paths().updates
}

pub fn load_manifest(updates_dir: &Path) -> Result<UpdateManifest, UpdateError> {
    let path = updates_dir.join("manifest.json");
    let content = std::fs::read_to_string(&path).map_err(|_| UpdateError::NoManifest)?;
    serde_json::from_str::<UpdateManifest>(&content)
        .map_err(|e| UpdateError::InvalidManifest(e.to_string()))
}

pub fn check(current: &str, updates_dir: &Path) -> Result<UpdateCheck, UpdateError> {
    let current = Version::parse(current)
        .map_err(|e| UpdateError::InvalidManifest(format!("当前版本号无效: {e}")))?;
    match load_manifest(updates_dir) {
        Ok(manifest) => {
            let update_available = manifest.version > current
                && updates_dir.join(&manifest.installer).is_file();
            Ok(UpdateCheck {
                update_available,
                manifest: Some(manifest),
            })
        }
        Err(e) => Err(e),
    }
}

pub fn launch_installer(
    updates_dir: &Path,
    manifest: &UpdateManifest,
) -> Result<(), UpdateError> {
    let installer = updates_dir.join(&manifest.installer);
    if !installer.is_file() {
        return Err(UpdateError::NotFound);
    }
    Command::new(&installer)
        .arg("/S") // NSIS 静默安装
        .spawn()
        .map_err(|e| UpdateError::LaunchFailed(e.to_string()))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;

    fn write_manifest(dir: &std::path::Path, content: &str) -> PathBuf {
        std::fs::create_dir_all(dir).unwrap();
        let p = dir.join("manifest.json");
        std::fs::write(&p, content).unwrap();
        p
    }

    #[test]
    fn check_reports_newer_version() {
        let dir = std::env::temp_dir().join(format!("hub-upd-{}", std::process::id()));
        write_manifest(
            &dir,
            r#"{"version": "0.2.0", "installer": "Hub-Setup-0.2.0.exe", "notes": "新功能"}"#,
        );
        std::fs::write(dir.join("Hub-Setup-0.2.0.exe"), "").unwrap();
        let result = check("0.1.0", &dir).unwrap();
        assert!(result.update_available);
        assert_eq!(result.manifest.unwrap().version.to_string(), "0.2.0");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn check_ignores_same_or_older() {
        let dir = std::env::temp_dir().join(format!("hub-upd-old-{}", std::process::id()));
        write_manifest(&dir, r#"{"version": "0.0.9", "installer": "x.exe"}"#);
        assert!(!check("0.1.0", &dir).unwrap().update_available);
        write_manifest(&dir, r#"{"version": "0.1.0", "installer": "x.exe"}"#);
        assert!(!check("0.1.0", &dir).unwrap().update_available);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn missing_manifest_is_no_manifest_error() {
        let dir = std::env::temp_dir().join(format!("hub-upd-none-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        assert!(matches!(check("0.1.0", &dir), Err(UpdateError::NoManifest)));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn invalid_manifest_reports_parse_error() {
        let dir = std::env::temp_dir().join(format!("hub-upd-bad-{}", std::process::id()));
        write_manifest(&dir, r#"{"version": "not-a-version"}"#);
        assert!(matches!(check("0.1.0", &dir), Err(UpdateError::InvalidManifest(_))));
        let _ = std::fs::remove_dir_all(&dir);
    }
}
