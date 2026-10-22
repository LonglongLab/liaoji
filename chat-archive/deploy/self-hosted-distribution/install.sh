#!/bin/sh
# Source entry; release tooling embeds the exact manager into the published script.
set -eu
umask 077
bundle_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
case "$(uname -s):$(uname -m)" in
    Linux:x86_64|Linux:amd64) ;;
    *) printf '%s\n' '此部署入口仅支持 Linux amd64；Windows 请使用桌面安装包。' >&2; exit 2 ;;
esac
help=no
approved=no
for argument do
    case "$argument" in --help|-h) help=yes ;; --install-dependencies) approved=yes ;; esac
done
if [ "$help" = no ]; then
        if [ "$(id -u)" != 0 ]; then
            command -v sudo >/dev/null 2>&1 || {
                printf '%s\n' '请在服务器管理员终端运行；当前用户没有 sudo。' >&2; exit 2;
            }
            printf '%s\n' '部署和安装系统依赖需要管理员权限，将由 sudo 请求授权。'
            exec sudo -- sh "$0" "$@"
        fi
fi
if ! command -v python3 >/dev/null 2>&1; then
    if [ "$approved" = no ]; then
        printf '%s\n' '需要 Python 3 执行安装检查。是否安装系统 Python 与 CA 证书？输入 y 同意：' >/dev/tty
        IFS= read -r answer </dev/tty || exit 2
        [ "$answer" != y ] || approved=yes
    fi
    [ "$approved" = yes ] || { printf '%s\n' '未安装依赖，未修改系统。' >&2; exit 2; }
    [ "$(id -u)" = 0 ] && command -v apt-get >/dev/null 2>&1 || {
        printf '%s\n' '请使用管理员权限在 Debian/Ubuntu 安装 Python 3 后重跑。' >&2; exit 2;
    }
    printf '%s\n' '正在安装系统 Python 与 CA 证书……'
    apt-get update && apt-get install -y python3 ca-certificates
fi
payload='__LIAOJI_MANAGER_BASE64__'
if [ "$payload" = '__LIAOJI_MANAGER_BASE64__' ]; then
    [ -f "$bundle_dir/installer.py" ] || {
        printf '%s\n' '这是源码入口，请保留同目录 installer.py；正式下载入口由发布工具生成。' >&2; exit 2;
    }
    exec python3 "$bundle_dir/installer.py" --bundle-dir "$bundle_dir" "$@"
fi
temporary=$(mktemp -d)
trap 'rm -rf "$temporary"' EXIT
trap 'exit 130' HUP INT TERM
printf '%s' "$payload" | base64 -d > "$temporary/installer.py"
python3 "$temporary/installer.py" --bundle-dir "$bundle_dir" "$@"
