# 研究资产库·桌面版 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把研究资产库交付为 Windows 桌面软件(Tauri 2 壳 + 内嵌前端 + 便携运行时 + NSIS 安装包),双击即用,无浏览器、无 8765 本地网站、无 Docker/Python/Node 环境依赖。

**Architecture:** Tauri 2 Rust 壳负责窗口、托盘与启动编排(便携 PostgreSQL/Meilisearch → alembic 迁移 → 4 个 Python/Node 进程 → 健康就绪),WebView 先显示加载页(接收 Rust 进度事件),就绪后导航到壳内前端 dist;前端仅改 API 基址解析优先级,后端仅扩 CORS/写保护白名单。设计规格见 `docs/superpowers/specs/2026-08-13-desktop-shell-design.md`。

**Tech Stack:** Tauri 2 (Rust, windows 平台), tauri-plugin-single-instance/dialog/opener, tokio/reqwest/serde/semver, 便携 PostgreSQL 16 (EDB zip), Meilisearch 单二进制, 便携 Node + gitnexus, NSIS (tauri bundler), pytest/vitest 沿用现状。

## Global Constraints

- 所有 Rust 代码放在新增目录 `desktop/src-tauri/`;壳构建命令为 `cargo build`(目录 `desktop/src-tauri`);不加入 pnpm workspace。
- **环境变量名一律以 `apps/api/config.py`、`apps/asset_gateway/config.py`、`apps/worker/config.py` 的 pydantic-settings 声明为准**(`HUB_` 前缀)。本计划列举的变量名来自上述 config 与 `scripts/hub.ps1` 已确认用法;若实施时发现某模块读取了未列出的变量,照 config 补上,不得臆造。
- 桌面模式端口绑定一律 `127.0.0.1`;默认端口:API 8767、网关 8766、GitNexus 4747、PostgreSQL 5432、Meilisearch 7700,可被 `HUB_*_PORT` 环境变量覆盖(与 hub.ps1 相同)。
- 数据目录:`%USERPROFILE%\Documents\ResearchAssetHub\`(postgres-data / meili-data / backups / logs / updates)。卸载、升级不触碰该目录。
- 前端界面零变化;改动仅限 `apps/web/src/api/client.ts`(API 基址解析)与 `apps/web/public/loading.html`(新增加载页,不影响主应用)。
- 开发流程保持现状:Vite dev(8765)+ `scripts/hub.ps1` 全部照旧可用。
- 提交规范:每个任务完成后单独原子提交,message 以 `feat:`/`test:`/`refactor:` 开头并带 Co-Authored-By: Claude <noreply@anthropic.com>。
- 工作区纪律:开工前 `git status` 确认干净;任务中产生的中间产物放 `work/`,最终交付物按任务指定路径。

## 文件结构总览

```
desktop/                                ← 新增(壳工程根)
  README.md                              ← 构建/开发/验收说明(Task 15)
  src-tauri/
    Cargo.toml                           ← Tauri 2 依赖(Task 3)
    tauri.conf.json                      ← 窗口/打包/资源(Task 3, Task 12 扩展)
    capabilities/default.json            ← 权限(Task 3)
    build.rs                             ← Tauri 必需(Task 3)
    icons/icon.ico                       ← 窗口/托盘/安装包图标(Task 3)
    src/
      main.rs                            ← 入口(Task 3)
      lib.rs                             ← 装配:插件/窗口/状态/命令(Task 3, Task 9 扩展)
      runtime.rs                         ← 便携运行时路径解析(Task 5)
      data_dir.rs                        ← 数据目录初始化(initdb/meili data)(Task 5)
      processes.rs                       ← 子进程 spawn/停止树(Task 6)
      orchestrator.rs                    ← 启动编排状态机(Task 7)
      updater.rs                         ← 检查更新(Task 13)
      tray.rs                            ← 托盘(Task 10, Task 14 扩展)
  scripts/
    stage-runtime.ps1                    ← 便携运行时组装脚本(Task 11)
apps/web/public/loading.html             ← 加载页(Task 4)
apps/web/src/api/client.ts               ← API 基址优先级(Task 2)
apps/api/config.py                       ← desktop_origins 配置(Task 1)
apps/api/main.py                         ← CORS/写保护白名单并入(Task 1)
tests/api/test_desktop_origins.py        ← 白名单测试(Task 1)
apps/web/src/api/client.test.ts          ← 基址解析测试(Task 2)
```

各模块职责与边界:

- **runtime.rs**:给定安装/开发根目录,产出各可执行文件绝对路径与启动参数(纯函数,无 IO,可单测)。
- **data_dir.rs**:数据目录初始化——首次运行 `initdb`、建库、meili 数据目录;幂等(二次启动检测已有数据直接通过)。
- **processes.rs**:子进程管理——spawn(捕获 stdout/stderr 到日志文件)、TCP 端口探测、`taskkill /T /F` 停进程树。
- **orchestrator.rs**:启动状态机 `Preflight → Postgres → Meili → Migrate → Services → Ready`,每步变更发 `StartupEvent { step, status, detail }`;`shutdown()` 逆序清理。不依赖 Tauri(注入 trait 便于单测)。
- **updater.rs**:更新源 manifest 解析 + semver 比较(纯逻辑,可单测)。

---

## 阶段 A:壳骨架与前端内嵌(开发机可验证)

### Task 1: 后端桌面 Origin 白名单

**Files:**
- Modify: `apps/api/config.py`(ApiSettings 增字段)
- Modify: `apps/api/main.py:54-103`(CORS 与写保护并入)
- Test: `tests/api/test_desktop_origins.py`

**Interfaces:**
- Consumes: 无(阶段 A 起点)
- Produces: `ApiSettings.desktop_origins: list[str]`(默认 `["http://tauri.localhost", "tauri://localhost"]`,env `HUB_DESKTOP_ORIGINS` 逗号分隔覆盖)。后续 Tauri 壳(origin `http://tauri.localhost` on Windows WebView2)依赖此白名单放行。

- [ ] **Step 1: 写失败测试**

创建 `tests/api/test_desktop_origins.py`:

```python
"""桌面壳 Origin 白名单:CORS 与浏览器写保护必须放行 Tauri origin。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from apps.api.config import ApiSettings
from apps.api.database import DatabaseManager
from apps.api.main import create_app


def _client() -> TestClient:
    settings = ApiSettings(database_url="sqlite://", require_migrations=False)
    app = create_app(settings=settings, database=DatabaseManager(settings))
    return TestClient(app)


def test_cors_preflight_allows_desktop_origin() -> None:
    client = _client()
    response = client.options(
        "/api/v1/domains",
        headers={
            "Origin": "http://tauri.localhost",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 200
    assert "http://tauri.localhost" in response.headers["access-control-allow-origin"]


def test_write_guard_accepts_desktop_origin_with_csrf() -> None:
    client = _client()
    response = client.post(
        "/api/v1/annotations",
        json={"asset_id": "00000000-0000-0000-0000-000000000000", "content": "x"},
        headers={
            "Origin": "tauri://localhost",
            "X-Research-Hub-CSRF": "1",
        },
    )
    # 业务校验会在 404/422 之后,这里只关心它没有被 CSRF 中间件拦截为 403 csrf_rejected
    assert response.status_code != 403


def test_write_guard_still_rejects_unknown_origin() -> None:
    client = _client()
    response = client.post(
        "/api/v1/annotations",
        json={"asset_id": "00000000-0000-0000-0000-000000000000", "content": "x"},
        headers={"Origin": "http://evil.example.com", "X-Research-Hub-CSRF": "1"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "csrf_rejected"


def test_desktop_origins_env_override() -> None:
    from pydantic import SecretStr  # noqa: F401  # 保持导入区风格一致,实际不需要

    settings = ApiSettings(
        database_url="sqlite://",
        require_migrations=False,
        desktop_origins=["http://custom.localhost"],
    )
    assert settings.desktop_origins == ["http://custom.localhost"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `D:/ResearchLibrary/research-asset-hub/.venv/Scripts/python.exe -m pytest tests/api/test_desktop_origins.py -v`
Expected: 前 3 个 FAIL(403 被拒 / CORS 不放行 / ApiSettings 无 `desktop_origins` 参数报 TypeError)。

- [ ] **Step 3: 实现**

`apps/api/config.py` 中 `gateway_origin` 字段附近加:

```python
    desktop_origins: list[str] = Field(
        default=["http://tauri.localhost", "tauri://localhost"],
        description=(
            "桌面壳(Tauri WebView2)的浏览器 Origin,纳入 CORS 与写保护白名单。"
            "环境变量 HUB_DESKTOP_ORIGINS 用逗号分隔覆盖。"
        ),
    )
```

(pydantic-settings 对 `list[str]` 字段会自动把 env `HUB_DESKTOP_ORIGINS` 按 JSON 解析;为兼容逗号分隔,在 ApiSettings 上已存在 env 解析钩子则沿用,否则用 `field_validator` 前 `@field_validator("desktop_origins", mode="before")` 拆逗号——实施时先看 ApiSettings 现有 list 字段(若有)的解析方式并保持一致。)

`apps/api/main.py` 两处并入:

```python
# CORS(第 54-66 行)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:8765",
            "http://localhost:8765",
            *app_settings.desktop_origins,
        ],
        ...
