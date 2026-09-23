#!/usr/bin/env bash
# Queue an idempotent ECMWF pool request. ECMWF remains serial per provider;
# task-required files are promoted ahead of background prefetch files.
set -euo pipefail

cycle="${1:-}"
horizon="${2:-72}"
requested="${3:-}"
intent="${4:-prefetch}"
root="${WRF_EC_DATA_ROOT:-${WRF_HPC_EC_DIR:-/home/tx-lab/.wrf-data/WRFwork/DATA/ECMWF}}"
template="${WRF_EC_URL_TEMPLATE:-}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
queue_script="${script_dir}/forcing_pool_queue.py"
storage_guard="${WRF_STORAGE_GUARD_SCRIPT:-${script_dir}/wrf_storage_guard.sh}"

if [[ ! "$cycle" =~ ^[0-9]{8}00$ ]]; then echo "ERROR|invalid_cycle" >&2; exit 2; fi
if ! [[ "$horizon" =~ ^[0-9]+$ ]] || (( horizon < 0 || horizon > 72 )); then echo "ERROR|invalid_horizon" >&2; exit 2; fi
if [[ "$intent" != "task" && "$intent" != "prefetch" ]]; then echo "ERROR|invalid_priority" >&2; exit 2; fi
if [[ -z "$requested" ]]; then
  requested=""
  for ((hour=0; hour<=horizon; hour+=3)); do
    requested+="${requested:+,}${hour}"
  done
fi
if ! command -v python3 >/dev/null 2>&1 || [[ ! -r "$queue_script" ]]; then echo "ERROR|pool_queue_script_unavailable" >&2; exit 4; fi
if [[ ! -r "$storage_guard" ]]; then echo "ERROR|storage_guard_unavailable" >&2; exit 3; fi
# shellcheck disable=SC1090
source "$storage_guard"
wrf_require_storage >/dev/null
mkdir -p "$root" "$root/logs"

common=(
  --source ecmwf --root "$root"
  --min-bytes "${WRF_EC_MIN_BYTES:-1048576}"
  --min-speed "${WRF_EC_MIN_SPEED_BPS:-1024}"
  --retry-max-attempts "${WRF_POOL_RETRY_MAX_ATTEMPTS:-8}"
  --retry-max-seconds "${WRF_POOL_RETRY_MAX_SECONDS:-900}"
  --publish-probe-seconds "${WRF_POOL_PUBLISH_PROBE_SECONDS:-300}"
  --history-retention-days "${WRF_POOL_HISTORY_RETENTION_DAYS:-30}"
)
if [[ -n "$template" ]]; then common+=(--ec-template "$template"); fi
python3 "$queue_script" enqueue "${common[@]}" --cycle "$cycle" --hours "$requested" --priority "$intent"
nohup python3 "$queue_script" worker "${common[@]}" >> "${root}/logs/pool_worker.out" 2>&1 < /dev/null &
