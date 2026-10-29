# 公开目录构建与验证记录

验证日期：2026-10-29。目录快照日期：2026-10-04。

## 构建结果

| 项目 | 数量 |
| --- | ---: |
| 只读输入论文 | 5,915 |
| 只读输入仓库（仅用来核对真实资产关系） | 3,326 |
| 非论文 collection（不发布） | 15 |
| 排除：无合格公开学术原文 URL | 390 |
| 排除：没有可见 taxonomy 领域 | 64 |
| 按相同规范化原文 URL 合并 | 1 |
| 最终公开论文 | **5,460** |
| 可见研究领域 | **14** |
| 已关联公开代码的论文 | **3,053** |
| 去重后公开代码 URL | **3,026** |

构建过程拒绝 10,880 个候选 URL 字段值；这个值包含重复字段、代码 URL、会议首页与不在白名单内的域名，不是被排除的论文数。隐藏的 `generation` 和 `navigation-compression` 不发布、不重映射；有其他可见领域的论文仍可保留。

`dist/` 共 6 个文件，合计 **2,596,693 字节**；其中目录 JSON 为 **2,564,957 字节**。前台无打包依赖、无外部字体或 CDN；每页仅渲染 20 篇。

目录 SHA-256：`acf983105f002d7cc086d957bb974186bfddab82bbab68c345fa203d72cbff51`。

## 已核实元数据纠错

2026-10-29 根据原站 https://arxiv.org/abs/2605.30349 已核实的英文题名，清除 AdaState 误配的 Audible World Models 中译，保留英文原题，不推测新译名。纠错由 `metadata-corrections.json` 记录 URL、预期题名、清除字段、原站证据和日期；只应用于公开导出层，不修改冻结来源或数据库。构建报告 `metadata_corrections_applied=1`。回归覆盖目标错配清除、无关条目保持、题名不符停止以及生成目录的实际结果。未扩大至全库联网重审，也未改动已核验正确的 Causal World Modeling → lingbot-va 关联。

## 自动检查

- `python -m pytest tests/unit/test_public_catalog.py -q`：**43 passed**。包含字段白名单、私人状态排除、XSS 文本写入约束、URL allowlist / 凭据 / IP / token / 转义边界、真实已确认关系匹配、公开 URL 传递性去重、隐藏领域、无结果、搜索与筛选组合、排序、分页头尾与越界。
- `ruff check scripts/build_public_catalog.py tests/unit/test_public_catalog.py`：通过。
- `mypy --strict scripts/build_public_catalog.py`：通过。
- 两个 JavaScript 模块通过 Node 语法检查；纯搜索与分页函数经过 Node 运行回归，不是仅检查文件存在。
- 完整生成目录逐条重验原文 / 代码 URL、输出字段、匿名 ID 唯一性、领域范围与数量。
- 最终 JSON 扫描：UUID 模式 **0**、Windows 本地路径模式 **0**、localhost / 127.0.0.1 **0**、禁止的私人 JSON 字段 **0**。
- 所有来自目录的文本使用 `textContent`；不使用 `innerHTML` / `insertAdjacentHTML`。
- 没有上传或复制原始 JSONL、PDF、私人笔记、截图或数据库。既有桌面截图包含阅读状态，因此未复制至站点。

## 验证边界

本记录不声称完成浏览器或线上验收。仍需发布负责人通过桌面 / 390px 窄屏浏览器检查页面布局、无横向溢出、键盘焦点、实际加载失败与重试、组合筛选、分页、分享 URL，以及新窗口外链。原文 / 代码 URL 通过静态规则检查，不代表逐个联网探测可用性或确认仓库所有权。

发布目录只能是 `dist/`。站点 canonical 与发布负责人提供的一致；GitHub、Issues 与预览版下载指向 `fingercd/research-asset-hub-public`，发行版本 `v0.3.0-rc.1`。安装包可用性、许可审核与 Sites 部署由其他任务统一核验，本任务不作完成声明。
