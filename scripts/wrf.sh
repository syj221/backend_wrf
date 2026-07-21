#!/bin/bash
#===============================================================================
# WRF 自动化处理脚本
# 功能：数据准备、环境搭建、WPS处理、WRF运行
# 用户：xiajingming / shaoyongjin
# 日期：2026-04-28
#===============================================================================

# 设置严格模式
set -e  # 遇到错误立即退出
set -u  # 使用未定义变量时退出

# 颜色定义（用于输出显示）
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

#===============================================================================
# 基础路径配置
#===============================================================================
WRF_RUNTIME="${WRF_RUNTIME:-hpc}"
if [ "$WRF_RUNTIME" = "macos" ]; then
    export USER_HOME="${USER_HOME:-$HOME}"
    export WORK_DIR="${WORK_DIR:-${USER_HOME}/wrf-macos/work}"
    export ERA5_DATA_DIR="${ERA5_DATA_DIR:-${USER_HOME}/Data/ERA5}"
    export ERA5_SOURCE_DIR="${ERA5_SOURCE_DIR:-}"
    export GEOG_DATA_PATH="${GEOG_DATA_PATH:-${USER_HOME}/wrf-macos/WPS_GEOG}"
    export WPS_INSTALL_DIR="${WPS_INSTALL_DIR:-${USER_HOME}/wrf-macos/install/WPS-4.7.0}"
    export WPS_RUNTIME_DIR="${WPS_RUNTIME_DIR:-${USER_HOME}/wrf-macos/src/WPS-4.7.0}"
    export WRF_INSTALL_DIR="${WRF_INSTALL_DIR:-${USER_HOME}/wrf-macos/install/WRF-4.8.0}"
    export WRF_ENV_DIR="${WRF_ENV_DIR:-${USER_HOME}/wrf-macos/env}"
else
    export USER_HOME="${USER_HOME:-${HOME}}"

# 数据与模型目录
export ERA5_DATA_DIR="${ERA5_DATA_DIR:-${USER_HOME}/Data/ERA5}" # 未由 GFS 入口使用
export MODEL_DIR="${MODEL_DIR:-${USER_HOME}/Model}"
export WORK_DIR="${WORK_DIR:-${USER_HOME}/WRFwork}"

# 系统级共享数据目录（只读）
export ERA5_SOURCE_DIR="${ERA5_SOURCE_DIR:-/share/data/ERA5}"
export GEOG_DATA_PATH="${GEOG_DATA_PATH:-/share/data/WPS_GEOG}"

# 模型源文件夹名称
WPS_SOURCE_DIR="${WPS_SOURCE_DIR:-${MODEL_DIR}/WPSV4.2}"
WRF_SOURCE_DIR="${WRF_SOURCE_DIR:-${MODEL_DIR}/WRFV4.5.2}"
fi

#===============================================================================
# 时间配置 - 交互式输入（支持通过环境变量 WRF_NONINTERACTIVE=true 跳过交互）
#===============================================================================
if [ "${WRF_NONINTERACTIVE:-}" = "true" ]; then
    START_YEAR=${WRF_START_YEAR:-2025}
    START_MONTH=${WRF_START_MONTH:-04}
    START_DAY=${WRF_START_DAY:-01}
    START_HOUR=${WRF_START_HOUR:-00}
    END_YEAR=${WRF_END_YEAR:-2025}
    END_MONTH=${WRF_END_MONTH:-04}
    END_DAY=${WRF_END_DAY:-04}
    END_HOUR=${WRF_END_HOUR:-00}
    REF_LAT=${WRF_REF_LAT:-32.048}
    REF_LON=${WRF_REF_LON:-118.825}
else
    DEF_START_YEAR=2025
    DEF_START_MONTH=04
    DEF_START_DAY=01
    DEF_START_HOUR=00
    DEF_END_YEAR=2025
    DEF_END_MONTH=04
    DEF_END_DAY=04
    DEF_END_HOUR=00

    read -p "请输入模拟开始日期 (YYYY-MM-DD HH) [默认: ${DEF_START_YEAR}-${DEF_START_MONTH}-${DEF_START_DAY} ${DEF_START_HOUR}:00]: " start_input
    if [ -n "$start_input" ]; then
        START_YEAR=$(echo "$start_input" | awk '{print $1}' | cut -d'-' -f1)
        START_MONTH=$(echo "$start_input" | awk '{print $1}' | cut -d'-' -f2)
        START_DAY=$(echo "$start_input" | awk '{print $1}' | cut -d'-' -f3)
        START_HOUR=$(echo "$start_input" | awk '{print $2}' | cut -d':' -f1)
        [ -z "$START_HOUR" ] && START_HOUR=00
    else
        START_YEAR=$DEF_START_YEAR
        START_MONTH=$DEF_START_MONTH
        START_DAY=$DEF_START_DAY
        START_HOUR=$DEF_START_HOUR
    fi

    read -p "请输入模拟结束日期 (YYYY-MM-DD HH) [默认: ${DEF_END_YEAR}-${DEF_END_MONTH}-${DEF_END_DAY} ${DEF_END_HOUR}:00]: " end_input
    if [ -n "$end_input" ]; then
        END_YEAR=$(echo "$end_input" | awk '{print $1}' | cut -d'-' -f1)
        END_MONTH=$(echo "$end_input" | awk '{print $1}' | cut -d'-' -f2)
        END_DAY=$(echo "$end_input" | awk '{print $1}' | cut -d'-' -f3)
        END_HOUR=$(echo "$end_input" | awk '{print $2}' | cut -d':' -f1)
        [ -z "$END_HOUR" ] && END_HOUR=00
    else
        END_YEAR=$DEF_END_YEAR
        END_MONTH=$DEF_END_MONTH
        END_DAY=$DEF_END_DAY
        END_HOUR=$DEF_END_HOUR
    fi
    REF_LAT=32.048
    REF_LON=118.825
fi

# 格式化补齐两位数
fmt2() { printf "%02d" "$((10#$1))"; }
fmt4() { printf "%04d" "$((10#$1))"; }

START_YEAR=$(fmt4 "$START_YEAR")
START_MONTH=$(fmt2 "$START_MONTH")
START_DAY=$(fmt2 "$START_DAY")
START_HOUR=$(fmt2 "$START_HOUR")
END_YEAR=$(fmt4 "$END_YEAR")
END_MONTH=$(fmt2 "$END_MONTH")
END_DAY=$(fmt2 "$END_DAY")
END_HOUR=$(fmt2 "$END_HOUR")

# 生成带时间特征的工作目录后缀，用于区分不同任务
TASK_TAG="${WRF_TASK_TAG:-${START_YEAR}${START_MONTH}${START_DAY}_${END_YEAR}${END_MONTH}${END_DAY}_lat${REF_LAT}_lon${REF_LON}}"

#===============================================================================
# 函数定义
#===============================================================================

# 打印带颜色的信息
log_info() {
    echo -e "${GREEN}[✓ INFO]${NC}  $(date '+%Y-%m-%d %H:%M:%S') - $1"
}

log_warn() {
    echo -e "${YELLOW}[⚠ WARN]${NC}  $(date '+%Y-%m-%d %H:%M:%S') - $1"
}

log_error() {
    echo -e "${RED}[✗ ERROR]${NC} $(date '+%Y-%m-%d %H:%M:%S') - $1"
}

log_step() {
    echo -e "\n${CYAN}╔════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║  $1${NC}"
    echo -e "${CYAN}╚════════════════════════════════════════════════════════════════╝${NC}"
}

# 错误检查函数
check_status() {
    if [ $? -ne 0 ]; then
        log_error "$1"
        return 1
    else
        log_info "$1 - 成功"
        return 0
    fi
}

configure_runtime_stack() {
    # Intel Fortran 版 ungrib 处理 GFS 0.25° 完整 GRIB2 时会在默认小栈下
    # 以 forrtl severe (174) / SIGSEGV 退出。优先解除软限制；若调度环境
    # 禁止 unlimited，则至少提升到当前硬限制，并明确记录最终值。
    local hard_stack soft_stack
    hard_stack=$(ulimit -H -s 2>/dev/null || printf unknown)
    if ! ulimit -S -s unlimited 2>/dev/null; then
        if [[ "$hard_stack" =~ ^[0-9]+$ ]] && ulimit -S -s "$hard_stack" 2>/dev/null; then
            log_warn "无法将进程栈设为 unlimited，已提升到硬限制 ${hard_stack} KiB"
        else
            log_error "无法提升 WPS/WRF 进程栈限制（hard=${hard_stack}）"
            return 1
        fi
    fi
    soft_stack=$(ulimit -S -s 2>/dev/null || printf unknown)
    export OMP_STACKSIZE="${OMP_STACKSIZE:-512M}"
    export KMP_STACKSIZE="${KMP_STACKSIZE:-512M}"
    log_info "WPS/WRF 栈限制: soft=${soft_stack}, hard=${hard_stack}; OMP_STACKSIZE=${OMP_STACKSIZE}; KMP_STACKSIZE=${KMP_STACKSIZE}"
}

load_runtime_environment() {
    if [ "${WRF_RUNTIME_ENV_LOADED:-}" = "true" ]; then
        return 0
    fi
    if [ "$WRF_RUNTIME" = "macos" ]; then
        if [ ! -x "${WRF_ENV_DIR}/bin/mpirun" ]; then
            log_error "未找到 macOS WRF 运行环境: ${WRF_ENV_DIR}"
            exit 1
        fi
        export PATH="${WRF_ENV_DIR}/bin:${PATH}"
        export DYLD_FALLBACK_LIBRARY_PATH="${WRF_ENV_DIR}/lib:${DYLD_FALLBACK_LIBRARY_PATH:-}"
        # WRF 随附的 RRTMG 查找表是大端序 Fortran 非格式化记录；
        # Apple Silicon 上的 gfortran 默认小端读取会在辐射初始化时误报 EOF。
        export GFORTRAN_CONVERT_UNIT="${GFORTRAN_CONVERT_UNIT:-big_endian}"
        export SDKROOT="${SDKROOT:-$(xcrun --show-sdk-path)}"
        export WRF_RUNTIME_ENV_LOADED="true"
        return 0
    fi
    configure_runtime_stack || return 1
    if ! command -v module &> /dev/null; then
        [ -r /etc/profile.d/modules.sh ] && . /etc/profile.d/modules.sh
        [ -r /usr/share/Modules/init/bash ] && . /usr/share/Modules/init/bash
    fi
    if command -v module &> /dev/null; then
        module purge
        if ! module load intel hdf5 netcdf jasper libpng zlib openmpi/4.1.4; then
            log_error "加载超算 WRF Modules 环境失败"
            return 1
        fi
        # ECMWF Open Data 使用 CCSDS/AEC 压缩；如超算提供 ecCodes 模块，
        # 自动加载 grib_filter 供 WPS 兼容重打包使用。
        module load eccodes 2>/dev/null || true
    else
        log_warn "module命令不可用，请手动加载环境变量"
    fi
    export WRF_RUNTIME_ENV_LOADED="true"
}

