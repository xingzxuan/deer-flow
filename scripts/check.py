#!/usr/bin/env python3
"""Cross-platform dependency checker for DeerFlow."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse


def configure_stdio() -> None:
    """Prefer UTF-8 output so Unicode status markers render on Windows."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                continue


def run_command(command: list[str]) -> str | None:
    """Run a command and return trimmed stdout, or None on failure."""
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=True, shell=False)
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip() or result.stderr.strip()


def find_pnpm_command() -> list[str] | None:
    """Return a pnpm-compatible command that exists on this machine."""
    pnpm_path = shutil.which("pnpm")
    if pnpm_path:
        return [str(Path(pnpm_path))]

    pnpm_cmd_path = shutil.which("pnpm.cmd")
    if pnpm_cmd_path:
        return [str(Path(pnpm_cmd_path))]

    corepack_path = shutil.which("corepack")
    if not corepack_path:
        corepack_path = shutil.which("corepack.cmd")
    if corepack_path:
        return [str(Path(corepack_path)), "pnpm"]
    return None


def parse_node_major(version_text: str) -> int | None:
    version = version_text.strip()
    if version.startswith("v"):
        version = version[1:]
    major_str = version.split(".", 1)[0]
    if not major_str.isdigit():
        return None
    return int(major_str)


def check_postgres_preflight() -> tuple[bool, str | None]:
    """If config.yaml selects postgres, verify the URL is set + host:port reachable.

    Returns ``(ok, message)``. Skips silently when:
      - config.yaml does not exist (user hasn't run 'make setup' yet)
      - database.backend is not 'postgres' (sqlite/memory don't need preflight)
      - DATABASE_URL env var is not set (user hasn't filled .env yet)

    Fails when database.backend is 'postgres' AND DATABASE_URL parses but the
    host:port socket cannot be opened in 3 seconds.
    """
    repo_root = Path(__file__).resolve().parent.parent
    config_path = repo_root / "config.yaml"
    if not config_path.exists():
        return True, None  # No config yet — let setup_wizard handle it

    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        return True, "PyYAML not installed; skipping Postgres preflight"

    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        return False, f"Failed to parse config.yaml: {exc}"

    db = data.get("database") or {}
    backend = (db.get("backend") or "sqlite").lower()
    if backend != "postgres":
        return True, None  # not on PG, no preflight needed

    raw_url = db.get("postgres_url") or db.get("url") or ""
    if isinstance(raw_url, str) and raw_url.startswith("$"):
        env_name = raw_url[1:]
        # Load .env if available so this preflight matches what serve.sh sees
        env_path = repo_root / ".env"
        if env_path.exists():
            try:
                from dotenv import load_dotenv  # type: ignore[import-not-found]

                load_dotenv(env_path, override=False)
            except ImportError:
                pass
        url = os.environ.get(env_name, "")
        if not url:
            return False, f"database.backend=postgres but {env_name} is not set in .env"
    else:
        url = raw_url

    if not url:
        return False, "database.backend=postgres but no postgres_url is configured"

    parsed = urlparse(url.replace("postgresql+asyncpg://", "postgresql://"))
    host = parsed.hostname
    port = parsed.port or 5432
    if not host:
        return False, f"Cannot parse host from DATABASE_URL: {url[:60]}..."

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(3)
    try:
        sock.connect((host, port))
        return True, f"Postgres reachable at {host}:{port}"
    except OSError as exc:
        return (
            False,
            f"Postgres unreachable at {host}:{port} ({exc}). "
            f"Run 'docker compose -f docker/docker-compose-dev.yaml up -d postgres' "
            f"or check DATABASE_URL.",
        )
    finally:
        sock.close()


def main() -> int:
    configure_stdio()
    print("==========================================")
    print("  Checking Required Dependencies")
    print("==========================================")
    print()

    failed = False

    print("Checking Node.js...")
    node_path = shutil.which("node")
    if node_path:
        node_version = run_command(["node", "-v"])
        if node_version:
            major = parse_node_major(node_version)
            if major is not None and major >= 22:
                print(f"  OK Node.js {node_version.lstrip('v')} (>= 22 required)")
            else:
                print(
                    f"  FAIL Node.js {node_version.lstrip('v')} found, but version 22+ is required"
                )
                print("    Install from: https://nodejs.org/")
                failed = True
        else:
            print("  INFO Unable to determine Node.js version")
            print("    Install from: https://nodejs.org/")
            failed = True
    else:
        print("  FAIL Node.js not found (version 22+ required)")
        print("    Install from: https://nodejs.org/")
        failed = True

    print()
    print("Checking pnpm...")
    pnpm_command = find_pnpm_command()
    if pnpm_command:
        pnpm_version = run_command([*pnpm_command, "-v"])
        if pnpm_version:
            if Path(pnpm_command[0]).stem.lower() == "corepack":
                print(f"  OK pnpm {pnpm_version} (via Corepack)")
            else:
                print(f"  OK pnpm {pnpm_version}")
        else:
            print("  INFO Unable to determine pnpm version")
            failed = True
    else:
        print("  FAIL pnpm not found")
        print("    Install: npm install -g pnpm")
        print("    Or enable Corepack: corepack enable")
        print("    Or visit: https://pnpm.io/installation")
        failed = True

    print()
    print("Checking uv...")
    if shutil.which("uv"):
        uv_version_text = run_command(["uv", "--version"])
        if uv_version_text:
            uv_version_parts = uv_version_text.split()
            uv_version = uv_version_parts[1] if len(uv_version_parts) > 1 else uv_version_text
            print(f"  OK uv {uv_version}")
        else:
            print("  INFO Unable to determine uv version")
            failed = True
    else:
        print("  FAIL uv not found")
        print("    Visit the official installation guide for your platform:")
        print("    https://docs.astral.sh/uv/getting-started/installation/")
        failed = True

    print()
    print("Checking nginx...")
    if shutil.which("nginx"):
        nginx_version_text = run_command(["nginx", "-v"])
        if nginx_version_text and "/" in nginx_version_text:
            nginx_version = nginx_version_text.split("/", 1)[1]
            print(f"  OK nginx {nginx_version}")
        else:
            print("  INFO nginx (version unknown)")
    else:
        print("  FAIL nginx not found")
        print("    macOS:   brew install nginx")
        print("    Ubuntu:  sudo apt install nginx")
        print("    Windows: use WSL for local mode or use Docker mode")
        print("    Or visit: https://nginx.org/en/download.html")
        failed = True

    print()
    print("Checking Postgres preflight...")
    pg_ok, pg_msg = check_postgres_preflight()
    if pg_ok:
        if pg_msg:
            print(f"  OK {pg_msg}")
        else:
            print("  -- skipped (config.yaml missing or backend != postgres)")
    else:
        print(f"  FAIL {pg_msg}")
        failed = True

    print()
    if not failed:
        print("==========================================")
        print("  OK All dependencies are installed!")
        print("==========================================")
        print()
        print("You can now run:")
        print("  make install  - Install project dependencies")
        print("  make setup    - Create a minimal working config (recommended)")
        print("  make config   - Copy the full config template (manual setup)")
        print("  make doctor   - Verify config and dependency health")
        print("  make dev      - Start development server")
        print("  make start    - Start production server")
        return 0

    print("==========================================")
    print("  FAIL Some dependencies are missing")
    print("==========================================")
    print()
    print("Please install the missing tools and run 'make check' again.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