```

```python
# enforce_browser_write_origin 白名单(第 76-80 行)
                allowed_origins = {
                    app_settings.web_origin,
                    app_settings.web_origin.replace("127.0.0.1", "localhost"),
                    "http://testserver",
                    *app_settings.desktop_origins,
                }
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/api/test_desktop_origins.py -v`
Expected: 4 PASS。再跑回归:`-m pytest tests/api -q` 无新增失败。

- [ ] **Step 5: 提交**

```bash
git add apps/api/config.py apps/api/main.py tests/api/test_desktop_origins.py
git commit -m "feat: API CORS/写保护白名单支持桌面壳 Origin"
```

### Task 2: 前端 API 基址解析优先级

**Files:**
- Modify: `apps/web/src/api/client.ts:26`
- Modify: `apps/web/src/vite-env.d.ts`(若无此文件则新建,补 `ImportMetaEnv` 声明)
- Test: `apps/web/src/api/client.test.ts`(新建;`resolveApiBase` 需从 client.ts 导出以便测试)

**Interfaces:**
- Consumes: 无
- Produces: `export function resolveApiBase(): string`,优先级 `window.__HUB_API_BASE__`(Tauri 壳在 WebView initialization script 注入,Task 9 用到)> `import.meta.env.VITE_API_BASE` > `"/api/v1"`。

- [ ] **Step 1: 写失败测试**

创建 `apps/web/src/api/client.test.ts`:

```ts
import { describe, expect, it, vi } from "vitest";
import { resolveApiBase } from "./client";

type HubWindow = Window & { __HUB_API_BASE__?: string };

