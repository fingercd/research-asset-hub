//! 启动编排状态机:预检 → PostgreSQL → Meilisearch → 迁移 → 应用进程 → 就绪。
//! 不依赖 Tauri;事件经 on_event 回调上报(Task 9 桥接到加载页)。

use std::path::PathBuf;
use std::process::Command as StdCommand;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};

use crate::data_dir::{data_paths, ensure_dirs, postgres_initialized, DataPaths};
use crate::processes::{kill_tree, spawn, wait_for_port, ChildHandle};
use crate::runtime::RuntimePaths;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Step {
    Preflight,
    Postgres,
    Meili,
    Migrate,
    Services,
}

impl Step {
    pub fn as_str(self) -> &'static str {
        match self {
            Step::Preflight => "预检环境",
            Step::Postgres => "启动数据库",
            Step::Meili => "启动搜索引擎",
            Step::Migrate => "数据迁移",
            Step::Services => "启动应用服务",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StepStatus {
    Pending,
    Done,
    Failed,
}

#[derive(Debug)]
pub struct StartupEvent {
    pub step: Step,
    pub status: StepStatus,
    pub detail: Option<String>,
}

#[derive(Clone, Copy, Debug)]
pub struct Ports {
    pub api: u16,
    pub gateway: u16,
    pub gitnexus: u16,
    pub postgres: u16,
    pub meili: u16,
}

impl Ports {
    pub fn from_env() -> Self {
        let get = |key: &str, default: u16| {
            std::env::var(key)
                .ok()
                .and_then(|v| v.parse::<u16>().ok())
                .filter(|p| *p > 0)
                .unwrap_or(default)
        };
        Ports {
            api: get("HUB_API_PORT", 8767),
            gateway: get("HUB_ASSET_GATEWAY_PORT", 8766),
            gitnexus: get("HUB_GITNEXUS_PORT", 4747),
            postgres: get("HUB_POSTGRES_PORT", 5432),
            meili: get("HUB_MEILISEARCH_PORT", 7700),
        }
    }
}

#[derive(Debug)]
pub enum StartupError {
    StepFailed { step: Step, detail: String },
}

impl std::fmt::Display for StartupError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            StartupError::StepFailed { step, detail } => {
                write!(f, "{}失败: {}", step.as_str(), detail)
            }
        }
    }
}

impl std::error::Error for StartupError {}

type Probe = Box<dyn Fn(u16) -> bool + Send + Sync>;

pub struct Orchestrator {
    paths: RuntimePaths,
    data: DataPaths,
    ports: Ports,
    probe: Probe,
    on_event: Box<dyn Fn(&StartupEvent) + Send + Sync>,
    children: Vec<ChildHandle>,
    shutting_down: Arc<AtomicBool>,
}

impl Orchestrator {
    pub fn new(
        paths: RuntimePaths,
        ports: Ports,
        on_event: impl Fn(&StartupEvent) + Send + Sync + 'static,
    ) -> Self {
        Orchestrator {
            paths,
            data: data_paths(),
            ports,
            probe: Box::new(crate::processes::tcp_probe),
            on_event: Box::new(on_event),
            children: Vec::new(),
            shutting_down: Arc::new(AtomicBool::new(false)),
        }
    }

    fn emit(&self, step: Step, status: StepStatus, detail: Option<String>) {
        (self.on_event)(&StartupEvent { step, status, detail });
    }

    fn fail(&self, step: Step, detail: impl Into<String>) -> StartupError {
        let detail = detail.into();
        // 失败事件统一在此发出:所有 `?`/`return Err(self.fail(...))` 路径都自动获得 (step, Failed)
        self.emit(step, StepStatus::Failed, Some(detail.clone()));
        StartupError::StepFailed { step, detail }
    }

    pub fn start(&mut self) -> Result<(), StartupError> {
        ensure_dirs(&self.data).map_err(|e| {
            self.fail(Step::Preflight, format!("无法创建数据目录: {e}"))
        })?;

        // 1. 预检:关键端口冲突检测(应用端口)
        self.emit(Step::Preflight, StepStatus::Pending, None);
        let busy: Vec<u16> = [self.ports.api, self.ports.gateway, self.ports.gitnexus]
            .into_iter()
            .filter(|p| (self.probe)(*p))
            .collect();
        if !busy.is_empty() {
            // fail 内部会先发出 (Preflight, Failed) 事件,此处不再手动 emit
            return Err(self.fail(Step::Preflight, format!("端口被占用: {busy:?}")));
        }
        self.emit(Step::Preflight, StepStatus::Done, None);

        // 2. PostgreSQL
        self.emit(Step::Postgres, StepStatus::Pending, None);
        self.start_postgres()?;
        self.emit(Step::Postgres, StepStatus::Done, None);

        // 3. Meilisearch
        self.emit(Step::Meili, StepStatus::Pending, None);
        self.start_meili()?;
        self.emit(Step::Meili, StepStatus::Done, None);

        // 4. 迁移
        self.emit(Step::Migrate, StepStatus::Pending, None);
        self.run_migrations()?;
        self.emit(Step::Migrate, StepStatus::Done, None);

        // 5. 应用进程
        self.emit(Step::Services, StepStatus::Pending, None);
        self.start_services()?;
        self.emit(Step::Services, StepStatus::Done, None);
        Ok(())
    }

