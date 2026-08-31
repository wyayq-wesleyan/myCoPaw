# ARM64 瀚高数据库

此部署基于 `qiuchenjun/highgo:hgdb-ee-6.0.4.1` 的 ARM64 镜像，并修正其初始化脚本的硬编码口令：首次初始化时使用 `HIGHGO_PASSWORD`，默认值为 `Thiner@123`。

> 注意：该第三方镜像未包含 `hgdb.lic`，本地 `check_lic` 检查返回授权文件异常。它只适合作为技术验证；正式信创项目必须从瀚高或其授权渠道取得与 ARM64 环境匹配的授权文件和交付镜像，完成授权核验后再部署。

```sh
cd deploy/highgo-arm64
cp .env.example .env
docker compose up -d --build
```

服务监听 `5866`，数据持久化在 `deploy/highgo-arm64/data`。首次启动后，在 `.env` 中设置符合部署规范的强密码；已有数据目录不会重新初始化或重置数据库密码。