preflight_hpc_runtime() {
    log_info "开始超算 WRF 运行环境预检"
    if [ "${WRF_RUNTIME:-hpc}" != "hpc" ]; then
        log_error "远端入口要求 WRF_RUNTIME=hpc"
        return 1
    fi
    load_runtime_environment || return 1
    local missing=0 command_name
    for command_name in bash awk sed grep wc date mpirun ncdump; do
        if ! command -v "$command_name" >/dev/null 2>&1; then
            log_error "预检缺少命令: ${command_name}"
            missing=$((missing + 1))
        fi
    done
    if ! command -v sha256sum >/dev/null 2>&1 && ! command -v shasum >/dev/null 2>&1; then
        log_error "预检缺少 SHA256 工具: sha256sum/shasum"
        missing=$((missing + 1))
    fi
    local required_dir required_file
    for required_dir in "${WPS_SOURCE_DIR}" "${WRF_SOURCE_DIR}" "${GEOG_DATA_PATH}" "${WRF_GFS_DATA_ROOT}"; do
        if [ ! -d "$required_dir" ]; then
            log_error "预检目录不存在: ${required_dir}"
            missing=$((missing + 1))
        fi
    done
    for required_file in \
        "${WPS_SOURCE_DIR}/geogrid.exe" \
        "${WPS_SOURCE_DIR}/ungrib.exe" \
        "${WPS_SOURCE_DIR}/metgrid.exe" \
        "${WRF_SOURCE_DIR}/run/real.exe" \
        "${WRF_SOURCE_DIR}/run/wrf.exe"; do
        if [ ! -x "$required_file" ]; then
            log_error "预检可执行文件不存在或不可执行: ${required_file}"
            missing=$((missing + 1))
        fi
    done
    if [ "$missing" -ne 0 ]; then
        log_error "超算 WRF 环境预检失败，共 ${missing} 项异常"
        return 1
    fi
    log_info "超算 WRF 环境预检通过"
}

resolve_grib_filter() {
    if [ -n "${WRF_GRIB_FILTER:-}" ] && [ -x "${WRF_GRIB_FILTER}" ]; then
        printf '%s\n' "${WRF_GRIB_FILTER}"
        return 0
    fi
    command -v grib_filter 2>/dev/null || return 1
}

prepare_ec_grib_for_wps() {
    local source_file="$1"
    local output_file="$2"
    local filter_file="${WRF_EC_GRIB_FILTER:-}"
    local grib_filter_bin

    if [ -z "$filter_file" ] || [ ! -f "$filter_file" ]; then
        log_error "EC GRIB 兼容规则不存在: ${filter_file:-<未配置>}"
        return 1
    fi
    if ! grib_filter_bin=$(resolve_grib_filter); then
        log_error "未找到 ecCodes grib_filter，无法把 ECMWF 模板 5.42 转为 WPS 支持的模板 5.0"
        log_error "本机请在 Web 服务所用 Python 环境安装 requirements.txt；超算请加载或安装 ecCodes"
        return 1
    fi
    if [ -s "$output_file" ] && [ "$output_file" -nt "$source_file" ] && [ "$output_file" -nt "$filter_file" ]; then
        log_info "  复用 EC WPS 兼容文件: $(basename "$output_file")"
        return 0
    fi
    log_info "  转换 EC GRIB: $(basename "$source_file") (CCSDS → grid_simple)"
    if ! "$grib_filter_bin" -o "$output_file" "$filter_file" "$source_file"; then
        log_error "EC GRIB 兼容转换失败: ${source_file}"
        return 1
    fi
    if [ ! -s "$output_file" ]; then
        log_error "EC GRIB 兼容转换未生成有效文件: ${output_file}"
        return 1
    fi
}

# 跨平台日期转 epoch 秒 (兼容 Linux date -d 和 macOS date -j)
# 用法: date_to_epoch "YYYY-MM-DD HH:MM:SS"
date_to_epoch() {
    local dt="$1"
    if date -d "$dt" +%s 2>/dev/null; then
        return 0
    fi
    # macOS fallback: date -j -f "%Y-%m-%d %H:%M:%S" "2025-04-01 00:00:00" +%s
    if date -j -f "%Y-%m-%d %H:%M:%S" "$dt" +%s 2>/dev/null; then
        return 0
    fi
    echo "0"
    return 1
}

domain_value() {
    local prefix="$1"
    local lvl="$2"
    local fallback="$3"
    local suffix
    suffix=$(fmt2 "$lvl")
    local var="${prefix}_D${suffix}"
    echo "${!var:-$fallback}"
}

csv_join_domains() {
    local prefix="$1"
    local fallback="$2"
    local out=""
    local max_dom=${WRF_MAX_DOM:-2}
    for lvl in $(seq 1 "$max_dom"); do
        local value
        value=$(domain_value "$prefix" "$lvl" "$fallback")
        if [ -z "$out" ]; then out="$value"; else out="${out}, ${value}"; fi
    done
    echo "$out"
}

csv_repeat() {
    local value="$1"
    local out=""
    local max_dom=${WRF_MAX_DOM:-2}
    for lvl in $(seq 1 "$max_dom"); do
        if [ -z "$out" ]; then out="$value"; else out="${out}, ${value}"; fi
    done
    echo "$out"
}

csv_fdda_by_resolution() {
    local enabled="$1"
    local out=""
    local max_dom=${WRF_MAX_DOM:-2}
    for lvl in $(seq 1 "$max_dom"); do
        local dx value=0
        dx=$(domain_value WRF_DX "$lvl" "27000")
        if [ "$enabled" = "1" ] && [ "${dx:-0}" -ge 9000 ]; then value=1; fi
        if [ -z "$out" ]; then out="$value"; else out="${out}, ${value}"; fi
    done
    echo "$out"
}

csv_parent_ids_wps() {
    local out=""
    local max_dom=${WRF_MAX_DOM:-2}
    for lvl in $(seq 1 "$max_dom"); do
        local value=1
        if [ "$lvl" -gt 2 ]; then value=$((lvl - 1)); fi
        if [ -z "$out" ]; then out="$value"; else out="${out}, ${value}"; fi
    done
    echo "$out"
}

csv_parent_ids_wrf() {
    local out=""
    local max_dom=${WRF_MAX_DOM:-2}
    for lvl in $(seq 1 "$max_dom"); do
        local value=0
        if [ "$lvl" -gt 1 ]; then value=$((lvl - 1)); fi
        if [ -z "$out" ]; then out="$value"; else out="${out}, ${value}"; fi
    done
    echo "$out"
}

csv_grid_ids() {
    local out=""
    local max_dom=${WRF_MAX_DOM:-2}
    for lvl in $(seq 1 "$max_dom"); do
        if [ -z "$out" ]; then out="$lvl"; else out="${out}, ${lvl}"; fi
    done
    echo "$out"
}

csv_grid_ratios() {
    local out="1"
    local max_dom=${WRF_MAX_DOM:-2}
    for lvl in $(seq 2 "$max_dom"); do
        local parent_dx child_dx ratio
        parent_dx=$(domain_value "WRF_DX" $((lvl - 1)) 27000)
        child_dx=$(domain_value "WRF_DX" "$lvl" 9000)
        ratio=$(( parent_dx / child_dx ))
        out="${out}, ${ratio}"
    done
    echo "$out"
}

csv_parent_starts() {
    local axis="$1"
    local out="1"
    local max_dom=${WRF_MAX_DOM:-2}
    for lvl in $(seq 2 "$max_dom"); do
        local var value
        if [ "$axis" = "i" ]; then
            var="WRF_I_PARENT_START_D$(printf "%02d" "$lvl")"
            value=${!var:-${WRF_I_PARENT_START:-6}}
        else
            var="WRF_J_PARENT_START_D$(printf "%02d" "$lvl")"
            value=${!var:-${WRF_J_PARENT_START:-6}}
        fi
        out="${out}, ${value}"
    done
    echo "$out"
}

csv_nested_flags() {
    local first="$1"
    local rest="$2"
    local out="$first"
    local max_dom=${WRF_MAX_DOM:-2}
    for lvl in $(seq 2 "$max_dom"); do
        out="${out}, ${rest}"
    done
    echo "$out"
}

select_safe_mpi_processes() {
    local requested="$1"
    local max_dom="${WRF_MAX_DOM:-1}"
    local min_e_we=999999 min_e_sn=999999
    local lvl e_we e_sn
    for lvl in $(seq 1 "$max_dom"); do
        e_we=$(domain_value "WRF_E_WE" "$lvl" 100)
        e_sn=$(domain_value "WRF_E_SN" "$lvl" 80)
        [ "$e_we" -lt "$min_e_we" ] && min_e_we="$e_we"
        [ "$e_sn" -lt "$min_e_sn" ] && min_e_sn="$e_sn"
    done

    if [ "$requested" -le 1 ]; then
        echo 1
        return
    fi

    # WRF 的二维域分解在每个方向至少需要 10 个格点。使用请求进程数的
    # 平方根作为保守分解尺度；不满足时退回单进程，避免 MPI_ABORT。
    local side=1
    while [ $((side * side)) -lt "$requested" ]; do side=$((side + 1)); done
    local required=$((10 * side))
    if [ "$min_e_we" -lt "$required" ] || [ "$min_e_sn" -lt "$required" ]; then
        log_warn "最小嵌套域为 ${min_e_we}×${min_e_sn}，不足以安全使用 ${requested} 个MPI进程（至少需 ${required}×${required}）；自动降为 1" >&2
        echo 1
        return
    fi
    echo "$requested"
}

