#!/usr/bin/env bash
# 超算 + GFS 专用入口。GFS 文件由超算共享下载脚本准备，Web 服务只上传校验索引。
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

if [[ "${WRF_DATA_SOURCE:-}" != "gfs" ]]; then
    echo "[错误] 超算 GFS 入口仅接受 WRF_DATA_SOURCE=gfs，当前为 ${WRF_DATA_SOURCE:-未设置}" >&2
    exit 2
fi
if [[ -z "${WRF_GFS_EXPECTED_INDEX:-}" || ! -s "${WRF_GFS_EXPECTED_INDEX}" ]]; then
    echo "[错误] 缺少 GFS 校验索引 WRF_GFS_EXPECTED_INDEX" >&2
    exit 2
fi

export WRF_RUNTIME="hpc"
export WRF_DATA_SOURCE="gfs"
export WRF_NONINTERACTIVE="true"
export WRF_GFS_DATA_ROOT="${WRF_GFS_DATA_ROOT:-${HOME}/Data/gfsdata}"

# shellcheck disable=SC1091
source "${SCRIPT_DIR}/wrf.sh"
if [[ "${WRF_PREFLIGHT_ONLY:-}" == "true" ]]; then
    preflight_hpc_runtime
    exit 0
fi
trap 'on_wrf_error "$?" "$LINENO" "$BASH_COMMAND"' ERR
main "$@"
