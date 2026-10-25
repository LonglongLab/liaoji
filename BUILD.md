# 从源码自行构建聊记

本包不含测试、个人配置、真实聊天数据、签名私钥或 Git 历史。
`source-release.json` 记录目标、版本、来源提交与逐文件 SHA-256；从发行下载页另行核对 ZIP 的 SHA-256 和签名。包内清单只能检查解压内容，不能独自认证发布来源。

## 工具与网络

- Python 3.12+、uv、Git、Node.js 22.13+（建议 24 LTS）、Corepack；Corepack 按 `packageManager` 使用锁定的 pnpm 11.15.0。
- Windows 安装器须在 Windows x64 构建；安装 Inno Setup 6 并使 `ISCC.exe` 可发现，否则仅生成便携 ZIP。
- 自托管镜像需要 Linux Docker/Buildx（可用 Docker Desktop Linux 容器）。
- 构建会联网获取固定锁文件依赖、公开 Git 依赖和 CPython/基础镜像；这不是离线依赖包。不会连接维护者的私有仓库，也不要求其云账号或签名私钥。

在**本文件所在目录**执行：

```sh
python build.py --verify-only
python build.py
```

本地包生成 `build/desktop/` 下的 Windows 便携 ZIP 与安装器；默认不绑定官方自动更新，避免自行构建后被官方二进制替换。所有本地解析/解密代码均在包内。

远程包生成 `local/liaoji-server:<version>` 与 `<version>-data-init` 两个本机镜像。部署模板位于 `chat-archive/deploy/self-hosted-distribution/`；复制 `configuration.example` 为 `.env`，填写自己的域名、对象存储等参数，再把 `SERVER_IMAGE`、`DATA_INIT_IMAGE` 改为自己构建的引用。执行部署前阅读该目录 README。镜像不自动上传，也不自动启动服务。自托管源码不含本地采集/解密模块。

官方预构建镜像位于 [longlonglab/liaoji-server](https://hub.docker.com/r/longlonglab/liaoji-server)；部署时使用对应版本或固定摘要。

自构建镜像使用 compose 模板直接部署，**不使用 install.sh 下载官方镜像的流程**。在模板目录填写 `.env` 的网站域名、公开来源、端口及联系邮箱，保持官方 Caddy 的固定引用；两条第一方镜像引用使用上一步的本机标签，然后执行：

```sh
docker compose --env-file .env -p liaoji-selfbuilt up -d
docker compose --env-file .env -p liaoji-selfbuilt exec -T api cat /var/lib/chat-archive/browser-bootstrap-token
```

第二条命令只用于首次设置，输出本机部署的一次性引导令牌。访问自己站点的 `/#setupToken=<令牌>`，完成网页配置；令牌不要分享，完成首次设置后失效。`--from-bundle` 所需的官方 images.json 不属于本源码包，不能把它与自行构建的镜像混用。

分项构建：

```sh
python build.py --frontend-only
python build.py --backend-only
python build.py --workers
python build.py --context-only
```

Workers 是远程包中的另一种运行时，`--workers` 完成类型检查与 Wrangler dry-run，不部署云资源。真实部署前编辑 `chat-archive/remote/wrangler.toml`，配置自己的 D1/R2，按迁移文件初始化数据库并设置自己的设备认证配置；自托管的文件系统、导出和长任务能力不等同 Workers。

核对、审阅和修改后可用 `python build.py --allow-modified` 构建自己的改动。默认校验会拒绝与发行清单不一致的文件；修改后生成的产物不能宣称为官方签名版本。完整安装器逐字节可复现尚未保证。

第一方源码使用授权见 `SOURCE-LICENSE.txt`；第三方许可和来源保留在相应目录、依赖包及 `chat-archive/third_party/`。本地端只提供本源码包下载，不在 GitHub 发布本地源码或私有 monorepo 历史。