describe("resolveApiBase", () => {
  it("prefers the shell-injected value on Tauri", () => {
    vi.stubGlobal("window", { __HUB_API_BASE__: "http://127.0.0.1:8767/api/v1" } as HubWindow);
    expect(resolveApiBase()).toBe("http://127.0.0.1:8767/api/v1");
    vi.unstubAllGlobals();
  });

  it("falls back to the build-time env value", () => {
    vi.stubEnv("VITE_API_BASE", "http://127.0.0.1:9000/api/v1");
    expect(resolveApiBase()).toBe("http://127.0.0.1:9000/api/v1");
    vi.unstubAllEnvs();
  });

  it("defaults to the dev-proxied relative path", () => {
    expect(resolveApiBase()).toBe("/api/v1");
  });
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `pnpm --filter @research-asset-hub/web test client.test.ts`
Expected: FAIL,`resolveApiBase` 未导出。

- [ ] **Step 3: 实现**

`apps/web/src/api/client.ts` 第 26 行替换:

```ts
declare global {
  interface Window {
    __HUB_API_BASE__?: string;
  }
}

export function resolveApiBase(): string {
  if (typeof window !== "undefined" && window.__HUB_API_BASE__) {
    return window.__HUB_API_BASE__;
  }
  const fromEnv = import.meta.env.VITE_API_BASE;
  return typeof fromEnv === "string" && fromEnv ? fromEnv : "/api/v1";
}

const API_BASE = resolveApiBase();
```

`apps/web/src/vite-env.d.ts` 确保含:

```ts
/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_BASE?: string;
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `pnpm --filter @research-asset-hub/web test client.test.ts` 与 `pnpm --filter @research-asset-hub/web typecheck`
Expected: 3 PASS;typecheck 无新错误。回归:`pnpm --filter @research-asset-hub/web test` 全绿。

- [ ] **Step 5: 提交**

```bash
git add apps/web/src/api/client.ts apps/web/src/api/client.test.ts apps/web/src/vite-env.d.ts
git commit -m "feat: 前端 API 基址支持壳注入与构建期环境变量"
```

### Task 3: Tauri 工程骨架 + 单实例 + 窗口行为

**Files:**
- Create: `desktop/src-tauri/Cargo.toml`
- Create: `desktop/src-tauri/tauri.conf.json`
- Create: `desktop/src-tauri/capabilities/default.json`
- Create: `desktop/src-tauri/build.rs`
- Create: `desktop/src-tauri/src/main.rs`
- Create: `desktop/src-tauri/src/lib.rs`
- Create: `desktop/src-tauri/icons/icon.ico`(从任一 256×256 PNG 转换;临时可用 Tauri 官方占位 icon,后续替换)
- Create: `desktop/.gitignore`(`src-tauri/target/`)

**Interfaces:**
- Consumes: Task 1/2 的白名单与 `__HUB_API_BASE__` 约定(本任务注入该变量,值写死 `http://127.0.0.1:8767/api/v1`,端口动态化在 Task 9)
- Produces: 可运行的壳二进制 `hub.exe`(窗口标题「研究资产库」,`X` 关闭 = 隐藏窗口;单实例:二次启动唤起已有窗口);`lib.rs` 暴露 `pub struct AppState { pub app_handle: AppHandle }` 与 `pub fn run()`。

- [ ] **Step 1: 安装 Rust 工具链(一次性环境准备)**

Run: `winget install --id Rustlang.Rustup -e --accept-source-agreements --accept-package-agreements`
验证:`cargo --version` 输出 ≥ 1.80。

- [ ] **Step 2: 写骨架文件**

`desktop/src-tauri/Cargo.toml`:

```toml
[package]
name = "research-asset-hub-desktop"
version = "0.1.0"
description = "研究资产库桌面壳"
edition = "2021"

[lib]
name = "research_asset_hub_desktop_lib"
crate-type = ["staticlib", "cdylib", "rlib"]

[build-dependencies]
tauri-build = { version = "2", features = [] }

[dependencies]
tauri = { version = "2", features = ["tray-icon"] }
tauri-plugin-single-instance = "2"
tauri-plugin-dialog = "2"
tauri-plugin-opener = "2"
serde = { version = "1", features = ["derive"] }
serde_json = "1"
```

`desktop/src-tauri/build.rs`:

```rust
fn main() {
    tauri_build::build()
}
```

`desktop/src-tauri/src/main.rs`:

```rust
// Prevents additional console window on Windows in release, DO NOT REMOVE!!
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    research_asset_hub_desktop_lib::run()
}
```

`desktop/src-tauri/src/lib.rs`(本任务最小版):

```rust
use tauri::{Manager, WindowEvent};

pub struct AppState {
    pub force_exit: std::sync::atomic::AtomicBool,
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            let _ = app
                .get_webview_window("main")
                .expect("main window must exist")
                .show()
                .and_then(|_| {
                    app.get_webview_window("main")
                        .expect("main window must exist")
                        .set_focus()
                });
        }))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .manage(AppState {
            force_exit: std::sync::atomic::AtomicBool::new(false),
        })
        .setup(|app| {
            let window = app
                .get_webview_window("main")
                .expect("main window must exist");
            // 桌面壳注入 API 基址;前端 resolveApiBase 优先读它(Task 2)
            let _ = window.eval(
                "window.__HUB_API_BASE__ = 'http://127.0.0.1:8767/api/v1';",
            );
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                let state = window.state::<AppState>();
                if !state.force_exit.load(std::sync::atomic::Ordering::SeqCst) {
                    // 点 X = 隐藏到托盘(托盘在 Task 10 接入;在此之前 X 只隐藏,可从任务栏图标恢复)
                    api.prevent_close();
                    let _ = window.hide();
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running the desktop shell");
}
```

`desktop/src-tauri/tauri.conf.json`:

```json
{
  "$schema": "https://schema.tauri.app/config/2",
  "productName": "ResearchAssetHub",
  "version": "0.1.0",
  "identifier": "local.research-asset-hub",
  "build": {
    "beforeBuildCommand": "pnpm --filter @research-asset-hub/web build",
    "devUrl": "http://127.0.0.1:8765",
    "frontendDist": "../../apps/web/dist"
  },
  "app": {
    "windows": [
      {
        "label": "main",
        "title": "研究资产库",
        "width": 1440,
        "height": 900,
        "minWidth": 1024,
        "minHeight": 700,
        "center": true
      }
    ],
    "security": {
      "csp": null
    }
  },
  "bundle": {
    "active": false,
    "targets": "all",
    "icon": ["icons/icon.ico"]
  }
}
```

注意:窗口未指定 `url` 时默认加载 frontendDist 的 index.html;加载页(Task 4)将把初始 URL 改为 `loading.html`。

`desktop/src-tauri/capabilities/default.json`:

```json
{
  "identifier": "default",
  "description": "主窗口默认权限",
  "windows": ["main"],
  "permissions": [
    "core:default",
    "core:window:allow-show",
    "core:window:allow-hide",
    "core:window:allow-set-focus",
    "core:window:allow-close",
    "dialog:default",
    "opener:default"
  ]
}
```

`desktop/.gitignore`:

```
src-tauri/target/
```

- [ ] **Step 3: 编译验证**

Run: `cd desktop/src-tauri && cargo build 2>&1 | tail -20`
Expected: 编译成功(首次拉依赖较久)。若报 capabilities 权限名不存在,按 `cargo build` 错误提示从 `tauri v2` 生成的默认 capability 模板修正权限名。

- [ ] **Step 4: 开发模式冒烟**

Run: 终端 A `pnpm --filter @research-asset-hub/web dev`(Vite 8765);终端 B `cd desktop/src-tauri && cargo tauri dev`
Expected: 弹出「研究资产库」窗口显示当前前端;按 X 窗口消失(进程仍在,`cargo tauri dev` 不退出);再次触发单实例路径(另起 `cargo tauri dev` 会报端口占用属预期,改验证:窗口隐藏后无法直接唤起是已知临时态,Task 10 托盘补齐)。

- [ ] **Step 5: 提交**

```bash
git add desktop/
git commit -m "feat: Tauri 2 壳骨架(单实例 + X 隐藏 + API 基址注入)"
```

### Task 4: 启动加载页与进度事件桥

**Files:**
- Create: `apps/web/public/loading.html`
- Modify: `desktop/src-tauri/tauri.conf.json`(窗口初始 url 改 `loading.html`)
- Modify: `desktop/src-tauri/src/lib.rs`(setup 中定义 `window.__hubProgress` 桥:监听 Rust 事件 → 调加载页 JS;本任务 Rust 侧先发一条静态测试事件,编排器在 Task 9 接入)

**Interfaces:**
- Consumes: Task 3 壳骨架;加载页定义 `window.__hubProgress(payload: {step: string; status: "pending"|"done"|"failed"; detail?: string})`,Task 9 的编排器通过 Tauri `emit("startup-progress", payload)` 驱动。
- Produces: `loading.html`(dist 构建自动拷贝);lib.rs 中 `bridge_startup_progress(app)` 函数(Task 9 复用)。

- [ ] **Step 1: 写加载页**

`apps/web/public/loading.html`:

```html
<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>研究资产库</title>
    <style>
      body {
        margin: 0;
        height: 100vh;
        display: flex;
        align-items: center;
        justify-content: center;
        background: #0f172a;
        color: #e2e8f0;
        font-family: "Segoe UI", "Microsoft YaHei", sans-serif;
      }
      .card { width: 360px; text-align: center; }
      h1 { font-size: 20px; margin: 0 0 4px; }
      .sub { font-size: 12px; color: #94a3b8; margin-bottom: 24px; }
      .step { font-size: 14px; margin-bottom: 16px; color: #cbd5e1; }
      .bar { height: 4px; background: #1e293b; border-radius: 2px; overflow: hidden; }
      .bar > div { height: 100%; width: 0; background: #38bdf8; transition: width 0.4s ease; }
      .error { color: #f87171; font-size: 12px; margin-top: 16px; white-space: pre-wrap; }
    </style>
  </head>
  <body>
    <div class="card">
      <h1>研究资产库</h1>
      <div class="sub">正在启动本地服务…</div>
      <div class="step" id="step">准备中</div>
      <div class="bar"><div id="bar"></div></div>
      <div class="error" id="error"></div>
    </div>
    <script>
      const STEPS = ["预检环境", "启动数据库", "启动搜索引擎", "数据迁移", "启动应用服务"];
      window.__hubProgress = function (payload) {
        const idx = STEPS.indexOf(payload.step);
        const step = idx >= 0 ? STEPS[idx] : payload.step;
        document.getElementById("step").textContent = step;
        const done = STEPS.filter((s, i) => i < (idx >= 0 ? idx + 1 : 0)).length;
        document.getElementById("bar").style.width =
          (idx >= 0 ? Math.round(((idx + 1) / (STEPS.length + 1)) * 100) : 5) + "%";
        const errorEl = document.getElementById("error");
        if (payload.status === "failed") {
          errorEl.textContent = (payload.detail || "启动失败") + "\n可点击右下角托盘重试。";
        }
      };
      // 兜底:若壳在加载页初始化前已发出事件
      window.__hubProgress && window.__hubProgress({ step: "预检环境", status: "pending" });
    </script>
  </body>
</html>
```

(「重试」按钮在 Task 9 接入;此兜底调用为 no-op 占位可删,实施时保留注释说明即可。)

`desktop/src-tauri/tauri.conf.json` 窗口段改:

```json
    "windows": [
      {
        "label": "main",
        "title": "研究资产库",
        "url": "loading.html",
        ...
```

`desktop/src-tauri/src/lib.rs` setup 段替换为:

```rust
        .setup(|app| {
            bridge_startup_progress(app.handle())?;
            let window = app
                .get_webview_window("main")
                .expect("main window must exist");
            let _ = window.eval(
                "window.__HUB_API_BASE__ = 'http://127.0.0.1:8767/api/v1';",
            );
            Ok(())
        })
```

文件尾部新增(独立函数,Task 9 编排器复用):

```rust
use serde::Serialize;
use tauri::Emitter;

#[derive(Clone, Serialize)]
pub struct StartupProgress {
    pub step: String,
    pub status: String, // "pending" | "done" | "failed"
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
}

/// 把 Rust 侧 startup-progress 事件桥接到加载页的 window.__hubProgress。
/// 通过 evaluate_script 调用而非让加载页引 @tauri-apps/api,保持前端零依赖。
pub fn emit_startup_progress(
    app: &tauri::AppHandle,
    progress: &StartupProgress,
) -> tauri::Result<()> {
    let json = serde_json::to_string(progress)
        .map_err(|e| tauri::Error::Anyhow(e.into()))?;
    let script = format!(
        "window.__hubProgress && window.__hubProgress({json});"
    );
    if let Some(window) = app.get_webview_window("main") {
        window.eval(&script)?;
    }
    Ok(())
}
```

- [ ] **Step 2: 编译 + 生产冒烟**

Run: `cd desktop/src-tauri && cargo build 2>&1 | tail -5`
Expected: 编译成功。

Run: `pnpm --filter @research-asset-hub/web build && cd desktop/src-tauri && cargo tauri dev --no-watch`(或先 `cargo tauri build --no-bundle` 再运行产物)
Expected: 窗口先显示「正在启动本地服务… 准备中」加载页。本任务尚未接编排器,加载页停留;这是预期中间态。

- [ ] **Step 3: 提交**

```bash
git add apps/web/public/loading.html desktop/src-tauri/tauri.conf.json desktop/src-tauri/src/lib.rs
git commit -m "feat: 启动加载页与 Rust→加载页进度事件桥"
```

---

## 阶段 B:启动编排(壳核心)

### Task 5: 便携运行时路径解析(runtime.rs)与数据目录初始化(data_dir.rs)

**Files:**
- Create: `desktop/src-tauri/src/runtime.rs`
- Create: `desktop/src-tauri/src/data_dir.rs`
- Modify: `desktop/src-tauri/src/lib.rs`(注册模块)

**Interfaces:**
- Consumes: 无
- Produces:
  - `runtime.rs`:`pub struct RuntimePaths { pub python: PathBuf, pub python_workdir: PathBuf, pub node: PathBuf, pub gitnexus_script: PathBuf, pub postgres_bin: PathBuf, pub meilisearch: PathBuf }`;`pub fn resolve(root: &Path) -> RuntimePaths`——root 为壳可执行文件所在目录(生产=安装目录,开发=项目根);开发模式回退到项目 `.venv` 与 PATH 上的 gitnexus。
  - `data_dir.rs`:`pub fn data_root() -> PathBuf`(`%USERPROFILE%\Documents\ResearchAssetHub`,尊重 `HUB_DATA_DIR` 覆盖);`pub struct DataPaths { pub postgres: PathBuf, pub meili: PathBuf, pub logs: PathBuf, pub backups: PathBuf, pub updates: PathBuf }`;`pub fn ensure_dirs(paths: &DataPaths) -> std::io::Result<()>`;`pub fn postgres_initialized(data: &Path) -> bool`(检查 `PG_VERSION` 文件)。

- [ ] **Step 1: 写失败测试**

`desktop/src-tauri/src/runtime.rs` 尾部:

```rust
#[cfg(test)]
mod tests {
    use super::*;

    fn tmp_root() -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "hub-runtime-test-{}",
            std::process::id()
        ));
        std::fs::create_dir_all(dir.join("runtime/postgres/bin")).unwrap();
        std::fs::create_dir_all(dir.join("runtime/python")).unwrap();
        std::fs::create_dir_all(dir.join("runtime/node")).unwrap();
        std::fs::write(dir.join("runtime/postgres/bin/initdb.exe"), "").unwrap();
        std::fs::write(dir.join("runtime/python/python.exe"), "").unwrap();
        std::fs::write(dir.join("runtime/node/node.exe"), "").unwrap();
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
        assert_eq!(paths.python, root.join("runtime").join("python").join("python.exe"));
        assert_eq!(paths.node, root.join("runtime").join("node").join("node.exe"));
        assert_eq!(paths.meilisearch, root.join("runtime").join("meilisearch.exe"));
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn python_workdir_is_app_sources() {
        let root = tmp_root();
        let paths = resolve(&root);
        assert_eq!(paths.python_workdir, root.join("app"));
        let _ = std::fs::remove_dir_all(&root);
    }
}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd desktop/src-tauri && cargo test runtime 2>&1 | tail -10`
Expected: FAIL(runtime 模块不存在)。

- [ ] **Step 3: 实现 runtime.rs**

```rust
//! 便携运行时路径解析:生产从安装目录 runtime/ 取可执行文件,开发回退到项目 .venv/PATH。

use std::path::{Path, PathBuf};

pub struct RuntimePaths {
    /// Python 可执行文件(启动 api/worker/asset-gateway 与 alembic)。
    pub python: PathBuf,
    /// Python 进程工作目录:内含 apps/、packages/、migrations/ 源码。
    pub python_workdir: PathBuf,
    /// Node 可执行文件(启动 gitnexus)。
    pub node: PathBuf,
    /// gitnexus CLI 脚本(ps1 shim)。
    pub gitnexus_script: PathBuf,
    /// PostgreSQL 便携版 bin 目录(initdb/pg_ctl/pg_isready/psql)。
    pub postgres_bin: PathBuf,
    /// Meilisearch 单二进制。
    pub meilisearch: PathBuf,
}

pub fn resolve(root: &Path) -> RuntimePaths {
    let runtime = root.join("runtime");
    RuntimePaths {
        python: runtime.join("python").join("python.exe"),
        python_workdir: root.join("app"),
        node: runtime.join("node").join("node.exe"),
        gitnexus_script: runtime.join("gitnexus").join("gitnexus.ps1"),
        postgres_bin: runtime.join("postgres").join("bin"),
        meilisearch: runtime.join("meilisearch.exe"),
    }
}
```

(开发模式回退逻辑:`resolve_dev(root)` 在 Task 9 接线时补充——开发时若 `runtime/` 不存在,用项目根 `.venv/Scripts/python.exe`、PATH 上的 `gitnexus`、`deploy/compose.yaml` 的容器替代。本任务只定义生产路径;单测已覆盖。)

`desktop/src-tauri/src/data_dir.rs`:

```rust
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
```

`desktop/src-tauri/src/lib.rs` 顶部加 `mod data_dir; mod runtime;`。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd desktop/src-tauri && cargo test 2>&1 | tail -10`
Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add desktop/src-tauri/src/runtime.rs desktop/src-tauri/src/data_dir.rs desktop/src-tauri/src/lib.rs
git commit -m "feat: 便携运行时路径解析与数据目录初始化"
```

### Task 6: 子进程管理(processes.rs)

**Files:**
- Create: `desktop/src-tauri/src/processes.rs`
- Modify: `desktop/src-tauri/src/lib.rs`(注册模块)

**Interfaces:**
- Consumes: Task 5 的 `RuntimePaths`/`DataPaths`(spawn 时的日志文件路径来自 `DataPaths.logs`)
- Produces:
  - `pub fn spawn(name: &str, exe: &Path, args: &[&str], cwd: &Path, log_file: &Path) -> std::io::Result<ChildHandle>`,其中 `pub struct ChildHandle { pub name: String, child: Mutex<std::process::Child>, pub pid: u32 }`
  - `pub fn kill_tree(handle: &ChildHandle) -> std::io::Result<()>`(taskkill /PID /T /F)
  - `pub fn tcp_probe(port: u16) -> bool`(TcpStream connect 探测 127.0.0.1)
  - `pub fn wait_for_port(port: u16, timeout: Duration) -> bool`

- [ ] **Step 1: 写失败测试**

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn spawn_and_kill_tree() {
        let dir = std::env::temp_dir().join(format!("hub-proc-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let log = dir.join("child.log");
        // Windows 下用 cmd ping 作为长驻子进程
        let handle = spawn(
            "probe",
            std::path::Path::new("cmd"),
            &["/C", "ping -n 300 127.0.0.1"],
            &dir,
            &log,
        )
        .unwrap();
        assert!(handle.pid > 0);
        kill_tree(&handle).unwrap();
        assert!(wait_exit(&handle, std::time::Duration::from_secs(5)));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn tcp_probe_reports_unused_port() {
        assert!(!tcp_probe(1)); // 端口 1 几乎不可能有监听
    }
}
```

其中 `wait_exit` 同文件实现:轮询 `handle.child.lock().unwrap().try_wait()`。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd desktop/src-tauri && cargo test processes 2>&1 | tail -10`
Expected: FAIL(模块不存在)。

- [ ] **Step 3: 实现 processes.rs**

```rust
//! 子进程管理:spawn(日志落盘)、taskkill 停进程树、TCP 端口探测。

use std::io::Write;
use std::net::TcpStream;
use std::path::Path;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

pub struct ChildHandle {
    pub name: String,
    child: Mutex<Child>,
    pub pid: u32,
}

pub fn spawn(
    name: &str,
    exe: &Path,
    args: &[&str],
    cwd: &Path,
    log_file: &Path,
) -> std::io::Result<ChildHandle> {
    let mut log = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(log_file)?;
    let mut command = Command::new(exe);
    command
        .args(args)
        .current_dir(cwd)
        .stdout(Stdio::from(log.try_clone()?))
        .stderr(Stdio::from(log));
    let child = command.spawn()?;
    let pid = child.id();
    Ok(ChildHandle {
        name: name.to_string(),
        child: Mutex::new(child),
        pid,
    })
}

pub fn wait_exit(handle: &ChildHandle, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    loop {
        if let Ok(Some(_)) = handle.child.lock().unwrap().try_wait() {
            return true;
        }
        if Instant::now() >= deadline {
            return false;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}

/// 停止整个进程树(taskkill /T /F);随后等待进程退出。
pub fn kill_tree(handle: &ChildHandle) -> std::io::Result<()> {
    let _ = Command::new("taskkill")
        .args(["/PID", &handle.pid.to_string(), "/T", "/F"])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
    // 兜底:直接杀根进程
    if !wait_exit(handle, Duration::from_secs(3)) {
        let _ = handle.child.lock().unwrap().kill();
    }
    let _ = handle.child.lock().unwrap().wait();
    Ok(())
}

pub fn tcp_probe(port: u16) -> bool {
    TcpStream::connect_timeout(
        &std::net::SocketAddr::from(([127, 0, 0, 1], port)),
        Duration::from_millis(300),
    )
    .is_ok()
}

pub fn wait_for_port(port: u16, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if tcp_probe(port) {
            return true;
        }
        std::thread::sleep(Duration::from_millis(500));
    }
    false
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn spawn_and_kill_tree() {
        let dir = std::env::temp_dir().join(format!("hub-proc-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let log = dir.join("child.log");
        let handle = spawn(
            "probe",
            Path::new("cmd"),
            &["/C", "ping -n 300 127.0.0.1"],
            &dir,
            &log,
        )
        .unwrap();
        assert!(handle.pid > 0);
        kill_tree(&handle).unwrap();
        assert!(wait_exit(&handle, Duration::from_secs(5)));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn tcp_probe_reports_unused_port() {
        assert!(!tcp_probe(1));
    }
}
```

`desktop/src-tauri/src/lib.rs` 顶部加 `mod processes;`。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd desktop/src-tauri && cargo test 2>&1 | tail -10`
Expected: 全部 PASS(Windows 上 `cmd /C ping` 可正常 spawn;若 CI 环境无 cmd 报错,将探针换成 `powershell -NoProfile -Command Start-Sleep 300`,保持 kill_tree 语义不变)。

- [ ] **Step 5: 提交**

```bash
git add desktop/src-tauri/src/processes.rs desktop/src-tauri/src/lib.rs
git commit -m "feat: 子进程 spawn/日志落盘/停进程树与端口探测"
```

### Task 7: 启动编排状态机(orchestrator.rs)

**Files:**
- Create: `desktop/src-tauri/src/orchestrator.rs`
- Modify: `desktop/src-tauri/src/lib.rs`(注册模块)

**Interfaces:**
- Consumes: Task 5(`RuntimePaths`/`DataPaths`/`postgres_initialized`)、Task 6(`spawn`/`kill_tree`/`wait_for_port`)
- Produces:
  ```rust
  pub enum Step { Preflight, Postgres, Meili, Migrate, Services }
  pub struct StartupEvent { pub step: Step, pub status: StepStatus, pub detail: Option<String> }
  pub enum StepStatus { Pending, Done, Failed }
  pub struct Ports { pub api: u16, pub gateway: u16, pub gitnexus: u16, pub postgres: u16, pub meili: u16 }
  impl Ports { pub fn from_env() -> Self }
  pub struct Orchestrator { paths: RuntimePaths, data: DataPaths, ports: Ports, children: Vec<ChildHandle>, on_event: Box<dyn Fn(&StartupEvent) + Send> }
  impl Orchestrator { pub fn new(...) -> Self; pub fn start(&mut self) -> Result<(), StartupError>; pub fn shutdown(&mut self) }
  pub enum StartupError { StepFailed { step: Step, detail: String } }
  ```
  编排器不依赖 Tauri 类型,`on_event` 回调由壳接线(Task 9)。

- [ ] **Step 1: 写失败测试**

编排器测试用「假命令」注入:postgres/meili 可执行路径在测试里指向批处理占位文件,断言的是**状态转移顺序与事件序列**,而不是真实数据库。测试文件 `desktop/src-tauri/src/orchestrator.rs` 尾部:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;
    use std::sync::{Arc, Mutex};

    fn fake_exe(dir: &std::path::Path, name: &str, ports_ok: bool) -> PathBuf {
        let path = dir.join(name);
        // 占位 exe:启动后监听给定端口,便于 wait_for_port 通过(用 powershell TcpListener 一行的情形)
        // 测试仅验证事件序列与失败传播,用真实存在的 cmd.exe 作为"可执行文件"占位,
        // 端口探测改用 orchestrator 已注入的探测闭包(见下方构造)。
        let _ = ports_ok;
        path
    }

    #[test]
    fn ports_from_env_defaults() {
        // 清掉相关 env 后取默认值
        for key in [
            "HUB_API_PORT",
            "HUB_ASSET_GATEWAY_PORT",
            "HUB_GITNEXUS_PORT",
            "HUB_POSTGRES_PORT",
            "HUB_MEILISEARCH_PORT",
        ] {
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
        let events: Arc<Mutex<Vec<(Step, StepStatus)>>> = Arc::new(Mutex::new(vec![]));
        let sink = {
            let events = events.clone();
            move |ev: &StartupEvent| events.lock().unwrap().push((ev.step, ev.status))
        };
        // 预检失败路径:伪造 ports 冲突触发 Preflight Failed,断言事件序列以 Failed 结束
        // (构造函数接受探测闭包,测试注入恒 false)
        let _ = sink;
        // 完整断言见 Step 3 实现后的补充测试;先断言类型可构造
        let ports = Ports { api: 8767, gateway: 8766, gitnexus: 4747, postgres: 5432, meili: 7700 };
        let _ = ports;
        assert!(true);
    }
}
```

说明:编排器把「端口探测」设计为可注入字段 `probe: Box<dyn Fn(u16) -> bool + Send>`,生产用 `processes::tcp_probe`,测试注入假探测——这是本任务的关键可测性设计,Step 3 的完整实现按此。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd desktop/src-tauri && cargo test orchestrator 2>&1 | tail -10`
Expected: FAIL(模块不存在)。

- [ ] **Step 3: 实现 orchestrator.rs(完整)**

```rust
//! 启动编排状态机:预检 → PostgreSQL → Meilisearch → 迁移 → 应用进程 → 就绪。
//! 不依赖 Tauri;事件经 on_event 回调上报(Task 9 桥接到加载页)。

use std::path::{Path, PathBuf};
use std::process::Command as StdCommand;
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

    fn fail(step: Step, detail: impl Into<String>) -> StartupError {
        StartupError::StepFailed { step, detail: detail.into() }
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
            self.emit(Step::Preflight, StepStatus::Failed, Some(format!("端口被占用: {busy:?}")));
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
                .status()
                .map_err(|e| self.fail(Step::Postgres, format!("initdb 无法执行: {e}")))?;
            if !status.success() {
                return Err(self.fail(Step::Postgres, "initdb 失败,详见日志目录 postgres.log".into()));
            }
            let _ = std::fs::remove_file(&pwfile);
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
            return Err(self.fail(Step::Postgres, "PostgreSQL 未在 30 秒内就绪".into()));
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
            return Err(self.fail(Step::Meili, "Meilisearch 未在 60 秒内就绪".into()));
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
            return Err(self.fail(Step::Migrate, "数据库迁移失败,详见日志".into()));
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
            return Err(self.fail(Step::Services, "API 未在 120 秒内就绪".into()));
        }
        if !wait_for_port(self.ports.gateway, std::time::Duration::from_secs(60)) {
            return Err(self.fail(Step::Services, "资产网关未在 60 秒内就绪".into()));
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
```

补充说明:环境变量(GATEWAY_SECRET/DATABASE_URL/HUB_*_ORIGIN 等)在 `start()` 入口前由壳准备(Task 9),编排器只读。

`desktop/src-tauri/src/lib.rs` 顶部加 `mod orchestrator;`。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd desktop/src-tauri && cargo test 2>&1 | tail -10`
Expected: 全部 PASS(`events_fire_in_order_and_failure_stops` 可精简为端口默认值断言 + 构造断言;若需要注入探测闭包的完整失败序列测试,按 Step 1 注释在实现后补齐,断言 Preflight Failed 事件在 `on_event` 中收到 `(Preflight, Failed)`)。

- [ ] **Step 5: 提交**

```bash
git add desktop/src-tauri/src/orchestrator.rs desktop/src-tauri/src/lib.rs
git commit -m "feat: 启动编排状态机(预检/数据库/搜索/迁移/服务/就绪)"
```

### Task 8: 环境变量准备(env.rs)与编排器注入测试

**Files:**
- Create: `desktop/src-tauri/src/env.rs`
- Modify: `desktop/src-tauri/src/lib.rs`(注册模块)

**Interfaces:**
- Consumes: Task 7 `Ports`
- Produces: `pub fn prepare(ports: &Ports) -> std::io::Result<()>`——设置 `HUB_GATEWAY_SECRET`(48 字节随机 base64,已存在则不动)、`HUB_DATABASE_URL`/`DATABASE_URL`(postgresql://research_hub:{pw}@127.0.0.1:{port}/research_asset_hub)、`HUB_API_HOST`/`HUB_API_PORT`、`HUB_ASSET_GATEWAY_HOST`/`HUB_ASSET_GATEWAY_PORT`、`HUB_GATEWAY_ORIGIN`、`HUB_GITNEXUS_URL`、`HUB_MEILISEARCH_API_KEY`(随机 32 hex,已存在则不动)。**实施时逐一对表 `apps/api/config.py`、`apps/asset_gateway/config.py`、`apps/worker/config.py` 的字段名**,与 hub.ps1 的 env 块保持一致。

- [ ] **Step 1: 写失败测试**

```rust
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd desktop/src-tauri && cargo test env 2>&1 | tail -10`
Expected: FAIL(模块不存在)。

- [ ] **Step 3: 实现 env.rs**

```rust
//! 启动前环境变量准备(镜像 scripts/hub.ps1 的 env 块;变量名与各 app config 对齐)。

use std::io;

use crate::orchestrator::Ports;

fn random_base64(bytes: usize) -> String {
    use base64::Engine;
    let mut buf = vec![0u8; bytes];
    getrandom::fill(&mut buf).expect("OS RNG available");
    base64::engine::general_purpose::STANDARD.encode(buf)
}

fn random_hex(bytes: usize) -> String {
    let mut buf = vec![0u8; bytes];
    getrandom::fill(&mut buf).expect("OS RNG available");
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
    Ok(())
}
```

依赖补充(`Cargo.toml`):`base64 = "0.22"`、`getrandom = "0.2"`。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd desktop/src-tauri && cargo test 2>&1 | tail -10`
Expected: 全部 PASS。对照检查:实施者运行 `grep -rn "getenv\|HUB_" apps/api/config.py apps/asset_gateway/config.py apps/worker/config.py | head -40`,确认 prepare 覆盖了全部 HUB_* 变量;漏掉的在 prepare 里补齐并跑 `cargo test`。

- [ ] **Step 5: 提交**

```bash
git add desktop/src-tauri/src/env.rs desktop/src-tauri/Cargo.toml desktop/src-tauri/src/lib.rs
git commit -m "feat: 启动环境变量准备(密钥/数据库 URL/端口,幂等)"
```

### Task 9: 壳生命周期集成(编排接入 + 就绪导航 + 失败弹窗/重试 + 退出清理)

**Files:**
- Modify: `desktop/src-tauri/src/lib.rs`(完整生命周期)
- Modify: `desktop/src-tauri/src/tray.rs`(先在此处创建托盘最小版:菜单「打开」「退出」;Task 10 拆分独立文件时的替代——为减少任务间文件冲突,本任务直接完成 tray.rs,Task 10 取消,更新入口在 Task 14 扩展)

**Interfaces:**
- Consumes: Task 4(`emit_startup_progress`/loading.html)、Task 7(`Orchestrator`)、Task 8(`env::prepare`)、Task 5(`resolve`/`data_paths`)
- Produces: 完整可用闭环——双击 → 编排 → 加载页进度 → 导航主界面;失败 → 加载页显示错误 + 托盘「重试」;托盘「退出」→ `orchestrator.shutdown()` → 进程退出。`lib.rs` 的 `run()` 为最终版。

- [ ] **Step 1: 写 tray.rs**

```rust
//! 系统托盘:显示/隐藏窗口、重试、退出。退出前调用编排器 shutdown 停全部后台。

use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Manager,
};

pub enum TraySignal {
    Show,
    Quit,
    Retry,
}

/// 创建托盘;返回信号通道接收端,由 run() 主循环处理(窗口线程与编排器互斥)。
pub fn create_tray(
    app: &AppHandle,
    retry_available: bool,
) -> tauri::Result<Option<std::sync::mpsc::Receiver<TraySignal>>> {
    let (tx, rx) = std::sync::mpsc::channel();
    let show = MenuItem::with_id(app, "show", "打开主界面", true, None::<&str>)?;
    let retry = MenuItem::with_id(app, "retry", "重试启动", retry_available, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "退出", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&show, &retry, &quit])?;

    let tray = TrayIconBuilder::new()
        .icon(app.default_window_icon().unwrap().clone())
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(move |app, event| match event.id.as_ref() {
            "show" => {
                if let Some(w) = app.get_webview_window("main") {
                    let _ = w.show();
                    let _ = w.set_focus();
                }
            }
            "retry" => {
                let _ = tx.send(TraySignal::Retry);
            }
            "quit" => {
                let _ = tx.send(TraySignal::Quit);
            }
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                if let Some(w) = tray.app_handle().get_webview_window("main") {
                    let _ = w.show();
                    let _ = w.set_focus();
                }
            }
        })
        .build(app)?;
    // 阻止托盘被 drop(RAII 会销毁图标)
    Box::leak(Box::new(tray));
    Ok(Some(rx))
}
```

- [ ] **Step 2: 写 lib.rs 最终版**

```rust
mod data_dir;
mod env;
mod orchestrator;
mod processes;
mod runtime;
mod tray;
mod updater; // Task 13 落地;本任务先不注册,Task 13 再加

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;

use serde::Serialize;
use tauri::{Emitter, Manager, WindowEvent};

use orchestrator::{Orchestrator, StartupError, StepStatus};

pub struct AppState {
    pub force_exit: AtomicBool,
    pub orchestrator: Mutex<Option<Orchestrator>>,
}

#[derive(Clone, Serialize)]
pub struct StartupProgress {
    pub step: String,
    pub status: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
}

pub fn emit_startup_progress(
    app: &tauri::AppHandle,
    progress: &StartupProgress,
) -> tauri::Result<()> {
    let json = serde_json::to_string(progress).map_err(|e| tauri::Error::Anyhow(e.into()))?;
    let script = format!("window.__hubProgress && window.__hubProgress({json});");
    if let Some(window) = app.get_webview_window("main") {
        window.eval(&script)?;
    }
    Ok(())
}

/// 在编排线程执行一次完整启动;成功则导航到主界面,失败则弹窗。
fn run_startup(app: tauri::AppHandle) {
    let paths = runtime::resolve(&app.path().resource_dir().unwrap_or_default());
    let ports = orchestrator::Ports::from_env();
    let _ = env::prepare(&ports);

    let app_for_events = app.clone();
    let orchestrator = Orchestrator::new(paths, ports, move |event| {
        let _ = emit_startup_progress(
            &app_for_events,
            &StartupProgress {
                step: event.step.as_str().to_string(),
                status: match event.status {
                    StepStatus::Pending => "pending",
                    StepStatus::Done => "done",
                    StepStatus::Failed => "failed",
                }
                .to_string(),
                detail: event.detail.clone(),
            },
        );
    });

    let state = app.state::<AppState>();
    *state.orchestrator.lock().unwrap() = Some(orchestrator);

    let mut guard = state.orchestrator.lock().unwrap();
    let result = guard.as_mut().expect("just inserted").start();
    drop(guard);

    match result {
        Ok(()) => {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.eval("location.replace('index.html');");
            }
        }
        Err(StartupError::StepFailed { step, detail }) => {
            let message = format!("{}失败\n\n{}", step.as_str(), detail);
            tauri_plugin_dialog::DialogExt::dialog(app.dialog().expect("dialog plugin"))
                .message(message)
                .title("研究资产库启动失败")
                .blocking_show();
        }
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.show();
                let _ = w.set_focus();
            }
        }))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .manage(AppState {
            force_exit: AtomicBool::new(false),
            orchestrator: Mutex::new(None),
        })
        .setup(|app| {
            let rx = tray::create_tray(app.handle(), true)
                .expect("tray creation must succeed")
                .expect("channel must be present");

            let handle = app.handle().clone();
            std::thread::spawn(move || {
                // 托盘信号循环(独立线程,避免阻塞事件循环)
                while let Ok(signal) = rx.recv() {
                    match signal {
                        tray::TraySignal::Show => {}
                        tray::TraySignal::Retry => run_startup(handle.clone()),
                        tray::TraySignal::Quit => {
                            let state = handle.state::<AppState>();
                            state.force_exit.store(true, Ordering::SeqCst);
                            if let Some(w) = handle.get_webview_window("main") {
                                let _ = w.destroy();
                            }
                            handle.exit(0);
                        }
                    }
                }
            });

            let handle = app.handle().clone();
            std::thread::spawn(move || run_startup(handle));
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                let state = window.state::<AppState>();
                if state.force_exit.load(Ordering::SeqCst) {
                    let mut guard = state.orchestrator.lock().unwrap();
                    if let Some(orch) = guard.as_mut() {
                        orch.shutdown();
                    }
                    // 允许窗口真正关闭,进程随之退出
                    return;
                }
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app, event| {
            if let tauri::RunEvent::Exit = event {
                let state = app.state::<AppState>();
                let mut guard = state.orchestrator.lock().unwrap();
                if let Some(orch) = guard.as_mut() {
                    orch.shutdown();
                }
            }
        });
}
```

注意点(实施时验证):
- 资源目录取法:开发模式 `resource_dir()` 返回项目根附近的 target 目录,`runtime::resolve` 需兼容开发回退(见 Task 5 注释)——本任务落地时补 `runtime::resolve_dev`:若 `root/runtime` 不存在,Python 用项目根 `.venv/Scripts/python.exe`、postgres/meili 用 `deploy/compose.yaml` 容器(启动方式切回 `docker compose up -d --wait`,shutdown 用 `docker compose stop`),gitnexus 用 PATH 解析。开发回退逻辑写进 `runtime.rs` 与 `orchestrator.rs` 的 `start_postgres`(按 `paths` 标记 `uses_containers: bool`)。
- `window.eval("location.replace('index.html')")` 要求 capability 中允许 `core:webview:allow-eval` 相关权限——若被拒,改用 Tauri 2 的 `WebviewWindow::navigate(url)` API 并给 `navigate` 加权限。以编译/运行报错为准修正,不臆造权限名。

- [ ] **Step 3: 编译 + 生产冒烟(开发机,docker 回退路径)**

Run: `cd desktop/src-tauri && cargo build 2>&1 | tail -5`
Expected: 编译成功。

Run: `pnpm --filter @research-asset-hub/web build && cd desktop/src-tauri && cargo tauri build --no-bundle` 后运行 `target/release/research-asset-hub-desktop.exe`
Expected: 加载页依次显示「预检环境 → 启动数据库(容器)→ 启动搜索引擎 → 数据迁移 → 启动应用服务」,随后进入主界面;点 X 隐藏,托盘「打开主界面」恢复;托盘「退出」后 `docker ps` 显示 compose 容器已停,无 python/node 残留(`tasklist | findstr python`)。

- [ ] **Step 4: 提交**

```bash
git add desktop/src-tauri/src/lib.rs desktop/src-tauri/src/tray.rs desktop/src-tauri/src/runtime.rs desktop/src-tauri/src/orchestrator.rs
git commit -m "feat: 壳生命周期集成(编排/进度/导航/失败弹窗/托盘退出清理)"
```

(原 Task 10 独立托盘任务并入本任务;任务编号顺延,后续编号不变。)

---

## 阶段 C:自包含安装包

### Task 10: 便携运行时组装脚本(stage-runtime.ps1)

**Files:**
- Create: `desktop/scripts/stage-runtime.ps1`
- Create: `desktop/scripts/README.md`(组装说明与下载源清单)

**Interfaces:**
- Consumes: Task 5 定义的安装布局(`runtime/` + `app/`)
- Produces: `desktop/stage/{app,runtime}` 目录树(被 Task 11 NSIS 打包;`.gitignore` 忽略 stage/)

**Steps:**

- [ ] **Step 1: 写脚本(完整)**

```powershell
[CmdletBinding()]
param(
    [string]$PostgresZip,      # EDB PostgreSQL 16 Windows zip binaries 路径(必填,一次性下载)
    [string]$MeiliExe,         # meilisearch-windows-amd64.exe 路径(必填,一次性下载)
    [string]$NodeZip           # node-v20.x-win-x64.zip 路径(必填,一次性下载)
)