    fn log_file(&self, name: &str) -> PathBuf {
        self.data.logs.join(format!("{name}.log"))
    }

    fn start_postgres(&mut self) -> Result<(), StartupError> {
        let bin = &self.paths.postgres_bin;
        let data = &self.data.postgres;
        if !postgres_initialized(data) {
            // 首次初始化:initdb + 启动 + 建库
            let pw = std::env::var("HUB_POSTGRES_PASSWORD")
                .unwrap_or_else(|_| "research_hub_local".to_string());
            let pwfile = self.data.postgres.join(".pwfile");
            std::fs::write(&pwfile, &pw).map_err(|e| {
                self.fail(Step::Postgres, format!("无法写密码文件: {e}"))
            })?;
            let status = StdCommand::new(bin.join("initdb.exe"))
                .args([
                    "-D", data.to_str().unwrap_or_default(),
                    "-U", "research_hub",
                    "--pwfile", pwfile.to_str().unwrap_or_default(),
                    "-E", "UTF8",
                    "--locale=C",
                ])
                .status();
            // 无论 initdb 执行成功、失败还是无法执行都清理密码文件:
            // spawn 失败路径同样不能残留明文密码,忽略删除错误
            let _ = std::fs::remove_file(&pwfile);
            let status = status
                .map_err(|e| self.fail(Step::Postgres, format!("initdb 无法执行: {e}")))?;
            if !status.success() {
                return Err(self.fail(Step::Postgres, "initdb 失败,详见日志目录 postgres.log"));
            }
        }
        // 启动(已有实例则 pg_ctl 报错,按端口探测判断)
        if !(self.probe)(self.ports.postgres) {
            let log = self.log_file("postgres");
            let out = StdCommand::new(bin.join("pg_ctl.exe"))
                .args([
                    "-D", data.to_str().unwrap_or_default(),
                    "-l", log.to_str().unwrap_or_default(),
                    "-o", &format!("-p {} -c listen_addresses=127.0.0.1", self.ports.postgres),
                    "start",
                ])
                .output()
                .map_err(|e| self.fail(Step::Postgres, format!("pg_ctl 无法执行: {e}")))?;
            if !out.status.success() {
                let stderr = String::from_utf8_lossy(&out.stderr);
                return Err(self.fail(Step::Postgres, format!("pg_ctl start 失败: {}", stderr.trim())));
            }
        }
        if !wait_for_port(self.ports.postgres, std::time::Duration::from_secs(30)) {
            return Err(self.fail(Step::Postgres, "PostgreSQL 未在 30 秒内就绪"));
        }
        Ok(())
    }

    fn start_meili(&mut self) -> Result<(), StartupError> {
        if (self.probe)(self.ports.meili) {
            return Ok(());
        }
        let key = std::env::var("HUB_MEILISEARCH_API_KEY")
            .unwrap_or_else(|_| "research-hub-local-meili-key".to_string());
        let handle = spawn(
            "meilisearch",
            &self.paths.meilisearch,
            &[
                "--db-path", self.data.meili.to_str().unwrap_or_default(),
                "--http-addr", &format!("127.0.0.1:{}", self.ports.meili),
                "--master-key", &key,
                "--env", "production",
                "--no-analytics",
            ],
            &self.data.logs,
            &self.log_file("meilisearch"),
        )
        .map_err(|e| self.fail(Step::Meili, format!("无法启动 Meilisearch: {e}")))?;
        self.children.push(handle);
        if !wait_for_port(self.ports.meili, std::time::Duration::from_secs(60)) {
            return Err(self.fail(Step::Meili, "Meilisearch 未在 60 秒内就绪"));
        }
        Ok(())
    }

    fn run_migrations(&self) -> Result<(), StartupError> {
        let status = StdCommand::new(&self.paths.python)
            .args(["-m", "alembic", "-c", "migrations/alembic.ini", "upgrade", "head"])
            .current_dir(&self.paths.python_workdir)
            .status()
            .map_err(|e| self.fail(Step::Migrate, format!("alembic 无法执行: {e}")))?;
        if !status.success() {
            return Err(self.fail(Step::Migrate, "数据库迁移失败,详见日志"));
        }
        Ok(())
    }

