#!/usr/bin/env bash
# Build a release artifact locally. This script deliberately does not publish
# to GitHub or any other external service.
#
# Usage:
#   ./scripts/release.sh [OUTPUT_DIR]
#
# The packager stages a clean file set, writes manifest.json, validates required
# and forbidden paths, and emits a single zip under OUTPUT_DIR (default: dist).
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
OUT_DIR="${1:-${ROOT}/dist}"

command -v "$PYTHON" >/dev/null 2>&1 || {
  printf 'error: Python interpreter not found: %s\n' "$PYTHON" >&2
  exit 127
}

cd "$ROOT"
"$PYTHON" scripts/release.py --out "$OUT_DIR"

artifact="$OUT_DIR/astrbot_plugin_group_cloud_storage.zip"
[[ -f "$artifact" ]] || { printf 'error: artifact was not created: %s\n' "$artifact" >&2; exit 1; }

# Re-check the archive boundary, independently of the staging implementation.
"$PYTHON" - "$artifact" <<'PY'
import json
import sys
import zipfile

archive = sys.argv[1]
required = {
    "main.py", "bootstrap.py", "_conf_schema.json", "metadata.yaml",
    "pages/storage-ng/index.html", "manifest.json",
}
forbidden = ("__pycache__", ".pytest_cache", ".ruff_cache", ".db", ".db-wal",
             ".db-shm", "conftest.py", "test_")
with zipfile.ZipFile(archive) as zf:
    names = set(zf.namelist())
    missing = sorted(required - names)
    bad = sorted(n for n in names if any(part in n for part in forbidden))
    if missing or bad:
        if missing:
            print("ERROR missing required: " + ", ".join(missing), file=sys.stderr)
        if bad:
            print("ERROR forbidden paths: " + ", ".join(bad), file=sys.stderr)
        raise SystemExit(1)
    manifest = json.loads(zf.read("manifest.json"))
    listed = {entry["path"] for entry in manifest["files"]}
    if not listed <= names:
        print("ERROR manifest lists files absent from archive", file=sys.stderr)
        raise SystemExit(1)
print("Archive boundary check: PASSED")
PY
printf 'Local release build complete: %s\n' "$artifact"