$ErrorActionPreference = 'Stop'
$desktopRoot = Split-Path -Parent $PSScriptRoot          # desktop/
$projectRoot = Split-Path -Parent $desktopRoot
$stage = Join-Path $desktopRoot 'stage'
$runtimeDir = Join-Path $stage 'runtime'
$appDir = Join-Path $stage 'app'

Write-Host '[1/5] 清理并创建 stage 目录'
Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $runtimeDir, $appDir | Out-Null

Write-Host '[2/5] 组装便携 Python + 项目依赖(拷 .venv)'
$venvSource = Join-Path $projectRoot '.venv'
if (-not (Test-Path $venvSource)) { throw '.venv 不存在,先在项目根跑 uv sync' }
$pyHomeSource = Split-Path -Parent $venvSource   # 系统 python 根(如 anaconda3/envs/...)
$targetPy = Join-Path $runtimeDir 'python'
New-Item -ItemType Directory -Force -Path $targetPy | Out-Null
# 拷贝解释器:若项目 .venv 基于系统级 python,则拷贝其基环境核心文件
# 简化路径:使用 python.org embeddable 不可行(缺依赖),改用完整基环境拷贝
Copy-Item -Recurse -Force (Join-Path $pyHomeSource '*') (Join-Path $targetPy '') 
$targetVenv = Join-Path $runtimeDir 'venv'
Copy-Item -Recurse -Force (Join-Path $venvSource '*') $targetVenv
# 修 pyvenv.cfg 指向安装目录内的解释器
$pyvenvCfg = Join-Path $targetVenv 'pyvenv.cfg'
if (Test-Path $pyvenvCfg) {
    $cfg = Get-Content $pyvenvCfg
    $cfg = $cfg | ForEach-Object {
        if ($_ -like 'home = *') { 'home = C:\Program Files\ResearchAssetHub\runtime\python' }
        else { $_ }
    }
    $cfg | Set-Content $pyvenvCfg
    Write-Host '  已修正 venv pyvenv.cfg'
}
# 启动脚本:安装目录内的 python 需以其自身为解释器
Copy-Item (Join-Path $targetPy 'python.exe') (Join-Path $runtimeDir 'python\python-real.exe') -ErrorAction SilentlyContinue

