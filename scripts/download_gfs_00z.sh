#!/usr/bin/env bash
# Queue an idempotent GFS pool request. The Python worker owns the only
# provider worker lock, so this entry point never blocks behind a 73-file fill.
set -euo pipefail

cycle="${1:-}"
horizon="${2:-72}"
requested="${3:-}"
intent="${4:-prefetch}"
reference="${5:-}"
root="${WRF_GFS_DATA_ROOT:-/home/tx-lab/.wrf-data/WRFwork/DATA/GFS}"
mount_root="${WRF_GFS_MOUNT:-/}"
minimum_free_gb="${WRF_GFS_MIN_FREE_GB:-120}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
queue_script="${script_dir}/forcing_pool_queue.py"
storage_guard="${WRF_STORAGE_GUARD_SCRIPT:-${script_dir}/wrf_storage_guard.sh}"

if [[ ! "$cycle" =~ ^[0-9]{8}00$ ]]; then echo "ERROR|invalid_cycle" >&2; exit 2; fi
if ! [[ "$horizon" =~ ^[0-9]+$ ]] || (( horizon < 0 || horizon > 72 )); then echo "ERROR|invalid_horizon" >&2; exit 2; fi
if [[ "$intent" != "task" && "$intent" != "prefetch" ]]; then echo "ERROR|invalid_priority" >&2; exit 2; fi
if [[ -z "$requested" ]]; then requested="$(seq -s, 0 "$horizon")"; fi
if ! command -v python3 >/dev/null 2>&1 || [[ ! -r "$queue_script" ]]; then echo "ERROR|pool_queue_script_unavailable" >&2; exit 4; fi
if [[ ! -r "$storage_guard" ]]; then echo "ERROR|storage_guard_unavailable" >&2; exit 3; fi
# shellcheck disable=SC1090
source "$storage_guard"
WRF_STORAGE_ALIAS="${mount_root}" wrf_require_storage >/dev/null
mkdir -p "$root" "$root/logs"
available_gb="$(df -Pk "$root" | awk 'NR == 2 { print int($4 / 1024 / 1024) }')"
if ! [[ "$available_gb" =~ ^[0-9]+$ ]] || (( available_gb < minimum_free_gb )); then
  echo "ERROR|insufficient_gfs_space" >&2
  exit 3
fi
probe="$(mktemp "${root}/logs/.queue-probe.XXXXXX")"
rm -f "$probe"

common=(
  --source gfs --root "$root"
  --min-bytes "${WRF_GFS_MIN_BYTES:-1048576}"
  --min-speed "${WRF_GFS_MIN_SPEED_BPS:-65536}"
  --request-interval "${WRF_GFS_REQUEST_INTERVAL_SECONDS:-10}"
  --retry-max-attempts "${WRF_POOL_RETRY_MAX_ATTEMPTS:-8}"
  --retry-max-seconds "${WRF_POOL_RETRY_MAX_SECONDS:-900}"
  --publish-probe-seconds "${WRF_POOL_PUBLISH_PROBE_SECONDS:-300}"
  --history-retention-days "${WRF_POOL_HISTORY_RETENTION_DAYS:-30}"
)
reference_args=()
if [[ -n "$reference" ]]; then reference_args=(--reference "$reference"); fi
python3 "$queue_script" enqueue "${common[@]}" --cycle "$cycle" --hours "$requested" --priority "$intent" "${reference_args[@]}"
# A second worker exits immediately if the source worker is active. A task
# enqueue therefore wakes the queue without touching its current .part file.
nohup python3 "$queue_script" worker "${common[@]}" >> "${root}/logs/pool_worker.out" 2>&1 < /dev/null &
