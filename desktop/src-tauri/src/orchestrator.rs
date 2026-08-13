//! 启动编排状态机:预检 → PostgreSQL → Meilisearch → 迁移 → 应用进程 → 就绪。
//! 不依赖 Tauri;事件经 on_event 回调上报(Task 9 桥接到加载页)。

use std::path::PathBuf;
use std::process::Command as StdCommand;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use crate::data_dir::{data_paths, ensure_dirs, postgres_initialized, DataPaths};
use crate::processes::{kill_tree, spawn, ChildHandle};
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

/// 取消路径的 detail 值:壳层以此区分"用户主动取消"与真实失败,不弹错误框。
pub const CANCEL_DETAIL: &str = "已取消";

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
/// 端口持有者 (PID, 命令行) 查询:生产实现经 PowerShell 查询,测试注入假数据。
type PortOwnerQuery = Box<dyn Fn(u16) -> Option<(u32, String)> + Send + Sync>;

/// 自有应用进程的命令行特征:壳异常退出残留的 api/worker/gateway 经
/// `python -m apps.{api,worker,asset_gateway}.main` 启动,gitnexus 经
/// `powershell -File ...gitnexus.ps1 serve` 启动。仅当进程同时实际持有冲突
/// 端口(经 port_owner 按端口归属确认)时才视为本应用残留进程并清理,
/// 同栈但非壳启动、未占用冲突端口的无关进程不会被误杀。
const OWNED_PROCESS_PATTERNS: [&str; 4] = [
    "apps.api.main",
    "apps.worker.main",
    "apps.asset_gateway.main",
    "gitnexus",
];

/// 命令行摘要:去首尾空白,超过 max 字符截断并以 "..." 结尾(按字符边界截取,
/// 避免切开多字节字符)。报错提示用,帮助用户判断占用进程来源;空命令行返回
/// 空串,调用方省略摘要部分。
fn cmdline_summary(cmd: &str, max: usize) -> String {
    let trimmed = cmd.trim();
    if trimmed.chars().count() <= max {
        return trimmed.to_string();
    }
    let cut: String = trimmed.chars().take(max - 3).collect();
    format!("{cut}...")
}

/// 端口占用者报错描述:含 PID 与命令行摘要,用户据此判断进程来源。
fn occupied_summary(port: u16, pid: u32, cmd: &str) -> String {
    let summary = cmdline_summary(cmd, 120);
    if summary.is_empty() {
        format!("端口 {port} 被进程 PID {pid} 占用")
    } else {
        format!("端口 {port} 被进程 PID {pid}({summary})占用")
    }
}

