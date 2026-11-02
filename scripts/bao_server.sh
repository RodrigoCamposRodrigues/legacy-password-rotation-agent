#!/usr/bin/env bash
# Runs OpenBao from a local binary when Docker is not available. Run from the repo root.
set -euo pipefail
mkdir -p .bao/data
exec "${BAO_BIN:-.tools/openbao/bao}" server -config=openbao/local.hcl
