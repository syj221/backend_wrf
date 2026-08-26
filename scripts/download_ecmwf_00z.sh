#!/usr/bin/env bash
# ECMWF 00Z 共享数据池下载入口。
#
# 默认只做“已有文件校验 + manifest 重建”。如需真正联网下载，可在服务器设置
# WRF_EC_URL_TEMPLATE，例如：
#   https://example/{cycle}/ecmwf.t00z.ifs.0p25.f{fh}.grib2
# 脚本会把 {cycle} 和 {fh} 替换为周期与三位预报时效。
set -euo pipefail

cycle="${1:-}"
horizon="${2:-72}"
requested="${3:-${WRF_EC_REQUESTED_HOURS:-}}"
root="${WRF_EC_DATA_ROOT:-${WRF_HPC_EC_DIR:-/home/tx-lab/WRFwork/DATA/ECMWF_CHINA}}"
template="${WRF_EC_URL_TEMPLATE:-}"
min_bytes="${WRF_EC_MIN_BYTES:-1048576}"

if [[ ! "$cycle" =~ ^[0-9]{10}$ || "${cycle:8:2}" != "00" ]]; then
    echo "ERROR|ECMWF cycle 必须是 YYYYMMDD00"
    exit 2
fi

if [[ -z "$requested" ]]; then
    requested="$(seq -s, 0 "$horizon")"
fi

target_dir="${root}/${cycle}"
log_dir="${root}/logs"
lock_path="${root}/.download.v2.lock"
mkdir -p "$target_dir" "$log_dir"

manifest_json() {
    python3 - "$cycle" "$target_dir" <<'PY'
import hashlib, json, sys
from datetime import datetime, timezone
from pathlib import Path

cycle = sys.argv[1]
target = Path(sys.argv[2])
files = []
for path in sorted(target.glob("ecmwf.t??z.ifs.0p25.f???.grib2")):
    try:
        hour = int(path.name.rsplit("f", 1)[1].split(".", 1)[0])
    except Exception:
        continue
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    files.append({"name": path.name, "forecast_hour": hour, "size": path.stat().st_size, "sha256": digest})
available = sorted({item["forecast_hour"] for item in files})
print(json.dumps({
    "provider": "ecmwf",
    "product": "ecmwf-ifs-0p25",
    "scope": "regional_subset",
    "source": "ECMWF IFS",
    "cycle": cycle,
    "forecast_hours": available,
    "complete": all(hour in available for hour in range(73)),
    "files": files,
    "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    "managed_by": "backend_wrf_remote_pool",
}, ensure_ascii=False, indent=2))
PY
}

download_one() {
    local fh="$1"
    local name="ecmwf.t00z.ifs.0p25.f${fh}.grib2"
    local out="${target_dir}/${name}"
    if [[ -s "$out" ]] \
        && [[ "$(head -c 4 "$out" 2>/dev/null)" == "GRIB" ]] \
        && [[ "$(tail -c 4 "$out" 2>/dev/null)" == "7777" ]] \
        && [[ "$(wc -c < "$out")" -ge "$min_bytes" ]]; then
        return 0
    fi
    if [[ -z "$template" ]]; then
        echo "ERROR|缺少 ${name}，且未配置 WRF_EC_URL_TEMPLATE"
        return 1
    fi
    local url="${template//\{cycle\}/$cycle}"
    url="${url//\{fh\}/$fh}"
    local part="${out}.part.$$"
    curl -fL --retry 3 --connect-timeout 30 --speed-time 120 --speed-limit 1024 -o "$part" "$url"
    if [[ "$(head -c 4 "$part" 2>/dev/null)" != "GRIB" || "$(tail -c 4 "$part" 2>/dev/null)" != "7777" ]]; then
        rm -f "$part"
        echo "ERROR|下载文件不是完整 GRIB：${name}"
        return 1
    fi
    mv -f "$part" "$out"
}

(
    flock -n 9 || { echo "RUNNING|shared"; exit 0; }
    IFS=',' read -r -a hours <<< "$requested"
    ready=0
    total=0
    for hour in "${hours[@]}"; do
        [[ -z "$hour" ]] && continue
        printf -v fh "%03d" "$hour"
        total=$((total + 1))
        if download_one "$fh"; then
            ready=$((ready + 1))
        fi
    done
    manifest_json > "${target_dir}/manifest.json"
    if [[ "$ready" -eq "$total" ]]; then
        echo "READY|${ready}/${total}"
    else
        echo "ERROR|ECMWF 文件未全部就绪：${ready}/${total}"
        exit 1
    fi
) 9>"$lock_path"