pub struct Orchestrator {
    paths: RuntimePaths,
    data: DataPaths,
    ports: Ports,
    probe: Probe,
    port_owner: PortOwnerQuery,
    on_event: Box<dyn Fn(&StartupEvent) + Send + Sync>,
    children: Vec<ChildHandle>,
    shutting_down: Arc<AtomicBool>,
    /// 取消标志:退出/重试置位后,start 的步骤检查点与端口等待循环尽快自行清理退出。
    cancel: Arc<AtomicBool>,
    /// 开发回退:postgres/meili 走 docker compose 容器而非便携二进制。
    uses_containers: bool,
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
            port_owner: Box::new(crate::processes::port_owner),
            on_event: Box::new(on_event),
            children: Vec::new(),
            shutting_down: Arc::new(AtomicBool::new(false)),
            cancel: Arc::new(AtomicBool::new(false)),
            uses_containers: false,
        }
    }

    /// 切到开发回退模式:postgres/meili 用 docker compose 容器,shutdown 时 compose stop。
    pub fn with_containers(mut self) -> Self {
        self.uses_containers = true;
        self
    }

    /// 克隆取消令牌:壳层在退出/重试时无需获取编排锁即可置位,
    /// 打断进行中的 start(其锁内等待循环会自行返回),避免与 start 争锁被阻塞。
    pub fn cancel_token(&self) -> Arc<AtomicBool> {
        self.cancel.clone()
    }

    fn emit(&self, step: Step, status: StepStatus, detail: Option<String>) {
        (self.on_event)(&StartupEvent {
            step,
            status,
            detail,
        });
    }

    fn fail(&self, step: Step, detail: impl Into<String>) -> StartupError {
        let detail = detail.into();
        // 失败事件统一在此发出:所有 `?`/`return Err(self.fail(...))` 路径都自动获得 (step, Failed)
        self.emit(step, StepStatus::Failed, Some(detail.clone()));
        StartupError::StepFailed { step, detail }
    }

    /// 步骤间取消检查:已取消则清理子进程并返回"已取消"错误。
    /// 与 fail 不同,不发 Failed 事件——用户主动取消不是启动失败。
    fn check_cancelled(&mut self, step: Step) -> Result<(), StartupError> {
        if self.cancel.load(Ordering::SeqCst) {
            self.cleanup_children();
            return Err(StartupError::StepFailed {
                step,
                detail: CANCEL_DETAIL.into(),
            });
        }
        Ok(())
    }

    /// 与 processes 的 wait_for_port 同语义的轮询等待,每轮检查取消标志;
    /// 被取消返回 false,调用处随后经 check_cancelled 返回"已取消"错误。
    /// 探测沿用 self.probe(生产即 tcp_probe,测试可注入确定性探测)。
    fn wait_for_port_cancellable(&self, port: u16, timeout: Duration) -> bool {
        let deadline = std::time::Instant::now() + timeout;
        while std::time::Instant::now() < deadline {
            if self.cancel.load(Ordering::SeqCst) {
                return false;
            }
            if (self.probe)(port) {
                return true;
            }
            std::thread::sleep(Duration::from_millis(500));
        }
        false
    }

    /// 停止并清空全部已 spawn 子进程(逆序)。start 取消路径与 shutdown 共用。
    fn cleanup_children(&mut self) {
        for handle in self.children.iter().rev() {
            let _ = kill_tree(handle);
        }
        self.children.clear();
    }

    /// Preflight 端口冲突恢复:按端口归属识别占用进程——对每个 busy 端口查询
    /// 实际持有者(port_owner: PID + 命令行),仅当命令行匹配自有特征
    /// (OWNED_PROCESS_PATTERNS)才 taskkill 清理;非自有进程一律不动,
    /// 记录「端口 X 被进程 PID Y(命令行摘要)占用」供报错提示。端口查询不到
    /// 归属同样不杀(无法确认是本应用残留),宁可不杀不误杀。
    /// kill 失败(进程已退出/权限不足)不终止启动流程,由重探结果判定端口是否释放。
    /// 返回仍占用端口的描述列表(含 PID 与命令行摘要),供 start 报错。
    fn recover_stale_owned_processes(&self, busy: &mut Vec<u16>) -> Vec<String> {
        let mut occupants: Vec<String> = Vec::new();
        let mut killed_any = false;
        for port in busy.iter().copied() {
            match (self.port_owner)(port) {
                Some((pid, cmd)) if OWNED_PROCESS_PATTERNS.iter().any(|p| cmd.contains(p)) => {
                    let _ = crate::processes::kill_pid(pid);
                    killed_any = true;
                }
                Some((pid, cmd)) => occupants.push(occupied_summary(port, pid, &cmd)),
                None => occupants.push(format!("端口 {port} 被占用(无法查询占用进程)")),
            }
        }
        if killed_any {
            // 给 OS 释放端口留时间,再重探一次
            std::thread::sleep(Duration::from_millis(800));
            busy.retain(|p| (self.probe)(*p));
            if !busy.is_empty() {
                // 清理后仍占用:持有者可能已更换,重新查询以给出准确提示
                occupants.clear();
                for port in busy.iter().copied() {
                    match (self.port_owner)(port) {
                        Some((pid, cmd)) => occupants.push(occupied_summary(port, pid, &cmd)),
                        None => {
                            occupants.push(format!("端口 {port} 被占用(无法查询占用进程)"))
                        }
                    }
                }
            }
        }
        occupants
    }

    /// 运行可能耗时较长的同步命令(compose up --wait、alembic 迁移可达分钟级):
    /// spawn 后轮询 try_wait(间隔 500ms),期间取消标志置位则 taskkill 命令子进程
    /// 并返回"已取消"错误,退出/重试不再挂起数分钟。
    /// 其余同步命令(initdb、pg_ctl stop)耗时秒级,保持 .status() 同步等待:
    /// 取消收益小,且 initdb 中途强杀会留下半初始化数据目录,同步等待更安全。
    fn run_command_cancellable(
        &mut self,
        step: Step,
        program: &std::path::Path,
        args: &[&str],
        cwd: Option<&std::path::Path>,
        fail_msg: &str,
    ) -> Result<(), StartupError> {
        let mut cmd = StdCommand::new(program);
        cmd.args(args);
        if let Some(dir) = cwd {
            cmd.current_dir(dir);
        }
        let mut child = cmd
            .spawn()
            .map_err(|e| self.fail(step, format!("{} 无法执行: {e}", program.display())))?;
        loop {
            if self.cancel.load(Ordering::SeqCst) {
                // 取消:杀整棵进程树并回收,再复用取消语义(清理已 spawn 子进程、
                // 返回"已取消"、不发 Failed 事件)。注:docker compose 被杀的是
                // 客户端,容器若仍在后台启动,由下次启动的 --wait/端口探测收敛。
                let _ = crate::processes::kill_pid(child.id());
                let _ = child.wait();
                return self.check_cancelled(step);
            }
            match child.try_wait() {
                Ok(Some(status)) => {
                    if status.success() {
                        return Ok(());
                    }
                    return Err(self.fail(step, fail_msg));
                }
                Ok(None) => {}
                Err(e) => {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Err(self.fail(
                        step,
                        format!("{} 状态检查失败: {e}", program.display()),
                    ));
                }
            }
            std::thread::sleep(Duration::from_millis(500));
        }
    }

    pub fn start(&mut self) -> Result<(), StartupError> {
        ensure_dirs(&self.data)
            .map_err(|e| self.fail(Step::Preflight, format!("无法创建数据目录: {e}")))?;

        // 1. 预检:关键端口冲突检测(应用端口)
        self.check_cancelled(Step::Preflight)?;
        self.emit(Step::Preflight, StepStatus::Pending, None);
        let mut busy: Vec<u16> = [self.ports.api, self.ports.gateway, self.ports.gitnexus]
            .into_iter()
            .filter(|p| (self.probe)(*p))
            .collect();
        if !busy.is_empty() {
            // 壳进程异常退出(如断电/强杀)会残留自有应用进程占端口:
            // 按端口归属识别占用进程(实际持有该端口的 PID 且命令行匹配
            // 自有特征)才 taskkill 清理并重探,避免误杀同栈但非壳启动的
            // 无关进程;非自有占用者的 PID 与命令行摘要随报错提示用户。
            let occupants = self.recover_stale_owned_processes(&mut busy);
            if !busy.is_empty() {
                // fail 内部会先发出 (Preflight, Failed) 事件,此处不再手动 emit
                let detail = if occupants.is_empty() {
                    format!("端口被占用: {busy:?}")
                } else {
                    format!("端口被占用: {}", occupants.join("; "))
                };
                return Err(self.fail(
                    Step::Preflight,
                    format!("{detail}。可能是上次异常退出残留,可重启电脑或手动结束该进程"),
                ));
            }
        }
        self.emit(Step::Preflight, StepStatus::Done, None);

        // 2. PostgreSQL
        self.check_cancelled(Step::Postgres)?;
        self.emit(Step::Postgres, StepStatus::Pending, None);
        self.start_postgres()?;
        self.emit(Step::Postgres, StepStatus::Done, None);

        // 3. Meilisearch
        self.check_cancelled(Step::Meili)?;
        self.emit(Step::Meili, StepStatus::Pending, None);
        self.start_meili()?;
        self.emit(Step::Meili, StepStatus::Done, None);

        // 4. 迁移
        self.check_cancelled(Step::Migrate)?;
        self.emit(Step::Migrate, StepStatus::Pending, None);
        self.run_migrations()?;
        self.emit(Step::Migrate, StepStatus::Done, None);

        // 5. 应用进程
        self.check_cancelled(Step::Services)?;
        self.emit(Step::Services, StepStatus::Pending, None);
        self.start_services()?;
        self.emit(Step::Services, StepStatus::Done, None);
        Ok(())
    }

    fn log_file(&self, name: &str) -> PathBuf {
        self.data.logs.join(format!("{name}.log"))
    }

    fn start_postgres(&mut self) -> Result<(), StartupError> {
        if self.uses_containers {
            // 开发回退:compose 一并拉起 postgres 与 meilisearch,--wait 等待健康检查通过。
            // workdir = 项目根(dev 下即 python_workdir),deploy/compose.yaml 相对其解析。
            // --wait 可达分钟级,走可取消轮询(取消时杀掉 compose 客户端,容器若仍在
            // 后台继续启动,由下次启动的 --wait/端口探测收敛)。
            let workdir = self.paths.python_workdir.clone();
            self.run_command_cancellable(
                Step::Postgres,
                std::path::Path::new("docker"),
                &["compose", "-f", "deploy/compose.yaml", "up", "-d", "--wait"],
                Some(&workdir),
                "docker compose up 失败,请确认 Docker Desktop 已启动",
            )?;
            if !self.wait_for_port_cancellable(self.ports.postgres, Duration::from_secs(60)) {
                self.check_cancelled(Step::Postgres)?;
                return Err(self.fail(Step::Postgres, "PostgreSQL 容器未在 60 秒内就绪"));
            }
            return Ok(());
        }
        let bin = &self.paths.postgres_bin;
        let data = &self.data.postgres;
        if !postgres_initialized(data) {
            // 首次初始化:initdb + 启动 + 建库。
            // initdb 通常数秒完成,保持 .status() 同步等待(见 run_command_cancellable 注释:
            // 中途强杀会留下半初始化数据目录,取消收益小而代价大)。
            let pw = std::env::var("HUB_POSTGRES_PASSWORD")
                .unwrap_or_else(|_| "research_hub_local".to_string());
            let pwfile = self.data.postgres.join(".pwfile");
            std::fs::write(&pwfile, &pw)
                .map_err(|e| self.fail(Step::Postgres, format!("无法写密码文件: {e}")))?;
            let status = StdCommand::new(bin.join("initdb.exe"))
                .args([
                    "-D",
                    data.to_str().unwrap_or_default(),
                    "-U",
                    "research_hub",
                    "--pwfile",
                    pwfile.to_str().unwrap_or_default(),
                    "-E",
                    "UTF8",
                    "--locale=C",
                ])
                .status();
            // 无论 initdb 执行成功、失败还是无法执行都清理密码文件:
            // spawn 失败路径同样不能残留明文密码,忽略删除错误
            let _ = std::fs::remove_file(&pwfile);
            let status =
                status.map_err(|e| self.fail(Step::Postgres, format!("initdb 无法执行: {e}")))?;
            if !status.success() {
                return Err(self.fail(Step::Postgres, "initdb 失败,详见日志目录 postgres.log"));
            }
        }
        // 启动(已有实例则 pg_ctl 报错,按端口探测判断)
        if !(self.probe)(self.ports.postgres) {
            let log = self.log_file("postgres");
            let out = StdCommand::new(bin.join("pg_ctl.exe"))
                .args([
                    "-D",
                    data.to_str().unwrap_or_default(),
                    "-l",
                    log.to_str().unwrap_or_default(),
                    "-o",
                    &format!("-p {} -c listen_addresses=127.0.0.1", self.ports.postgres),
                    "start",
                ])
                .output()
                .map_err(|e| self.fail(Step::Postgres, format!("pg_ctl 无法执行: {e}")))?;
            if !out.status.success() {
                let stderr = String::from_utf8_lossy(&out.stderr);
                return Err(self.fail(
                    Step::Postgres,
                    format!("pg_ctl start 失败: {}", stderr.trim()),
                ));
            }
        }
        if !self.wait_for_port_cancellable(self.ports.postgres, Duration::from_secs(30)) {
            self.check_cancelled(Step::Postgres)?;
            return Err(self.fail(Step::Postgres, "PostgreSQL 未在 30 秒内就绪"));
        }
        Ok(())
    }

    fn start_meili(&mut self) -> Result<(), StartupError> {
        if self.uses_containers {
            // meili 容器已随 Postgres 步骤的 compose up 拉起,此处仅等端口就绪。
            if !self.wait_for_port_cancellable(self.ports.meili, Duration::from_secs(60)) {
                self.check_cancelled(Step::Meili)?;
                return Err(self.fail(Step::Meili, "Meilisearch 容器未在 60 秒内就绪"));
            }
            return Ok(());
        }
        if (self.probe)(self.ports.meili) {
            return Ok(());
        }
        let key = std::env::var("HUB_MEILISEARCH_API_KEY")
            .unwrap_or_else(|_| "research-hub-local-meili-key".to_string());
        let handle = spawn(
            "meilisearch",
            &self.paths.meilisearch,
            &[
                "--db-path",
                self.data.meili.to_str().unwrap_or_default(),
                "--http-addr",
                &format!("127.0.0.1:{}", self.ports.meili),
                "--master-key",
                &key,
                "--env",
                "production",
                "--no-analytics",
            ],
            &self.data.logs,
            &self.log_file("meilisearch"),
        )
        .map_err(|e| self.fail(Step::Meili, format!("无法启动 Meilisearch: {e}")))?;
        self.children.push(handle);
        if !self.wait_for_port_cancellable(self.ports.meili, Duration::from_secs(60)) {
            self.check_cancelled(Step::Meili)?;
            return Err(self.fail(Step::Meili, "Meilisearch 未在 60 秒内就绪"));
        }
        Ok(())
    }

    fn run_migrations(&mut self) -> Result<(), StartupError> {
        // 迁移可达分钟级(首启建表+索引),走可取消轮询;取消时 taskkill 迁移子进程。
        // 路径先克隆,避免 &mut self 与 &self.paths 的借用冲突。
        let python = self.paths.python.clone();
        let workdir = self.paths.python_workdir.clone();
        self.run_command_cancellable(
            Step::Migrate,
            &python,
            &["-m", "alembic", "-c", "migrations/alembic.ini", "upgrade", "head"],
            Some(&workdir),
            "数据库迁移失败,详见日志",
        )
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

        if !self.wait_for_port_cancellable(self.ports.api, Duration::from_secs(120)) {
            self.check_cancelled(Step::Services)?;
            return Err(self.fail(Step::Services, "API 未在 120 秒内就绪"));
        }
        if !self.wait_for_port_cancellable(self.ports.gateway, Duration::from_secs(60)) {
            self.check_cancelled(Step::Services)?;
            return Err(self.fail(Step::Services, "资产网关未在 60 秒内就绪"));
        }
        Ok(())
    }

    /// 逆序停止:应用进程 → meili → postgres。幂等,可重复调用。
    /// 先置位取消标志(打断可能仍在进行的 start 端口等待),再清子进程与容器/postgres。
    pub fn shutdown(&mut self) {
        if self.shutting_down.swap(true, Ordering::SeqCst) {
            return;
        }
        self.cancel.store(true, Ordering::SeqCst);
        self.cleanup_children();
        if self.uses_containers {
            // 只 stop 不下线:保留 compose 卷,下次启动复用数据。
            let _ = StdCommand::new("docker")
                .args(["compose", "-f", "deploy/compose.yaml", "stop"])
                .current_dir(&self.paths.python_workdir)
                .status();
            return;
        }
        // meili 由 kill_tree 已停;postgres 优雅停止。
        // pg_ctl stop 秒级完成,保持 .status() 同步等待(见 run_command_cancellable 注释)。
        if (self.probe)(self.ports.postgres) {
            let bin = &self.paths.postgres_bin;
            let _ = StdCommand::new(bin.join("pg_ctl.exe"))
                .args([
                    "-D",
                    self.data.postgres.to_str().unwrap_or_default(),
                    "-m",
                    "fast",
                    "stop",
                ])
                .status();
        }
    }
}

