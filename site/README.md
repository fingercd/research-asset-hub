# 公开科研参考文献目录

这是 Research Asset Hub 的独立公开静态站点，不是桌面应用的远程数据库界面。站点源文件直接放在 `dist/`，没有打包器、Node 运行时服务、第三方 CDN、统计脚本或在线账户。浏览器下载经过筛选的公开 JSON 后，在本地完成关键词搜索、领域 / 会议 / 年份筛选、排序与分页。

## 发布边界

**只能发布 `dist/`，绝不能上传 `outputs/open-catalog/`。** 原始导出包含个人状态、批注与来源信息，不能直接作为公开 API 或下载文件。公开数据必须经过 `scripts/build_public_catalog.py` 的显式字段和 URL 白名单重新导出。

公开字段只有：匿名派生条目标识、论文题名、已登记中文译名、年份、会议 / 期刊、可见领域、公开学术原文 URL、通过已确认 `implemented_by` 关系关联的公开代码 URL。只接受 `paper` 类型且具有受信任公开学术链接的记录，不发布论文正文、摘要、作者清单、导读、笔记、阅读状态、批注、来源声明、资产 UUID、磁盘路径或用户信息。

原始 `visibility=private` 是本地库默认值；本次独立目录基于用户明确的公开目录授权，不更改资产可见性，也不读取或修改实时数据库。原文与代码仍由第三方网站提供，不保证链接长期有效。代码关联表示登记时已确认，不等于对第三方内容的安全背书。

当前来源是 **2026-10-04** 快照，不是实时全库。统计里的输入仓库数量是用于解析关系的内部快照总数，**并非所有仓库都在站点发布**；前台仅显示收录论文数、可见领域数和关联代码的论文数。

URL 策略采用精确域名白名单、闭合查询参数规则和学术条目路径校验；拒绝未知域名、凭据、非标准端口、localhost / IP、本机路径、任意 query、fragment 与可疑转义。OpenReview 只允许单个 `id` 参数，代码只允许公开托管平台的仓库身份路径。白名单偏保守，可能排除真实的个人项目页；不会为了覆盖率放行未知站点。只按相同的规范化公开原文链接去重，不进行模糊题名合并。隐藏领域不重映射、不修改配置，只有其他可见领域的记录仍可保留。

## 重建目录

在应用仓库根目录运行（Python 环境需要项目已有依赖 PyYAML）：

```powershell
./.venv/Scripts/python.exe scripts/build_public_catalog.py
./.venv/Scripts/python.exe -m pytest tests/unit/test_public_catalog.py -q
```

脚本只读已有快照、manifest 和 taxonomy，输出 `site/dist/assets/catalog.json`。控制台逐项列出公开数量、代码数量、无可验证原文 / 无可见领域等排除数量，以及按同一公开 URL 去重的数量。`rejected_url_candidates` 是逐次拒绝的候选 URL 字段值数量，可能包含重复值、代码 URL、会议首页或未知域名，**不是被排除的论文数**。

已核实的展示元数据错误记录在 `site/metadata-corrections.json`，包含匹配 URL、预期英文题名、清除字段、原站证据和核验日期。构建默认应用这些纠错并报告数量；仅允许清除误配的 `title_zh`，不猜测新译名，不修改冻结快照或数据库。若目标 URL 不再收录、或英文题名与预期不符，构建停止，避免把纠错误用到别的条目。当前纠错是清除 AdaState（arXiv:2605.30349）误配的 Audible World Models 中译。

更新快照后应检查日期、公开字段、排除数量与 URL 策略，不应直接替换为原始 JSONL。当前快照之外的新领域和新条目不会自动出现。

## 本地预览与上线

```powershell
./.venv/Scripts/python.exe -m http.server 8080 --bind 127.0.0.1 --directory site/dist
```

用浏览器打开本机 8080 端口；不能直接双击 `index.html`，因为 `file:` 页面无法正常 fetch 目录。部署不需要 build command，只需上传 `site/dist/`。Sites 的配置位于 `site/.openai/hosting.json`，由发布任务统一管理；不要把原始资料目录或应用数据库加入站点源目录。

键盘支持 Tab、表单选择、按钮和 `/` 聚焦搜索。筛选条件与页码同步到 URL，可复制分享。无结果、加载失败、重新加载、清除条件与分页边界有专门处理。静态 / 单元测试不替代浏览器检查：发布前仍需在桌面与 390px 窄屏验证检索、筛选、分页、无横向溢出、键盘焦点和外链打开。

## 文件

- `dist/index.html`：中文结构、说明、隐私边界、预览版下载和 GitHub 入口。
- `dist/assets/styles.css`：无外部字体依赖的白 / 冷灰 / 深青绿响应式界面。
- `dist/assets/app.js`：DOM 交互；所有目录文本通过 `textContent` 写入，禁用 HTML 注入。
- `dist/assets/catalog-core.js`：纯搜索、筛选、排序、分页函数，可用 Node 回归测试。
- `dist/assets/catalog.json`：白名单化公开数据；不是本地库完整导出。
- `dist/assets/favicon.svg`：站点图标。

网页软件随应用源码许可证发布。参考文献元数据与外链不改变第三方论文、代码或商标的权利与许可证；请遵守原始来源的使用条件。
