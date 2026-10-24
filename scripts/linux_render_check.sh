#!/usr/bin/env bash
# Developer check: run the document-renderer tests on Linux, in a Docker container (M6.2; ADR-108).
# Not part of verify.sh, CI or pre-commit. Usage: scripts/linux_render_check.sh
#
# The repository is mounted read-only; the Python environment and caches (pytest's cache is off,
# Hypothesis's lives in /tmp) are inside the container and vanish with it. The container drops
# every Linux capability and cannot gain privileges. Network access is needed only to pull the
# image, to let pip fetch the pinned uv and to let uv download the locked packages. No
# credentials, keychain, home folder or user data are mounted.
# Prints "LINUX RENDER CHECK: PASS" as its last line, or "... FAIL (...)" and a non-zero exit.
set -euo pipefail

IMAGE="${LINUX_RENDER_IMAGE:-python:3.11-slim-bookworm}"
UV_VERSION="0.11.28" # pinned; bump it together with a deliberate uv upgrade
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

fail() {
  echo "LINUX RENDER CHECK: FAIL ($1)"
  exit 1
}

command -v docker >/dev/null 2>&1 || fail "docker is not installed"
docker info >/dev/null 2>&1 || fail "the Docker daemon is not running"

# Runs inside the container: install uv, sync the locked environment, run the renderer suite.
INSIDE='set -euo pipefail
pip install --quiet --disable-pip-version-check --root-user-action=ignore "uv==$UV_VERSION"
uv sync --locked --quiet
uv run --no-sync python -m pytest -p no:cacheprovider -q tests/contract/test_document_renderer_contract.py tests/unit/render'

if docker run --rm \
  --volume "$ROOT:/work:ro" \
  --workdir /work \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --env UV_PROJECT_ENVIRONMENT=/tmp/venv \
  --env UV_CACHE_DIR=/tmp/uv-cache \
  --env PYTHONDONTWRITEBYTECODE=1 \
  --env HYPOTHESIS_STORAGE_DIRECTORY=/tmp/hypothesis \
  --env UV_VERSION="$UV_VERSION" \
  --env JOBPIPELINE_LIVE_TESTS= \
  "$IMAGE" bash -c "$INSIDE"; then
  echo "LINUX RENDER CHECK: PASS ($IMAGE)"
else
  fail "the renderer tests failed in $IMAGE"
fi
