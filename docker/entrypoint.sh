#!/usr/bin/env bash
set -euo pipefail

PRESET="${SPYONCINO_PRESET:-sim}"
CONFIG_DIR="${SPYONCINO_CONFIG_DIR:-/config}"
EXTRA_ARGS="${SPYONCINO_EXTRA_ARGS:-}"

read -r -a EXTRA_ARRAY <<< "${EXTRA_ARGS}"

echo "[entrypoint] starting spyoncino-modular preset=${PRESET} config_dir=${CONFIG_DIR}"
exec spyoncino-modular \
  --preset "${PRESET}" \
  --config-dir "${CONFIG_DIR}" \
  "${EXTRA_ARRAY[@]}"
