# 便携运行时组装

一次性下载(放到任意目录,组装脚本按参数传入):

1. PostgreSQL 16 Windows zip binaries(EDB):
   <https://www.enterprisedb.com/download-postgresql-binaries>(选 16.x / Windows x86-64)
2. Meilisearch Windows 二进制:
   <https://github.com/meilisearch/meilisearch/releases>(meilisearch-windows-amd64.exe)

组装(在开发机执行一次):

```powershell
.\desktop\scripts\stage-runtime.ps1 `
  -PostgresZip D:\downloads\postgresql-16.x-windows-x64-binaries.zip `
  -MeiliExe D:\downloads\meilisearch-windows-amd64.exe
```

产物:`desktop/stage/{app,runtime}`(已被 .gitignore 忽略)。

便携运行时只包含 Python、PostgreSQL 与 Meilisearch。Node.js 仍用于开发机上的前端构建，但不会装入桌面安装包。

`-SourceWorkspaceRoot` 可选，用于覆盖安装版 `sources.yaml` 解析后的 `workspace_root`；缺省为 `research-asset-hub` 项目根。脚本只会把该值写入已忽略的 `desktop/stage/app/config/workspace-root.txt`，不会改写或迁移任何来源文件。桌面壳仅在目录仍然存在时注入 `HUB_HOST_WORKSPACE_ROOT`；用户显式设置的环境变量优先。