/// 仅测试可见:注入端口探测/端口持有者查询闭包与数据目录(tempdir),不触碰真实 Documents 数据区。
#[cfg(test)]
impl Orchestrator {
    fn with_probe(
        paths: RuntimePaths,
        data: DataPaths,
        ports: Ports,
        probe: impl Fn(u16) -> bool + Send + Sync + 'static,
        port_owner: impl Fn(u16) -> Option<(u32, String)> + Send + Sync + 'static,
        on_event: impl Fn(&StartupEvent) + Send + Sync + 'static,
    ) -> Self {
        Orchestrator {
            paths,
            data,
            ports,
            probe: Box::new(probe),
            port_owner: Box::new(port_owner),
            on_event: Box::new(on_event),
            children: Vec::new(),
            shutting_down: Arc::new(AtomicBool::new(false)),
            cancel: Arc::new(AtomicBool::new(false)),
            uses_containers: false,
        }
    }

    /// 容器模式(开发回退)的测试构造:与 with_probe 相同,但 uses_containers = true。
    fn with_probe_and_containers(
        paths: RuntimePaths,
        data: DataPaths,
        ports: Ports,
        probe: impl Fn(u16) -> bool + Send + Sync + 'static,
        port_owner: impl Fn(u16) -> Option<(u32, String)> + Send + Sync + 'static,
        on_event: impl Fn(&StartupEvent) + Send + Sync + 'static,
    ) -> Self {
        Orchestrator {
            paths,
            data,
            ports,
            probe: Box::new(probe),
            port_owner: Box::new(port_owner),
            on_event: Box::new(on_event),
            children: Vec::new(),
            shutting_down: Arc::new(AtomicBool::new(false)),
            cancel: Arc::new(AtomicBool::new(false)),
            uses_containers: true,
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

    /// 全局计数器:rustc 测试默认并行执行,fake_world 的 root 须逐实例唯一,
    /// 避免并行的 remove_dir_all 删掉其他测试正在使用的目录。
    static FAKE_WORLD_SEQ: std::sync::atomic::AtomicU32 = std::sync::atomic::AtomicU32::new(0);

    /// tempdir 下的假运行时路径与数据目录:任何真实命令都不会执行。
    /// 失败要么发生在 Preflight 探测阶段,要么发生在执行不存在的假 exe(报 NotFound)。
    fn fake_world() -> (std::path::PathBuf, RuntimePaths, DataPaths) {
        let n = FAKE_WORLD_SEQ.fetch_add(1, Ordering::SeqCst);
        let root = std::env::temp_dir().join(format!("hub-orch-test-{}-{n}", std::process::id()));
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

    #[allow(clippy::type_complexity)]
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
        // 注入恒 true 探测:Preflight 视关键端口为"被占用",端口持有者查询返回
        // None(无归属 → 不清理,宁可不杀不误杀),启动在探测阶段即失败,
        // 假 exe 路径不会被真正执行。
        let mut orch =
            Orchestrator::with_probe(paths, data, test_ports(), |_| true, |_| None, sink);
        let result = orch.start();
        match result {
            Err(StartupError::StepFailed { step, detail }) => {
                assert_eq!(step, Step::Preflight);
                assert!(
                    detail.contains("端口被占用"),
                    "detail 应说明占用端口: {detail}"
                );
                assert!(
                    detail.contains("重启电脑"),
                    "非自有进程占用时应提示重启电脑/手动结束: {detail}"
                );
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
    fn preflight_cleans_stale_owned_process_and_retries() {
        let (root, paths, data) = fake_world();
        let (events, sink) = record();
        // 探测:api 端口首次占用(残留进程),清理重探后释放;其余端口恒空闲。
        // 端口持有者查询注入:api 端口归属自有进程(特征 apps.api.main)→ 应被
        // 识别并清理(真实 taskkill 对假 pid 失败被忽略,由重探结果判定)。
        // 重探通过后 Preflight Done,流程进入 Postgres,假 initdb.exe 不存在 →
        // Postgres Failed,序列结束。仅 busy 的 api 端口被查询持有者且仅一次:
        // 清理重探后端口已释放,不再二次查询,未占用端口的自有进程不会被误杀。
        let probes = Arc::new(std::sync::atomic::AtomicU32::new(0));
        let probe_counter = probes.clone();
        let owner_calls: Arc<Mutex<Vec<u16>>> = Arc::new(Mutex::new(Vec::new()));
        let owner_record = owner_calls.clone();
        let ports = test_ports();
        let mut orch = Orchestrator::with_probe(
            paths,
            data,
            ports,
            move |p| {
                if p == ports.api {
                    probe_counter.fetch_add(1, Ordering::SeqCst) == 0
                } else {
                    false
                }
            },
            move |port| {
                owner_record.lock().unwrap().push(port);
                (port == ports.api)
                    .then_some((4242, "python.exe -m apps.api.main".to_string()))
            },
            sink,
        );
        let result = orch.start();
        match result {
            Err(StartupError::StepFailed { step, .. }) => {
                assert_eq!(step, Step::Postgres, "清理重探后 Preflight 应通过并进入 Postgres");
            }
            other => panic!("期望进入 Postgres 后失败,得到: {other:?}"),
        }
        assert_eq!(
            probes.load(Ordering::SeqCst),
            2,
            "api 端口应被探测两次:初始 busy 检测 + 清理后重探"
        );
        assert_eq!(
            *owner_calls.lock().unwrap(),
            vec![ports.api],
            "仅 busy 的 api 端口被查询持有者一次(清理重探通过后不再查询)"
        );
        let seq = events.lock().unwrap();
        assert!(
            seq.contains(&(Step::Preflight, StepStatus::Done)),
            "Preflight 应在清理残留进程后通过: {seq:?}"
        );
        assert!(
            !seq.iter().any(|(s, st)| *s == Step::Preflight && *st == StepStatus::Failed),
            "清理重探后 Preflight 不应失败: {seq:?}"
        );
        assert!(
            seq.iter().any(|(s, st)| *s == Step::Postgres && *st == StepStatus::Failed),
            "序列应以 Postgres Failed 结束: {seq:?}"
        );
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn preflight_reports_foreign_process_with_hint() {
        let (root, paths, data) = fake_world();
        let (events, sink) = record();
        // 端口被占且实际持有者命令行不匹配任何自有特征 → 不清理,报错含
        // 「端口 X 被进程 PID Y(命令行摘要)占用」;超长命令行截断为 120 字符摘要。
        let ports = test_ports();
        let long_cmd = format!("foreign-app.exe {}", "z".repeat(200));
        let mut orch = Orchestrator::with_probe(
            paths,
            data,
            ports,
            |_| true,
            move |port| {
                Some(if port == ports.gitnexus {
                    (7777, long_cmd.clone())
                } else {
                    (999, "some-other-app.exe --serve".to_string())
                })
            },
            sink,
        );
        match orch.start() {
            Err(StartupError::StepFailed { step, detail }) => {
                assert_eq!(step, Step::Preflight);
                assert!(detail.contains("端口被占用"), "detail 应说明占用端口: {detail}");
                assert!(
                    detail.contains("端口 8767 被进程 PID 999(some-other-app.exe --serve)占用"),
                    "非自有占用者应含 PID 与命令行摘要: {detail}"
                );
                assert!(
                    detail.contains("端口 4747 被进程 PID 7777("),
                    "超长命令行占用者同样应含 PID: {detail}"
                );
                assert!(
                    !detail.contains(&"z".repeat(120)),
                    "命令行摘要应截断到 120 字符以内: {detail}"
                );
                assert!(
                    detail.contains("..."),
                    "截断的摘要应以 ... 结尾: {detail}"
                );
                assert!(
                    detail.contains("重启电脑"),
                    "非自有进程占用时应提示重启电脑/手动结束: {detail}"
                );
            }
            other => panic!("期望 Preflight 失败,得到: {other:?}"),
        }
        let seq = events.lock().unwrap();
        assert_eq!(
            *seq,
            vec![
                (Step::Preflight, StepStatus::Pending),
                (Step::Preflight, StepStatus::Failed),
            ]
        );
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn preflight_unresolvable_port_owner_reports_without_pid() {
        let (root, paths, data) = fake_world();
        let (events, sink) = record();
        // 端口 busy 但查询不到持有者(如 Get-NetTCPConnection 失败/端口无监听
        // 归属):无法确认归属 → 不清理(宁可不杀不误杀),报错提示端口占用且
        // 注明无法查询占用进程。
        let mut orch =
            Orchestrator::with_probe(paths, data, test_ports(), |_| true, |_| None, sink);
        match orch.start() {
            Err(StartupError::StepFailed { step, detail }) => {
                assert_eq!(step, Step::Preflight);
                assert!(detail.contains("端口被占用"), "detail 应说明占用端口: {detail}");
                assert!(
                    detail.contains("端口 8767 被占用(无法查询占用进程)"),
                    "查询不到持有者时应明确说明无法查询,而非猜测 PID: {detail}"
                );
            }
            other => panic!("期望 Preflight 失败,得到: {other:?}"),
        }
        let seq = events.lock().unwrap();
        assert_eq!(
            *seq,
            vec![
                (Step::Preflight, StepStatus::Pending),
                (Step::Preflight, StepStatus::Failed),
            ]
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
        let mut orch =
            Orchestrator::with_probe(paths, data, test_ports(), |_| false, |_| None, sink);
        let result = orch.start();
        match result {
            Err(StartupError::StepFailed { step, detail }) => {
                assert_eq!(step, Step::Postgres);
                assert!(
                    detail.contains("initdb"),
                    "detail 应说明 initdb 失败: {detail}"
                );
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

    #[test]
    fn with_containers_flips_flag() {
        // Orchestrator::new 只拼 data 路径,不触碰磁盘,可安全直用
        let (root, paths, _data) = fake_world();
        let (_, sink) = record();
        let orch = Orchestrator::new(paths, test_ports(), sink);
        assert!(!orch.uses_containers, "new 保持生产语义:不启用容器");
        assert!(
            orch.with_containers().uses_containers,
            "with_containers 打开容器回退"
        );
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn container_mode_preflight_failure_and_shutdown_are_safe() {
        let (root, paths, data) = fake_world();
        let (events, sink) = record();
        // 恒 true 探测:Preflight 视关键端口为占用,容器分支不会被执行到;
        // 序列与生产模式一致,证明 uses_containers 不影响早期失败路径。
        let mut orch = Orchestrator::with_probe_and_containers(
            paths,
            data,
            test_ports(),
            |_| true,
            |_| None,
            sink,
        );
        match orch.start() {
            Err(StartupError::StepFailed { step, .. }) => assert_eq!(step, Step::Preflight),
            other => panic!("期望 Preflight 失败,得到: {other:?}"),
        }
        let seq = events.lock().unwrap();
        assert_eq!(
            *seq,
            vec![
                (Step::Preflight, StepStatus::Pending),
                (Step::Preflight, StepStatus::Failed),
            ]
        );
        drop(seq);
        // 容器模式 shutdown 执行 docker compose stop(fake root 下命令失败被忽略),不得 panic
        orch.shutdown();
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn start_is_cancellable_while_waiting_for_port() {
        let (root, paths, data) = fake_world();
        // postgres 视为已初始化:跳过 initdb;注入探测对 postgres 端口返回 true,
        // 使 pg_ctl 与 postgres 端口等待都被跳过,start 直达 meili 端口等待循环。
        std::fs::create_dir_all(&data.postgres).unwrap();
        std::fs::write(data.postgres.join("PG_VERSION"), "16\n").unwrap();
        // meilisearch 假可执行文件:用真实存在的 cmd.exe 副本让 spawn 成功
        // (spawn 只要求进程能启动),此时 children 已含 meili 进程,等待循环开始。
        let cmd =
            std::env::var("ComSpec").unwrap_or_else(|_| "C:\\Windows\\System32\\cmd.exe".into());
        std::fs::create_dir_all(paths.meilisearch.parent().unwrap()).unwrap();
        std::fs::copy(&cmd, &paths.meilisearch).unwrap();
        let (events, sink) = record();
        let ports = test_ports();
        let orch = Arc::new(Mutex::new(Orchestrator::with_probe(
            paths,
            data,
            ports,
            move |p| p == ports.postgres,
            |_| None,
            sink,
        )));
        // 与壳层一致:start 前取出取消令牌,打断时经令牌无锁置位——
        // start 全程持有编排锁,直接拿锁置位取消会被阻塞到 start 结束。
        let cancel = orch.lock().unwrap().cancel_token();

        // start 在独立线程执行(与壳层编排线程一致);主线程模拟退出/重试信号线程。
        let start_handle = {
            let orch = orch.clone();
            std::thread::spawn(move || {
                let begin = std::time::Instant::now();
                let result = orch.lock().unwrap().start();
                (result, begin.elapsed())
            })
        };
        // 等 start 进入 meili 端口等待(Meili Pending 事件已发出),留少量余量后打断
        let deadline = std::time::Instant::now() + Duration::from_secs(10);
        while std::time::Instant::now() < deadline
            && !events
                .lock()
                .unwrap()
                .iter()
                .any(|(s, st)| *s == Step::Meili && *st == StepStatus::Pending)
        {
            std::thread::sleep(Duration::from_millis(50));
        }
        std::thread::sleep(Duration::from_millis(300));
        cancel.store(true, Ordering::SeqCst);

        let (result, elapsed) = start_handle.join().expect("start 线程应正常结束");
        assert!(
            elapsed < Duration::from_secs(3),
            "取消后 start 应在远小于端口超时前返回,实际 {elapsed:?}"
        );
        match result {
            Err(StartupError::StepFailed { step, detail }) => {
                assert_eq!(step, Step::Meili, "应在 meili 端口等待处被取消");
                assert_eq!(detail, CANCEL_DETAIL);
            }
            other => panic!("期望取消错误,得到: {other:?}"),
        }
        assert!(
            orch.lock().unwrap().children.is_empty(),
            "取消后已 spawn 的子进程应被清理"
        );
        let seq = events.lock().unwrap();
        assert!(
            seq.iter()
                .any(|(s, st)| *s == Step::Meili && *st == StepStatus::Pending),
            "start 应已进入 Meili 等待: {seq:?}"
        );
        assert!(
            !seq.iter().any(|(_, st)| *st == StepStatus::Failed),
            "取消路径不应发 Failed 事件: {seq:?}"
        );
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn cancellable_command_kills_child_and_returns_cancel() {
        // 直接驱动 run_command_cancellable:长跑命令(ping 300 秒量级)期间置位取消,
        // 应快速返回 CANCEL_DETAIL 错误而非等命令自然结束(否则退出会挂起数分钟)。
        let (root, paths, data) = fake_world();
        let (_, sink) = record();
        let orch = Arc::new(Mutex::new(Orchestrator::with_probe(
            paths,
            data,
            test_ports(),
            |_| false,
            |_| None,
            sink,
        )));
        let cancel = orch.lock().unwrap().cancel_token();
        let start_handle = {
            let orch = orch.clone();
            std::thread::spawn(move || {
                let begin = std::time::Instant::now();
                let result = orch.lock().unwrap().run_command_cancellable(
                    Step::Migrate,
                    std::path::Path::new("cmd"),
                    &["/C", "ping -n 300 127.0.0.1"],
                    None,
                    "不应走到失败分支",
                );
                (result, begin.elapsed())
            })
        };
        // 等子进程真正启动(留足余量),再经取消令牌无锁置位(与壳层退出路径一致)
        std::thread::sleep(Duration::from_millis(800));
        cancel.store(true, Ordering::SeqCst);
        let (result, elapsed) = start_handle.join().expect("命令线程应正常结束");
        assert!(
            elapsed < Duration::from_secs(3),
            "取消后长命令应在远小于命令时长前返回,实际 {elapsed:?}"
        );
        match result {
            Err(StartupError::StepFailed { step, detail }) => {
                assert_eq!(step, Step::Migrate, "取消错误应归属当前步骤");
                assert_eq!(detail, CANCEL_DETAIL);
            }
            other => panic!("期望取消错误,得到: {other:?}"),
        }
        let _ = std::fs::remove_dir_all(&root);
    }
}
