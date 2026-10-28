//! 桌面监督器为本轮受管运行时构造环境；开发环境不继承其他资料库配置。

use std::io;
use std::path::{Path, PathBuf};

use crate::orchestrator::Ports;

fn random_base64(bytes: usize) -> String {
    use base64::Engine;
    let mut buf = vec![0u8; bytes];
    getrandom::getrandom(&mut buf).expect("OS RNG available");
    base64::engine::general_purpose::STANDARD.encode(buf)
}

fn random_hex(bytes: usize) -> String {
    let mut buf = vec![0u8; bytes];
    getrandom::getrandom(&mut buf).expect("OS RNG available");
    buf.iter().map(|b| format!("{b:02x}")).collect()
}

/// Meilisearch master key 持久化:首启生成后写入 `secrets_dir/meili-master-key`,
/// 后续启动复用;文件缺失、为空或内容非法(非 16 字节 hex)时重新生成覆盖。
/// 否则每次启动都换随机 key,meili 数据目录(dumps/索引密钥)与旧 key 绑定而不可读。
///
/// 权限说明:Windows 上不强制 0600(密钥文件位于用户数据目录,继承用户级 ACL,
/// 个人单机工具可接受);Unix 上如需收紧可加 `#[cfg(unix)]` 的 set_permissions,本期不实现。
fn load_or_create_meili_key(secrets_dir: &Path) -> io::Result<String> {
    std::fs::create_dir_all(secrets_dir)?;
    let key_file = secrets_dir.join("meili-master-key");
    if let Ok(content) = std::fs::read_to_string(&key_file) {
        let trimmed = content.trim();
        let is_valid_hex = trimmed.len() == 32 && trimmed.chars().all(|c| c.is_ascii_hexdigit());
        if is_valid_hex {
            return Ok(trimmed.to_string());
        }
        // 损坏/为空:落入重新生成路径,写新 key 覆盖旧文件
    }
    let key = random_hex(16);
    std::fs::write(&key_file, &key)?;
    Ok(key)
}

/// 业务库名:initdb 只建 postgres/template0/template1 模板库,业务库由壳的
/// ensure_database 显式创建,alembic 迁移经 HUB_DATABASE_URL 连接它。
pub const APP_DB_NAME: &str = "research_asset_hub";

const WORKSPACE_ROOT_HINT_FILE: &str = "workspace-root.txt";
const PROJECT_DIRECTORY_NAME: &str = "research-asset-hub";

fn configured_workspace_root(path: &Path) -> Option<PathBuf> {
    if path.is_dir() && path.join("config").join("sources.yaml").is_file() {
        return Some(path.to_path_buf());
    }

    // 兼容早期安装包：hint 曾被误写成项目根的父目录（例如“核心领域”）。
    // 只接受具有完整 config/sources.yaml 标记的确定子目录，避免猜测任意目录。
    let repaired = path.join(PROJECT_DIRECTORY_NAME);
    if repaired.is_dir() && repaired.join("config").join("sources.yaml").is_file() {
        return Some(repaired);
    }
    None
}

/// 安装包会在 `app/config/workspace-root.txt` 写入组装时的宿主资料根。
/// 仅当用户没有显式设置 `HUB_HOST_WORKSPACE_ROOT` 且提示指向现存目录时
/// 才注入环境；开发模式没有该文件，因此保持 sources.yaml 原有的相对解析。
fn prepare_host_workspace_root(app_config_dir: &Path) {
    if std::env::var("HUB_HOST_WORKSPACE_ROOT")
        .map(|value| !value.trim().is_empty())
        .unwrap_or(false)
    {
        return;
    }

    let hint_path = app_config_dir.join(WORKSPACE_ROOT_HINT_FILE);
    let Ok(raw_hint) = std::fs::read_to_string(hint_path) else {
        return;
    };
    // Windows PowerShell 5 的 UTF-8 Set-Content 可能带 BOM，同时容忍末尾换行。
    let hint = raw_hint.trim().trim_start_matches('\u{feff}').trim();
    if hint.is_empty() {
        return;
    }
    if let Some(workspace_root) = configured_workspace_root(Path::new(hint)) {
        std::env::set_var(
            "HUB_HOST_WORKSPACE_ROOT",
            workspace_root.to_string_lossy().to_string(),
        );
    }
}

