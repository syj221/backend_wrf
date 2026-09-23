#!/usr/bin/env bash
# 受管高分 WRF 分级链入口；合同校验和所有计算由同目录 Python 编排器完成。
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$BASH_SOURCE")" && pwd)"
storage_guard="$(printenv WRF_STORAGE_GUARD_SCRIPT || true)"
if [[ -z "$storage_guard" ]]; then storage_guard="$SCRIPT_DIR/wrf_storage_guard.sh"; fi
[[ -r "$storage_guard" ]] || { echo "[错误] 缺少数据盘保护脚本: $storage_guard" >&2; exit 3; }
# shellcheck disable=SC1090
source "$storage_guard"
wrf_require_storage >/dev/null
chain_script="$SCRIPT_DIR/wrf_hpc_downscale_chain.py"
[[ -r "$chain_script" ]] || { echo "[错误] 缺少高分链编排器: $chain_script" >&2; exit 2; }
exec python3 "$chain_script"

