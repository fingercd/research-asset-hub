# 公共源码默认配置

[返回首页](../README.md) · [开发指南](../docs/DEVELOPMENT.md) · [来源契约](../docs/architecture/source-boundaries.md)

这里是可独立启动的空资料库配置，不是开发者个人资料库的导出。`sources.yaml` 没有登记任何论文来源，`taxonomy.yaml` 没有预设用户领域，领域门户索引也为空。首次使用可以在界面建立领域并上传 PDF。

| 路径 | 用途 |
| --- | --- |
| [sources.yaml](sources.yaml) | 空来源注册表；托管上传的实际配置保存在用户数据目录 |
| [taxonomy.yaml](taxonomy.yaml) | 空领域表及通用阅读状态、访问级别、资产类型 |
| [domain-portals/v2/portal-index.yaml](domain-portals/v2/portal-index.yaml) | 空精选门户索引；不影响创建普通领域 |
| [check-unit-actions-allowlist.yaml](check-unit-actions-allowlist.yaml) | 空材料例外清单 |
| [extraction-policy.yaml](extraction-policy.yaml) | 通用抽取规则 |
| [html-policy.yaml](html-policy.yaml) | 通用 HTML 安全规则 |
| [relation-types.yaml](relation-types.yaml) | 通用资料关系类型 |
| [../newconfig/release-defaults/](../newconfig/release-defaults/) | 安装包空库模板 |

个人来源清单、冻结导入快照、受限第三方代码摘录和验收台账不随公共源码分发。登记外部资料时，必须先确认读取和使用权限。已经正式导入的来源及 manifest 仍是不可改写的证据；更正应创建新来源并留下审计记录，不能通过编辑旧文件或直接改数据库绕过冻结校验。

外部论文、代码和用户数据独立备份；不能把应用 Git 仓库当成资料库备份。
