"""Build this source distribution without a private repository or release key."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

ROOT = Path(__file__).resolve().parent


def verify() -> dict[str, Any]:
    manifest = json.loads((ROOT / "source-release.json").read_bytes())
    if manifest["schema"] != "chat-archive.source-release.v1":
        raise SystemExit("Unsupported source manifest")
    for name, expected in manifest["files"].items():
        path = ROOT / name
        if not path.resolve().is_relative_to(ROOT) or any(
            p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction())
            for p in (path, *path.parents) if p != ROOT
        ):
            raise SystemExit("Source path is not an ordinary file")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise SystemExit("Source differs from the release inventory; use --allow-modified after review")
    return manifest


def run(command: list[str], cwd: Path) -> None:
    # Only the user's own build environment is used, never publisher credentials.
    executable = shutil.which(command[0])
    if executable is None:
        raise SystemExit("Missing build tool: " + command[0])
    subprocess.run([executable, *command[1:]], cwd=cwd, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--allow-modified", action="store_true", help="Build your reviewed edits")
    parser.add_argument("--frontend-only", action="store_true")
    parser.add_argument("--backend-only", action="store_true", help="Build the Python wheel only")
    parser.add_argument("--workers", action="store_true", help="Build the separate Workers profile")
    parser.add_argument("--context-only", action="store_true", help="Prepare a self-hosted Docker context without running Docker")
    parser.add_argument("--repository", default="local/liaoji-server")
    args = parser.parse_args()
    manifest = (json.loads((ROOT / "source-release.json").read_bytes())
                if args.allow_modified else verify())
    if args.verify_only:
        print(json.dumps({"verified_files": len(manifest["files"]), "target": manifest["target"]}))
        return
    backend = ROOT / "chat-archive/backend"
    frontend = ROOT / "chat-archive/frontend"
    if args.workers:
        if manifest["target"] != "server":
            raise SystemExit("Workers is available in the server source package")
        workers = ROOT / "chat-archive/remote"
        run(["corepack", "pnpm", "install", "--frozen-lockfile"], workers)
        run(["corepack", "pnpm", "exec", "tsc", "-b"], workers)
        run(["corepack", "pnpm", "exec", "wrangler", "deploy", "--dry-run",
             "--outdir", str(ROOT / "build/workers")], workers)
        return
    if args.backend_only:
        run(["uv", "build", "--wheel", "--out-dir", str(ROOT / "build/wheels"), str(backend)], ROOT)
        return
    run(["corepack", "pnpm", "install", "--frozen-lockfile"], frontend)
    mode = "local" if manifest["target"] == "desktop" else "remote"
    run(["corepack", "pnpm", "build:" + mode], frontend)
    if args.frontend_only:
        return
    if manifest["target"] == "desktop":
        if os.name != "nt":
            raise SystemExit("The Windows installer must be built on Windows")
        run([sys.executable, str(ROOT / "chat-archive/packaging/build_release.py"),
             "--skip-frontend", "--no-source", "--no-prune", "--output", str(ROOT / "build/desktop")], ROOT)
        return
    # Recreate exactly the minimal layout expected by the audited distribution Dockerfile.
    context = ROOT / "build/server-context"
    if context.exists():
        raise SystemExit("build/server-context already exists; select a fresh extraction")
    context.mkdir(parents=True)
    shutil.copytree(backend / "src", context / "src")
    shutil.copytree(frontend / "dist", context / "frontend-dist")
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copy2(backend / name, context / name)
    deploy = ROOT / "chat-archive/deploy/self-hosted-distribution"
    for name in ("Dockerfile", "initialize-api-data.sh"):
        shutil.copy2(deploy / name, context / name)
    version = manifest["version"]
    (context / "release.json").write_text(json.dumps({"version": version,
        "base_revision": manifest["revision"], "source_sha256": manifest["input_sha256"]}) + "\n")
    (context / ".dockerignore").write_text("**/__pycache__\n**/*.pyc\n")
    if args.context_only:
        print("Self-hosted context prepared in build/server-context; no Docker command was run.")
        return
    for target, suffix in (("runtime", ""), ("data-init", "-data-init")):
        run(["docker", "build", "--target", target, "-t", args.repository + ":" + version + suffix,
             str(context)], ROOT)
    print("Images built locally; configure your own deployment. No upload or deployment was performed.")


if __name__ == "__main__":
    main()