    fn start_services(&mut self) -> Result<(), StartupError> {
        let workdir = self.paths.python_workdir.clone();
        let api = spawn(
            "api",
            &self.paths.python,
            &["-m", "apps.api.main"],
            &workdir,
            &self.log_file("api"),
        )
        .map_err(|e| self.fail(Step::Services, format!("API 无法启动: {e}")))?;
        self.children.push(api);

        let worker = spawn(
            "worker",
            &self.paths.python,
            &["-m", "apps.worker.main"],
            &workdir,
            &self.log_file("worker"),
        )
        .map_err(|e| self.fail(Step::Services, format!("worker 无法启动: {e}")))?;
        self.children.push(worker);

        let gateway = spawn(
            "asset-gateway",
            &self.paths.python,
            &["-m", "apps.asset_gateway.main"],
            &workdir,
            &self.log_file("asset-gateway"),
        )
        .map_err(|e| self.fail(Step::Services, format!("网关无法启动: {e}")))?;
        self.children.push(gateway);

        let gitnexus = spawn(
            "gitnexus-serve",
            std::path::Path::new("powershell"),
            &[
                "-NoProfile",
                "-File",
                self.paths.gitnexus_script.to_str().unwrap_or_default(),
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                &self.ports.gitnexus.to_string(),
            ],
            &workdir,
            &self.log_file("gitnexus"),
        )
        .map_err(|e| self.fail(Step::Services, format!("gitnexus 无法启动: {e}")))?;
        self.children.push(gitnexus);

        if !wait_for_port(self.ports.api, std::time::Duration::from_secs(120)) {
            return Err(self.fail(Step::Services, "API 未在 120 秒内就绪"));
        }
        if !wait_for_port(self.ports.gateway, std::time::Duration::from_secs(60)) {
            return Err(self.fail(Step::Services, "资产网关未在 60 秒内就绪"));
        }
        Ok(())
    }

    /// 逆序停止:应用进程 → meili → postgres。幂等,可重复调用。
    pub fn shutdown(&mut self) {
        if self.shutting_down.swap(true, Ordering::SeqCst) {
            return;
        }
        for handle in self.children.iter().rev() {
            let _ = kill_tree(handle);
        }
        self.children.clear();
        // meili 由 kill_tree 已停;postgres 优雅停止
        if (self.probe)(self.ports.postgres) {
            let bin = &self.paths.postgres_bin;
            let _ = StdCommand::new(bin.join("pg_ctl.exe"))
                .args([
                    "-D", self.data.postgres.to_str().unwrap_or_default(),
                    "-m", "fast",
                    "stop",
                ])
                .status();
        }
    }
}

