#!/usr/bin/env bash
# tx-lab + GFS/ECMWF 专用 CPU 入口。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -z "${WRF_TASK_ENV:-}" || ! -s "${WRF_TASK_ENV}" ]]; then
    echo "[错误] 缺少任务运行环境文件 WRF_TASK_ENV" >&2
    exit 2
fi
# task.env 只由 backend_wrf 根据已经校验的任务字段生成。
# shellcheck disable=SC1090
source "${WRF_TASK_ENV}"
export WRF_TASK_ENV_LOADED="true"
export WRF_FAILURE_FILE="${WRF_FAILURE_FILE:-${WRF_TASK_ENV%/*}/failure.json}"

if [[ "${WRF_DATA_SOURCE:-}" != "gfs" && "${WRF_DATA_SOURCE:-}" != "ec" ]]; then
    echo "[错误] tx-lab 入口仅接受 WRF_DATA_SOURCE=gfs/ec，当前为 ${WRF_DATA_SOURCE:-未设置}" >&2
    exit 2
fi
if [[ "${WRF_DATA_SOURCE:-}" == "gfs" && ( -z "${WRF_GFS_EXPECTED_INDEX:-}" || ! -s "${WRF_GFS_EXPECTED_INDEX}" ) ]]; then
    echo "[错误] 缺少 GFS 校验索引 WRF_GFS_EXPECTED_INDEX" >&2
    exit 2
fi
if [[ "${WRF_DATA_SOURCE:-}" == "ec" && ( -z "${WRF_EC_EXPECTED_INDEX:-}" || ! -s "${WRF_EC_EXPECTED_INDEX}" ) ]]; then
    echo "[错误] 缺少 ECMWF 校验索引 WRF_EC_EXPECTED_INDEX" >&2
    exit 2
fi

export WRF_RUNTIME="hpc"
export WRF_NONINTERACTIVE="true"
export WRF_GFS_DATA_ROOT="${WRF_GFS_DATA_ROOT:-/mnt/wrf-data/WRF/GFS}"
export WRF_EC_CACHE_ROOT="${WRF_EC_CACHE_ROOT:-/home/tx-lab/WRFwork/DATA/ECMWF_CHINA}"
export WRF_RUNTIME_ENV="${WRF_RUNTIME_ENV:-/home/tx-lab/WRFwork/env_wrf_nvhpc.sh}"
export WRF_CPU_SOURCE_DIR="${WRF_CPU_SOURCE_DIR:-/home/tx-lab/WRFwork/WRF_BUILD/WRF_CPU}"
export WRF_REQUESTED_RUNTIME_PROFILE="cpu"
export WRF_RUNTIME_PROFILE="cpu"
export WRF_SOURCE_DIR="${WRF_CPU_SOURCE_DIR}"
export WRF_MPI_PROCESSES="${WRF_CPU_MPI_PROCESSES:-4}"
BASE_TASK_TAG="${WRF_TASK_TAG}"
TASK_STATE_DIR="${WRF_TASK_ENV%/*}"

# shellcheck disable=SC1091
source "${SCRIPT_DIR}/wrf.sh"
set +e

preflight_cpu() (
    set -e
    preflight_hpc_runtime
)

run_cpu() (
    set -e
    export WRF_TASK_TAG="${BASE_TASK_TAG}_attempt-1-cpu"
    export STAGE_STATUS_FILE="${WORK_DIR}/stage_status_${WRF_TASK_TAG}.jsonl"
    trap 'on_wrf_error "$?" "$LINENO" "$BASH_COMMAND"' ERR
    main
)

if [[ "${WRF_PREFLIGHT_ONLY:-}" == "true" ]]; then
    if preflight_cpu; then
        printf 'PREFLIGHT_OK|requested=cpu\n'
        exit 0
    fi
    exit 1
fi

attempt_log="${TASK_STATE_DIR}/attempt-1-cpu.log"
run_cpu 2>&1 | tee "$attempt_log"
profile_exit=${PIPESTATUS[0]}

if [[ "$profile_exit" -ne 0 ]]; then
    exit "$profile_exit"
fi

actual_tag="${BASE_TASK_TAG}_attempt-1-cpu"
actual_root="${WORK_DIR}/WRF_${actual_tag}"
canonical_root="${WORK_DIR}/WRF_${BASE_TASK_TAG}"
ln -sfn "$actual_root" "$canonical_root"
printf '%s\n' "cpu" > "${TASK_STATE_DIR}/actual_runtime_profile"
printf '%s\n' "${actual_root}/run" > "${TASK_STATE_DIR}/remote_output_dir"
printf '{"requested":"cpu","actual":"cpu","fallback_used":false}\n' \
    > "${TASK_STATE_DIR}/runtime_profile.json"
