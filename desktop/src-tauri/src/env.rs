//! 启动前环境变量准备(镜像 scripts/hub.ps1 的 env 块;变量名与各 app config 对齐)。

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

pub fn prepare(ports: &Ports, secrets_dir: &Path, app_config_dir: &Path) -> io::Result<()> {
    prepare_host_workspace_root(app_config_dir);
    if std::env::var("HUB_GATEWAY_SECRET")
        .map(|v| v.trim().is_empty())
        .unwrap_or(true)
    {
        std::env::set_var("HUB_GATEWAY_SECRET", random_base64(48));
    }
    if std::env::var("HUB_MEILISEARCH_API_KEY")
        .map(|v| v.trim().is_empty())
        .unwrap_or(true)
    {
        // 用户显式设置时尊重用户值,不落盘;仅自动生成路径做持久化复用
        std::env::set_var(
            "HUB_MEILISEARCH_API_KEY",
            load_or_create_meili_key(secrets_dir)?,
        );
    }
    if std::env::var("HUB_DATABASE_URL")
        .map(|v| v.trim().is_empty())
        .unwrap_or(true)
    {
        let pw = std::env::var("HUB_POSTGRES_PASSWORD")
            .unwrap_or_else(|_| "research_hub_local".to_string());
        let url = format!(
            "postgresql://research_hub:{pw}@127.0.0.1:{}/{}",
            ports.postgres, APP_DB_NAME
        );
        std::env::set_var("HUB_DATABASE_URL", &url);
        std::env::set_var(
            "DATABASE_URL",
            url.replace("postgresql://", "postgresql+psycopg://"),
        );
    }
    std::env::set_var("HUB_API_HOST", "127.0.0.1");
    std::env::set_var("HUB_API_PORT", ports.api.to_string());
    std::env::set_var("HUB_ASSET_GATEWAY_HOST", "127.0.0.1");
    std::env::set_var("HUB_ASSET_GATEWAY_PORT", ports.gateway.to_string());
    std::env::set_var(
        "HUB_GATEWAY_ORIGIN",
        format!("http://127.0.0.1:{}", ports.gateway),
    );
    // 对照检查补齐(api/asset_gateway/worker 共用的 ApiSettings 字段 + hub.ps1 env 块):
    // HUB_WEB_HOST/HUB_WEB_PORT/HUB_WEB_ORIGIN(web_host/web_port/web_origin)、
    // HUB_MEILISEARCH_URL(meilisearch_url)。web 端口保留用户设置,缺省 8765(与 hub.ps1 一致)。
    let web_port = std::env::var("HUB_WEB_PORT").unwrap_or_else(|_| "8765".to_string());
    std::env::set_var("HUB_WEB_HOST", "127.0.0.1");
    std::env::set_var("HUB_WEB_PORT", &web_port);
    std::env::set_var("HUB_WEB_ORIGIN", format!("http://127.0.0.1:{web_port}"));
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
    use std::sync::Mutex;

    /// prepare 读写进程级 env var,rustc 测试默认并行执行会互相干扰;
    /// 本模块全部测试用同一把锁串行化。
    static ENV_LOCK: Mutex<()> = Mutex::new(());

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

    fn cleanup_env() {
        for key in [
            "HUB_GATEWAY_SECRET",
            "HUB_MEILISEARCH_API_KEY",
            "HUB_DATABASE_URL",
            "DATABASE_URL",
            "HUB_WORKER_HEARTBEAT_PATH",
            "HUB_HOST_WORKSPACE_ROOT",
        ] {
            std::env::remove_var(key);
        }
    }

    #[test]
    fn prepare_sets_database_urls_and_keeps_secrets_stable() {
        let _guard = ENV_LOCK.lock().unwrap();
        let secrets = temp_secrets();
        cleanup_env();
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
        cleanup_env();
        let _ = std::fs::remove_dir_all(&secrets);
    }

    #[test]
    fn meili_key_persists_across_prepares() {
        let _guard = ENV_LOCK.lock().unwrap();
        let secrets = temp_secrets();
        cleanup_env();
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
        cleanup_env();
        let _ = std::fs::remove_dir_all(&secrets);
    }

    #[test]
    fn meili_key_reuses_preexisting_file() {
        let _guard = ENV_LOCK.lock().unwrap();
        let secrets = temp_secrets();
        std::fs::create_dir_all(&secrets).unwrap();
        let preloaded = "0123456789abcdef0123456789abcdef";
        std::fs::write(secrets.join("meili-master-key"), preloaded).unwrap();
        cleanup_env();
        prepare(&test_ports(), &secrets, &secrets.join("missing-config")).unwrap();
        assert_eq!(std::env::var("HUB_MEILISEARCH_API_KEY").unwrap(), preloaded);
        // 文件保持原内容(未被覆盖)
        assert_eq!(
            std::fs::read_to_string(secrets.join("meili-master-key")).unwrap(),
            preloaded
        );
        cleanup_env();
        let _ = std::fs::remove_dir_all(&secrets);
    }

    #[test]
    fn meili_key_regenerates_on_corrupt_or_empty_file() {
        let _guard = ENV_LOCK.lock().unwrap();
        let ports = test_ports();
        // 损坏内容:非 hex
        let secrets = temp_secrets();
        std::fs::create_dir_all(&secrets).unwrap();
        std::fs::write(secrets.join("meili-master-key"), "garbage!!\n").unwrap();
        cleanup_env();
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
        cleanup_env();
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
        cleanup_env();
        let _ = std::fs::remove_dir_all(&secrets);
    }

    #[test]
    fn prepare_loads_valid_workspace_hint_when_env_is_unset() {
        let _guard = ENV_LOCK.lock().unwrap();
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
        cleanup_env();

        prepare(&test_ports(), &secrets, &config_dir).unwrap();

        assert_eq!(
            std::env::var_os("HUB_HOST_WORKSPACE_ROOT"),
            Some(workspace_root.as_os_str().to_owned())
        );
        cleanup_env();
        let _ = std::fs::remove_dir_all(&secrets);
    }

    #[test]
    fn prepare_repairs_legacy_parent_workspace_hint() {
        let _guard = ENV_LOCK.lock().unwrap();
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
        cleanup_env();

        prepare(&test_ports(), &secrets, &config_dir).unwrap();

        assert_eq!(
            std::env::var_os("HUB_HOST_WORKSPACE_ROOT"),
            Some(workspace_root.as_os_str().to_owned())
        );
        cleanup_env();
        let _ = std::fs::remove_dir_all(&secrets);
    }

    #[test]
    fn prepare_preserves_explicit_workspace_root_and_ignores_invalid_hint() {
        let _guard = ENV_LOCK.lock().unwrap();
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
        cleanup_env();
        std::env::set_var("HUB_HOST_WORKSPACE_ROOT", &explicit_root);

        prepare(&test_ports(), &secrets, &config_dir).unwrap();

        assert_eq!(
            std::env::var_os("HUB_HOST_WORKSPACE_ROOT"),
            Some(explicit_root.as_os_str().to_owned())
        );
        cleanup_env();

        prepare(&test_ports(), &secrets, &config_dir).unwrap();
        assert!(std::env::var_os("HUB_HOST_WORKSPACE_ROOT").is_none());
        cleanup_env();
        let _ = std::fs::remove_dir_all(&secrets);
    }
}
