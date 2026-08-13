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
