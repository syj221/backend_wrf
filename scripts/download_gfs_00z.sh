#!/usr/bin/env bash
# tx-lab 中国区域 GFS 0.25° 逐小时下载器。只写入指定周期，不清理任何旧数据。
set -euo pipefail

cycle="${1:-}"
horizon="${2:-72}"
requested_spec="${3:-}"
data_root="${WRF_GFS_DATA_ROOT:-/home/tx-lab/WRFwork/DATA/GFS_CHINA}"
mount_root="${WRF_GFS_MOUNT:-/}"
minimum_free_gb="${WRF_GFS_MIN_FREE_GB:-120}"
minimum_bytes="${WRF_GFS_MIN_BYTES:-1048576}"
minimum_speed_bps="${WRF_GFS_MIN_SPEED_BPS:-65536}"
slow_seconds="${WRF_GFS_SLOW_SECONDS:-120}"
request_interval="${WRF_GFS_REQUEST_INTERVAL_SECONDS:-10}"
region_west="${WRF_GFS_REGION_WEST:-65}"
region_east="${WRF_GFS_REGION_EAST:-145}"
region_south="${WRF_GFS_REGION_SOUTH:-5}"
region_north="${WRF_GFS_REGION_NORTH:-60}"

if [[ ! "$cycle" =~ ^[0-9]{8}00$ ]]; then
    echo "ERROR invalid_cycle: 仅支持 YYYYMMDD00" >&2
    exit 2
fi
if ! [[ "$horizon" =~ ^[0-9]+$ ]] || (( horizon < 0 || horizon > 72 )); then
    echo "ERROR invalid_horizon: 必须为 0-72" >&2
    exit 2
fi
if ! [[ "$minimum_bytes" =~ ^[0-9]+$ ]] || (( minimum_bytes < 65536 )); then
    echo "ERROR invalid_minimum_bytes: 必须不低于 65536" >&2
    exit 2
fi
if ! [[ "$minimum_speed_bps" =~ ^[0-9]+$ ]] || (( minimum_speed_bps < 1024 )); then
    echo "ERROR invalid_minimum_speed: 必须不低于 1024 B/s" >&2
    exit 2
fi
if ! [[ "$slow_seconds" =~ ^[0-9]+$ ]] || (( slow_seconds < 30 )); then
    echo "ERROR invalid_slow_seconds: 必须不低于 30 秒" >&2
    exit 2
fi
if ! [[ "$request_interval" =~ ^[0-9]+$ ]] || (( request_interval < 10 )); then
    echo "ERROR invalid_request_interval: NOMADS 请求间隔必须不低于 10 秒" >&2
    exit 2
fi
for coordinate in "$region_west" "$region_east" "$region_south" "$region_north"; do
    if ! [[ "$coordinate" =~ ^-?[0-9]+([.][0-9]+)?$ ]]; then
        echo "ERROR invalid_region_coordinate: ${coordinate}" >&2
        exit 2
    fi
done

download_hours=()
if [[ -z "$requested_spec" ]]; then
    for ((hour=0; hour<=horizon; hour+=1)); do
        download_hours+=("$hour")
    done
