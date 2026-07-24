# Research Asset Hub 未来云部署模板

状态：`template-only`，已做静态验证，**当前没有部署到公网**。本机
`deploy/compose.yaml` 和应用默认监听仍为 `127.0.0.1`。

## 安全边界

- 只有 Caddy 暴露 `80/443`；PostgreSQL、Meilisearch、Qdrant、OIDC 代理和
  Hub 进程都没有宿主机端口。
- Caddy 为主站和资产子域自动申请 TLS，两个域都先经过 OAuth2 Proxy 的
  OIDC 校验。
- OAuth2 Proxy 使用邮箱白名单、PKCE、Secure/SameSite Cookie；7.15.2
  包含 2026 年公开的认证绕过修复，并且只信任 Caddy 的固定边缘网络地址所
  提供的 `X-Forwarded-*`。
- 密钥由 Compose 文件型 secrets 注入。为兼容现有应用配置，容器入口只在
  进程内把三个核心密钥桥接为环境变量，既不打印，也不写入磁盘。
- 私人来源只读挂载到 `/srv/research-assets`；网关仍只接受数据库已登记的
  representation 和短时签名会话。
- 数据网络设为 `internal`，Caddy 不接入数据网络，所有容器移除 Linux
  capabilities（Caddy 仅保留绑定低端口所需的 `NET_BIND_SERVICE`）。

## 部署前接口

1. 构建并推送 `Dockerfile.cloud` 生成的不可变应用镜像；镜像包含正式构建的
   React 前端，不包含来源资料、运行状态或密钥。
2. 复制 `env.example` 为服务器上的 `.env`，填写两个 DNS 域、ACME 邮箱、
   OIDC issuer/client ID、镜像标签和只读来源根。
3. 按 `secrets/README.md` 创建六个密钥文件，并限制文件权限。
4. 将来源快照按 `sources.cloud.example.yaml` 的稳定 alias 放入私有来源根。
   原文件继续只读；数据库和开放导出应通过正式备份恢复。
5. 在 OIDC 提供商登记回调：
   `https://<HUB_DOMAIN>/oauth2/callback`。Cookie 域必须覆盖主站和资产子域。
6. 先执行以下验证，再由运维人员单独批准真正的 `up`：

```sh
docker compose --env-file .env -f compose.cloud.yaml config --quiet
docker run --rm \
  -e HUB_DOMAIN=research.example.com \
  -e HUB_ASSET_DOMAIN=assets.research.example.com \
  -e HUB_ACME_EMAIL=operator@example.com \
  -v "$PWD/Caddyfile:/etc/caddy/Caddyfile:ro" \
  caddy:2.11.4-alpine caddy validate --config /etc/caddy/Caddyfile
```

本模板刻意不提供自动部署命令、默认密码或跳过 TLS/认证的 profile。

## OIDC 与业务身份接口

OIDC 当前位于反向代理边界，向上游传入 `X-Auth-Request-User` 和
`X-Auth-Request-Email`。现有 v1 业务层仍固定使用逻辑用户 `local`；模板不会
用 OIDC subject 重写 asset、annotation、relation 或其他业务资源 ID。未来
启用多用户时，应增加独立的 principal 映射表：

```text
issuer + subject -> principal_id
principal_id -> local user/role
business resource UUID -> unchanged
```

在该映射和授权中间件完成前，此模板只允许单人邮箱白名单，不应扩展为多人
共享服务。

## 私有资产存储接口

`storage/private-storage.schema.json` 定义 `v1alpha1` 契约。当前已接线的驱动
只有只读 filesystem，要求 `stat/range-read/list/checksum`，正好匹配 PDF
Range、目录 bundle 和 hash 校验。`s3-compatible` 仅是预留契约，状态为
`not-wired`：以后实现时必须保持 `representation_id + descendant path` 的
授权边界、Range/HEAD/ETag、一致的 digest 校验，并使用
`/run/secrets/private_storage_credentials`，不能返回对象存储永久公开 URL。

## 已知上线门禁

- 云端需要把数据库备份、开放导出和私有来源快照放到独立持久存储，并完成
  一次隔离恢复演练；Meili/Qdrant 继续视为可重建缓存。
- 当前资产 HTML 的 CSP 与 gateway session cookie 是本地 v1 策略。正式上云
  前必须做浏览器级回归，确认资产子域嵌入策略和 Secure Cookie；未通过时
  只能新标签页打开原始 HTML。
- `HUB_APP_IMAGE` 应改成带 digest 的镜像引用，并为全部基础镜像建立定期漏洞
  扫描与升级窗口。

## 参考

- [Caddy `forward_auth`](https://caddyserver.com/docs/caddyfile/directives/forward_auth)
- [OAuth2 Proxy 的 Caddy 集成](https://oauth2-proxy.github.io/oauth2-proxy/configuration/integrations/caddy/)
- [Docker Compose secrets](https://docs.docker.com/reference/compose-file/secrets/)