/// 仅测试可见:注入端口探测闭包与数据目录(tempdir),不触碰真实 Documents 数据区。
#[cfg(test)]
impl Orchestrator {
    fn with_probe(
        paths: RuntimePaths,
        data: DataPaths,
        ports: Ports,
        probe: impl Fn(u16) -> bool + Send + Sync + 'static,
        on_event: impl Fn(&StartupEvent) + Send + Sync + 'static,
    ) -> Self {
        Orchestrator {
            paths,
            data,
            ports,
            probe: Box::new(probe),
            on_event: Box::new(on_event),
            children: Vec::new(),
            shutting_down: Arc::new(AtomicBool::new(false)),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, Mutex};

    const ENV_KEYS: [&str; 5] = [
        "HUB_API_PORT",
        "HUB_ASSET_GATEWAY_PORT",
        "HUB_GITNEXUS_PORT",
        "HUB_POSTGRES_PORT",
        "HUB_MEILISEARCH_PORT",
    ];

    fn test_ports() -> Ports {
        Ports {
            api: 8767,
            gateway: 8766,
            gitnexus: 4747,
            postgres: 5432,
            meili: 7700,
        }
    }

    /// tempdir 下的假运行时路径与数据目录:任何真实命令都不会执行。
    /// 失败要么发生在 Preflight 探测阶段,要么发生在执行不存在的假 exe(报 NotFound)。
    fn fake_world() -> (std::path::PathBuf, RuntimePaths, DataPaths) {
        let root = std::env::temp_dir().join(format!("hub-orch-test-{}", std::process::id()));
        let paths = RuntimePaths {
            python: root.join("runtime/python/python.exe"),
            python_workdir: root.join("app"),
            node: root.join("runtime/node/node.exe"),
            gitnexus_script: root.join("runtime/gitnexus/gitnexus.ps1"),
            postgres_bin: root.join("runtime/postgres/bin"),
            meilisearch: root.join("runtime/meilisearch.exe"),
        };
        let data = DataPaths {
            postgres: root.join("data/postgres-data"),
            meili: root.join("data/meili-data"),
            logs: root.join("data/logs"),
            backups: root.join("data/backups"),
            updates: root.join("data/updates"),
        };
        (root, paths, data)
    }

    fn record() -> (
        Arc<Mutex<Vec<(Step, StepStatus)>>>,
        impl Fn(&StartupEvent) + Send + Sync,
    ) {
        let events: Arc<Mutex<Vec<(Step, StepStatus)>>> = Arc::new(Mutex::new(vec![]));
        let sink = {
            let events = events.clone();
            move |ev: &StartupEvent| events.lock().unwrap().push((ev.step, ev.status))
        };
        (events, sink)
    }

    #[test]
    fn ports_from_env_defaults() {
        // 清掉相关 env 后取默认值
        for key in ENV_KEYS {
            std::env::remove_var(key);
        }
        let ports = Ports::from_env();
        assert_eq!(ports.api, 8767);
        assert_eq!(ports.gateway, 8766);
        assert_eq!(ports.gitnexus, 4747);
        assert_eq!(ports.postgres, 5432);
        assert_eq!(ports.meili, 7700);
    }

    #[test]
    fn events_fire_in_order_and_failure_stops() {
        let (root, paths, data) = fake_world();
        let (events, sink) = record();
        // 注入恒 true 探测:Preflight 视关键端口为"被占用",启动在探测阶段即失败,
        // 假 exe 路径不会被真正执行。
        let mut orch = Orchestrator::with_probe(paths, data, test_ports(), |_| true, sink);
        let result = orch.start();
        match result {
            Err(StartupError::StepFailed { step, detail }) => {
                assert_eq!(step, Step::Preflight);
                assert!(detail.contains("端口被占用"), "detail 应说明占用端口: {detail}");
            }
            other => panic!("期望 Preflight 失败,得到: {other:?}"),
        }
        let seq = events.lock().unwrap();
        assert_eq!(
            *seq,
            vec![
                (Step::Preflight, StepStatus::Pending),
                (Step::Preflight, StepStatus::Failed),
            ],
            "事件序列应以 (Preflight, Failed) 结束"
        );
        // 失败即停:不存在 (Postgres, Pending) 及其之后的任何事件。
        assert!(
            !seq.iter().any(|(s, _)| *s == Step::Postgres),
            "Preflight 失败后不得进入 Postgres"
        );
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn postgres_failure_stops_without_later_events() {
        let (root, paths, data) = fake_world();
        // data 将被 with_probe 按值消费,提前克隆数据目录路径用于断言密码文件清理
        let data_dir = data.postgres.clone();
        let (events, sink) = record();
        // 恒 false 探测:Preflight 通过;Postgres 阶段假 initdb.exe 不存在,
        // 执行报 NotFound → fail 发出 (Postgres, Failed) 事件后 start() 返回 Err,
        // 序列以 (Postgres, Pending) 后跟 (Postgres, Failed) 结束,无 Meili 及之后事件。
        let mut orch = Orchestrator::with_probe(paths, data, test_ports(), |_| false, sink);
        let result = orch.start();
        match result {
            Err(StartupError::StepFailed { step, detail }) => {
                assert_eq!(step, Step::Postgres);
                assert!(detail.contains("initdb"), "detail 应说明 initdb 失败: {detail}");
            }
            other => panic!("期望 Postgres 失败,得到: {other:?}"),
        }
        let seq = events.lock().unwrap();
        assert_eq!(
            *seq,
            vec![
                (Step::Preflight, StepStatus::Pending),
                (Step::Preflight, StepStatus::Done),
                (Step::Postgres, StepStatus::Pending),
                (Step::Postgres, StepStatus::Failed),
            ],
            "序列应以 (Postgres, Pending) 后跟 (Postgres, Failed) 结束"
        );
        // 失败即停:不得出现 Meili 及之后的任何事件。
        assert!(
            !seq.iter()
                .any(|(s, _)| matches!(s, Step::Meili | Step::Migrate | Step::Services)),
            "Postgres 失败后不得进入 Meili 及之后的步骤"
        );
        // initdb spawn 失败路径同样必须清理密码文件,不得残留明文密码
        assert!(
            !data_dir.join(".pwfile").exists(),
            "initdb 执行失败后不得残留密码文件"
        );
        let _ = std::fs::remove_dir_all(&root);
    }
}
