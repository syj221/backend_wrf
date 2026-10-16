#!/usr/bin/env bash
# Stable system-cron entrypoint for tx-lab. The cron file may remain on the
# system disk, but every data and runtime write is redirected to the volume
# resolved by UUID. Shared WRF/WPS binaries remain on the system disk.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
storage_guard="${WRF_STORAGE_GUARD_SCRIPT:-${SCRIPT_DIR}/wrf_storage_guard.sh}"
if [[ ! -r "$storage_guard" ]]; then
    echo "WRF_STORAGE_GUARD_UNAVAILABLE|path=${storage_guard}" >&2
    exit 3
fi
# shellcheck disable=SC1090
source "$storage_guard"
wrf_require_storage >/dev/null

storage_alias="${WRF_STORAGE_ALIAS:-/home/tx-lab/.wrf-data}"
work_root="${WRF_WORK_ROOT:-${storage_alias}/WRFwork}"
service_dir="${WRF_SERVICE_DIR:-${work_root}/RUNTIME/backend_wrf_service}"

export WRF_STORAGE_UUID="${WRF_STORAGE_UUID:-7EEE64E3EE6494E5}"
export WRF_STORAGE_ALIAS="$storage_alias"
export WRF_STORAGE_GUARD_SCRIPT="${service_dir}/wrf_storage_guard.sh"
export WRF_WORK_ROOT="$work_root"
export WRF_RUNTIME_DIR="${WRF_RUNTIME_DIR:-${work_root}/RUNTIME}"
export WRF_SERVICE_DIR="$service_dir"
export WRF_GFS_DATA_ROOT="${WRF_GFS_DATA_ROOT:-${work_root}/DATA/GFS}"
export WRF_EC_DATA_ROOT="${WRF_EC_DATA_ROOT:-${work_root}/DATA/ECMWF}"
export WRF_EC_CACHE_ROOT="${WRF_EC_CACHE_ROOT:-${work_root}/DATA/ECMWF}"
export WRF_GFS_MOUNT="$storage_alias"

# These are intentionally kept on the system disk as shared execution assets.
export WPS_SOURCE_DIR="${WPS_SOURCE_DIR:-/home/tx-lab/WRFwork/WPS/WPS-4.6.0-nvhpc}"
export WRF_SOURCE_DIR="${WRF_SOURCE_DIR:-/home/tx-lab/WRFwork/WRF_BUILD/WRF_CPU}"
export WRF_CPU_SOURCE_DIR="${WRF_CPU_SOURCE_DIR:-${WRF_SOURCE_DIR}}"
export GEOG_DATA_PATH="${GEOG_DATA_PATH:-/home/tx-lab/WRFwork/DATA/WPS_GEOG/WPS_GEOG}"
export WRF_RUNTIME_ENV="${WRF_RUNTIME_ENV:-/home/tx-lab/WRFwork/env_wrf_nvhpc.sh}"
export WRF_EC_GRIB_FILTER="${WRF_EC_GRIB_FILTER:-${service_dir}/ecmwf_to_wps.filter}"

download_script="${service_dir}/download_gfs_00z.sh"
if [[ ! -r "$download_script" ]]; then
    echo "WRF_PREFETCH_ENTRY_UNAVAILABLE|path=${download_script}" >&2
    exit 4
fi
# Cron may enqueue forcing only. Task discovery and WPS/WRF/CFD launches belong
# exclusively to backend_wrf; never chain to the historical directory worker.
exec bash "$download_script" "$(date -u +%Y%m%d)00" 72 "" prefetch