fn copy_initial_configuration(source: &Path, destination: &Path) -> io::Result<()> {
    std::fs::create_dir_all(destination)?;
    for entry in std::fs::read_dir(source)? {
        let entry = entry?;
        let target = destination.join(entry.file_name());
        if entry.file_type()?.is_dir() {
            copy_initial_configuration(&entry.path(), &target)?;
        } else if entry.file_type()?.is_file() {
            std::fs::copy(entry.path(), target)?;
        }
    }
    Ok(())
}

/// Public first boot uses empty bundled defaults. Existing user configuration
/// wins on every upgrade. Personal/legacy hints preserve frozen relative roots.
fn prepare_user_configuration(data_root: &Path, app_config_dir: &Path) -> io::Result<PathBuf> {
    let user_config = data_root.join("config");
    if user_config.join("sources.yaml").is_file() {
        return Ok(user_config);
    }
    let packaged = app_config_dir.join("release-profile.txt").is_file()
        || app_config_dir.join(WORKSPACE_ROOT_HINT_FILE).is_file();
    if packaged && app_config_dir.join("sources.yaml").is_file() {
        if user_config.exists() {
            return Err(io::Error::new(
                io::ErrorKind::AlreadyExists,
                "User configuration exists without sources.yaml; inspect it before initializing",
            ));
        }
        copy_initial_configuration(app_config_dir, &user_config)?;
        return Ok(user_config);
    }
    Ok(app_config_dir.to_path_buf())
}

/// Direct executable launches receive the same isolation as the developer script.
pub fn isolate_development(profile: &crate::runtime::DevelopmentProfile) {
    for key in [
        "HUB_DATABASE_URL",
        "DATABASE_URL",
        "HUB_MEILISEARCH_URL",
        "HUB_MEILISEARCH_API_KEY",
        "HUB_GATEWAY_SECRET",
        "HUB_POSTGRES_PASSWORD",
        "HUB_SOURCES_CONFIG_PATH",
        "HUB_TAXONOMY_CONFIG_PATH",
        "HUB_DOMAIN_PORTAL_CONFIG_PATH",
        "HUB_WORKER_HEARTBEAT_PATH",
        "HUB_HOST_WORKSPACE_ROOT",
        "PGHOST",
        "PGPORT",
        "PGDATABASE",
        "PGUSER",
        "PGPASSWORD",
        "PGSERVICE",
        "PGSERVICEFILE",
        "PGOPTIONS",
    ] {
        std::env::remove_var(key);
    }
    std::env::set_var("HUB_DATA_DIR", &profile.data_dir);
    std::env::set_var("HUB_HOST_WORKSPACE_ROOT", &profile.source_root);
}