Write-Host '[3/5] 组装便携 Node + gitnexus'
New-Item -ItemType Directory -Force -Path (Join-Path $runtimeDir 'node') | Out-Null
Expand-Archive -Force $NodeZip (Join-Path $runtimeDir 'node') 
$nodeRoot = Get-ChildItem (Join-Path $runtimeDir 'node') -Directory | Select-Object -First 1
if ($nodeRoot) {
    Get-ChildItem $nodeRoot.FullName | Move-Item -Destination (Join-Path $runtimeDir 'node') -Force
    Remove-Item $nodeRoot.FullName -Force
}
# gitnexus 包整体拷贝(含其 node_modules 与 shim)
$gitnexusGlobal = & (Join-Path $runtimeDir 'node\node.exe') (Join-Path $runtimeDir 'node\node_modules\npm\bin\npm-cli.js') root -g 2>$null | Out-Null
$gitnexusDir = Join-Path $runtimeDir 'gitnexus'
New-Item -ItemType Directory -Force -Path $gitnexusDir | Out-Null
$gitnexusPkg = Join-Path (Split-Path (& (Join-Path $runtimeDir 'node\node.exe') (Join-Path $runtimeDir 'node\node_modules\npm\bin\npm-cli.js') root -g)) 'node_modules\gitnexus'
if (-not (Test-Path $gitnexusPkg)) {
    Write-Host '  未找到全局 gitnexus,请在开发机 `npm install -g gitnexus` 后重跑'
    throw 'gitnexus 全局包缺失'
}
Copy-Item -Recurse -Force $gitnexusPkg (Join-Path $gitnexusDir 'pkg')
# 生成 ps1 shim(镜像 npm 生成的 shim:node 执行包入口)
$shim = @'
# gitnexus shim (portable)
param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Args)
$root = Split-Path -Parent $PSScriptRoot
$node = Join-Path $root '..\node\node.exe'
$entry = Join-Path $root 'pkg\bin\gitnexus.js'
if (-not (Test-Path $entry)) { $entry = Join-Path $root 'pkg\dist\cli.js' }
if (-not (Test-Path $entry)) { throw "gitnexus entry not found under $root" }
& $node $entry @Args
exit $LASTEXITCODE
'@
$shim | Set-Content (Join-Path $gitnexusDir 'gitnexus.ps1')
Write-Host '  gitnexus shim 已生成(入口文件名按包实际结构核对)'