load_task_config() {
    if [ "${WRF_TASK_ENV_LOADED:-}" = "true" ]; then
        log_info "任务参数已从 Shell 环境文件加载，JSON 仅作审计"
        return 0
    fi
    if [ -z "${WRF_TASK_CONFIG:-}" ] || [ ! -f "${WRF_TASK_CONFIG}" ]; then
        return 0
    fi
    log_info "读取任务配置: ${WRF_TASK_CONFIG}"
    local pybin=""
    if command -v python3 >/dev/null 2>&1; then
        pybin=python3
    elif command -v python >/dev/null 2>&1; then
        pybin=python
    else
        log_warn "未找到 python/python3，跳过 task_config.json，使用环境变量"
        return 0
    fi
    local exports
    exports=$("$pybin" - "$WRF_TASK_CONFIG" <<'PY'
import json, shlex, sys
path = sys.argv[1]
with open(path, encoding="utf-8") as fh:
    cfg = json.load(fh)

def emit(name, value):
    if value is None:
        return
    print(f"export {name}={shlex.quote(str(value))}")

tr = cfg.get("time_range", {})
center = cfg.get("center", {})
phys = cfg.get("physics", {})
emit("WRF_TASK_TAG", cfg.get("output", {}).get("task_tag") or cfg.get("display_id"))
emit("WRF_DATA_SOURCE", cfg.get("data_source", "era5"))
emit("WRF_MAX_DOM", cfg.get("max_dom", len(cfg.get("domains", [])) or 1))
emit("WRF_START_YEAR", tr.get("start_year"))
emit("WRF_START_MONTH", f"{int(tr.get('start_month')):02d}" if tr.get("start_month") is not None else None)
emit("WRF_START_DAY", f"{int(tr.get('start_day')):02d}" if tr.get("start_day") is not None else None)
emit("WRF_START_HOUR", f"{int(tr.get('start_hour')):02d}" if tr.get("start_hour") is not None else None)
emit("WRF_END_YEAR", tr.get("end_year"))
emit("WRF_END_MONTH", f"{int(tr.get('end_month')):02d}" if tr.get("end_month") is not None else None)
emit("WRF_END_DAY", f"{int(tr.get('end_day')):02d}" if tr.get("end_day") is not None else None)
emit("WRF_END_HOUR", f"{int(tr.get('end_hour')):02d}" if tr.get("end_hour") is not None else None)
emit("WRF_REF_LAT", center.get("lat"))
emit("WRF_REF_LON", center.get("lon"))
mapping = {
    "num_metgrid_levels": "WRF_NUM_METGRID_LEVELS",
    "mp_physics": "WRF_MP_PHYSICS",
    "cu_physics": "WRF_CU_PHYSICS",
    "ra_lw_physics": "WRF_RA_LW_PHYSICS",
    "ra_sw_physics": "WRF_RA_SW_PHYSICS",
    "bl_pbl_physics": "WRF_BL_PBL_PHYSICS",
    "sf_sfclay_physics": "WRF_SF_SFCLAY_PHYSICS",
    "sf_surface_physics": "WRF_SF_SURFACE_PHYSICS",
    "sf_urban_physics": "WRF_SF_URBAN_PHYSICS",
    "num_soil_layers": "WRF_NUM_SOIL_LAYERS",
    "num_land_cat": "WRF_NUM_LAND_CAT",
    "radt": "WRF_RADT",
}
for key, env in mapping.items():
    emit(env, phys.get(key))
if cfg.get("data_source", "era5") == "gfs":
    # 标准 GFS Vtable 的 met_em 垂直维度为 34；即使旧任务配置残留了
    # ERA5 的 38，也必须在运行前纠正，否则 real.exe 会拒绝初始化。
    emit("WRF_NUM_METGRID_LEVELS", 34)
gfs = cfg.get("gfs_cache", {})
emit("WRF_GFS_DATE", gfs.get("gfs_cycle_date") or gfs.get("cycle_date"))
emit("WRF_GFS_HOUR", gfs.get("gfs_cycle_hour") or gfs.get("cycle_hour"))
if gfs.get("gfs_forecast_hours") or gfs.get("forecast_hours"):
    hours = gfs.get("gfs_forecast_hours") or gfs.get("forecast_hours")
    emit("WRF_GFS_FORECAST_HOURS", " ".join(f"{int(h):03d}" for h in hours))
emit("WRF_GFS_CACHE_MODE", gfs.get("mode"))
emit("WRF_GFS_FILE_INTERVAL_HOURS", gfs.get("gfs_file_interval_hours"))
ec = cfg.get("ec_cache", {})
emit("WRF_EC_DATE", ec.get("ec_cycle_date") or ec.get("cycle_date"))
emit("WRF_EC_HOUR", ec.get("ec_cycle_hour") or ec.get("cycle_hour"))
if ec.get("ec_forecast_hours") or ec.get("forecast_hours"):
    hours = ec.get("ec_forecast_hours") or ec.get("forecast_hours")
    emit("WRF_EC_FORECAST_HOURS", " ".join(f"{int(h):03d}" for h in hours))
emit("WRF_EC_CACHE_MODE", ec.get("mode"))
emit("WRF_EC_FILE_INTERVAL_HOURS", ec.get("ec_file_interval_hours"))
emit("WRF_FORECAST_FILE_INTERVAL_HOURS", gfs.get("gfs_file_interval_hours") or ec.get("ec_file_interval_hours"))
assim = cfg.get("assimilation", {})
emit("WRF_ASSIMILATION_SCHEME", assim.get("scheme"))
for key, value in (assim.get("params") or {}).items():
    emit(f"WRF_ASSIM_{key.upper()}", value)
for idx, domain in enumerate(cfg.get("domains", []), 1):
    suffix = f"D{idx:02d}"
    emit(f"WRF_DX_{suffix}", domain.get("dx"))
    emit(f"WRF_DY_{suffix}", domain.get("dy"))
    emit(f"WRF_E_WE_{suffix}", domain.get("e_we"))
    emit(f"WRF_E_SN_{suffix}", domain.get("e_sn"))
    emit(f"WRF_I_PARENT_START_{suffix}", domain.get("i_parent_start"))
    emit(f"WRF_J_PARENT_START_{suffix}", domain.get("j_parent_start"))
PY
)
    eval "$exports"
    START_YEAR=${WRF_START_YEAR:-$START_YEAR}
    START_MONTH=${WRF_START_MONTH:-$START_MONTH}
    START_DAY=${WRF_START_DAY:-$START_DAY}
    START_HOUR=${WRF_START_HOUR:-$START_HOUR}
    END_YEAR=${WRF_END_YEAR:-$END_YEAR}
    END_MONTH=${WRF_END_MONTH:-$END_MONTH}
    END_DAY=${WRF_END_DAY:-$END_DAY}
    END_HOUR=${WRF_END_HOUR:-$END_HOUR}
    REF_LAT=${WRF_REF_LAT:-$REF_LAT}
    REF_LON=${WRF_REF_LON:-$REF_LON}
    TASK_TAG=${WRF_TASK_TAG:-$TASK_TAG}
}

stage_status_file() {
    echo "${STAGE_STATUS_FILE:-${WORK_DIR}/stage_status_${TASK_TAG}.jsonl}"
}

write_stage() {
    local stage="$1"
    local status="$2"
    local message="${3:-}"
    local file
    local record
    file=$(stage_status_file)
    mkdir -p "$(dirname "$file")" 2>/dev/null || true
    record=$(printf '{"time":"%s","stage":"%s","status":"%s","message":"%s"}' \
        "$(date '+%Y-%m-%d %H:%M:%S')" "$stage" "$status" "$(echo "$message" | sed 's/"/\\"/g')")
    printf '%s\n' "$record" >> "$file"
    # 本机任务没有远端轮询器；同时输出机器可读记录，供 Web 服务实时更新
    # 与超算一致的页面阶段。远端仍以 stage_status 文件为权威来源。
    printf '[stage-status] %s\n' "$record"
}