pub fn prepare(ports: &Ports, secrets_dir: &Path, app_config_dir: &Path) -> io::Result<()> {
    let data_root = crate::data_dir::data_root();
    std::env::set_var("HUB_DATA_DIR", &data_root);
    let config = prepare_user_configuration(&data_root, app_config_dir)?;
    prepare_host_workspace_root(&config);
    for (key, filename) in [
        ("HUB_SOURCES_CONFIG_PATH", "sources.yaml"),
        ("HUB_TAXONOMY_CONFIG_PATH", "taxonomy.yaml"),
        ("HUB_DOMAIN_PORTAL_CONFIG_PATH", "domain-portals/v2"),
    ] {
        if std::env::var(key)
            .map(|value| value.trim().is_empty())
            .unwrap_or(true)
        {
            std::env::set_var(key, config.join(filename));
        }
    }
    if std::env::var("HUB_GATEWAY_SECRET")
        .map(|v| v.trim().is_empty())
        .unwrap_or(true)
    {
        std::env::set_var("HUB_GATEWAY_SECRET", random_base64(48));
    }
    // Desktop always owns its database/search process; external connection
    // overrides belong to the standalone CLI/API contracts, not this supervisor.
    std::env::set_var(
        "HUB_MEILISEARCH_API_KEY",
        load_or_create_meili_key(secrets_dir)?,
    );
    let pw =
        std::env::var("HUB_POSTGRES_PASSWORD").unwrap_or_else(|_| "research_hub_local".to_string());
    let url = format!(
        "postgresql://research_hub:{pw}@127.0.0.1:{}/{}",
        ports.postgres, APP_DB_NAME
    );
    std::env::set_var("HUB_DATABASE_URL", &url);
    std::env::set_var(
        "DATABASE_URL",
        url.replace("postgresql://", "postgresql+psycopg://"),
    );
    std::env::set_var("HUB_API_HOST", "127.0.0.1");
    std::env::set_var("HUB_API_PORT", ports.api.to_string());
    std::env::set_var("HUB_ASSET_GATEWAY_HOST", "127.0.0.1");
    std::env::set_var("HUB_ASSET_GATEWAY_PORT", ports.gateway.to_string());
    std::env::set_var(
        "HUB_GATEWAY_ORIGIN",
        format!("http://127.0.0.1:{}", ports.gateway),
    );
    std::env::set_var(
        "HUB_MEILISEARCH_URL",
        format!("http://127.0.0.1:{}", ports.meili),
    );
    // worker 心跳路径:配置默认值是相对路径 work/state/worker-heartbeat.json,
    // 桌面壳内 api/worker 的 cwd 是安装目录(C:\Program Files\...\app,无写权限),
    // worker 启动即 PermissionError 崩溃。壳注入数据根下的绝对路径,api 与
    // worker 经同一 env(HUB_WORKER_HEARTBEAT_PATH,ApiSettings env_prefix=HUB_)
    // 读到一致路径,健康检查的 worker stale 判断才有效。
    if std::env::var("HUB_WORKER_HEARTBEAT_PATH")
        .map(|v| v.trim().is_empty())
        .unwrap_or(true)
    {
        let heartbeat = crate::data_dir::data_root()
            .join("work")
            .join("state")
            .join("worker-heartbeat.json");
        std::env::set_var(
            "HUB_WORKER_HEARTBEAT_PATH",
            heartbeat.to_string_lossy().to_string(),
        );
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::test_env::EnvironmentGuard;

    fn prepare(ports: &Ports, secrets_dir: &Path, app_config_dir: &Path) -> io::Result<()> {
        std::env::set_var("HUB_DATA_DIR", secrets_dir.join("test-data"));
        super::prepare(ports, secrets_dir, app_config_dir)
    }

    /// 逐实例唯一的临时 secrets 目录,避免并行 remove_dir_all 冲突。
    static SECRETS_SEQ: std::sync::atomic::AtomicU32 = std::sync::atomic::AtomicU32::new(0);

    fn temp_secrets() -> std::path::PathBuf {
        let n = SECRETS_SEQ.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
        std::env::temp_dir().join(format!("hub-env-test-{}-{n}", std::process::id()))
    }

    fn test_ports() -> Ports {
        Ports {
            api: 8767,
            gateway: 8766,
            postgres: 5432,
            meili: 7700,
        }
    }

    #[test]
    fn public_initial_configuration_is_copied_once_and_user_changes_survive_upgrade() {
        let root = temp_secrets();
        let bundled = root.join("app/config");
        let data = root.join("user-data");
        std::fs::create_dir_all(&bundled).unwrap();
        std::fs::write(bundled.join("release-profile.txt"), "public").unwrap();
        std::fs::write(bundled.join("sources.yaml"), "sources: []\n").unwrap();
        let config = prepare_user_configuration(&data, &bundled).unwrap();
        assert_eq!(config, data.join("config"));
        std::fs::write(config.join("sources.yaml"), "sources: [user-managed]\n").unwrap();
        std::fs::write(bundled.join("sources.yaml"), "sources: [new-default]\n").unwrap();
        assert_eq!(prepare_user_configuration(&data, &bundled).unwrap(), config);
        assert_eq!(
            std::fs::read_to_string(config.join("sources.yaml")).unwrap(),
            "sources: [user-managed]\n"
        );
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn direct_development_launch_rebuilds_environment_from_explicit_roots() {
        let _guard = EnvironmentGuard::isolated();
        let root = temp_secrets();
        let profile = crate::runtime::DevelopmentProfile {
            source_root: root.join("source"),
            runtime_root: root.join("runtime"),
            data_dir: root.join("isolated-data"),
        };
        for key in [
            "HUB_DATABASE_URL",
            "DATABASE_URL",
            "HUB_MEILISEARCH_URL",
            "HUB_MEILISEARCH_API_KEY",
            "HUB_GATEWAY_SECRET",
            "HUB_POSTGRES_PASSWORD",
            "HUB_DATA_DIR",
            "HUB_HOST_WORKSPACE_ROOT",
            "HUB_SOURCES_CONFIG_PATH",
            "HUB_TAXONOMY_CONFIG_PATH",
            "HUB_DOMAIN_PORTAL_CONFIG_PATH",
            "HUB_WORKER_HEARTBEAT_PATH",
            "PGSERVICE",
            "PGOPTIONS",
            "PGPASSWORD",
        ] {
            std::env::set_var(key, "other-library-do-not-use");
        }
        isolate_development(&profile);
        let ports = Ports {
            api: 18767,
            gateway: 18766,
            postgres: 15432,
            meili: 17700,
        };
        super::prepare(
            &ports,
            &profile.data_dir.join("secrets"),
            &profile.source_root.join("config"),
        )
        .unwrap();
        assert_eq!(crate::data_dir::data_root(), profile.data_dir);
        assert_eq!(
            std::env::var("HUB_DATABASE_URL").unwrap(),
            "postgresql://research_hub:research_hub_local@127.0.0.1:15432/research_asset_hub"
        );
        assert_eq!(
            std::env::var("HUB_MEILISEARCH_URL").unwrap(),
            "http://127.0.0.1:17700"
        );
        for (key, file) in [
            ("HUB_SOURCES_CONFIG_PATH", "sources.yaml"),
            ("HUB_TAXONOMY_CONFIG_PATH", "taxonomy.yaml"),
            ("HUB_DOMAIN_PORTAL_CONFIG_PATH", "domain-portals/v2"),
        ] {
            assert_eq!(
                std::env::var_os(key),
                Some(
                    profile
                        .source_root
                        .join("config")
                        .join(file)
                        .into_os_string()
                )
            );
        }
        assert_eq!(
            std::env::var_os("HUB_WORKER_HEARTBEAT_PATH"),
            Some(
                profile
                    .data_dir
                    .join("work")
                    .join("state")
                    .join("worker-heartbeat.json")
                    .into_os_string()
            )
        );
        assert_eq!(
            std::env::var_os("HUB_HOST_WORKSPACE_ROOT"),
            Some(profile.source_root.into_os_string())
        );
        for key in ["PGSERVICE", "PGOPTIONS", "PGPASSWORD"] {
            assert!(std::env::var_os(key).is_none());
        }
        assert_ne!(
            std::env::var("HUB_MEILISEARCH_API_KEY").unwrap(),
            "other-library-do-not-use"
        );
        let _ = std::fs::remove_dir_all(root);
    }

    #[test]
    fn desktop_connections_override_external_database_and_search_settings() {
        let _guard = EnvironmentGuard::isolated();
        let secrets = temp_secrets();
        std::env::set_var("HUB_DATABASE_URL", "postgresql://foreign/production");
        std::env::set_var("DATABASE_URL", "postgresql://foreign/production");
        std::env::set_var("HUB_MEILISEARCH_URL", "http://foreign:7700");
        std::env::set_var("HUB_MEILISEARCH_API_KEY", "foreign-key");
        prepare(&test_ports(), &secrets, &secrets.join("config")).unwrap();
        assert!(std::env::var("HUB_DATABASE_URL")
            .unwrap()
            .contains("127.0.0.1:5432/research_asset_hub"));
        assert!(std::env::var("DATABASE_URL")
            .unwrap()
            .contains("127.0.0.1:5432/research_asset_hub"));
        assert_eq!(
            std::env::var("HUB_MEILISEARCH_URL").unwrap(),
            "http://127.0.0.1:7700"
        );
        assert_eq!(
            std::env::var("HUB_MEILISEARCH_API_KEY").unwrap(),
            std::fs::read_to_string(secrets.join("meili-master-key")).unwrap()
        );
        let _ = std::fs::remove_dir_all(secrets);
    }

    #[test]
    fn prepare_sets_database_urls_and_keeps_secrets_stable() {
        let guard = EnvironmentGuard::isolated();
        let secrets = temp_secrets();
        guard.clear();
        let ports = test_ports();
        prepare(&ports, &secrets, &secrets.join("missing-config")).unwrap();
        let secret = std::env::var("HUB_GATEWAY_SECRET").unwrap();
        assert!(secret.len() >= 32);
        let db = std::env::var("HUB_DATABASE_URL").unwrap();
        assert!(db.contains("127.0.0.1:5432"));
        assert!(db.contains("research_asset_hub"));
        // worker 心跳路径注入数据根(避免安装目录无写权限导致 worker 崩溃)
        let heartbeat = std::env::var("HUB_WORKER_HEARTBEAT_PATH").unwrap();
        assert!(
            heartbeat.ends_with("worker-heartbeat.json"),
            "心跳路径应指向 worker-heartbeat.json: {heartbeat}"
        );
        assert!(
            !heartbeat.contains("Program Files"),
            "心跳路径不得落在安装目录: {heartbeat}"
        );
        // 幂等:再次 prepare 不换 gateway secret
        prepare(&ports, &secrets, &secrets.join("missing-config")).unwrap();
        assert_eq!(std::env::var("HUB_GATEWAY_SECRET").unwrap(), secret);
        guard.clear();
        let _ = std::fs::remove_dir_all(&secrets);
    }

    #[test]
    fn meili_key_persists_across_prepares() {
        let guard = EnvironmentGuard::isolated();
        let secrets = temp_secrets();
        guard.clear();
        let ports = test_ports();
        prepare(&ports, &secrets, &secrets.join("missing-config")).unwrap();
        let first = std::env::var("HUB_MEILISEARCH_API_KEY").unwrap();
        // 二次 prepare:env 已被置位走不到生成路径,清掉后重走持久化路径
        std::env::remove_var("HUB_MEILISEARCH_API_KEY");
        prepare(&ports, &secrets, &secrets.join("missing-config")).unwrap();
        let second = std::env::var("HUB_MEILISEARCH_API_KEY").unwrap();
        assert_eq!(first, second, "二次 prepare 应复用落盘的 meili key");
        // 落盘内容与 env 一致
        let on_disk = std::fs::read_to_string(secrets.join("meili-master-key")).unwrap();
        assert_eq!(on_disk.trim(), first);
        guard.clear();
        let _ = std::fs::remove_dir_all(&secrets);
    }

    #[test]
    fn meili_key_reuses_preexisting_file() {
        let guard = EnvironmentGuard::isolated();
        let secrets = temp_secrets();
        std::fs::create_dir_all(&secrets).unwrap();
        let preloaded = "0123456789abcdef0123456789abcdef";
        std::fs::write(secrets.join("meili-master-key"), preloaded).unwrap();
        guard.clear();
        prepare(&test_ports(), &secrets, &secrets.join("missing-config")).unwrap();
        assert_eq!(std::env::var("HUB_MEILISEARCH_API_KEY").unwrap(), preloaded);
        // 文件保持原内容(未被覆盖)
        assert_eq!(
            std::fs::read_to_string(secrets.join("meili-master-key")).unwrap(),
            preloaded
        );
        guard.clear();
        let _ = std::fs::remove_dir_all(&secrets);
    }

    #[test]
    fn meili_key_regenerates_on_corrupt_or_empty_file() {
        let guard = EnvironmentGuard::isolated();
        let ports = test_ports();
        // 损坏内容:非 hex
        let secrets = temp_secrets();
        std::fs::create_dir_all(&secrets).unwrap();
        std::fs::write(secrets.join("meili-master-key"), "garbage!!\n").unwrap();
        guard.clear();
        prepare(&ports, &secrets, &secrets.join("missing-config")).unwrap();
        let regenerated = std::env::var("HUB_MEILISEARCH_API_KEY").unwrap();
        assert_ne!(regenerated, "garbage!!");
        assert_eq!(
            regenerated.len(),
            32,
            "重新生成的 key 应为 16 字节 hex: {regenerated}"
        );
        assert!(
            regenerated.chars().all(|c| c.is_ascii_hexdigit()),
            "重新生成的 key 应为合法 hex: {regenerated}"
        );
        let _ = std::fs::remove_dir_all(&secrets);

        // 空文件
        let secrets = temp_secrets();
        std::fs::create_dir_all(&secrets).unwrap();
        std::fs::write(secrets.join("meili-master-key"), "\n").unwrap();
        guard.clear();
        prepare(&ports, &secrets, &secrets.join("missing-config")).unwrap();
        let key = std::env::var("HUB_MEILISEARCH_API_KEY").unwrap();
        assert_eq!(key.len(), 32);
        assert_eq!(
            std::fs::read_to_string(secrets.join("meili-master-key"))
                .unwrap()
                .trim(),
            key,
            "空文件应被覆盖为新 key"
        );
        guard.clear();
        let _ = std::fs::remove_dir_all(&secrets);
    }

    #[test]
    fn prepare_loads_valid_workspace_hint_when_env_is_unset() {
        let guard = EnvironmentGuard::isolated();
        let secrets = temp_secrets();
        let config_dir = secrets.join("app").join("config");
        let workspace_root = secrets.join("host-workspace");
        std::fs::create_dir_all(&config_dir).unwrap();
        std::fs::create_dir_all(workspace_root.join("config")).unwrap();
        std::fs::write(
            workspace_root.join("config").join("sources.yaml"),
            "workspace_root: ..\n",
        )
        .unwrap();
        std::fs::write(
            config_dir.join(WORKSPACE_ROOT_HINT_FILE),
            format!("\u{feff}{}\n", workspace_root.display()),
        )
        .unwrap();
        guard.clear();

        prepare(&test_ports(), &secrets, &config_dir).unwrap();

        assert_eq!(
            std::env::var_os("HUB_HOST_WORKSPACE_ROOT"),
            Some(workspace_root.as_os_str().to_owned())
        );
        guard.clear();
        let _ = std::fs::remove_dir_all(&secrets);
    }

    #[test]
    fn prepare_repairs_legacy_parent_workspace_hint() {
        let guard = EnvironmentGuard::isolated();
        let secrets = temp_secrets();
        let config_dir = secrets.join("app").join("config");
        let parent_root = secrets.join("core-domain");
        let workspace_root = parent_root.join(PROJECT_DIRECTORY_NAME);
        std::fs::create_dir_all(&config_dir).unwrap();
        std::fs::create_dir_all(workspace_root.join("config")).unwrap();
        std::fs::write(
            workspace_root.join("config").join("sources.yaml"),
            "workspace_root: ..\n",
        )
        .unwrap();
        std::fs::write(
            config_dir.join(WORKSPACE_ROOT_HINT_FILE),
            parent_root.to_string_lossy().as_bytes(),
        )
        .unwrap();
        guard.clear();

        prepare(&test_ports(), &secrets, &config_dir).unwrap();

        assert_eq!(
            std::env::var_os("HUB_HOST_WORKSPACE_ROOT"),
            Some(workspace_root.as_os_str().to_owned())
        );
        guard.clear();
        let _ = std::fs::remove_dir_all(&secrets);
    }

    #[test]
    fn prepare_preserves_explicit_workspace_root_and_ignores_invalid_hint() {
        let guard = EnvironmentGuard::isolated();
        let secrets = temp_secrets();
        let config_dir = secrets.join("app").join("config");
        let explicit_root = secrets.join("explicit-workspace");
        std::fs::create_dir_all(&config_dir).unwrap();
        std::fs::create_dir_all(&explicit_root).unwrap();
        std::fs::write(
            config_dir.join(WORKSPACE_ROOT_HINT_FILE),
            secrets
                .join("missing-workspace")
                .to_string_lossy()
                .as_bytes(),
        )
        .unwrap();
        guard.clear();
        std::env::set_var("HUB_HOST_WORKSPACE_ROOT", &explicit_root);

        prepare(&test_ports(), &secrets, &config_dir).unwrap();

        assert_eq!(
            std::env::var_os("HUB_HOST_WORKSPACE_ROOT"),
            Some(explicit_root.as_os_str().to_owned())
        );
        guard.clear();

        prepare(&test_ports(), &secrets, &config_dir).unwrap();
        assert!(std::env::var_os("HUB_HOST_WORKSPACE_ROOT").is_none());
        guard.clear();
        let _ = std::fs::remove_dir_all(&secrets);
    }
}