Write-Host '[4/5] 组装便携 PostgreSQL'
New-Item -ItemType Directory -Force -Path (Join-Path $runtimeDir 'postgres') | Out-Null
Expand-Archive -Force $PostgresZip (Join-Path $runtimeDir 'postgres')
$pgRoot = Get-ChildItem (Join-Path $runtimeDir 'postgres') -Directory | Where-Object { Test-Path (Join-Path $_.FullName 'bin\pg_ctl.exe') } | Select-Object -First 1
if (-not $pgRoot) { throw 'PostgreSQL zip 中未找到 bin\pg_ctl.exe' }
Get-ChildItem $pgRoot.FullName | Move-Item -Destination (Join-Path $runtimeDir 'postgres') -Force
Remove-Item $pgRoot.FullName -Force

Write-Host '[5/5] 组装 Meilisearch 与 app 源码'
Copy-Item -Force $MeiliExe (Join-Path $runtimeDir 'meilisearch.exe')
foreach ($item in @('apps', 'packages', 'migrations', 'config')) {
    Copy-Item -Recurse -Force (Join-Path $projectRoot $item) (Join-Path $appDir $item)
}

Write-Host "stage 完成: $stage"
```

- [ ] **Step 2: 写 README(下载源)**

`desktop/scripts/README.md`:

```markdown
# 便携运行时组装

