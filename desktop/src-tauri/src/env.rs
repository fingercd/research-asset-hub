//! 启动前环境变量准备(镜像 scripts/hub.ps1 的 env 块;变量名与各 app config 对齐)。

use std::io;

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

pub fn prepare(ports: &Ports) -> io::Result<()> {
    if std::env::var("HUB_GATEWAY_SECRET").map(|v| v.trim().is_empty()).unwrap_or(true) {
        std::env::set_var("HUB_GATEWAY_SECRET", random_base64(48));
    }
    if std::env::var("HUB_MEILISEARCH_API_KEY").map(|v| v.trim().is_empty()).unwrap_or(true) {
        std::env::set_var("HUB_MEILISEARCH_API_KEY", random_hex(16));
    }
    if std::env::var("HUB_DATABASE_URL").map(|v| v.trim().is_empty()).unwrap_or(true) {
        let pw = std::env::var("HUB_POSTGRES_PASSWORD")
            .unwrap_or_else(|_| "research_hub_local".to_string());
        let url = format!(
            "postgresql://research_hub:{pw}@127.0.0.1:{}/research_asset_hub",
            ports.postgres
        );
        std::env::set_var("HUB_DATABASE_URL", &url);
        std::env::set_var("DATABASE_URL", url.replace("postgresql://", "postgresql+psycopg://"));
    }
    std::env::set_var("HUB_API_HOST", "127.0.0.1");
    std::env::set_var("HUB_API_PORT", ports.api.to_string());
    std::env::set_var("HUB_ASSET_GATEWAY_HOST", "127.0.0.1");
    std::env::set_var("HUB_ASSET_GATEWAY_PORT", ports.gateway.to_string());
    std::env::set_var("HUB_GATEWAY_ORIGIN", format!("http://127.0.0.1:{}", ports.gateway));
    std::env::set_var("HUB_GITNEXUS_URL", format!("http://127.0.0.1:{}", ports.gitnexus));
    // 对照检查补齐(api/asset_gateway/worker 共用的 ApiSettings 字段 + hub.ps1 env 块):
    // HUB_WEB_HOST/HUB_WEB_PORT/HUB_WEB_ORIGIN(web_host/web_port/web_origin)、
    // HUB_MEILISEARCH_URL(meilisearch_url)。web 端口保留用户设置,缺省 8765(与 hub.ps1 一致)。
    let web_port = std::env::var("HUB_WEB_PORT").unwrap_or_else(|_| "8765".to_string());
    std::env::set_var("HUB_WEB_HOST", "127.0.0.1");
    std::env::set_var("HUB_WEB_PORT", &web_port);
    std::env::set_var("HUB_WEB_ORIGIN", format!("http://127.0.0.1:{web_port}"));
    std::env::set_var("HUB_MEILISEARCH_URL", format!("http://127.0.0.1:{}", ports.meili));
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn prepare_sets_database_urls_and_keeps_secrets_stable() {
        std::env::remove_var("HUB_GATEWAY_SECRET");
        std::env::remove_var("HUB_DATABASE_URL");
        let ports = Ports { api: 8767, gateway: 8766, gitnexus: 4747, postgres: 5432, meili: 7700 };
        prepare(&ports).unwrap();
        let secret = std::env::var("HUB_GATEWAY_SECRET").unwrap();
        assert!(secret.len() >= 32);
        let db = std::env::var("HUB_DATABASE_URL").unwrap();
        assert!(db.contains("127.0.0.1:5432"));
        assert!(db.contains("research_asset_hub"));
        // 幂等:再次 prepare 不换 secret
        prepare(&ports).unwrap();
        assert_eq!(std::env::var("HUB_GATEWAY_SECRET").unwrap(), secret);
        for key in ["HUB_GATEWAY_SECRET", "HUB_DATABASE_URL"] {
            std::env::remove_var(key);
        }
    }
}