else
    seen_hours=","
    IFS=',' read -r -a requested_hours <<< "$requested_spec"
    for hour in "${requested_hours[@]}"; do
        if ! [[ "$hour" =~ ^[0-9]+$ ]] || (( hour < 0 || hour > 72 )); then
            echo "ERROR invalid_forecast_hour: ${hour}" >&2
            exit 2
        fi
        case "$seen_hours" in
            *",${hour},"*) ;;
            *) seen_hours+="${hour},"; download_hours+=("$hour") ;;
        esac
    done
    if (( ${#download_hours[@]} == 0 )); then
        echo "ERROR empty_forecast_hours" >&2
        exit 2
    fi
fi

if ! command -v findmnt >/dev/null 2>&1 || ! findmnt -rn -T "$mount_root" >/dev/null 2>&1; then
    echo "ERROR gfs_mount_unavailable: ${mount_root} 所在文件系统不可用" >&2
    exit 3
fi
if [[ "$mount_root" != "/" ]]; then
    case "$data_root/" in
        "$mount_root"/*) ;;
        *)
            echo "ERROR unsafe_data_root: ${data_root} 不在 ${mount_root} 下" >&2
            exit 3
            ;;
    esac
fi

if ! mkdir -p "$data_root"; then
    echo "ERROR gfs_storage_io_error: 无法创建 ${data_root}" >&2
    exit 3
fi
write_probe=""
if ! write_probe=$(mktemp "${data_root}/.write-probe.XXXXXX" 2>/dev/null); then
    echo "ERROR gfs_storage_io_error: ${data_root} 无法写入" >&2
    exit 3
fi
rm -f "$write_probe"
if ! command -v flock >/dev/null 2>&1; then
    echo "ERROR missing_command: flock" >&2
    exit 4
fi
if ! command -v curl >/dev/null 2>&1; then
    echo "ERROR missing_command: curl" >&2
    exit 4
fi
exec 9>"${data_root}/.download.v2.lock"
if ! flock -n 9; then
    echo "ERROR another GFS download is already running" >&2
    exit 5
fi

available_gb=$(df -Pk "$data_root" | awk 'NR==2 {print int($4/1024/1024)}')
if [[ ! "$available_gb" =~ ^[0-9]+$ ]] || (( available_gb < minimum_free_gb )); then
    echo "ERROR insufficient_gfs_space: ${available_gb:-unknown}GB < ${minimum_free_gb}GB at ${data_root}" >&2
    exit 7
fi

cycle_date="${cycle:0:8}"
cycle_hour="${cycle:8:2}"
target_dir="${data_root}/${cycle}"
mkdir -p "$target_dir"

validate_grib() {
    local path="$1"
    [[ -s "$path" ]] || return 1
    [[ "$(head -c 4 "$path" 2>/dev/null)" == "GRIB" ]] || return 1
    [[ "$(tail -c 4 "$path" 2>/dev/null)" == "7777" ]] || return 1
    (( $(wc -c < "$path") >= minimum_bytes ))
}

nomads_filter="https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"
nomads_dir="%2Fgfs.${cycle_date}%2F${cycle_hour}%2Fatmos"
last_request_epoch=0

throttle_nomads() {
    local now wait_seconds
    now=$(date +%s)
    if (( last_request_epoch > 0 )); then
        wait_seconds=$(( request_interval - (now - last_request_epoch) ))
        if (( wait_seconds > 0 )); then
            printf 'RATE_LIMIT_WAIT %s seconds\n' "$wait_seconds"
            sleep "$wait_seconds"
        fi
    fi
    last_request_epoch=$(date +%s)
}

regional_url() {
    local name="$1"
    printf '%s?file=%s&all_lev=on&all_var=on&subregion=&leftlon=%s&rightlon=%s&toplat=%s&bottomlat=%s&dir=%s' \
        "$nomads_filter" "$name" "$region_west" "$region_east" "$region_north" "$region_south" "$nomads_dir"
}

download_once() {
    local url="$1" part_path="$2" error_path="$3" resume="$4"
    local resume_args=()
    if [[ "$resume" == "1" ]]; then
        resume_args=(--continue-at -)
    fi
    throttle_nomads
    curl --fail --location "${resume_args[@]}" --connect-timeout 20 --max-time 900 \
        --silent --show-error --speed-limit "$minimum_speed_bps" --speed-time "$slow_seconds" \
        --output "$part_path" "$url" 2>"$error_path"
}

download_hour() {
    local hour="$1"
    local forecast_hour name final_path part_path error_path url attempt preserved
    forecast_hour=$(printf '%03d' "$hour")
    name="gfs.t${cycle_hour}z.pgrb2.0p25.f${forecast_hour}"
    final_path="${target_dir}/${name}"
    if validate_grib "$final_path"; then
        printf 'READY %s\n' "$name"
        return 0
    fi

    part_path="${final_path}.part.nomads"
    error_path="${part_path}.curl-error"
    url=$(regional_url "$name")
    for attempt in 1 2 3; do
        printf 'DOWNLOAD %s source=nomads_filter attempt=%s bounds=%s,%s,%s,%s\n' \
            "$name" "$attempt" "$region_west" "$region_south" "$region_east" "$region_north"
        if download_once "$url" "$part_path" "$error_path" 1; then
            if validate_grib "$part_path"; then
                mv "$part_path" "$final_path"
                rm -f "$error_path"
                printf 'COMPLETED %s %s source=nomads_filter\n' "$name" "$(wc -c < "$final_path")"
                return 0
            fi
            printf 'INVALID_GRIB %s response_was_not_valid_regional_grib2\n' "$name" >&2
            return 1
        fi
        if [[ -s "$part_path" ]] && grep -Eqi 'support byte ranges|cannot resume|requested range' "$error_path"; then
            preserved="${part_path}.unresumable.$(date -u +%Y%m%dT%H%M%SZ)"
            mv "$part_path" "$preserved"
            printf 'PART_PRESERVED %s\n' "$preserved" >&2
            if download_once "$url" "$part_path" "$error_path" 0 && validate_grib "$part_path"; then
                mv "$part_path" "$final_path"
                rm -f "$error_path"
                printf 'COMPLETED %s %s source=nomads_filter\n' "$name" "$(wc -c < "$final_path")"
                return 0
            fi
        fi
        if [[ -s "$error_path" ]]; then
            tail -n 1 "$error_path" >&2
        fi
    done
    printf 'ERROR download_failed: %s\n' "$name" >&2
    return 1
}

printf 'REGIONAL_DOWNLOAD %s bounds=%s,%s,%s,%s interval=%ss\n' \
    "$cycle" "$region_west" "$region_south" "$region_east" "$region_north" "$request_interval"
for hour in "${download_hours[@]}"; do
    if ! download_hour "$hour"; then
        exit 8
    fi
done

printf 'DOWNLOAD_COMPLETE %s %s\n' "$cycle" "${#download_hours[@]}"