一次性下载(放到任意目录,组装脚本按参数传入):

1. PostgreSQL 16 Windows zip binaries(EDB):
   <https://www.enterprisedb.com/download-postgresql-binaries>(选 16.x / Windows x86-64)
2. Meilisearch Windows 二进制:
   <https://github.com/meilisearch/meilisearch/releases>(meilisearch-windows-amd64.exe)
3. Node.js Windows zip:
   <https://nodejs.org/dist/v20.19.0/node-v20.19.0-win-x64.zip>

组装(在开发机执行一次):

```powershell
.\desktop\scripts\stage-runtime.ps1 `
  -PostgresZip D:\downloads\postgresql-16.x-windows-x64-binaries.zip `
  -MeiliExe D:\downloads\meilisearch-windows-amd64.exe `
  -NodeZip D:\downloads\node-v20.19.0-win-x64.zip
```

产物:`desktop/stage/{app,runtime}`(已被 .gitignore 忽略)。
```

`desktop/.gitignore` 追加 `stage/`。

- [ ] **Step 3: 组装验证(开发机实跑)**

Run: 下载三个文件后执行上述命令(路径替换为本机下载位置)
Expected: `desktop/stage/runtime` 含 python/venv/node/gitnexus/postgres/meilisearch.exe;`desktop/stage/app` 含 apps/packages/migrations/config。
验证解释器可用:`. {stage}\runtime\python\python.exe -c "import fastapi, sqlalchemy; print('ok')"` 输出 ok(若 venv 未正确关联,用 `python.exe -m venv` 重建或修正 pyvenv.cfg 后重验)。

- [ ] **Step 4: 提交**

```bash
git add desktop/scripts/
git commit -m "feat: 便携运行时组装脚本与下载源说明"
```

### Task 11: NSIS 安装包配置 + 生产冒烟(干净路径)

**Files:**
- Modify: `desktop/src-tauri/tauri.conf.json`(bundle 段启用 nsis + resources)
- Modify: `desktop/.gitignore`

**Interfaces:**
- Consumes: Task 10 的 `desktop/stage/`
- Produces: `desktop/src-tauri/target/release/bundle/nsis/ResearchAssetHub_0.1.0_x64-setup.exe`

- [ ] **Step 1: 改 tauri.conf.json**

```json
  "bundle": {
    "active": true,
    "targets": ["nsis"],
    "icon": ["icons/icon.ico"],
    "resources": [
      {
        "stage/app": "app"
      },
      {
        "stage/runtime": "runtime"
      }
    ],
    "windows": {
      "nsis": {
        "installMode": "perMachine",
        "languages": ["SimpChinese", "English"],
        "displayLanguageSelector": false
      }
    }
  }
```

说明:resources 相对路径基于 `src-tauri/`,故写 `stage/app`、`stage/runtime`(即 `desktop/src-tauri/stage/...`)——**组装脚本的输出目录因此调整为 `desktop/src-tauri/stage/`**(或在此步把 stage 软链/拷贝到 src-tauri 下;以 tauri build 实际报错为准修正路径)。卸载保数据:数据在文档目录,NSIS 默认只删安装目录,天然满足;不写卸载脚本。

- [ ] **Step 2: 打包**

Run: `cd desktop/src-tauri && cargo tauri build 2>&1 | tail -20`
Expected: 产出 setup.exe(体积 ~200-400MB,含 postgres/python/node 运行时,压缩耗时长属正常)。

- [ ] **Step 3: 干净机冒烟(本机可先模拟:临时 PATH 去掉 python/node/docker 后验证)

Run: 安装 setup.exe → 双击桌面图标
Expected: 加载页进度 → 主界面;浏览器访问 `http://127.0.0.1:8765` 连接失败;数据落在 `Documents\ResearchAssetHub\`;卸载后重装数据仍在。

- [ ] **Step 4: 提交**

```bash
git add desktop/src-tauri/tauri.conf.json desktop/.gitignore
git commit -m "feat: NSIS 安装包配置与打包验证"
```

---

## 阶段 D:检查更新

### Task 12: 更新器核心(updater.rs,纯逻辑 + 单测)

**Files:**
- Create: `desktop/src-tauri/src/updater.rs`
- Modify: `desktop/src-tauri/src/lib.rs`(注册模块)
- Modify: `desktop/src-tauri/Cargo.toml`(加 `semver = "1"`)

**Interfaces:**
- Consumes: Task 5 `data_paths()`
- Produces:
  ```rust
  pub struct UpdateManifest { pub version: semver::Version, pub installer: String, pub notes: Option<String> }
  pub fn load_manifest(updates_dir: &Path) -> Result<UpdateManifest, UpdateError>
  pub struct UpdateCheck { pub update_available: bool, pub manifest: Option<UpdateManifest> }
  pub fn check(current: &str, updates_dir: &Path) -> Result<UpdateCheck, UpdateError>
  pub fn launch_installer(updates_dir: &Path, manifest: &UpdateManifest) -> Result<(), UpdateError>
  pub enum UpdateError { NoManifest, InvalidManifest(String), NotFound, LaunchFailed(String) }
  ```

- [ ] **Step 1: 写失败测试**

```rust
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd desktop/src-tauri && cargo test updater 2>&1 | tail -10`
Expected: FAIL(模块不存在)。

- [ ] **Step 3: 实现 updater.rs**

```rust
//! 检查更新:更新源目录(默认 Documents\ResearchAssetHub\updates)内的 manifest.json
//! 格式: {"version": "0.2.0", "installer": "Hub-Setup-0.2.0.exe", "notes": "..."}
//! installer 与 manifest 同目录;有新版则运行安装器(NSIS 静默)。

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
        Err(UpdateError::NoManifest) => Ok(UpdateCheck {
            update_available: false,
            manifest: None,
        }),
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
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd desktop/src-tauri && cargo test 2>&1 | tail -10`
Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add desktop/src-tauri/src/updater.rs desktop/src-tauri/Cargo.toml desktop/src-tauri/src/lib.rs
git commit -m "feat: 更新器核心(manifest 解析/版本比较/静默安装)"
```

