"""Chinese self-hosted deployment manager. Standard library only; never sources .env."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import re
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

CHANNEL = "https://releases.example.invalid/server/deployment/stable.json"
OFFICIAL_CADDY = (
    "caddy:2.10.0-alpine@sha256:f43d8810c944e27e6f7ea80b165de11dbdab16cff15c8a80d83ae3e1a109a35d"
)
BUNDLE_FILES = {
    "compose.yaml",
    "Caddyfile",
    ".env.example",
    "README.md",
    "initialize-api-data.sh",
    "install.sh",
    "images.json",
}
MAX_BUNDLE = 2 * 1024 * 1024
IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[0-9a-f]{64}")


class InstallError(RuntimeError):
    """A user-readable failure. Never contains credentials or application logs."""


def checked_site(value: str) -> str:
    value = value.strip().lower()
    labels = value.split(".")
    if (
        len(value) > 253
        or len(labels) < 2
        or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", item) for item in labels)
    ):
        raise InstallError(
            "网站域名格式不正确。请填完整域名，例如 liaoji.example.com，不含 https://、路径或端口。"
        )
    return value


def port(value: str | int) -> int:
    try:
        result = int(value)
    except (ValueError, TypeError):
        raise InstallError("端口必须是 1–65535 的整数。") from None
    if not 1 <= result <= 65535:
        raise InstallError("端口必须是 1–65535 的整数。")
    return result


def site_origin(site: str, https_port: str | int) -> str:
    host, number = checked_site(site), port(https_port)
    return "https://" + host + ("" if number == 443 else ":" + str(number))


def load_env(path: Path) -> dict[str, str]:
    if path.is_symlink():
        raise InstallError("配置文件是链接，停止操作并保留现状。")
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        key, sep, value = text.partition("=")
        if not sep or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in values:
            raise InstallError(".env 格式不正确或含重复项，请保留文件并按手册修复。")
        if not (
            key.startswith("CHAT_ARCHIVE_")
            or key
            in {
                "SERVER_IMAGE",
                "DATA_INIT_IMAGE",
                "CADDY_IMAGE",
                "COMPOSE_PROJECT_NAME",
            }
        ):
            raise InstallError(".env 含非部署配置变量，停止以避免影响系统命令环境。")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if any(c in value for c in ("$", "`", "\x00", "\n", "\r")):
            raise InstallError(".env 仅接受配置值，不执行命令或变量展开。")
        values[key] = value
    return values


def configured_site(values: dict[str, str]) -> str:
    return values.get("CHAT_ARCHIVE_SITE") or "archive." + values.get("CHAT_ARCHIVE_DOMAIN", "")


def merge_config(old: dict[str, str], supplied: dict[str, object]) -> dict[str, str]:
    pairs = {
        "site": configured_site(old),
        "email": old.get("CHAT_ARCHIVE_ACME_EMAIL"),
        "server_image": old.get("SERVER_IMAGE"),
        "data_init_image": old.get("DATA_INIT_IMAGE"),
        "https_port": old.get("CHAT_ARCHIVE_HTTPS_PORT", "8443"),
        "http_port": old.get("CHAT_ARCHIVE_HTTP_PORT", "80"),
        "api_port": old.get("CHAT_ARCHIVE_API_PORT", "8080"),
    }
    for key, value in supplied.items():
        if value is not None and str(value) != str(pairs[key]):
            raise InstallError(
                "已有配置已保留，未用新参数覆盖。相同参数可重试；修改网站/端口请运行 --configure。"
            )
    return old.copy()


def valid_url(value: str) -> urllib.parse.SplitResult:
    try:
        parsed = urllib.parse.urlsplit(value)
        parsed.port
    except ValueError:
        raise InstallError("HTTPS 地址格式或端口不正确。") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise InstallError("下载地址必须为不含账号、查询参数的 HTTPS 地址。")
    return parsed


def validate_release(value: object, channel: str) -> dict:
    try:
        if not isinstance(value, dict) or value["schema"] != "chat-archive.server-deployment.v1":
            raise ValueError()
        if value["platform"] != "linux/amd64" or not re.fullmatch(
            r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?", value["version"]
        ):
            raise ValueError()
        images = value["images"]
        if set(images) != {"runtime", "data-init", "caddy"} or not all(
            IMAGE.fullmatch(v) for v in images.values()
        ):
            raise ValueError()
        bundle = value["bundle"]
        source, target = valid_url(channel), valid_url(bundle["url"])
        if (source.hostname, source.port) != (target.hostname, target.port):
            raise ValueError()
        if (
            type(bundle["size"]) is not int
            or not 1 <= bundle["size"] <= MAX_BUNDLE
            or not re.fullmatch(r"[a-f0-9]{64}", bundle["sha256"])
        ):
            raise ValueError()
    except (KeyError, ValueError, TypeError):
        raise InstallError("部署清单不完整、不匹配本平台或引用可变镜像，停止安装。") from None
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise InstallError("下载发生重定向，停止并请维护者核对正式地址。")


def fetch(url: str, maximum: int) -> bytes:
    valid_url(url)
    try:
        opener = urllib.request.build_opener(NoRedirect())
        with opener.open(
            urllib.request.Request(url, headers={"User-Agent": "Liaoji-Installer/1"}),
            timeout=30,
        ) as response:
            data = response.read(maximum + 1)
        if len(data) > maximum:
            raise InstallError("下载内容超过允许大小，停止安装。")
        return data
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise InstallError(
                "当前地址尚未发布部署包（404）。请使用维护者已交付的候选包或等待正式发行。"
            ) from None
        raise InstallError(
            f"下载失败（HTTP {error.code}），现有配置和数据已保留，可原样重试。"
        ) from None
    except urllib.error.URLError as error:
        if isinstance(error.reason, ssl.SSLCertVerificationError):
            raise InstallError(
                "HTTPS 证书不可信或与域名不匹配；请检查证书和系统时间，不要跳过校验。"
            ) from None
        raise InstallError(
            "无法通过 HTTPS 下载。请检查网络、DNS 和系统时间/证书，然后原样重试。"
        ) from None


def extract_bundle(data: bytes, root: Path, descriptor: dict, images: dict | None = None) -> None:
    if len(data) != descriptor["size"] or hashlib.sha256(data).hexdigest() != descriptor["sha256"]:
        raise InstallError("部署包大小或 SHA-256 不符，未写入文件。")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            rows = archive.infolist()
            names = [row.filename for row in rows]
            if set(names) != BUNDLE_FILES or len(names) != len(BUNDLE_FILES):
                raise ValueError()
            contents = {}
            for row in rows:
                if (
                    row.is_dir()
                    or stat.S_ISLNK(row.external_attr >> 16)
                    or row.file_size > 1024 * 1024
                ):
                    raise ValueError()
                contents[row.filename] = archive.read(row)
            metadata = json.loads(contents["images.json"])
            if (
                not isinstance(metadata, dict)
                or metadata.get("schema") != "chat-archive.deployment-images.v1"
                or set(metadata["images"]) != {"runtime", "data-init", "caddy"}
            ):
                raise ValueError()
            if not all(IMAGE.fullmatch(ref) for ref in metadata["images"].values()) or (
                images is not None and images != metadata["images"]
            ):
                raise ValueError()
    except (zipfile.BadZipFile, ValueError, RuntimeError, KeyError, TypeError):
        raise InstallError("部署包文件清单或路径不合法，未写入文件。") from None
    for name in names:
        if (root / name).is_symlink():
            raise InstallError("部署目录含链接，保留现状并停止。")
    for name, payload in contents.items():
        (root / name).write_bytes(payload)


def docker_install_plan(system: dict[str, str], architecture: str) -> tuple[list[list[str]], str]:
    distro = system.get("ID", "")
    codename = system.get("UBUNTU_CODENAME") or system.get("VERSION_CODENAME", "")
    if (
        distro not in {"ubuntu", "debian"}
        or architecture != "amd64"
        or not re.fullmatch("[a-z0-9]+", codename)
    ):
        raise InstallError(
            "此系统暂不支持自动安装 Docker。请按 Docker 官方说明安装 Engine 与 Compose plugin 后重跑。"
        )
    source = (
        "Types: deb\nURIs: https://download.docker.com/linux/"
        + distro
        + "\nSuites: "
        + codename
        + "\nComponents: stable\nArchitectures: amd64\nSigned-By: /etc/apt/keyrings/liaoji-docker.asc\n"
    )
    return [
        ["apt-get", "update"],
        ["apt-get", "install", "-y", "ca-certificates", "curl"],
        ["apt-get", "update"],
        [
            "apt-get",
            "install",
            "-y",
            "docker-ce",
            "docker-ce-cli",
            "containerd.io",
            "docker-buildx-plugin",
            "docker-compose-plugin",
        ],
    ], source


def tty_input(message: str, default: str = "") -> str:
    try:
        with open("/dev/tty", "w", encoding="utf-8") as output:
            output.write(message + (f" [{default}]" if default else "") + "：")
            output.flush()
        with open("/dev/tty", "r", encoding="utf-8") as console:
            value = console.readline()
            if not value:
                raise OSError()
            return value.strip() or default
    except OSError:
        raise InstallError(
            "没有交互终端，请显式提供 --site、--email 等参数；安装依赖另加 --install-dependencies。"
        ) from None


def can_prompt() -> bool:
    try:
        with open("/dev/tty") as console:
            return console.isatty()
    except OSError:
        return False


class Installer:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        local = Path(args.bundle_dir)
        recognized_bundle = (local / "compose.yaml").is_file() and (
            getattr(args, "from_bundle", False)
            or Path(__file__).parent.absolute() == local.absolute()
        )
        self.root = (
            Path(args.install_dir)
            if args.install_dir
            else local
            if recognized_bundle
            else Path("/opt/liaoji-server")
        )
        self.root = self.root.absolute()
        if self.root.resolve() != self.root or self.root.is_symlink():
            raise InstallError("安装目录不能是链接，请选择独立真实目录。")
        self.project = ""
        self.env: dict[str, str] = {}
        self.lock = None

    def run(self, command: list[str], *, capture: bool = False, optional: bool = False) -> str:
        if not capture and (self.root / "install.log").is_symlink():
            raise InstallError("安装日志路径是链接，未执行命令或修改目标文件。")
        result = subprocess.run(
            command,
            cwd=self.root,
            env=self.command_env(),
            capture_output=True,
            text=True,
        )
        if not capture:
            with (self.root / "install.log").open("a", encoding="utf-8") as log:
                log.write("[" + command[0] + "]\n" + result.stdout + result.stderr + "\n")
        if result.returncode and not optional:
            if "pull" in command:
                raise InstallError(
                    "镜像拉取失败。请检查网络及镜像访问权限，排除后原样重跑；现有配置/数据已保留，详细输出在 install.log。"
                )
            raise InstallError(
                "命令执行失败，现有配置/数据已保留。详细信息在安装目录 install.log；排除问题后原样重跑。"
            )
        return result.stdout.strip() if result.returncode == 0 else ""

    def command_env(self) -> dict[str, str]:
        # Existing shell values must not silently override a private .env.
        result = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("CHAT_ARCHIVE_")
            and k
            not in {
                "SERVER_IMAGE",
                "DATA_INIT_IMAGE",
                "CADDY_IMAGE",
                "COMPOSE_PROJECT_NAME",
                "COMPOSE_FILE",
                "COMPOSE_PROFILES",
            }
        }
        result.update(self.env)
        if self.project:
            result["COMPOSE_PROJECT_NAME"] = self.project
        return result

    def compose(self, *arguments: str, capture: bool = False, optional: bool = False) -> str:
        files = ["-f", str(self.root / "compose.yaml")]
        if self.args.compose_override:
            files += ["-f", str(Path(self.args.compose_override).absolute())]
        return self.run(
            [
                "docker",
                "compose",
                "--project-name",
                self.project,
                "--env-file",
                str(self.root / ".env"),
                *files,
                *arguments,
            ],
            capture=capture,
            optional=optional,
        )

    def dependencies(self) -> None:
        docker = shutil.which("docker")
        if os.environ.get("DOCKER_HOST", "unix://").split(":", 1)[0] != "unix":
            raise InstallError(
                "此入口只部署到当前服务器，请先取消远程 DOCKER_HOST；未改动远程服务。"
            )
        ready = (
            docker
            and subprocess.run([docker, "compose", "version"], capture_output=True).returncode == 0
        )
        if not ready:
            approved = (
                self.args.install_dependencies
                or tty_input(
                    "需要安装 Docker Engine/Compose，会安装官方系统包。是否继续？输入 y 同意",
                    "n",
                ).lower()
                == "y"
            )
            if not approved:
                raise InstallError("尚未安装依赖，未修改系统。安装 Docker/Compose 后重跑即可。")
            self.args.install_dependencies = True
            if os.geteuid() != 0:
                raise InstallError("安装依赖需要管理员权限，请使用 sudo 重新运行安装入口。")
            system = {}
            for line in Path("/etc/os-release").read_text().splitlines():
                key, sep, value = line.partition("=")
                if sep:
                    system[key] = value.strip('"')
            commands, source = docker_install_plan(system, "amd64")
            if docker:
                raise InstallError(
                    "已有 Docker 但缺 Compose。为保留现有引擎，不自动替换；请安装 docker-compose-plugin 后重跑。"
                )
            conflicts = subprocess.run(
                [
                    "dpkg-query",
                    "-W",
                    "-f=${db:Status-Status}\n",
                    "docker.io",
                    "podman-docker",
                    "containerd",
                    "runc",
                ],
                capture_output=True,
                text=True,
            )
            if "installed" in conflicts.stdout.splitlines():
                raise InstallError(
                    "发现现有容器运行时，未卸载或替换。请先按 Docker 官方兼容说明处理，再重跑。"
                )
            print("正在安装官方 Docker 软件包，请稍候……", flush=True)
            self.run(commands[0])
            self.run(commands[1])
            keyring = Path("/etc/apt/keyrings")
            keyring.mkdir(mode=0o755, exist_ok=True)
            keyfile = keyring / "liaoji-docker.asc"
            keyfile.write_bytes(
                fetch("https://download.docker.com/linux/" + system["ID"] + "/gpg", 65536)
            )
            keyfile.chmod(0o644)
            sourcefile = Path("/etc/apt/sources.list.d/liaoji-docker.sources")
            if sourcefile.exists() and sourcefile.read_text() != source:
                raise InstallError("同名软件源配置已存在且不同，未覆盖。请核对后重跑。")
            sourcefile.write_text(source)
            sourcefile.chmod(0o644)
            self.run(commands[2])
            self.run(commands[3])
        compose_version = subprocess.run(
            ["docker", "compose", "version", "--short"], capture_output=True, text=True
        )
        found_version = re.search(r"(\d+)\.(\d+)", compose_version.stdout)
        if (
            compose_version.returncode
            or found_version is None
            or tuple(map(int, found_version.groups())) < (2, 20)
        ):
            raise InstallError(
                "需要 Docker Compose plugin 2.20 或更新版本；请升级插件后重跑，不必替换现有引擎或数据。"
            )
        if subprocess.run(["docker", "info"], capture_output=True).returncode:
            if (
                self.args.install_dependencies
                and Path("/run/systemd/system").exists()
                and os.geteuid() == 0
            ):
                self.run(["systemctl", "enable", "--now", "docker"])
            if subprocess.run(["docker", "info"], capture_output=True).returncode:
                raise InstallError(
                    "无法连接 Docker 服务。检查服务是否启动和当前用户权限；现有配置未清空。"
                )
        endpoint = subprocess.run(
            ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
            capture_output=True,
            text=True,
        )
        if endpoint.returncode or not endpoint.stdout.strip().startswith("unix://"):
            raise InstallError(
                "当前 Docker context 不是本机 Unix socket，请切回本机 context 后重跑。"
            )
        print("[通过] Docker、Compose 与服务连接", flush=True)

    def material(self, existing: bool) -> dict[str, str]:
        manifest = self.root / "deployment-release.json"
        if existing or (self.root / "compose.yaml").exists():
            if not (self.root / "compose.yaml").is_file():
                raise InstallError(
                    "已有 .env 但部署文件不完整，保留现状；请恢复同版本部署包，不自动换版本。"
                )
            if manifest.is_symlink():
                raise InstallError("部署清单不能是链接。")
            if not manifest.exists():
                images_file = self.root / "images.json"
                if not images_file.exists():
                    return {}
                if images_file.is_symlink():
                    raise InstallError("镜像清单不能是链接。")
                try:
                    doc = json.loads(images_file.read_text())
                    images = doc["images"]
                    if (
                        doc["schema"] != "chat-archive.deployment-images.v1"
                        or set(images) != {"runtime", "data-init", "caddy"}
                        or not all(IMAGE.fullmatch(ref) for ref in images.values())
                    ):
                        raise ValueError()
                    return images
                except (KeyError, ValueError, TypeError):
                    raise InstallError("本地镜像清单无效，请取得同版本的完整部署包。") from None
            try:
                doc = json.loads(manifest.read_text())
                if not isinstance(doc, dict):
                    raise ValueError()
                return validate_release(doc, doc.get("source_manifest_url", doc["bundle"]["url"]))[
                    "images"
                ]
            except (ValueError, KeyError, TypeError):
                raise InstallError("已保存的部署清单无效，保留配置并停止。") from None
        print("正在取得经过 HTTPS 和摘要校验的部署材料……", flush=True)
        try:
            release = validate_release(
                json.loads(fetch(self.args.manifest_url, 131072)),
                self.args.manifest_url,
            )
        except json.JSONDecodeError:
            raise InstallError("部署清单不是有效 JSON，停止安装。") from None
        package = fetch(release["bundle"]["url"], MAX_BUNDLE)
        extract_bundle(package, self.root, release["bundle"], release["images"])
        release["source_manifest_url"] = self.args.manifest_url
        manifest.write_text(json.dumps(release, ensure_ascii=False, indent=2), encoding="utf-8")
        return release["images"]

    def settings(self, old: dict[str, str], images: dict[str, str]) -> dict[str, str]:
        supplied = {
            "site": self.args.site or ("archive." + self.args.domain if self.args.domain else None),
            "email": self.args.email,
            "server_image": self.args.server_image,
            "data_init_image": self.args.data_init_image,
            "https_port": self.args.https_port,
            "http_port": self.args.http_port,
            "api_port": self.args.api_port,
        }
        if supplied["site"] is not None:
            supplied["site"] = checked_site(str(supplied["site"]))
        if old and not self.args.configure:
            return merge_config(old, supplied)
        values = old.copy() if old else load_env(self.root / ".env.example")
        site = supplied["site"] or tty_input(
            "网站完整域名（例如 liaoji.example.com）",
            configured_site(old) if old else "",
        )
        email = supplied["email"] or tty_input(
            "证书联系邮箱", old.get("CHAT_ARCHIVE_ACME_EMAIL", "")
        )
        checked_site(str(site))
        if not re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", str(email)):
            raise InstallError("邮箱格式不正确，请重新填写。")
        if old and site != configured_site(old) and old.get("CHAT_ARCHIVE_WEBAUTHN_RP_ID"):
            raise InstallError("已设置通行密钥身份，改域名需先按迁移手册处理；原配置已保留。")
        values.update(
            CHAT_ARCHIVE_SITE=str(site),
            CHAT_ARCHIVE_DOMAIN=str(site).removeprefix("archive."),
            CHAT_ARCHIVE_ACME_EMAIL=str(email),
        )
        for flag, key, default in [
            ("https_port", "CHAT_ARCHIVE_HTTPS_PORT", "8443"),
            ("http_port", "CHAT_ARCHIVE_HTTP_PORT", "80"),
            ("api_port", "CHAT_ARCHIVE_API_PORT", "8080"),
        ]:
            values[key] = str(port(supplied[flag] or values.get(key) or default))
        values["SERVER_IMAGE"] = str(
            supplied["server_image"] or values.get("SERVER_IMAGE") or images.get("runtime", "")
        )
        values["DATA_INIT_IMAGE"] = str(
            supplied["data_init_image"]
            or values.get("DATA_INIT_IMAGE")
            or images.get("data-init", "")
        )
        values["CADDY_IMAGE"] = images.get("caddy") or values.get("CADDY_IMAGE") or OFFICIAL_CADDY
        if not all(
            IMAGE.fullmatch(values[k]) for k in ("SERVER_IMAGE", "DATA_INIT_IMAGE", "CADDY_IMAGE")
        ):
            raise InstallError(
                "部署包没有提供经核验的镜像摘要，请维护者提供完整部署清单；不用自行填写镜像名。"
            )
        return values

    def preflight(self) -> None:
        site = configured_site(self.env)
        checked_site(site)
        try:
            socket.getaddrinfo(site, None, type=socket.SOCK_STREAM)
        except socket.gaierror:
            raise InstallError(
                "域名尚不能解析。请在 DNS 中给此完整域名添加记录，等待生效后重跑。"
            ) from None
        owned = set()
        ids = self.run(
            [
                "docker",
                "ps",
                "-q",
                "--filter",
                "label=com.docker.compose.project=" + self.project,
            ],
            capture=True,
            optional=True,
        )
        if ids:
            try:
                format_arg = '{"directory":{{json (index .Config.Labels "com.docker.compose.project.working_dir")}},"ports":{{json .NetworkSettings.Ports}}}'
                details = self.run(
                    ["docker", "inspect", "--format", format_arg, *ids.split()],
                    capture=True,
                )
                for line in details.splitlines():
                    container = json.loads(line)
                    if (
                        container.get("directory")
                        and Path(container["directory"]).absolute() != self.root
                    ):
                        raise InstallError(
                            "相同项目名已由另一部署目录使用，未修改该服务；请为新实例使用独立目录/项目名。"
                        )
                    for bindings in container.get("ports", {}).values():
                        owned.update(int(b["HostPort"]) for b in bindings or [])
            except (ValueError, KeyError, TypeError):
                raise InstallError("无法核对本实例的端口归属，停止避免影响其他服务。") from None
        numbers = [
            port(self.env.get(k, v))
            for k, v in [
                ("CHAT_ARCHIVE_HTTP_PORT", "80"),
                ("CHAT_ARCHIVE_HTTPS_PORT", "8443"),
                ("CHAT_ARCHIVE_API_PORT", "8080"),
            ]
        ]
        if len(set(numbers)) != 3:
            raise InstallError("HTTP、HTTPS、API 端口不能相同，请重新选择。")
        for index, number in enumerate(numbers):
            if number in owned:
                continue
            for attempt in range(4):
                with socket.socket() as probe:
                    try:
                        probe.bind(("127.0.0.1" if index == 2 else "0.0.0.0", number))
                        break
                    except OSError:
                        pass
                if not can_prompt() or attempt == 3:
                    raise InstallError(
                        f"端口 {number} 已被其他服务占用，未停止该服务。请用 --http-port/--https-port/--api-port 选择可用端口；已有配置可加 --configure。"
                    ) from None
                if (self.root / ".env").exists() and not self.args.configure:
                    if (
                        tty_input(
                            "端口被其他服务占用。是否修改本实例端口并备份旧配置？y/n",
                            "n",
                        )
                        != "y"
                    ):
                        raise InstallError("原配置已保留，未停止或修改其他服务。")
                    self.args.configure = True
                label = ("HTTP", "HTTPS", "API")[index]
                number = port(tty_input(f"{label} 端口已占用，请填写替代端口"))
                if number in numbers:
                    raise InstallError("不同服务不能使用相同端口，请重新运行选择。")
                numbers[index] = number
                self.env[
                    (
                        "CHAT_ARCHIVE_HTTP_PORT",
                        "CHAT_ARCHIVE_HTTPS_PORT",
                        "CHAT_ARCHIVE_API_PORT",
                    )[index]
                ] = str(number)
                self.env["CHAT_ARCHIVE_PUBLIC_ORIGIN"] = site_origin(site, numbers[1])
        print("[通过] 域名解析与本机端口；公网入站还会在 HTTPS 验证时检查", flush=True)

    def save(self, exists: bool) -> None:
        path = self.root / ".env"
        if path.is_symlink():
            raise InstallError("配置路径是链接，未修改任何文件。")
        if exists and not self.args.configure:
            return
        text = "".join(key + "=" + value + "\n" for key, value in self.env.items())
        if exists:
            backup = self.root / ".env.previous"
            if backup.is_symlink():
                raise InstallError("配置备份路径是链接，停止并保留现状。")
            shutil.copyfile(path, backup)
            backup.chmod(0o600)
            with tempfile.NamedTemporaryFile(
                dir=self.root, prefix=".env.new-", delete=False
            ) as handle:
                handle.write(text.encode())
                temporary = Path(handle.name)
            os.replace(temporary, path)
        else:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(text)
        path.chmod(0o600)

    def verify_https(self) -> None:
        origin = self.env["CHAT_ARCHIVE_PUBLIC_ORIGIN"]
        last = "网络或服务尚未就绪"
        for _ in range(self.args.wait_seconds // 2 + 1):
            try:
                data = json.loads(fetch(origin + "/api/v1/health", 65536))
                html = fetch(origin + "/setup", 1024 * 1024)
                if (
                    isinstance(data, dict)
                    and data.get("status") == "ok"
                    and data.get("runtime_profile") == "self_hosted"
                    and b'id="app"' in html
                ):
                    print("[通过] HTTPS 证书、API 和配置页面", flush=True)
                    return
                last = "HTTPS 地址没有返回本应用的健康与配置页面"
            except (InstallError, ValueError):
                last = "HTTPS 验证失败：请检查 DNS 是否指向本机、公网 80/HTTPS 端口、安全组和证书；内部服务已保留"
            time.sleep(2)
        raise InstallError(last + "。排除问题后原样重跑，不需要删除数据。")

    def execute(self) -> None:
        if platform.system() != "Linux" or platform.machine() not in {
            "x86_64",
            "amd64",
        }:
            raise InstallError("此部署入口仅支持 Linux amd64；Windows 请使用桌面安装包。")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in BUNDLE_FILES | {
            ".env",
            ".env.previous",
            "deployment-release.json",
            ".install.lock",
            "install.log",
        }:
            if (self.root / name).is_symlink():
                raise InstallError("部署目录含配置/日志链接，停止并保留所有现状。")
        import fcntl

        lockpath = self.root / ".install.lock"
        if lockpath.is_symlink():
            raise InstallError("安装锁不能是链接。")
        self.lock = lockpath.open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise InstallError("另一个安装进程正在使用此目录，请等待它结束。") from None
        path = self.root / ".env"
        exists = path.exists()
        if (self.args.check or self.args.diagnose or self.args.print_setup_link) and not exists:
            raise InstallError("此目录尚无配置，请先运行安装入口，再检查或重新显示设置链接。")
        old = load_env(path) if exists else {}
        images = self.material(exists)
        self.env = self.settings(old, images)
        self.project = self.env.get("COMPOSE_PROJECT_NAME") or (
            self.root.name.lower().replace(".", "").replace(" ", "")
            if exists
            else "liaoji-" + hashlib.sha256(str(self.root).encode()).hexdigest()[:10]
        )
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", self.project):
            raise InstallError("项目名格式不正确，请选择新的独立部署目录。")
        self.env["COMPOSE_PROJECT_NAME"] = self.project
        self.env["CHAT_ARCHIVE_SITE"] = configured_site(self.env)
        self.env["CHAT_ARCHIVE_PUBLIC_ORIGIN"] = site_origin(
            self.env["CHAT_ARCHIVE_SITE"],
            self.env.get("CHAT_ARCHIVE_HTTPS_PORT", "8443"),
        )
        if not all(
            IMAGE.fullmatch(self.env.get(k, ""))
            for k in ("SERVER_IMAGE", "DATA_INIT_IMAGE", "CADDY_IMAGE")
        ):
            raise InstallError("已有镜像引用不是不可变摘要，保留配置并停止。")
        self.dependencies()
        if self.args.print_setup_link:
            self.verify_https()
        else:
            self.preflight()
            self.save(exists)
            self.compose("config", "--quiet")
            if self.args.check or self.args.diagnose:
                if self.args.diagnose:
                    self.verify_https()
                print("检查完成；未拉取镜像、未启动或重启服务。")
                return
            print("正在拉取已核验镜像并启动服务……", flush=True)
            self.compose("pull", "api", "data-init", "caddy")
            self.compose("up", "-d", "--wait", "api", "caddy")
            self.verify_https()
        token = self.compose(
            "exec",
            "-T",
            "api",
            "cat",
            "/var/lib/chat-archive/browser-bootstrap-token",
            capture=True,
            optional=True,
        )
        if not token:
            try:
                state = json.loads(
                    fetch(
                        self.env["CHAT_ARCHIVE_PUBLIC_ORIGIN"] + "/api/v1/capabilities",
                        131072,
                    )
                )
            except ValueError:
                state = None
            if not isinstance(state, dict):
                raise InstallError(
                    "无法确认服务的首次设置状态；服务与数据已保留，请稍后用 --print-setup-link 重试。"
                )
            if state.get("auth_state") == "setup_required":
                raise InstallError(
                    "服务尚待首次设置，但未能读取引导令牌；请检查容器权限后用 --print-setup-link 重试。"
                )
        if token and not re.fullmatch(r"[A-Za-z0-9._-]+", token):
            raise InstallError("引导令牌格式异常，未打印任何凭据。")
        link = (
            self.env["CHAT_ARCHIVE_PUBLIC_ORIGIN"]
            + "/setup"
            + ("#setupToken=" + token if token else "")
        )
        print(
            "\n安装完成。请在浏览器打开下方"
            + ("私有一次性配置链接（不要转发）：" if token else "地址，使用原口令继续：")
        )
        print(link)
        print("接下来按网页完成：口令 → 两步验证 → 附件存储 → 定期备份 → 连接电脑。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="聊记一键引导部署；现有配置、数据、证书始终保留")
    parser.add_argument("--bundle-dir", default=str(Path(__file__).parent))
    parser.add_argument("--from-bundle", action="store_true", help="使用已核验当前目录离线部署包")
    parser.add_argument("--install-dir")
    parser.add_argument("--manifest-url", default=CHANNEL)
    parser.add_argument("--site", help="完整站点域名，不含协议/端口")
    parser.add_argument("--domain", help="旧入口兼容：基础域名，自动加 archive 前缀")
    parser.add_argument("--email")
    parser.add_argument("--server-image")
    parser.add_argument("--data-init-image")
    for name in ("http-port", "https-port", "api-port"):
        parser.add_argument("--" + name, type=int)
    parser.add_argument(
        "--configure",
        action="store_true",
        help="显式修改站点/端口，保留旧 .env.previous",
    )
    parser.add_argument(
        "--install-dependencies",
        action="store_true",
        help="明确同意在支持系统上安装官方 Docker 依赖",
    )
    parser.add_argument(
        "--compose-override",
        help="高级：显式使用自有反代/证书叠加文件，不关闭 HTTPS 校验",
    )
    parser.add_argument("--wait-seconds", type=int, default=90)
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="检查配置及实际 HTTPS 页面，不重启服务，不输出令牌",
    )
    parser.add_argument("--print-setup-link", action="store_true")
    args = parser.parse_args(argv)
    instance = None
    try:
        if sys.version_info < (3, 10):
            raise InstallError(
                "此入口需要 Python 3.10 或更新版本；请使用 Ubuntu 22.04+/Debian 12+。"
            )
        if args.site and args.domain:
            raise InstallError("--site 与旧 --domain 不能同时使用。")
        if not 0 <= args.wait_seconds <= 600:
            raise InstallError("等待时间须为 0–600 秒。")
        instance = Installer(args)
        instance.execute()
        return 0
    except (InstallError, OSError, UnicodeError) as error:
        print(
            "\n未完成："
            + (
                str(error)
                if isinstance(error, InstallError)
                else "文件、权限或系统环境异常；原有数据已保留。"
            ),
            file=sys.stderr,
        )
        print(
            "重试：使用刚才相同的下载入口和参数；无需删除 .env 或数据卷。",
            file=sys.stderr,
        )
        return 2
    finally:
        if instance is not None and instance.lock is not None:
            instance.lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
