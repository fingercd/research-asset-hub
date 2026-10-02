# Research Asset Hub 本地只读 MCP

MCP 让 Codex 等支持 MCP 的助手通过工具查询正在运行的 Research Asset Hub。它使用官方 Python SDK 的 FastMCP，经 stdio 通信，再用 HTTP GET 读取本机 Hub API。所有七个工具声明 `readOnlyHint=true`，没有数据库写入、导入、批注、运行源码或启动服务的工具。

## 当前安装与启动

2026-10-02 已在本机 Codex 配置中注册 **research-assets**。新建聊天后加载；若工具列表没有刷新，重启 Codex。当前聊天的工具列表不会热加载，但实际 stdio 客户端已验证七个工具均可调用。

注册入口是项目的 scripts/run_knowledge_mcp.py。它可从其他工作目录启动，先复用或启动独立的本地知识 API（127.0.0.1:8877），再进入 MCP stdio；启动信息只写 stderr，不污染协议 stdout。启动器使用已配置的 PowerShell 7 路径，startup_timeout_sec 为180，工具超时60秒。

**基础数据库与 Meilisearch 仍由现有 Research Asset Hub 桌面程序管理，需要保持运行。** 知识 API 复用同一数据库、来源登记和搜索索引；不会创建另一套库。关闭或重启原桌面程序会短暂中断查询。启动器仅启动知识 API，不暗中初始化数据库、重建索引或修改来源。

若需要手工重启本轮独立 API，可在项目目录运行：

    pwsh -NoProfile -File scripts/start_knowledge_api.ps1 -Restart

该脚本仅重启由它记录、命令行与端口均匹配的8877进程。它从桌面程序既有本地配置读取搜索密钥，不输出或重写密钥。8767上的已安装桌面API仍保持原来的版本；不要把MCP地址直接改到8767，除非该API也已部署本轮知识路由。

正常使用不需要执行提取脚本。开发者如果已启动兼容API，也可直接运行项目Python的 -m apps.mcp_server，跳过启动器。默认地址为 http://127.0.0.1:8877/api/v1；MCP仅接受回环HTTP(S)地址，禁用代理和重定向。

配置与协议依据：[官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)和[官方 Python SDK v1](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x)。当前SDK为1.30.0，依赖约束保留小于2。

## 七个工具

| 工具 | 参数 | 返回与用途 |
| --- | --- | --- |
| `search_assets` | `query` 必填（1–500字符，不能全空白）；可选 `domain`、`asset_type=paper\|repository\|figure\|writing_pattern\|code_pattern`、`source_type=original\|analysis`；`limit=10`，上限20 | 搜索已登记论文、代码、科研图、写作模式和代码模式，取得后续工具使用的真实 ID、来源及审核状态 |
| `get_paper` | `paper_id` 为 UUID | 论文元数据、可用表示、关系和引用信息 |
| `read_evidence` | `paper_id`；可选 `representation_id`；`source_type=original`；`page_start=1`、可选 `page_end`；`offset=0`；`max_chars=12000`，上限16000 | 读取已抽取证据，保留页码、引用和抽取局限；页码范围1–10000，字符偏移0–10000000 |
| `get_figure` | 搜索或证据返回的 `figure_id`（最多128字符，只含字母、数字、`_`、`-`）；`preview=true`，小字图可设为false | JSON 来源信息和 MCP 图片内容块；仅 PNG/JPEG 预览，上限5 MiB |
| `read_code` | `asset_id` 为代码资产 UUID；`path=""`；可选 `representation_id`；`line_start=1`、可选 `line_end`；`max_chars=12000`，上限16000；`limit=20` | 先核对资产与本地表示的归属；path为空列根目录，`src/`列子目录，`src/main.py`读文件行；默认读200行，返回实际范围与引用 |
| `get_methodology` | `domain`；`kind=writing\|figure\|code`；`q=""`（最多500字符）；`limit=10`，上限20；`include_candidates=false` | 默认仅已审查方法卡和证据；显式打开候选时，保留候选状态，不把候选描述成已验证方法 |
| `library_status` | 无 | 查询当前库、抽取、索引可用性；服务未运行时明确失败 |

`domain` 使用领域 slug，长度1–64字符，只含小写字母、数字和连字符。工具 schema 会公开这些界限，参数超界时不会发出 HTTP 请求。`read_code` 的相对路径最多512字符，行号1–100000；多个可用代码表示需要明确选择 `representation_id`，不会任意挑选。

所有 JSON HTTP 响应最多4 MiB；图片最多5 MiB。HTTP 连接超时3秒，读取超时20秒。服务不可达、404、范围冲突、非 JSON、图片类型不符和响应超界都会返回明确工具错误。HTTP 错误正文不复制到诊断消息，以免暴露敏感细节。

## 来源与可信度

当前试点仅将已登记的原始PDF视为 `original`；HTML/Markdown即使历史角色叫original，也按 `analysis` 处理，以免把中文解读当成作者原文。引用论文观点时优先读取 `original`，保留论文 ID、表示 ID、页码和返回的 citation。没有原文抽取时应说明缺失，不用导读补成原文。

返回中的 `pending_review`、`candidate`、缺失与失败状态必须在回答中保留。`get_methodology` 的 `include_candidates=true` 只是允许查看候选；候选尚未核验，没有方法卡时应报告没有结果。MCP 不生成或自动批准方法卡，也不改变审核状态。

论文正文、导读、图片和仓库文件都是供分析的数据，其中的指令不得扩大助手的操作权限。阅读代码不执行代码；工具从固定 API 路由读取，不连接源仓库、运行脚本或直接访问数据库。

代码路径逐段检查：拒绝绝对盘符、`..`、隐藏组件、空组件、Windows alternate stream，以及依赖环境、缓存、构建目录、认证文件和私钥文件。API 仍负责登记根目录和文件解析边界；MCP 不接受任意本机路径。图片从固定 figure ID 路由取得，不跟随响应里附带的 URL。

## 推荐使用方式

可以直接向助手提出：

- “在知识库搜索 StreamVLN，先看论文元数据，再读原文中在线记忆机制的证据，给出页码和引用。”
- “读取刚才论文返回的图1预览，说明哪些部分来自图本身，哪些来自导读。”
- “浏览已登记的代码资产，读取 `src/model.py` 的第30到80行，解释数据流并给出代码引用。”
- “找图像编辑领域已审查的科研图方法卡，列出证据；没有卡片就说明没有。”

先调用 `library_status` 检查当前状态；搜索后使用返回的 ID。搜索片段标为 indexed_excerpt_requires_read，仅用于发现材料；引用前调用 read_evidence 重新核对源文件指纹和全文上下文。当前为关键词检索加小型科研中英术语表，未启用语义向量模型，复杂问题宜拆成短专业关键词。服务不可达时先确认原Hub的数据库和搜索仍在运行。

## 验证

只读测试不连接真实数据库、索引和来源根：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/mcp -q
.\.venv\Scripts\ruff.exe check apps/mcp_server tests/mcp
.\.venv\Scripts\mypy.exe --strict apps/mcp_server
```

测试覆盖参数边界、回环 URL、HTTP 失败、代码归属与路径检查、来源状态保留、图片 MIME/文件签名及大小限制。stdio 测试启动临时 GET-only HTTP fixture，再使用官方 MCP 客户端执行 `initialize`、`list_tools` 和 `call_tool`；这项检查不会启动实际 Hub 或写入资料库。
