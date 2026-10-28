//! Process environment isolation shared by tests in every module.
//! Never compiled into the desktop application.

use std::ffi::OsString;
use std::sync::{Mutex, MutexGuard};

static ENVIRONMENT: Mutex<()> = Mutex::new(());

// Settings mutated by env::prepare or directly exercised by desktop tests.
const KEYS: &[&str] = &[
    "PGHOST",
    "PGPORT",
    "PGDATABASE",
    "PGUSER",
    "PGPASSWORD",
    "PGSERVICE",
    "PGSERVICEFILE",
    "PGOPTIONS",
    "HUB_DATA_DIR",
    "HUB_GATEWAY_SECRET",
    "HUB_MEILISEARCH_API_KEY",
    "HUB_DATABASE_URL",
    "DATABASE_URL",
    "HUB_POSTGRES_PASSWORD",
    "HUB_WORKER_HEARTBEAT_PATH",
    "HUB_HOST_WORKSPACE_ROOT",
    "HUB_SOURCES_CONFIG_PATH",
    "HUB_TAXONOMY_CONFIG_PATH",
    "HUB_DOMAIN_PORTAL_CONFIG_PATH",
    "HUB_API_HOST",
    "HUB_API_PORT",
    "HUB_ASSET_GATEWAY_HOST",
    "HUB_ASSET_GATEWAY_PORT",
    "HUB_GATEWAY_ORIGIN",
    "HUB_MEILISEARCH_URL",
    "HUB_POSTGRES_PORT",
    "HUB_MEILISEARCH_PORT",
];

pub struct EnvironmentGuard {
    _lock: MutexGuard<'static, ()>,
    saved: Vec<(&'static str, Option<OsString>)>,
}

impl EnvironmentGuard {
    pub fn isolated() -> Self {
        // Drop restores the environment during panic unwinding, so a poisoned
        // lock need not cause unrelated tests to fail after the original failure.
        let lock = ENVIRONMENT
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let saved = KEYS
            .iter()
            .map(|key| (*key, std::env::var_os(key)))
            .collect();
        let guard = Self { _lock: lock, saved };
        guard.clear();
        guard
    }

    pub fn clear(&self) {
        for key in KEYS {
            std::env::remove_var(key);
        }
    }

    fn restore(&self) {
        for (key, value) in &self.saved {
            match value {
                Some(value) => std::env::set_var(key, value),
                None => std::env::remove_var(key),
            }
        }
    }
}

impl Drop for EnvironmentGuard {
    fn drop(&mut self) {
        self.restore();
    }
}

#[test]
fn restores_original_values_and_missing_variables_before_unlocking() {
    let guard = EnvironmentGuard::isolated();
    for key in KEYS {
        std::env::set_var(key, "test-only replacement");
    }
    guard.restore();
    for (key, original) in &guard.saved {
        assert_eq!(std::env::var_os(key), *original, "{key}");
    }
}
