# 构建聊记远程端

需要 Python 3.12+、uv、Node.js 22.13+、Corepack 和 Linux Docker/Buildx。
从仓库根目录执行 `python build.py --verify-only` 核对源码清单，执行 `python build.py` 构建自托管镜像。
镜像只在本机构建，不上传、不启动服务。构建需要联网下载锁定依赖。

官方预构建镜像位于 [longlonglab/liaoji-server](https://hub.docker.com/r/longlonglab/liaoji-server)；部署时使用对应版本或固定摘要。

镜像标签为 `local/liaoji-server:<version>` 和 `local/liaoji-server:<version>-data-init`。
复制 `chat-archive/deploy/self-hosted-distribution/configuration.example` 为同目录的 `.env`，
填写自己的域名、邮箱和站点来源，将 SERVER_IMAGE、DATA_INIT_IMAGE 设为上面的本机标签，保持 Caddy 固定引用。
在该部署目录执行：

```sh
docker compose --env-file .env -p liaoji-selfbuilt up -d
docker compose --env-file .env -p liaoji-selfbuilt exec -T api cat /var/lib/chat-archive/browser-bootstrap-token
```

第二条命令读取首次设置令牌；仅在自己的站点 `/#setupToken=<令牌>` 使用，不要分享。
网页引导配置口令、两步验证、附件存储、备份和电脑配对。不要用源码模板中的下载脚本替代自构建流程；其发行地址是占位符。

`python build.py --frontend-only` 构建远程网页；`--backend-only` 构建 Python wheel；
`--context-only` 仅准备 Docker 上下文；`--workers` 检查另一种 Workers 运行时，不部署资源。
Workers 需自行配置 wrangler.toml 的 D1/R2、执行迁移并设置认证；它与自托管的文件系统、导出和长任务能力不同。

修改后审阅差异，可用 `python build.py --allow-modified` 构建自己的版本。
授权见 SOURCE-LICENSE.txt，第三方许可保留在相应目录。