#===============================================================================
# 步骤1: 数据准备
# 在 ${ERA5_DATA_DIR}/ 下按年月建目录，链接 /share/data/ERA5/ 源文件
#===============================================================================
step1_data_preparation() {
    log_step "步骤1: 数据准备 - 按模拟时段检查用户数据/从系统源目录链接ERA5数据"
    log_info "用户数据目录: ${ERA5_DATA_DIR}"
    log_info "系统源数据目录: ${ERA5_SOURCE_DIR}"

    # 收集模拟时段覆盖的所有年月
    local year_months=()
    local cur_year=$START_YEAR
    while [ "$cur_year" -le "$END_YEAR" ]; do
        local first_month last_month
        if [ "$cur_year" -eq "$START_YEAR" ] && [ "$cur_year" -eq "$END_YEAR" ]; then
            first_month=$((10#$START_MONTH))
            last_month=$((10#$END_MONTH))
        elif [ "$cur_year" -eq "$START_YEAR" ]; then
            first_month=$((10#$START_MONTH))
            last_month=12
        elif [ "$cur_year" -eq "$END_YEAR" ]; then
            first_month=1
            last_month=$((10#$END_MONTH))
        else
            first_month=1
            last_month=12
        fi

        local cur_month=$first_month
        while [ "$cur_month" -le "$last_month" ]; do
            local ym=$(printf "%04d%02d" $cur_year $cur_month)
            year_months+=("$ym")
            cur_month=$((cur_month + 1))
        done
        cur_year=$((cur_year + 1))
    done

    log_info "需要覆盖的年月: ${year_months[*]}"

    local total_links=0
    local already_have=0

    for ym in "${year_months[@]}"; do
        local ym_year=${ym:0:4}
        local ym_month=${ym:4:2}
        local user_dir="${ERA5_DATA_DIR}/${ym_year}/${ym_month}"

        # 1. 先检查用户目录下是否已有对应时段的文件
        if [ -d "$user_dir" ] && [ "$(ls -A "$user_dir" 2>/dev/null)" ]; then
            local file_count=$(ls -1 "$user_dir" | wc -l)
            log_info "  ${ym_year}/${ym_month}: 用户目录已有 ${file_count} 个文件，跳过"
            already_have=$((already_have + file_count))
            continue
        fi

        # 2. 用户目录为空，去系统源目录的对应年子目录下查找
        local source_year_dir="${ERA5_SOURCE_DIR}/${ym_year}"
        if [ ! -d "$source_year_dir" ]; then
            log_error "系统源目录缺少 ${ym_year} 年子目录: ${source_year_dir}"
            exit 1
        fi

        log_info "  ${ym_year}/${ym_month}: 用户目录为空，从系统源目录 ${source_year_dir} 查找并链接..."
        mkdir -p "$user_dir"

        # 在该年目录下查找包含对应年月模式的文件
        local found=0
        for src_file in "${source_year_dir}"/*; do
            [ -f "$src_file" ] || continue
            local filename=$(basename "$src_file")

            # 从文件名提取年月，确认匹配目标年月
            local file_year=""
            local file_month=""
            if [[ "$filename" =~ ([0-9]{4})(0[1-9]|1[0-2]) ]]; then
                file_year="${BASH_REMATCH[1]}"
                file_month="${BASH_REMATCH[2]}"
            elif [[ "$filename" =~ ([0-9]{4})-(0[1-9]|1[0-2]) ]]; then
                file_year="${BASH_REMATCH[1]}"
                file_month="${BASH_REMATCH[2]}"
            fi

            if [ "$file_year" = "$ym_year" ] && [ "$file_month" = "$ym_month" ]; then
                ln -sf "$src_file" "${user_dir}/${filename}"
                total_links=$((total_links + 1))
                found=$((found + 1))
                log_info "    链接: ${filename}"
            fi
        done

        if [ "$found" -eq 0 ]; then
            log_error "系统源目录 ${source_year_dir} 下没有找到 ${ym_year}${ym_month} 时段的数据文件"
            exit 1
        fi
    done

    log_info "用户目录已有文件: ${already_have} 个"
    log_info "本次新创建链接: ${total_links} 个"

    # 显示用户数据目录结构
    if [ -d "${ERA5_DATA_DIR}" ]; then
        log_info "用户数据目录内容:"
        # 数据清单仅用于日志展示，不得因 head 提前关闭管道而中止任务。
        find "${ERA5_DATA_DIR}" \( -type f -o -type l \) -print 2>/dev/null \
            | sort | head -30 || true
    fi

    log_info "数据准备完成！"
}

#===============================================================================
# 步骤1b: GFS 数据准备 (当 WRF_DATA_SOURCE=gfs 时替代 step1)
# 检查本机缓存的 GFS 0.25° 全球分析/预报数据，并按 cycle/fhour 复用
#===============================================================================
step1_forecast_data_preparation() {
    local provider="$1"
    local data_root data_date data_hour forecast_hours file_prefix file_suffix file_extension
    if [ "$provider" = "ec" ]; then
        data_root="${WRF_EC_CACHE_ROOT:-${USER_HOME}/Data/ecdata}"
        data_date="${WRF_EC_DATE:-}"
        data_hour="${WRF_EC_HOUR:-}"
        forecast_hours="${WRF_EC_FORECAST_HOURS:-}"
        file_prefix="ecmwf.t"
        file_suffix=".ifs.0p25.f"
        file_extension=".grib2"
    else
        data_root="${WRF_GFS_DATA_ROOT:-${USER_HOME}/Data/gfsdata}"
        data_date="${WRF_GFS_DATE:-}"
        data_hour="${WRF_GFS_HOUR:-}"
        forecast_hours="${WRF_GFS_FORECAST_HOURS:-}"
        file_prefix="gfs.t"
        file_suffix=".pgrb2.0p25.f"
        file_extension=""
    fi
    local provider_upper
    provider_upper=$(printf '%s' "$provider" | tr '[:lower:]' '[:upper:]')
    log_step "步骤1 (${provider_upper}): 检查预报数据池"
    if [ -z "$data_date" ] || [ -z "$data_hour" ] || [ -z "$forecast_hours" ]; then
        log_error "缺少 Web 端选择的 ${provider_upper} cycle/forecast hour 信息"
        write_stage "failed" "error" "${provider_upper} forecast hour 未导出"
        exit 1
    fi
    data_hour=$(fmt2 "$data_hour")

    log_info "${provider_upper} Cycle: ${data_date}/${data_hour}Z"
    local data_dir="${data_root}/${data_date}${data_hour}"

    log_info "${provider_upper}缓存目录: ${data_dir}"
    if [ "${WRF_RUNTIME:-hpc}" = "macos" ]; then
        log_info "${provider_upper} 数据由 Web 服务下载并保存在本机数据池；本机运行不会上传任何文件"
    else
        log_info "${provider_upper} 数据由超算共享下载脚本准备；Web 服务只检查并等待数据池"
    fi
    log_info "需要的 forecast hours: ${forecast_hours}"
    log_info "预报缓存模式: ${WRF_GFS_CACHE_MODE:-${WRF_EC_CACHE_MODE:-global_cycle_fhour}}"

    if [ ! -d "$data_dir" ]; then
        log_error "${provider_upper}缓存目录不存在: ${data_dir}"
        log_error "请先运行超算共享下载脚本并校验完整预报文件到该缓存目录"
        write_stage "failed" "error" "${provider_upper}缓存目录不存在: ${data_dir}"
        exit 1
    fi

    local manifest="${data_dir}/manifest.json"
    local expected_index="${WRF_GFS_EXPECTED_INDEX:-}"
    if [ -s "$expected_index" ]; then
        log_info "发现任务级 GFS 校验索引，将使用 Shell 校验文件大小和 SHA256"
    elif [ -f "$manifest" ]; then
        log_info "发现 manifest.json，将校验文件大小和 SHA256"
    else
        log_warn "未发现 manifest.json，仅校验文件存在且非空"
    fi
    local manifest_py=""
    if command -v python3 >/dev/null 2>&1; then
        manifest_py=python3
    elif command -v python >/dev/null 2>&1; then
        manifest_py=python
    fi

    # 检查按 cycle/fhour 复用的预报数据。
    local ready=0
    local missing=0
    for fh in ${forecast_hours}; do
        fh=$(printf "%03d" "$((10#$fh))")
        local outfile="${data_dir}/${file_prefix}${data_hour}z${file_suffix}${fh}${file_extension}"
        if [ -f "$outfile" ] && [ -s "$outfile" ]; then
            local fsize
            # macOS 的 wc 输出带前导空格；须去除空白后再与 JSON 数字比较。
            fsize=$(wc -c < "$outfile" 2>/dev/null | tr -d '[:space:]')
            fsize=${fsize:-?}
            if [ -s "$expected_index" ]; then
                local expected_record expected_size expected_sha actual_sha
                if ! expected_record=$(awk -F '\t' -v name="$(basename "$outfile")" '
                    $1 == name { count += 1; record = $2 "|" $3 }
                    END { if (count != 1) exit 2; print record }
                ' "$expected_index"); then
                    log_error "  GFS 校验索引缺少或重复记录: $(basename "$outfile")"
                    missing=$((missing + 1))
                    continue
                fi
                expected_size=${expected_record%%|*}
                expected_sha=${expected_record#*|}
                if [ "$fsize" != "$expected_size" ]; then
                    log_error "  文件大小不一致: f${fh} (${fsize}/${expected_size} bytes)"
                    missing=$((missing + 1))
                    continue
                fi
                if command -v sha256sum >/dev/null 2>&1; then
                    actual_sha=$(sha256sum "$outfile" | awk '{print $1}')
                else
                    actual_sha=$(shasum -a 256 "$outfile" | awk '{print $1}')
                fi
                if [ "$actual_sha" != "$expected_sha" ]; then
                    log_error "  SHA256 不一致: f${fh} (${actual_sha} != ${expected_sha})"
                    missing=$((missing + 1))
                    continue
                fi
            elif [ -f "$manifest" ]; then
                if [ -z "$manifest_py" ]; then
                    log_error "  无法校验 manifest: 未找到 python/python3"
                    missing=$((missing + 1))
                    continue
                fi
                local expected_size expected_sha actual_sha
                expected_size=$("$manifest_py" - "$manifest" "$(basename "$outfile")" <<'PY'
import json, sys
manifest, name = sys.argv[1], sys.argv[2]
with open(manifest, encoding="utf-8") as fh:
    data = json.load(fh)
for item in data.get("files", []):
    if item.get("name") == name:
        print(item.get("size", ""))
        break
PY
)
                expected_sha=$("$manifest_py" - "$manifest" "$(basename "$outfile")" <<'PY'
import json, sys
manifest, name = sys.argv[1], sys.argv[2]
with open(manifest, encoding="utf-8") as fh:
    data = json.load(fh)
for item in data.get("files", []):
    if item.get("name") == name:
        print(item.get("sha256", ""))
        break
PY
)
                if [ -z "$expected_size" ] || [ -z "$expected_sha" ]; then
                    log_error "  manifest 缺少文件记录: $(basename "$outfile")"
                    missing=$((missing + 1))
                    continue
                fi
                if [ "$fsize" != "$expected_size" ]; then
                    log_error "  文件大小不一致: f${fh} (${fsize}/${expected_size} bytes)"
                    missing=$((missing + 1))
                    continue
                fi
                if command -v sha256sum >/dev/null 2>&1; then
                    actual_sha=$(sha256sum "$outfile" | awk '{print $1}')
                else
                    actual_sha=$(shasum -a 256 "$outfile" | awk '{print $1}')
                fi
                if [ "$actual_sha" != "$expected_sha" ]; then
                    log_error "  SHA256 不一致: f${fh} (${actual_sha} != ${expected_sha})"
                    missing=$((missing + 1))
                    continue
                fi
            fi
            log_info "  缓存校验通过: f${fh} (${fsize} bytes)"
            ready=$((ready + 1))
            continue
        fi
        log_error "  缺少 forecast hour: f${fh} (${outfile})"
        missing=$((missing + 1))
    done

    if [ "$missing" -gt 0 ]; then
        log_error "${provider_upper} 数据不完整: 缺少 ${missing} 个必需 forecast hour"
        write_stage "failed" "error" "${provider_upper} 数据不完整: 缺少 ${missing} 个必需 forecast hour"
        exit 1
    fi

    log_info "${provider_upper} 数据准备完成: 共 ${ready} 个文件 → ${data_dir}/"
}

step1_gfs_data_preparation() { step1_forecast_data_preparation gfs; }
step1_ec_data_preparation() { step1_forecast_data_preparation ec; }

#===============================================================================
# 步骤2: 环境搭建
# 创建带时间特征命名的 WRF/WPS 工作目录
#===============================================================================
step2_environment_setup() {
    log_step "步骤2: 环境搭建 - 加载环境、创建工作目录、复制模型源码"

    # 1. 加载运行环境
    log_info "加载编译和运行环境..."
    load_runtime_environment

    # 2. 创建带时间特征命名的工作目录
    local wps_work_dir="${WORK_DIR}/WPS_${TASK_TAG}"
    local wrf_work_dir="${WORK_DIR}/WRF_${TASK_TAG}"
    local logs_dir="${WORK_DIR}/logs_${TASK_TAG}"

    export WPS_WORK_DIR="$wps_work_dir"
    export WRF_WORK_DIR="$wrf_work_dir"
    export LOGS_DIR="$logs_dir"

    log_info "WPS工作目录: ${WPS_WORK_DIR}"
    log_info "WRF工作目录: ${WRF_WORK_DIR}"
    log_info "日志目录: ${LOGS_DIR}"

    mkdir -p ${WPS_WORK_DIR}
    mkdir -p ${WRF_WORK_DIR}/run
    mkdir -p ${LOGS_DIR}
    check_status "创建工作目录" || exit 1

    # 3. 准备 WPS 运行表
    log_info "准备WPS模型文件..."
    if [ "$WRF_RUNTIME" = "macos" ]; then
        if [ ! -d "${WPS_RUNTIME_DIR}" ] || [ ! -x "${WPS_INSTALL_DIR}/bin/geogrid" ]; then
            log_error "macOS WPS 安装不完整: ${WPS_RUNTIME_DIR} / ${WPS_INSTALL_DIR}"
            exit 1
        fi
        cp -R "${WPS_RUNTIME_DIR}/geogrid" "${WPS_RUNTIME_DIR}/ungrib" \
              "${WPS_RUNTIME_DIR}/metgrid" "${WPS_WORK_DIR}/"
        ln -sf "${WPS_INSTALL_DIR}/bin/geogrid" "${WPS_WORK_DIR}/geogrid.exe"
        ln -sf "${WPS_INSTALL_DIR}/bin/ungrib" "${WPS_WORK_DIR}/ungrib.exe"
        ln -sf "${WPS_INSTALL_DIR}/bin/metgrid" "${WPS_WORK_DIR}/metgrid.exe"
        ln -sf "${WPS_INSTALL_DIR}/bin/link_grib.csh" "${WPS_WORK_DIR}/link_grib.csh"
    elif [ -d "${WPS_SOURCE_DIR}" ]; then
        cp -r ${WPS_SOURCE_DIR}/* ${WPS_WORK_DIR}/
        check_status "复制WPS文件 (${WPS_SOURCE_DIR} → ${WPS_WORK_DIR})" || exit 1
        log_info "WPS源码文件列表:"
        # 文件列表仅用于诊断；pipefail 下 ls 收到 SIGPIPE 不能影响主流程。
        ls -lh "${WPS_WORK_DIR}/" | head -15 || true
    else
        log_error "WPS源码目录不存在: ${WPS_SOURCE_DIR}"
        log_error "请检查 ${MODEL_DIR}/ 下是否有 WPSV4.2 文件夹"
        exit 1
    fi

    # 4. 准备 WRF 运行表
    log_info "准备WRF模型文件..."
    if [ "$WRF_RUNTIME" = "macos" ]; then
        if [ ! -d "${WRF_INSTALL_DIR}/run" ] || [ ! -x "${WRF_INSTALL_DIR}/bin/real" ]; then
            log_error "macOS WRF 安装不完整: ${WRF_INSTALL_DIR}"
            exit 1
        fi
        cp -R "${WRF_INSTALL_DIR}/run/." "${WRF_WORK_DIR}/run/"
        ln -sf "${WRF_INSTALL_DIR}/bin/real" "${WRF_WORK_DIR}/run/real.exe"
        ln -sf "${WRF_INSTALL_DIR}/bin/wrf" "${WRF_WORK_DIR}/run/wrf.exe"
    elif [ -d "${WRF_SOURCE_DIR}" ]; then
        cp -r ${WRF_SOURCE_DIR}/* ${WRF_WORK_DIR}/
        check_status "复制WRF文件 (${WRF_SOURCE_DIR} → ${WRF_WORK_DIR})" || exit 1
        log_info "WRF源码文件列表:"
        ls -lh "${WRF_WORK_DIR}/" | head -15 || true
    else
        log_error "WRF源码目录不存在: ${WRF_SOURCE_DIR}"
        log_error "请检查 ${MODEL_DIR}/ 下是否有 WRFV4.5.2 文件夹"
        exit 1
    fi

    # 检查WPS可执行文件
    log_info "检查WPS可执行文件..."
    for exe in geogrid.exe ungrib.exe metgrid.exe; do
        if [ -f "${WPS_WORK_DIR}/${exe}" ]; then
            log_info "  ✓ 找到: ${exe}"
        else
            log_warn "  ✗ 未找到: ${exe} (可能需要编译)"
        fi
    done

    # 检查WRF可执行文件
    log_info "检查WRF可执行文件..."
    for exe in real.exe wrf.exe; do
        if [ -f "${WRF_WORK_DIR}/run/${exe}" ]; then
            log_info "  ✓ 找到: ${exe}"
        else
            log_warn "  ✗ 未找到: ${exe} (可能需要编译)"
        fi
    done

    log_info "环境搭建完成！"
}

#===============================================================================
# 步骤3: WPS处理
#===============================================================================
step3_wps_processing() {
    log_step "步骤3: WPS处理 - 运行geogrid、ungrib、metgrid"

    log_info "加载运行环境..."
    load_runtime_environment

    local geog_topo_dir="topo_gmted2010_30s"
    if [ "${WRF_GEOG_DATA_RES:-default}" = "lowres" ]; then
        geog_topo_dir="topo_gmted2010_5m"
        if [ "$WRF_RUNTIME" = "macos" ] \
                && [ ! -f "${GEOG_DATA_PATH}/${geog_topo_dir}/index" ] \
                && [ -f "${GEOG_DATA_PATH}/topo_gmted2010_30s/index" ]; then
            log_warn "未安装可选 lowres 5m 地理数据，自动使用已安装的 mandatory 30s 数据"
            export WRF_GEOG_DATA_RES="default"
            geog_topo_dir="topo_gmted2010_30s"
        fi
    fi
    if [ "$WRF_RUNTIME" = "macos" ] && [ ! -f "${GEOG_DATA_PATH}/${geog_topo_dir}/index" ]; then
        log_error "WPS 静态地理数据不完整: 缺少 ${GEOG_DATA_PATH}/${geog_topo_dir}/index"
        log_error "请安装官方 mandatory static data，或将完整 WPS_GEOG 路径设置为 GEOG_DATA_PATH"
        exit 1
    fi

    cd ${WPS_WORK_DIR}
    log_info "当前工作目录: $(pwd)"

    local max_dom=${WRF_MAX_DOM:-2}
    local WPS_START_DATES WPS_END_DATES WPS_PARENT_IDS WPS_GRID_RATIOS WPS_I_STARTS WPS_J_STARTS
    local WPS_E_WE WPS_E_SN WPS_GEOG_RES
    WPS_START_DATES=$(csv_repeat "'${START_YEAR}-${START_MONTH}-${START_DAY}_${START_HOUR}:00:00'")
    WPS_END_DATES=$(csv_repeat "'${END_YEAR}-${END_MONTH}-${END_DAY}_${END_HOUR}:00:00'")
    WPS_PARENT_IDS=$(csv_parent_ids_wps)
    WPS_GRID_RATIOS=$(csv_grid_ratios)
    WPS_I_STARTS=$(csv_parent_starts i)
    WPS_J_STARTS=$(csv_parent_starts j)
    WPS_E_WE=$(csv_join_domains WRF_E_WE 100)
    WPS_E_SN=$(csv_join_domains WRF_E_SN 80)
    WPS_GEOG_RES=$(csv_repeat "'${WRF_GEOG_DATA_RES:-default}'")
    WRF_FORECAST_FILE_INTERVAL_HOURS=${WRF_FORECAST_FILE_INTERVAL_HOURS:-${WRF_GFS_FILE_INTERVAL_HOURS:-${WRF_EC_FILE_INTERVAL_HOURS:-6}}}
    WRF_INTERVAL_SECONDS=$((10#$WRF_FORECAST_FILE_INTERVAL_HOURS * 3600))
    log_info "嵌套层数: ${max_dom}, parent_grid_ratio: ${WPS_GRID_RATIOS}"

    # 3.1 创建namelist.wps配置文件
    log_info "创建namelist.wps配置文件..."
    cat > namelist.wps << WPS_EOF
&share
 wrf_core             = 'ARW'
 max_dom              = ${max_dom}
 start_date           = ${WPS_START_DATES}
 end_date             = ${WPS_END_DATES}
 interval_seconds     = ${WRF_INTERVAL_SECONDS}
 io_form_geogrid      = 2
 debug_level          = 0
/

&geogrid
 parent_id            = ${WPS_PARENT_IDS}
 parent_grid_ratio    = ${WPS_GRID_RATIOS}
 i_parent_start       = ${WPS_I_STARTS}
 j_parent_start       = ${WPS_J_STARTS}
 e_we                 = ${WPS_E_WE}
 e_sn                 = ${WPS_E_SN}
 geog_data_res        = ${WPS_GEOG_RES}
 dx                   = ${WRF_DX_D01:-9000}
 dy                   = ${WRF_DY_D01:-9000}
 map_proj             = 'lambert'
 ref_lat              = ${REF_LAT}
 ref_lon              = ${REF_LON}
 truelat1             = ${REF_LAT}
 truelat2             = ${REF_LAT}
 pole_lat             = 90
 pole_lon             = 0
 stand_lon            = ${REF_LON}
 geog_data_path       = '${GEOG_DATA_PATH}'
 opt_geogrid_tbl_path = './geogrid/'
/

&ungrib
 out_format           = 'WPS'
 prefix               = 'FILE'
/

&metgrid
 fg_name              = 'FILE'
 io_form_metgrid      = 2
 opt_metgrid_tbl_path = './metgrid'
/
WPS_EOF
    check_status "创建namelist.wps" || exit 1
    log_info "namelist.wps创建完成"

    # 3.2 链接模拟时段对应的数据
    local link_count=0
    if [ "${WRF_DATA_SOURCE:-era5}" = "gfs" ] || [ "${WRF_DATA_SOURCE:-era5}" = "ec" ]; then
        local forecast_provider="${WRF_DATA_SOURCE}"
        local data_date data_hour forecast_hours data_root file_prefix file_suffix file_extension
        if [ "$forecast_provider" = "ec" ]; then
            data_date="${WRF_EC_DATE:-}"; data_hour="${WRF_EC_HOUR:-}"; forecast_hours="${WRF_EC_FORECAST_HOURS:-}"
            data_root="${WRF_EC_CACHE_ROOT:-${USER_HOME}/Data/ecdata}"; file_prefix="ecmwf.t"; file_suffix=".ifs.0p25.f"; file_extension=".grib2"
        else
            data_date="${WRF_GFS_DATE:-}"; data_hour="${WRF_GFS_HOUR:-}"; forecast_hours="${WRF_GFS_FORECAST_HOURS:-}"
            data_root="${WRF_GFS_DATA_ROOT:-${USER_HOME}/Data/gfsdata}"; file_prefix="gfs.t"; file_suffix=".pgrb2.0p25.f"; file_extension=""
        fi
        local provider_upper
        provider_upper=$(printf '%s' "$forecast_provider" | tr '[:lower:]' '[:upper:]')
        log_info "链接${provider_upper}缓存数据..."
        if [ -z "$data_date" ] || [ -z "$data_hour" ] || [ -z "$forecast_hours" ]; then
            log_error "缺少 Web 端选择的 ${provider_upper} cycle/forecast hour 信息"
            exit 1
        fi
        data_hour=$(fmt2 "$data_hour")
        local forecast_dir="${data_root}/${data_date}${data_hour}"
        if [ ! -d "$forecast_dir" ]; then
            log_error "${provider_upper}缓存目录不存在: ${forecast_dir}"
            exit 1
        fi
        local ec_compat_dir="${WPS_WORK_DIR}/ec_wps_compat"
        if [ "$forecast_provider" = "ec" ]; then
            mkdir -p "$ec_compat_dir"
            log_info "EC 数据将转换为当前 WPS 可解码格式；原始数据池文件保持不变"
        fi
        for fh in ${forecast_hours}; do
            fh=$(printf "%03d" "$((10#$fh))")
            local file="${forecast_dir}/${file_prefix}${data_hour}z${file_suffix}${fh}${file_extension}"
            if [ -f "$file" ]; then
                if [ "$forecast_provider" = "ec" ]; then
                    local compat_file="${ec_compat_dir}/$(basename "$file")"
                    prepare_ec_grib_for_wps "$file" "$compat_file" || exit 1
                    ln -sf "$compat_file" "$(basename "$file")"
                else
                    ln -sf "$file" "$(basename "$file")"
                fi
                link_count=$((link_count + 1))
            else
                log_error "${provider_upper}缓存文件不存在: ${file}"
                exit 1
            fi
        done
        log_info "共链接 ${link_count} 个${provider_upper}文件"
        if [ "$link_count" -eq 0 ]; then
            log_error "没有找到${provider_upper}缓存文件: ${forecast_dir}"
            exit 1
        fi
    else
        log_info "链接模拟时段对应的ERA5数据..."

        # 遍历年份/月份目录，链接与模拟时段匹配的数据
        local cur_year=$START_YEAR
        while [ "$cur_year" -le "$END_YEAR" ]; do
        local start_m=$((10#$START_MONTH))
        local end_m=$((10#$END_MONTH))
        if [ "$cur_year" -eq "$START_YEAR" ] && [ "$cur_year" -eq "$END_YEAR" ]; then
            # 同年
            first_month=$start_m
            last_month=$end_m
        elif [ "$cur_year" -eq "$START_YEAR" ]; then
            first_month=$start_m
            last_month=12
        elif [ "$cur_year" -eq "$END_YEAR" ]; then
            first_month=1
            last_month=$end_m
        else
            first_month=1
            last_month=12
        fi

        local cur_month=$first_month
        while [ "$cur_month" -le "$last_month" ]; do
            local month_str=$(fmt2 "$cur_month")
            local data_dir="${ERA5_DATA_DIR}/${cur_year}/${month_str}"
            if [ -d "$data_dir" ]; then
                log_info "  链接 ${cur_year}/${month_str} 的数据..."
                for file in ${data_dir}/*; do
                    if [ -f "$file" ] || [ -L "$file" ]; then
                        ln -sf "$file" "$(basename "$file")"
                        link_count=$((link_count + 1))
                    fi
                done
            else
                log_warn "  ${data_dir} 不存在，跳过"
            fi
            cur_month=$((cur_month + 1))
        done
            cur_year=$((cur_year + 1))
        done

        log_info "共链接 ${link_count} 个数据文件"
        if [ "$link_count" -eq 0 ]; then
            log_error "没有找到模拟时段对应的ERA5数据！"
            log_error "请确认 ${ERA5_DATA_DIR}/ 下是否有对应年月的数据"
            exit 1
        fi
    fi
    log_info "当前目录中的GRIB文件:"
    # NOAA 完整 GFS 使用 *.pgrb2.0p25.fNNN 命名。逐个检查通配符，既能
    # 展示真实文件/软链接，也不会让无匹配的 ls 退出码在 set -e 下误杀任务。
    local shown_grib=0 grib_file
    for grib_file in *.grb* *.grib* *.pgrb2* *.nc; do
        if [ -f "$grib_file" ]; then
            ls -lh -- "$grib_file"
            shown_grib=$((shown_grib + 1))
            [ "$shown_grib" -ge 10 ] && break
        fi
    done
    if [ "$shown_grib" -eq 0 ]; then
        log_warn "当前目录未找到可展示的 GRIB/NetCDF 文件；后续严格检查将决定是否继续"
    fi

    # 3.3 设置Vtable (根据数据源选择)
    log_info "设置Vtable链接..."
    if [ "${WRF_DATA_SOURCE:-era5}" = "gfs" ]; then
        if [ -f "ungrib/Variable_Tables/Vtable.GFS" ]; then
            ln -sf ungrib/Variable_Tables/Vtable.GFS Vtable
            log_info "Vtable设置完成 (GFS)"
        else
            log_warn "未找到 Vtable.GFS，回退到 ERA-interim"
            ln -sf ungrib/Variable_Tables/Vtable.ERA-interim.pl Vtable
        fi
    elif [ "${WRF_DATA_SOURCE:-era5}" = "ec" ]; then
        if [ -n "${WRF_EC_VTABLE:-}" ] && [ -f "${WRF_EC_VTABLE}" ]; then
            ln -sf "${WRF_EC_VTABLE}" Vtable
            log_info "Vtable设置完成 (EC Open Data)"
        elif [ -f "ungrib/Variable_Tables/Vtable.ECMWF" ]; then
            ln -sf ungrib/Variable_Tables/Vtable.ECMWF Vtable
            log_info "Vtable设置完成 (ECMWF 回退表)"
        else
            log_error "未找到 EC Vtable"
            exit 1
        fi
    elif [ -f "ungrib/Variable_Tables/Vtable.ERA-interim.pl" ]; then
        ln -sf ungrib/Variable_Tables/Vtable.ERA-interim.pl Vtable
        log_info "Vtable设置完成 (ERA-interim)"
    elif [ -f "ungrib/Variable_Tables/Vtable.ECMWF" ]; then
        ln -sf ungrib/Variable_Tables/Vtable.ECMWF Vtable
        log_info "Vtable设置完成 (ECMWF)"
    else
        log_warn "未找到标准Vtable，请手动设置"
    fi

    # 3.4 运行link_grib.csh
    log_info "运行link_grib.csh处理GRIB文件为标准命名格式..."
    if [ -f "link_grib.csh" ]; then
        # 只传递实际的GRIB文件，避免链接到 . 和 ..
        local grib_files=()
        for f in *.grb *.grib *.grb2 *.grib2 *.pgrb2 *pgrb2* *.GRB *.GRIB; do
            if [ -f "$f" ]; then
                grib_files+=("$f")
            fi
        done

        if [ ${#grib_files[@]} -eq 0 ]; then
            log_error "当前目录没有找到GRIB数据文件，无法运行link_grib.csh"
            log_error "请确认步骤1的数据链接是否成功，或手动检查数据文件"
            exit 1
        fi

        log_info "找到 ${#grib_files[@]} 个GRIB文件，运行link_grib.csh..."
        ./link_grib.csh "${grib_files[@]}" 2>/dev/null
        check_status "link_grib.csh执行" || log_warn "link_grib.csh执行异常"
    else
        log_error "link_grib.csh不存在"
        exit 1
    fi

    log_info "GRIBFILE链接:"
    ls -lh GRIBFILE.* 2>/dev/null | head -10 \
        || log_warn "当前没有可展示的 GRIBFILE 链接，交由 ungrib 输出检查判定"

    # 3.5 运行geogrid.exe。tee 将实时日志转发给本机 Web 任务页；pipefail
    # 会保留可执行程序的真实退出状态。
    log_info "运行geogrid.exe (地理数据插值)..."
    if [ -f "./geogrid.exe" ]; then
        local geogrid_status=0
        if ./geogrid.exe 2>&1 | tee geogrid.log; then
            geogrid_status=0
        else
            geogrid_status=${PIPESTATUS[0]}
        fi
        if [ "$geogrid_status" -ne 0 ]; then
            log_error "geogrid.exe执行失败，查看日志:"
            tail -20 geogrid.log
            exit 1
        fi
        log_info "geogrid.exe执行 - 成功"
        for lvl in $(seq 1 "$max_dom"); do
            local geo_file
            geo_file=$(printf "geo_em.d%02d.nc" "$lvl")
            if [ ! -f "$geo_file" ]; then
                log_error "geogrid.exe 未生成 ${geo_file}，查看日志:"
                tail -30 geogrid.log
                exit 1
            fi
        done
        log_info "geogrid输出保存至: geogrid.log"
    else
        log_error "geogrid.exe不存在，请先编译WPS"
        exit 1
    fi

    # 3.6 运行ungrib.exe
    log_info "运行ungrib.exe (解码GRIB数据)..."
    if [ -f "./ungrib.exe" ]; then
        local ungrib_status=0
        if ./ungrib.exe 2>&1 | tee ungrib.log; then
            ungrib_status=0
        else
            ungrib_status=${PIPESTATUS[0]}
        fi
        if [ "$ungrib_status" -ne 0 ]; then
            if [ "$ungrib_status" -eq 174 ] || grep -q "severe (174).*SIGSEGV" ungrib.log; then
                log_error "ungrib.exe 栈内存异常（SIGSEGV/174）；当前 soft stack=$(ulimit -S -s 2>/dev/null || printf unknown)"
            fi
            log_error "ungrib.exe执行失败（退出码 ${ungrib_status}），查看日志:"
            tail -20 ungrib.log
            exit 1
        fi
        if grep -q "DRS Template[[:space:]]*42[[:space:]]*not defined" ungrib.log; then
            log_error "ungrib.exe 不支持 ECMWF CCSDS 模板 5.42，EC 兼容转换未生效"
            exit 1
        fi
        if ! compgen -G 'FILE:*' >/dev/null || find . -maxdepth 1 -name 'FILE:*' -size 0 | grep -qv '^$'; then
            log_error "ungrib.exe 未生成有效的 FILE:* 中间场"
            exit 1
        fi
        log_info "ungrib输出保存至: ungrib.log"
    else
        log_error "ungrib.exe不存在，请先编译WPS"
        exit 1
    fi

    # 3.7 运行metgrid.exe
    log_info "运行metgrid.exe (气象数据插值)..."
    if [ -f "./metgrid.exe" ]; then
        local metgrid_status=0
        if ./metgrid.exe 2>&1 | tee metgrid.log; then
            metgrid_status=0
        else
            metgrid_status=${PIPESTATUS[0]}
        fi
        if [ "$metgrid_status" -ne 0 ]; then
            log_error "metgrid.exe执行失败，查看日志:"
            tail -20 metgrid.log
            exit 1
        fi
        log_info "metgrid.exe执行 - 成功"
        for lvl in $(seq 1 "$max_dom"); do
            local met_file
            met_file=$(printf "met_em.d%02d.*" "$lvl")
            if ! compgen -G "$met_file" >/dev/null; then
                log_error "metgrid.exe 未生成 D0${lvl} 的 met_em 文件，查看日志:"
                tail -30 metgrid.log
                exit 1
            fi
        done
        log_info "metgrid输出保存至: metgrid.log"
    else
        log_error "metgrid.exe不存在，请先编译WPS"
        exit 1
    fi

    # 检查输出
    log_info "检查WPS输出文件 (met_em.*)..."
    if ls met_em.d0* 1> /dev/null 2>&1; then
        log_info "成功生成的met_em文件:"
        ls -lh met_em.d0*
    else
        log_error "未生成met_em文件，WPS执行可能失败"
        log_error "请检查日志: geogrid.log, ungrib.log, metgrid.log"
        exit 1
    fi

    log_info "WPS处理完成！"
}

#===============================================================================
# 步骤4: WRF运行
#===============================================================================
step4_wrf_running() {
    log_step "步骤4: WRF运行 - 运行real.exe和wrf.exe"

    log_info "加载运行环境..."
    load_runtime_environment

    cd ${WRF_WORK_DIR}/run
    log_info "当前工作目录: $(pwd)"

    local max_dom=${WRF_MAX_DOM:-1}
    local WRF_GRID_RATIOS
    WRF_GRID_RATIOS=$(csv_grid_ratios)
    if [ "$max_dom" -gt 1 ]; then
        for lvl in $(seq 2 "$max_dom"); do
            local parent_dx child_dx
            parent_dx=$(domain_value "WRF_DX" $((lvl - 1)) 27000)
            child_dx=$(domain_value "WRF_DX" "$lvl" 9000)
            if [ "$child_dx" -le 0 ] || [ $(( parent_dx % child_dx )) -ne 0 ]; then
                log_error "无效嵌套分辨率: D0$((lvl - 1))=${parent_dx}m, D0${lvl}=${child_dx}m"
                exit 1
            fi
        done
    fi
    log_info "WRF嵌套层数: ${max_dom}, parent_grid_ratio: ${WRF_GRID_RATIOS}"

    # 4.1 链接WPS生成的met_em文件
    log_info "链接WPS输出文件到WRF运行目录..."
    ln -sf ${WPS_WORK_DIR}/met_em.* .
    log_info "met_em文件链接完成:"
    ls -lh met_em.d0* 2>/dev/null
    # 由 metgrid 实际输出决定大气与土壤垂直层数，避免固定默认值和输入数据
    # 不一致。完整 GFS + Vtable.GFS 应同时生成四层土壤温度和四层土壤湿度。
    if command -v ncdump >/dev/null 2>&1; then
        local met_sample met_header met_levels met_st_levels met_sm_levels
        # WRF 运行目录中的 met_em 文件是指向 WPS 输出的符号链接；跟随链接
        # 后再按普通文件校验，避免将有效的 met_em 误判为不存在。
        met_sample=$(find -L . -maxdepth 1 -type f -name 'met_em.d01.*' -print -quit)
        if [ -z "$met_sample" ]; then
            log_error "未找到 D01 met_em 样本，无法检测垂直层数"
            exit 1
        fi
        met_header=$(ncdump -h "$met_sample" 2>/dev/null)
        met_levels=$(printf '%s\n' "$met_header" | awk '/num_metgrid_levels[[:space:]]*=/ {gsub(/[^0-9]/,"",$0); print; exit}')
        if [[ "$met_levels" =~ ^[0-9]+$ ]] && [ "$met_levels" -gt 0 ]; then
            export WRF_NUM_METGRID_LEVELS="$met_levels"
            log_info "从 met_em 检测到 num_metgrid_levels=${WRF_NUM_METGRID_LEVELS}"
        else
            log_error "无法从 met_em 检测有效的 num_metgrid_levels"
            exit 1
        fi

        met_st_levels=$(printf '%s\n' "$met_header" | awk '/num_st_layers[[:space:]]*=/ {gsub(/[^0-9]/,"",$0); print; exit}')
        met_sm_levels=$(printf '%s\n' "$met_header" | awk '/num_sm_layers[[:space:]]*=/ {gsub(/[^0-9]/,"",$0); print; exit}')
        if ! [[ "$met_st_levels" =~ ^[0-9]+$ ]] || ! [[ "$met_sm_levels" =~ ^[0-9]+$ ]]; then
            log_error "met_em 缺少土壤层维度；GFS 文件可能不是 NOAA 完整文件"
            exit 1
        fi
        if [ "$met_st_levels" -ne 4 ] || [ "$met_sm_levels" -ne 4 ]; then
            log_error "GFS 土壤层不完整: 温度=${met_st_levels} 层, 湿度=${met_sm_levels} 层；完整 GFS 应均为 4 层"
            exit 1
        fi
        export WRF_NUM_METGRID_SOIL_LEVELS="$met_st_levels"
        log_info "从 met_em 检测到 num_metgrid_soil_levels=${WRF_NUM_METGRID_SOIL_LEVELS}（温度/湿度均为 4 层）"
    else
        log_error "未找到 ncdump，无法校验 met_em 大气和土壤层数"
        exit 1
    fi

    # 4.2 动态计算运行时长 (end - start)，跨平台 date
    log_info "计算运行时长..."
    start_epoch=$(date_to_epoch "${START_YEAR}-${START_MONTH}-${START_DAY} ${START_HOUR}:00:00")
    end_epoch=$(date_to_epoch "${END_YEAR}-${END_MONTH}-${END_DAY} ${END_HOUR}:00:00")
    if [ "${start_epoch}" = "0" ] || [ "${end_epoch}" = "0" ]; then
        # date_to_epoch 失败时回退：用日期差值估算天数
        RUN_DAYS=$(( (END_YEAR - START_YEAR) * 365 + (END_MONTH - START_MONTH) * 30 + (END_DAY - START_DAY) ))
        RUN_HOURS=$(( END_HOUR - START_HOUR ))
        RUN_MINUTES=0
        RUN_SECONDS=0
        diff_seconds=$(( RUN_DAYS * 86400 + RUN_HOURS * 3600 ))
        log_warn "日期计算回退到估算模式: ${RUN_DAYS}天 ${RUN_HOURS}时"
    else
        diff_seconds=$((end_epoch - start_epoch))
        RUN_DAYS=$((diff_seconds / 86400))
        RUN_HOURS=$(((diff_seconds % 86400) / 3600))
        RUN_MINUTES=$(((diff_seconds % 3600) / 60))
        RUN_SECONDS=$((diff_seconds % 60))
    fi
    log_info "运行时长: ${RUN_DAYS}天 ${RUN_HOURS}时 ${RUN_MINUTES}分 ${RUN_SECONDS}秒 (${diff_seconds}s)"

    # 4.3 动态计算 time_step (CFL: time_step ≤ 6×dx(km)，取 5×dx 更稳健)
    WRF_DX=${WRF_DX:-9000}
    DX_OUTER_KM=$(( ${WRF_DX} / 1000 ))
    TIME_STEP=$((5 * DX_OUTER_KM))
    log_info "外层分辨率: ${WRF_DX}m, time_step: ${TIME_STEP}s (5×${DX_OUTER_KM}km)"

    # 4.4 创建namelist.input配置文件
    log_info "创建namelist.input配置文件..."
    local WRF_START_YEARS WRF_START_MONTHS WRF_START_DAYS WRF_START_HOURS
    local WRF_END_YEARS WRF_END_MONTHS WRF_END_DAYS WRF_END_HOURS
    local WRF_BOOL_TRUE WRF_HISTORY WRF_HISTORY_BEGIN WRF_E_WE WRF_E_SN WRF_E_VERT WRF_DX_CSV WRF_DY_CSV
    local WRF_GRID_IDS WRF_PARENT_IDS WRF_I_STARTS WRF_J_STARTS WRF_SPECIFIED WRF_NESTED
    local WRF_MP WRF_CU WRF_RA_LW WRF_RA_SW WRF_PBL WRF_SFCLAY WRF_SURFACE WRF_RADT WRF_URBAN
    WRF_START_YEARS=$(csv_repeat "${START_YEAR}")
    WRF_START_MONTHS=$(csv_repeat "${START_MONTH}")
    WRF_START_DAYS=$(csv_repeat "${START_DAY}")
    WRF_START_HOURS=$(csv_repeat "${START_HOUR}")
    WRF_END_YEARS=$(csv_repeat "${END_YEAR}")
    WRF_END_MONTHS=$(csv_repeat "${END_MONTH}")
    WRF_END_DAYS=$(csv_repeat "${END_DAY}")
    WRF_END_HOURS=$(csv_repeat "${END_HOUR}")
    WRF_BOOL_TRUE=$(csv_repeat ".true.")
    WRF_HISTORY=$(csv_repeat "60")
    WRF_HISTORY_BEGIN=$(csv_repeat "${WRF_HISTORY_BEGIN_MINUTES:-0}")
    WRF_E_WE=$(csv_join_domains WRF_E_WE 100)
    WRF_E_SN=$(csv_join_domains WRF_E_SN 80)
    WRF_E_VERT=$(csv_repeat "38")
    WRF_DX_CSV=$(csv_join_domains WRF_DX 27000)
    WRF_DY_CSV=$(csv_join_domains WRF_DY 27000)
    WRF_GRID_IDS=$(csv_grid_ids)
    WRF_PARENT_IDS=$(csv_parent_ids_wrf)
    WRF_I_STARTS=$(csv_parent_starts i)
    WRF_J_STARTS=$(csv_parent_starts j)
    WRF_SPECIFIED=$(csv_nested_flags ".true." ".false.")
    WRF_NESTED=$(csv_nested_flags ".false." ".true.")
    WRF_MP=$(csv_repeat "${WRF_MP_PHYSICS:-8}")
    WRF_CU=${WRF_CU_PHYSICS_BY_DOMAIN:-$(csv_repeat "${WRF_CU_PHYSICS:-0}")}
    WRF_RA_LW=$(csv_repeat "${WRF_RA_LW_PHYSICS:-4}")
    WRF_RA_SW=$(csv_repeat "${WRF_RA_SW_PHYSICS:-4}")
    WRF_PBL=$(csv_repeat "${WRF_BL_PBL_PHYSICS:-1}")
    WRF_SFCLAY=$(csv_repeat "${WRF_SF_SFCLAY_PHYSICS:-1}")
    WRF_SURFACE=$(csv_repeat "${WRF_SF_SURFACE_PHYSICS:-2}")
    WRF_RADT=$(csv_repeat "${WRF_RADT:-5}")
    WRF_URBAN=${WRF_SF_URBAN_PHYSICS_BY_DOMAIN:-$(csv_repeat "${WRF_SF_URBAN_PHYSICS:-0}")}
    WRF_FORECAST_FILE_INTERVAL_HOURS=${WRF_FORECAST_FILE_INTERVAL_HOURS:-${WRF_GFS_FILE_INTERVAL_HOURS:-${WRF_EC_FILE_INTERVAL_HOURS:-6}}}
    WRF_INTERVAL_SECONDS=$((10#$WRF_FORECAST_FILE_INTERVAL_HOURS * 3600))
    WRF_ASSIMILATION_SCHEME=${WRF_ASSIMILATION_SCHEME:-off}
    local WRF_GRID_FDDA WRF_FDDA_END_H WRF_GUV WRF_GT WRF_GQ
    WRF_ASSIM_SPINUP_HOURS=${WRF_ASSIM_SPINUP_HOURS:-0}
    if [ "$WRF_ASSIMILATION_SCHEME" = "off" ] || [ "$WRF_ASSIM_SPINUP_HOURS" -le 0 ]; then
        WRF_GRID_FDDA=$(csv_repeat "0")
        WRF_FDDA_END_H=$(csv_repeat "0")
    else
        WRF_GRID_FDDA=$(csv_fdda_by_resolution "${WRF_ASSIM_GRID_FDDA:-1}")
        WRF_FDDA_END_H=$(csv_repeat "${WRF_ASSIM_SPINUP_HOURS}")
    fi
    WRF_GUV=$(csv_repeat "${WRF_ASSIM_GUV:-0.0}")
    WRF_GT=$(csv_repeat "${WRF_ASSIM_GT:-0.0}")
    WRF_GQ=$(csv_repeat "${WRF_ASSIM_GQ:-0.0}")
    cat > namelist.input << WRF_EOF
&time_control
 run_days                            = ${RUN_DAYS},
 run_hours                           = ${RUN_HOURS},
 run_minutes                         = ${RUN_MINUTES},
 run_seconds                         = ${RUN_SECONDS},
 start_year                          = ${WRF_START_YEARS},
 start_month                         = ${WRF_START_MONTHS},
 start_day                           = ${WRF_START_DAYS},
 start_hour                          = ${WRF_START_HOURS},
 start_minute                        = $(csv_repeat "00"),
 start_second                        = $(csv_repeat "00"),
 end_year                            = ${WRF_END_YEARS},
 end_month                           = ${WRF_END_MONTHS},
 end_day                             = ${WRF_END_DAYS},
 end_hour                            = ${WRF_END_HOURS},
 end_minute                          = $(csv_repeat "00"),
 end_second                          = $(csv_repeat "00"),
 interval_seconds                    = ${WRF_INTERVAL_SECONDS},
 input_from_file                     = ${WRF_BOOL_TRUE},
 history_interval                    = ${WRF_HISTORY},
 history_begin                       = ${WRF_HISTORY_BEGIN},
 frames_per_outfile                  = $(csv_repeat "1"),
 restart                             = .false.,
 restart_interval                    = 7200,
 io_form_history                     = 2,
 io_form_restart                     = 2,
 io_form_input                       = 2,
 io_form_boundary                    = 2,
 debug_level                         = 0
 /

&domains
 time_step                           = ${TIME_STEP},
 time_step_fract_num                 = 0,
 time_step_fract_den                 = 1,
 max_dom                             = ${max_dom},
 e_we                                = ${WRF_E_WE},
 e_sn                                = ${WRF_E_SN},
 e_vert                              = ${WRF_E_VERT},
 p_top_requested                     = 5000,
 num_metgrid_levels                  = ${WRF_NUM_METGRID_LEVELS:-38},
 num_metgrid_soil_levels             = ${WRF_NUM_METGRID_SOIL_LEVELS:-4},
 dx                                  = ${WRF_DX_CSV},
 dy                                  = ${WRF_DY_CSV},
 grid_id                             = ${WRF_GRID_IDS},
 parent_id                           = ${WRF_PARENT_IDS},
 i_parent_start                      = ${WRF_I_STARTS},
 j_parent_start                      = ${WRF_J_STARTS},
 parent_grid_ratio                   = ${WRF_GRID_RATIOS},
 parent_time_step_ratio              = ${WRF_GRID_RATIOS},
 feedback                            = 1,
 smooth_option                       = 0
 /

&physics
 mp_physics                          = ${WRF_MP},
 cu_physics                          = ${WRF_CU},
 ra_lw_physics                       = ${WRF_RA_LW},
 ra_sw_physics                       = ${WRF_RA_SW},
 bl_pbl_physics                      = ${WRF_PBL},
 sf_sfclay_physics                   = ${WRF_SFCLAY},
 sf_surface_physics                  = ${WRF_SURFACE},
 radt                                = ${WRF_RADT},
 isfflx                              = 1,
 ifsnow                              = 0,
 icloud                              = 1,
 surface_input_source                = 1,
 num_soil_layers                     = ${WRF_NUM_SOIL_LAYERS:-4},
 num_land_cat                        = ${WRF_NUM_LAND_CAT:-21},
 sf_urban_physics                    = ${WRF_URBAN}
 /

&dynamics
 hybrid_opt                          = 2,
 w_damping                           = 0,
 diff_opt                            = 1,
 km_opt                              = 4,
 non_hydrostatic                     = ${WRF_BOOL_TRUE},
 moist_adv_opt                       = $(csv_repeat "1"),
 scalar_adv_opt                      = $(csv_repeat "1"),
 gwd_opt                             = 0
 /

&bdy_control
 spec_bdy_width                      = 5,
 specified                           = ${WRF_SPECIFIED},
 nested                              = ${WRF_NESTED}
 /

&fdda
 grid_fdda                           = ${WRF_GRID_FDDA},
 gfdda_inname                        = "wrffdda_d<domain>",
 gfdda_end_h                         = ${WRF_FDDA_END_H},
 gfdda_interval_m                    = $(csv_repeat "$((10#$WRF_FORECAST_FILE_INTERVAL_HOURS * 60))"),
 io_form_gfdda                       = 2,
 fgdt                                = $(csv_repeat "0"),
 if_no_pbl_nudging_uv                = $(csv_repeat "1"),
 if_no_pbl_nudging_t                 = $(csv_repeat "1"),
 if_no_pbl_nudging_q                 = $(csv_repeat "1"),
 if_zfac_uv                          = $(csv_repeat "0"),
 if_zfac_t                           = $(csv_repeat "0"),
 if_zfac_q                           = $(csv_repeat "0"),
 guv                                 = ${WRF_GUV},
 gt                                  = ${WRF_GT},
 gq                                  = ${WRF_GQ},
 if_ramping                          = 1,
 dtramp_min                          = 60.0
 /

&grib2
 /

&namelist_quilt
 nio_tasks_per_group                 = 0,
 nio_groups                          = 1
 /
WRF_EOF
    check_status "创建namelist.input" || exit 1
    log_info "namelist.input创建完成"
    rm -f real.log wrf.log rsl.out.* rsl.error.* wrfinput_d0* wrfbdy_d01 wrffdda_d0*

    # 4.5 运行real.exe
    log_info "运行real.exe (生成初始和边界条件)..."
    if [ -f "./real.exe" ]; then
        local real_status=0
        if ./real.exe 2>&1 | tee real.log; then
            real_status=0
        else
            real_status=${PIPESTATUS[0]}
        fi
        if [ "$real_status" -ne 0 ]; then
            log_error "real.exe执行失败，查看日志:"
            tail -20 real.log
            exit 1
        fi
        log_info "real.exe执行 - 成功"
        log_info "real输出保存至: real.log"
    else
        log_error "real.exe不存在，请先编译WRF"
        exit 1
    fi

    # 检查real.exe输出
    log_info "检查real.exe输出文件..."
    for lvl in $(seq 1 "$max_dom"); do
        file=$(printf "wrfinput_d%02d" "$lvl")
        if [ -f "$file" ]; then
            log_info "  ✓ 找到: $file"
        else
            log_error "  ✗ 缺失: $file"
            exit 1
        fi
    done
    if [ ! -f "wrfbdy_d01" ]; then
        log_error "  ✗ 缺失: wrfbdy_d01"
        exit 1
    fi
    if [ "$WRF_ASSIMILATION_SCHEME" != "off" ] && [ "${WRF_ASSIM_SPINUP_HOURS:-0}" -gt 0 ]; then
        log_info "检查 real.exe 生成的网格松弛输入..."
        for lvl in $(seq 1 "$max_dom"); do
            local domain_dx
            domain_dx=$(domain_value WRF_DX "$lvl" "27000")
            if [ "${domain_dx:-0}" -lt 9000 ]; then
                log_info "  - d$(fmt2 "$lvl") 分辨率 ${domain_dx}m，不启用 spin-up 网格松弛"
                continue
            fi
            file=$(printf "wrffdda_d%02d" "$lvl")
            if [ -s "$file" ]; then
                log_info "  ✓ 找到: $file"
            else
                log_error "  ✗ 同化方案 ${WRF_ASSIMILATION_SCHEME} 缺失: $file"
                exit 1
            fi
        done
    else
        log_info "同化方案已关闭，不要求 wrffdda_d*"
    fi

    # 4.6 运行wrf.exe
    log_info "运行wrf.exe (开始数值模拟)..."
    local requested_mpi_processes="${WRF_MPI_PROCESSES:-4}"
    local mpi_processes
    mpi_processes=$(select_safe_mpi_processes "$requested_mpi_processes")
    log_info "使用MPI并行，进程数: ${mpi_processes}"
    if [ -f "./wrf.exe" ]; then
        # WRF 的逐步 Timing 与错误信息主要写入 rsl.error.0000，而非 stdout。
        # 使用有限的增量轮询转发日志。不要使用永久 tail -F 管道：杀掉外层
        # 子 shell 后，tail/sed 仍可能持有 Web 服务的 stdout，导致模型已经完成
        # 但后端永远等不到 EOF，无法进入 wrfout 归档和可视化阶段。
        mpirun -np "${mpi_processes}" ./wrf.exe > wrf.log 2>&1 &
        local wrf_pid=$!
        (
            local emitted_lines=0
            while ! [ -f rsl.error.0000 ]; do
                kill -0 "${wrf_pid}" 2>/dev/null || exit 0
                sleep 1
            done
            while kill -0 "${wrf_pid}" 2>/dev/null; do
                local current_lines
                current_lines=$(wc -l < rsl.error.0000 2>/dev/null || echo 0)
                if [ "${current_lines}" -gt "${emitted_lines}" ]; then
                    sed -n "$((emitted_lines + 1)),${current_lines}p" rsl.error.0000 | sed -u 's/^/[wrf-rsl] /'
                    emitted_lines=${current_lines}
                fi
                sleep 1
            done
            # mpirun 退出和最后一批日志落盘之间可能有极短延迟，退出前再补发一次。
            sleep 1
            local final_lines
            final_lines=$(wc -l < rsl.error.0000 2>/dev/null || echo 0)
            if [ "${final_lines}" -gt "${emitted_lines}" ]; then
                sed -n "$((emitted_lines + 1)),${final_lines}p" rsl.error.0000 | sed -u 's/^/[wrf-rsl] /'
            fi
        ) &
        local rsl_tail_pid=$!
        local wrf_exit=0
        wait "${wrf_pid}" || wrf_exit=$?
        wait "${rsl_tail_pid}" 2>/dev/null || true
        if [ "${wrf_exit}" -ne 0 ]; then
            log_error "wrf.exe执行失败，查看日志:"
            tail -30 wrf.log
            exit 1
        fi
        log_info "wrf输出保存至: wrf.log"
    else
        log_error "wrf.exe不存在，请先编译WRF"
        exit 1
    fi

    # 检查WRF输出
    log_info "检查WRF模拟结果..."
    if ls wrfout_d01_* 1> /dev/null 2>&1; then
        log_info "成功生成的WRF输出文件:"
        for lvl in $(seq 1 "$max_dom"); do
            dom=$(printf "d%02d" "$lvl")
            ls -lh wrfout_${dom}_* 2>/dev/null || log_warn "未找到 wrfout_${dom}_*"
        done
    else
        log_error "未生成wrfout文件，模拟可能失败"
        log_error "请检查wrf.log文件"
        exit 1
    fi

    log_info "WRF运行完成！"
}

#===============================================================================
# 主程序
#===============================================================================
main() {
    load_task_config
    STAGE_STATUS_FILE="${STAGE_STATUS_FILE:-${WORK_DIR}/stage_status_${TASK_TAG}.jsonl}"
    : > "$STAGE_STATUS_FILE" 2>/dev/null || true
    echo -e "${BLUE}"
    echo "╔═══════════════════════════════════════════════════════════════════╗"
    echo "║                                                                   ║"
    echo "║              WRF 数值天气预报自动化处理流程                      ║"
    echo "║                                                                   ║"
    echo "║  主目录: ${USER_HOME}"
    echo "║  模拟时段: ${START_YEAR}-${START_MONTH}-${START_DAY} ${START_HOUR}:00 至 ${END_YEAR}-${END_MONTH}-${END_DAY} ${END_HOUR}:00"
    echo "║  任务标识: ${TASK_TAG}"
    echo "║  执行时间: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "║                                                                   ║"
    echo "╚═══════════════════════════════════════════════════════════════════╝"
    echo -e "${NC}"

    # 确认执行（非交互模式跳过）
    if [ "${WRF_NONINTERACTIVE:-}" != "true" ]; then
        echo -e "${YELLOW}请确认以上配置信息无误${NC}"
        read -p "是否继续执行？(y/n): " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            log_info "用户取消执行"
            exit 0
        fi
    fi

    # 记录开始时间
    local start_time=$(date +%s)
    write_stage "prepare" "running" "任务开始"

    # 预计算 epoch 时间戳 (跨平台，GFS 下载和 run_days 计算依赖)
    START_EPOCH=$(date_to_epoch "${START_YEAR}-${START_MONTH}-${START_DAY} ${START_HOUR}:00:00")
    END_EPOCH=$(date_to_epoch "${END_YEAR}-${END_MONTH}-${END_DAY} ${END_HOUR}:00:00")

    # 执行各个步骤 (根据数据源分发)
    if [ "${WRF_DATA_SOURCE:-era5}" = "gfs" ]; then
        log_info "数据源: GFS"
        write_stage "data" "running" "准备GFS数据"
        step1_gfs_data_preparation
    elif [ "${WRF_DATA_SOURCE:-era5}" = "ec" ]; then
        log_info "数据源: ECMWF IFS Open Data"
        write_stage "data" "running" "准备EC数据"
        step1_ec_data_preparation
    else
        log_info "数据源: ERA5"
        write_stage "data" "running" "准备ERA5数据"
        step1_data_preparation
    fi
    write_stage "data" "done" "数据准备完成"
    write_stage "workspace" "running" "创建工作目录"
    step2_environment_setup
    write_stage "workspace" "done" "工作目录完成"
    write_stage "wps" "running" "运行WPS"
    step3_wps_processing
    write_stage "wps" "done" "WPS完成"
    write_stage "wrf" "running" "运行real/wrf"
    step4_wrf_running
    write_stage "wrf" "done" "WRF完成"

    # 计算总耗时
    local end_time=$(date +%s)
    local duration=$((end_time - start_time))
    local hours=$((duration / 3600))
    local minutes=$(((duration % 3600) / 60))
    local seconds=$((duration % 60))

    # 最终总结
    echo -e "\n${GREEN}"
    echo "╔═══════════════════════════════════════════════════════════════════╗"
    echo "║                   所有步骤成功完成！                            ║"
    echo "║                                                                   ║"
    echo "║  总耗时: ${hours}时 ${minutes}分 ${seconds}秒                                    ║"
    echo "║                                                                   ║"
    echo "║  输出文件位置:                                                    ║"
    echo "║  ├── WPS输出: ${WPS_WORK_DIR}/met_em.*          ║"
    echo "║  ├── WRF输出: ${WRF_WORK_DIR}/run/wrfout_*      ║"
    echo "║  ├── 日志文件: ${LOGS_DIR}/                     ║"
    echo "║  └── 详细日志:                                                    ║"
    echo "║      ├── geogrid.log    - 地理数据插值日志                        ║"
    echo "║      ├── ungrib.log     - GRIB数据解码日志                        ║"
    echo "║      ├── metgrid.log    - 气象数据插值日志                        ║"
    echo "║      ├── real.log       - 初始条件生成日志                        ║"
    echo "║      └── wrf.log        - WRF模拟日志                             ║"
    echo "╚═══════════════════════════════════════════════════════════════════╝"
    echo -e "${NC}"

    # 保存完整日志
    # 日志归档仅为辅助信息。本机任务没有 nohup/wrf_run 日志时不能因 set -e
    # 被误判为模型失败，否则会阻断后续 wrfout 归档和可视化。
    cp ${WPS_WORK_DIR}/*.log ${LOGS_DIR}/ 2>/dev/null || true
    cp ${WRF_WORK_DIR}/run/*.log ${LOGS_DIR}/ 2>/dev/null || true
    cp "${WORK_DIR}/stage_status_${TASK_TAG}.jsonl" "${LOGS_DIR}/" 2>/dev/null || true
    cp "${WORK_DIR}/wrf_nohup_${TASK_TAG}.log" "${LOGS_DIR}/" 2>/dev/null || true
    cp "${WORK_DIR}/wrf_run_${TASK_TAG}.log" "${LOGS_DIR}/" 2>/dev/null || true
    log_info "所有日志已保存至: ${LOGS_DIR}/"
    write_stage "done" "done" "所有步骤成功完成"
}

#===============================================================================
# 脚本入口
#===============================================================================
on_wrf_error() {
    local exit_code="$1"
    local line="$2"
    local command="$3"
    set +e
    log_error "脚本失败: line=${line}, exit=${exit_code}, command=${command}"
    write_stage "failed" "error" "第 ${line} 行失败，退出码 ${exit_code}: ${command}"
    exit "$exit_code"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    trap 'on_wrf_error "$?" "$LINENO" "$BASH_COMMAND"' ERR
    main "$@"
fi
