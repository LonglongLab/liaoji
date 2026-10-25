# 自托管一键引导部署（PSU-2.231，候选未发布）

用户主入口是从可信 HTTPS 地址下载自包含 install.sh，中文脚本引导到六步配置网页。
固定正式地址预留 `https://dl.liaoji.gcdev.dev/server/install.sh`，部署频道独立为
`https://dl.liaoji.gcdev.dev/server/deployment/stable.json`；本轮未上传正式地址或修改生产。

## 用户操作

正式发行后：

```sh
curl --fail --proto '=https' --tlsv1.2 --location --max-redirs 0 https://dl.liaoji.gcdev.dev/server/install.sh -o liaoji-install.sh && sh ./liaoji-install.sh
```

脚本使用 sudo 请求必要权限，缺少系统 Python/Docker 时先询问是否安装官方依赖；
首期自动安装支持 Ubuntu 22.04+ / Debian 12+，已有其他容器运行时不会自动卸载。
初始下载需要 curl 与 CA；域名/DNS、安全组及已有云桶仍由部署者准备。

填写完整网站域名与邮箱；默认 HTTP 80 / HTTPS 8443，API 仅宿主机回环。
正常不用输入镜像名或 SHA-256，不手改 .env。端口冲突可交互改选，不停止别人的服务。
下载的 ZIP 校验大小、摘要、同源 HTTPS 与固定文件白名单，镜像按 digest 拉取。
本信任模型依赖正式 HTTPS 入口；摘要校验不等同单独的离线签名验证。

只有实际 HTTPS health/配置页验证成功才输出私有设置链接。
打开后完成口令、两步验证、附件存储、定期备份、连接电脑、完成；首台同样网页配对，不 provision/adopt。
私有链接里的 setupToken 不进入普通安装日志；不要分享终端中的该链接。

## 失败恢复与已有实例

原命令同值可重跑；配置、主密钥、卷、口令与证书保留，不自动升级已有实例。
改变站点/端口使用 `--configure`，保留 .env.previous；通行密钥身份已配置时更换域名需按迁移方案处理。
`--check` 校验已有配置；`--diagnose` 另验 HTTPS，不重启、不输出令牌；
`--print-setup-link` 重新显示入口，不重置口令。不要 down -v 或全机 prune。
镜像拉取/DNS/证书/端口错误有中文阶段提示，详细系统输出保留在部署目录的私有 install.log。

## 维护者交付

源码 install.sh + installer.py 用于开发；正式文件由 packaging/server/build_deployment.py 嵌入管理器生成，
生成脚本不会执行同目录不相干的 installer.py。prepare_context 也为候选包嵌入同一份管理器。
发布部署 ZIP 附 images.json；descriptor 使用 server-deployment.v1，与 server/stable.json 的源码发行格式隔离。
工具只生成文件，不自动上传。先核对当前源码、镜像 digest 和独立六步验收，再发布固定入口与频道。
普通用户收到已核验部署 ZIP，可解压后 sh ./install.sh --from-bundle；单独源码模板没有核验镜像材料，不能冒充交付包。

## 边界

单 Linux amd64 API + SQLite，本地磁盘数据卷；默认不安装 MinIO、不建云桶或开放控制台端口。
不覆盖现有 Docker daemon、防火墙、其他服务或共享标签；Docker context 必须为当前服务器 Unix socket。
公网证书由官方 Caddy 在用户主机申请，不进镜像；自带证书/已有反代可显式 --compose-override，仍验证 HTTPS。
合成 CA/S3 验收不能替代真实 DNS/ACME、云 provider 权限与灾备恢复。