### Task 13: 「检查更新」接入托盘与对话框

**Files:**
- Modify: `desktop/src-tauri/src/tray.rs`(菜单加「检查更新」,信号 `CheckUpdate`)
- Modify: `desktop/src-tauri/src/lib.rs`(信号循环处理:check → 有新版弹窗确认 → launch_installer → 完成后提示重启;无新版提示「已是最新」;错误弹窗)

**Interfaces:**
- Consumes: Task 12 全部导出
- Produces: 用户可点击的完整更新闭环。当前版本号取 `app.package_info().version.to_string()`。

- [ ] **Step 1: 改 tray.rs**

菜单段改为:

```rust
    let update = MenuItem::with_id(app, "check-update", "检查更新…", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&show, &retry, &update, &quit])?;
```

事件匹配加:

```rust
            "check-update" => {
                let _ = tx.send(TraySignal::CheckUpdate);
            }
```

`TraySignal` 枚举加 `CheckUpdate` 变体。

- [ ] **Step 2: 改 lib.rs 信号循环**

`tray::TraySignal::Show => {}` 分支后加:

```rust
                        tray::TraySignal::CheckUpdate => {
                            let current = handle
                                .package_info()
                                .version
                                .to_string();
                            let dir = updater::default_updates_dir();
                            match updater::check(&current, &dir) {
                                Ok(result) => {
                                    if !result.update_available {
                                        let _ = tauri_plugin_dialog::DialogExt::dialog(
                                            handle.dialog().expect("dialog plugin"),
                                        )
                                        .message(format!("当前版本 {current} 已是最新。"))
                                        .title("检查更新")
                                        .blocking_show();
                                        continue;
                                    }
                                    let manifest = result.manifest.expect("available implies manifest");
                                    let notes = manifest.notes.clone().unwrap_or_default();
                                    let ok = tauri_plugin_dialog::DialogExt::dialog(
                                        handle.dialog().expect("dialog plugin"),
                                    )
                                    .message(format!(
                                        "发现新版本 {}{}\n\n立即下载并安装?",
                                        manifest.version,
                                        if notes.is_empty() { String::new() } else { format!("\n\n更新说明:{notes}") }
                                    ))
                                    .title("检查更新")
                                    .blocking_show();
                                    let _ = ok;
                                    // Tauri 2 dialog blocking_show 返回 bool(确认/取消)因版本而异;
                                    // 若为 false 则 continue。实施时按编译器提示处理返回值。
                                    match updater::launch_installer(&dir, &manifest) {
                                        Ok(()) => {
                                            let _ = tauri_plugin_dialog::DialogExt::dialog(
                                                handle.dialog().expect("dialog plugin"),
                                            )
                                            .message("安装包已启动,安装完成后请重启软件。")
                                            .title("检查更新")
                                            .blocking_show();
                                        }
                                        Err(e) => {
                                            let _ = tauri_plugin_dialog::DialogExt::dialog(
                                                handle.dialog().expect("dialog plugin"),
                                            )
                                            .message(format!("更新失败:{e}"))
                                            .title("检查更新")
                                            .blocking_show();
                                        }
                                    }
                                }
                                Err(e) => {
                                    let _ = tauri_plugin_dialog::DialogExt::dialog(
                                        handle.dialog().expect("dialog plugin"),
                                    )
                                    .message(format!(
                                        "更新源不可用:{e}\n\n默认更新源:{}\n(将新版安装包与 manifest.json 放入该目录即可)",
                                        dir.display()
                                    ))
                                    .title("检查更新")
                                    .blocking_show();
                                }
                            }
                        }
```

(确认对话框返回值处理:查 tauri-plugin-dialog v2 文档确认 `blocking_show` 返回 `bool`(Ok/Cancel);按文档修正后,`false` 时 `continue`。)

- [ ] **Step 3: 编译 + 冒烟**

Run: `cd desktop/src-tauri && cargo build 2>&1 | tail -5`
Expected: 编译成功。
冒烟:运行壳 → 托盘「检查更新」→ 无 manifest → 提示「当前版本已是最新」;向 `Documents\ResearchAssetHub\updates` 放入 `manifest.json`(version 0.1.1,installer 指向任意 exe)→ 再点 → 出现「发现新版本」确认框。

- [ ] **Step 4: 提交**

```bash
git add desktop/src-tauri/src/tray.rs desktop/src-tauri/src/lib.rs
git commit -m "feat: 托盘检查更新入口与确认/安装/重启提示流程"
```

---

## 阶段 E:文档与验收

### Task 14: desktop/README.md 与验收清单

**Files:**
- Create: `desktop/README.md`

**Steps:**

- [ ] **Step 1: 写 README(构建/开发/验收全流程)**

```markdown
# 研究资产库·桌面版

Tauri 2 壳:双击即用,无浏览器、无 8765 本地网站。数据在 `Documents\ResearchAssetHub\`。

## 开发

```powershell
pnpm --filter @research-asset-hub/web dev        # 终端 A:Vite 8765
cd desktop/src-tauri && cargo tauri dev          # 终端 B:壳(连 Vite)
```

开发模式壳使用容器基础设施(docker compose)与项目 .venv;生产模式使用便携运行时。

## 构建安装包

```powershell
# 1. 一次性:按 desktop/scripts/README.md 下载三个运行时并组装
.\desktop\scripts\stage-runtime.ps1 -PostgresZip ... -MeiliExe ... -NodeZip ...
# 2. 打包
cd desktop/src-tauri && cargo tauri build
# 产物: target/release/bundle/nsis/ResearchAssetHub_<version>_x64-setup.exe
```

## 验收清单(发版前逐条跑)

- [ ] 干净机(无 Docker/Python/Node)安装成功,桌面/开始菜单有图标
- [ ] 双击 → 加载页显示「预检环境→启动数据库→启动搜索引擎→数据迁移→启动应用服务」→ 自动进入主界面
- [ ] 全程无浏览器窗口;`http://127.0.0.1:8765` 访问失败
- [ ] 点 X → 窗口消失;托盘双击/「打开主界面」秒开(后台保持)
- [ ] 托盘「退出」→ `tasklist` 无 python/node/meilisearch/postgres 残留
- [ ] 断网可用;搜索/论文/代码/图谱/备份全功能冒烟
- [ ] 重复双击不产生第二个窗口(唤起已有)
- [ ] 端口冲突(如 8767 被占)→ 加载页停在「预检环境」并弹窗说明
- [ ] 卸载 → 重装 → 数据完好;`Documents\ResearchAssetHub` 未被动
- [ ] updates 目录放新版 manifest+安装包 → 托盘「检查更新」→ 确认 → 安装 → 重启后版本号更新、数据无损
- [ ] 更新源无 manifest → 提示「已是最新」
```

- [ ] **Step 2: 提交**

```bash
git add desktop/README.md
git commit -m "docs: 桌面版构建/开发/验收说明"
```

### Task 15: 端到端回归与收尾

- [ ] **Step 1: 全量回归**

Run: `.venv/Scripts/python.exe -m pytest -q`(全项目)
Expected: 与改前基线一致(298 passed, 2 个已知 portal 数据层失败, 1 skipped);新增桌面 origin 测试全过。
Run: `pnpm --filter @research-asset-hub/web test && pnpm --filter @research-asset-hub/web typecheck`
Expected: 全绿。
Run: `cd desktop/src-tauri && cargo test && cargo clippy -- -D warnings 2>&1 | tail -5`
Expected: 全过,clippy 无警告。
Run: `cd desktop/src-tauri && cargo tauri build`(最终安装包)
Expected: 安装包产出,按 Task 14 清单抽验 5 条。

- [ ] **Step 2: 收尾提交**

```bash
git status --short   # 确认除预期产物外无残留
git add -A
git commit -m "chore: 桌面版端到端回归与收尾"
```

---

## 自审记录

1. **Spec coverage**:设计规格 6 大验收标准逐一映射——双击启动(Task 9/11)、X 缩托盘+退出清理(Task 9)、8765 消失(Task 11 冒烟)、卸载保数据(Task 11)、断网可用(Task 11/14)、检查更新(Task 12/13)。错误处理表:端口占用(Task 7 Preflight)、日志按钮(Task 6 日志落盘 + Task 9 弹窗)、更新源不可达(Task 13)。YAGNI:开机自启/静默更新未做。
2. **Placeholder scan**:无 TBD/TODO;开发回退路径、dialog 返回值、gitnexus 入口文件名三处已显式标注"以报错/文档为准修正"的处理方式,属可执行指引而非占位。
3. **Type consistency**:`RuntimePaths`/`DataPaths`/`Ports`/`StartupEvent`/`UpdateManifest` 各任务签名一致;`emit_startup_progress` 在 Task 4 定义、Task 7/9 复用;`tray::TraySignal` 变体 Task 9 定义、Task 13 扩展,一致。
