#!/usr/bin/env python3
"""Release packager — staging + manifest + smoke check.

Usage: python3 scripts/release.py [--out dist/]

Creates a clean release zip containing only production files,
with manifest.json and required-file checks.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ── Include / Exclude ─────────────────────────────────────────

ROOT_FILES = {
    "main.py", "bootstrap.py", "_conf_schema.json", "metadata.yaml",
    "requirements.txt", "README.md", "LICENSE", "logo.png",
}

INCLUDE_DIRS = {"core", "adapters", "ports", "commands", "webapi", "pages"}

EXCLUDE_DIRS = {
    "tests", "testing", "__pycache__", ".pytest_cache", ".ruff_cache",
    "docs", "docs-public", "tools", "scripts", "data",
    "node_modules", ".git", ".mypy_cache", ".mimosa",
}

INCLUDE_EXTS = {".py", ".js", ".mjs", ".ts", ".css", ".html", ".json", ".yaml", ".yml"}

REQUIRED_FILES = [
    "main.py", "bootstrap.py", "_conf_schema.json",
    "pages/storage-ng/index.html",
]

FORBIDDEN_PATTERNS = [
    "__pycache__", ".pytest_cache", ".db", ".db-wal", ".db-shm",
    "test_", "conftest.py",
]


def collect_files() -> list[Path]:
    """Collect files that should go into the release zip."""
    files = []

    # Root-level files
    for name in ROOT_FILES:
        p = ROOT / name
        if p.exists():
            files.append(p)

    # Include directories
    for d in INCLUDE_DIRS:
        dp = ROOT / d
        if not dp.exists():
            continue
        for f in dp.rglob("*"):
            if not f.is_file():
                continue
            # Check exclude dirs
            if any(ex in f.parts for ex in EXCLUDE_DIRS):
                continue
            if f.suffix in INCLUDE_EXTS or f.name in ("metadata.yaml",):
                files.append(f)

    return sorted(files)


def make_manifest(files: list[Path], staging: Path) -> dict:
    """Create manifest.json with file info."""
    entries = []
    for f in files:
        rel = str(f.relative_to(ROOT))
        dest = staging / rel
        if dest.exists():
            data = dest.read_bytes()
            entries.append({
                "path": rel,
                "size": dest.stat().st_size,
                "sha256": hashlib.sha256(data).hexdigest(),
            })
    return {"files": entries, "total_files": len(entries), "total_bytes": sum(e["size"] for e in entries)}


def smoke_check(staging: Path) -> list[str]:
    """Verify required files exist and forbidden patterns are absent."""
    errors = []

    # Required files
    for req in REQUIRED_FILES:
        if not (staging / req).exists():
            errors.append(f"MISSING required: {req}")

    # Forbidden patterns
    for f in staging.rglob("*"):
        if f.is_file():
            for pat in FORBIDDEN_PATTERNS:
                if pat in f.name:
                    errors.append(f"FORBIDDEN found: {f.relative_to(staging)}")

    return errors


def main():
    parser = argparse.ArgumentParser(description="Package release zip")
    parser.add_argument("--out", default=str(ROOT / "dist"), help="Output directory")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    staging = Path(tempfile.mkdtemp(prefix="release-staging-"))
    try:
        files = collect_files()
        print(f"Collecting {len(files)} files...")

        # Copy to staging
        for f in files:
            rel = f.relative_to(ROOT)
            dest = staging / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)

        # Smoke check
        errors = smoke_check(staging)
        if errors:
            for e in errors:
                print(f"  ERROR: {e}", file=sys.stderr)
            sys.exit(1)

        # Manifest
        manifest = make_manifest(files, staging)
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))

        # Zip
        zip_name = out_dir / "astrbot_plugin_group_cloud_storage.zip"
        with zipfile.ZipFile(zip_name, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in staging.rglob("*"):
                if f.is_file():
                    zf.write(f, f.relative_to(staging))

        print(f"Release zip: {zip_name} ({zip_name.stat().st_size:,} bytes)")
        print(f"Manifest: {manifest['total_files']} files, {manifest['total_bytes']:,} bytes")
        print("Smoke check: PASSED")

    finally:
        shutil.rmtree(staging, ignore_errors=True)


if __name__ == "__main__":
    main()
