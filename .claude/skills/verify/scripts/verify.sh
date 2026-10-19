#!/usr/bin/env bash
# Compatibility entry point. Both agents use the same verification recipe.
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../../.." && pwd)"
exec bash "$ROOT/scripts/verify.sh" "$@"
