#!/usr/bin/env bash
# 受管高分路线入口：普通 WRF d04 原生输出交接与 d05-d08 ndown 前置检查。
#
# 本入口只接受 backend_wrf 生成的 downscale.job.json。它不会把 WebP 或
# scene.meta.json 当成初始化场，也不会在上游没有 wrfrst 时强行启动 ndown。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
storage_guard="${WRF_STORAGE_GUARD_SCRIPT:-${SCRIPT_DIR}/wrf_storage_guard.sh}"
if [[ ! -r "$storage_guard" ]]; then
    echo "[错误] 缺少数据盘保护脚本: ${storage_guard}" >&2
    exit 3
fi
# shellcheck disable=SC1090
source "$storage_guard"
wrf_require_storage >/dev/null

contract="${WRF_DOWNSCALE_CONTRACT:-}"
task_dir="${WRF_DOWNSCALE_TASK_DIR:-}"
output_dir="${WRF_DOWNSCALE_OUTPUT_DIR:-}"
if [[ -z "$contract" || ! -s "$contract" || -z "$task_dir" || -z "$output_dir" ]]; then
    echo "[错误] 高分任务缺少受管合同、任务目录或输出目录" >&2
    exit 2
fi
if ! command -v python3 >/dev/null 2>&1; then
    echo "[错误] 高分运行需要 python3 解析受管合同" >&2
    exit 2
fi

contract_values=$(python3 - "$contract" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    value = json.load(handle)
upstream = value.get("upstream") or {}
source_artifact = upstream.get("source_artifact") or {}
source_id = str(upstream.get("source_wrf_task_id") or "")
final_domain = str(source_artifact.get("final_domain") or "")
bridge = str((value.get("execution_plan") or {}).get("wrf_les", {}).get("bridge") or "")
target_dx = (value.get("execution_plan") or {}).get("wrf_les", {}).get("target_dx_m")
execution_scope = str(value.get("execution_scope") or "")
upstream_mode = str(upstream.get("mode") or "")
for key, item in (("SOURCE_TASK_ID", source_id), ("SOURCE_FINAL_DOMAIN", final_domain), ("BRIDGE", bridge), ("TARGET_DX_M", target_dx), ("EXECUTION_SCOPE", execution_scope), ("UPSTREAM_MODE", upstream_mode)):
    text = "" if item is None else str(item)
    if any(char in text for char in "\n\r\t;|&$\\"):
        raise SystemExit(f"非法合同字段: {key}")
    print(f"{key}={text}")
PY
)
while IFS='=' read -r key value; do
    case "$key" in
        SOURCE_TASK_ID|SOURCE_FINAL_DOMAIN|BRIDGE|TARGET_DX_M|EXECUTION_SCOPE|UPSTREAM_MODE) printf -v "$key" '%s' "$value" ;;
        *) echo "[错误] 合同字段名无效: ${key}" >&2; exit 2 ;;
    esac
done <<< "$contract_values"

# Phase 2 has a separate source contract and runner.  It must branch before
# the normal WRF d04 hand-off checks below, because a completed forcing task
# is the only input for this CFD-only job.
if [[ "$EXECUTION_SCOPE" == "wrf_cfd" && "$UPSTREAM_MODE" == "reuse_10m" ]]; then
    cfd_script="${WRF_DOWNSCALE_CFD_10M_SCRIPT:-${SCRIPT_DIR}/wrf_hpc_cfd_10m_chain.py}"
    [[ -r "$cfd_script" ]] || { echo "[错误] 缺少约 10 m → CFD 运行器: ${cfd_script}" >&2; exit 20; }
    exec python3 "$cfd_script"
fi

if [[ ! "$SOURCE_TASK_ID" =~ ^wrf_((gfs|ecmwf)_)?[0-9]{8}T[0-9]{6}Z_[0-9a-f]{8}$ ]]; then
    echo "[错误] 合同中的普通 WRF 任务 ID 无效: ${SOURCE_TASK_ID}" >&2
    exit 2
fi
if [[ ! "$SOURCE_FINAL_DOMAIN" =~ ^d[0-9]{2}$ ]]; then
    echo "[错误] 合同缺少普通 WRF 最细域" >&2
    exit 2
fi
if [[ "$SOURCE_FINAL_DOMAIN" != "d04" ]]; then
    echo "[错误] 约 10 m forcing 只允许使用普通 WRF d04 · 1 km 原生输出" >&2
    exit 10
fi

source_run="${WORK_DIR:-/home/tx-lab/.wrf-data/WRFwork/RUNTIME}/WRF_${SOURCE_TASK_ID}/run"
if [[ ! -d "$source_run" ]]; then
    source_run="${WORK_DIR:-${WRF_STORAGE_ALIAS:-/home/tx-lab/.wrf-data}/WRFwork}/RUNTIME/WRF_${SOURCE_TASK_ID}/run"
fi
if [[ ! -d "$source_run" && -n "${WRF_STORAGE_ALIAS:-}" ]]; then
    source_run="${WRF_STORAGE_ALIAS}/WRFwork/RUNTIME/WRF_${SOURCE_TASK_ID}/run"
fi
if [[ ! -d "$source_run" ]]; then
    echo "[错误] 普通 WRF 原生输出目录不存在: ${source_run}" >&2
    exit 11
fi
ndown_exe="${WRF_NDOWN_EXE:-${WRF_CPU_SOURCE_DIR:-}/main/ndown.exe}"
if [[ ! -x "$ndown_exe" ]]; then
    echo "[错误] ndown.exe 不存在或不可执行: ${ndown_exe}" >&2
    exit 12
fi

mapfile -t source_files < <(find -L "$source_run" -maxdepth 1 -type f \
    \( -name "wrfout_${SOURCE_FINAL_DOMAIN}_*" -o -name "wrfrst_${SOURCE_FINAL_DOMAIN}_*" \) \
    -printf '%f\n' | sort)
if (( ${#source_files[@]} == 0 )); then
    echo "[错误] 普通 WRF ${SOURCE_FINAL_DOMAIN} 没有 wrfout/wrfrst 原生输出" >&2
    exit 13
fi
sleep 2
source_manifest=$(mktemp "${task_dir}/source-manifest.XXXXXX")
trap 'rm -f "$source_manifest"' EXIT
python3 - "$source_run" "$SOURCE_TASK_ID" "$SOURCE_FINAL_DOMAIN" "$ndown_exe" "$source_manifest" "${source_files[@]}" <<'PY'
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

root = Path(sys.argv[1])
task_id, final_domain, ndown_exe, manifest_path = sys.argv[2:6]
names = sys.argv[6:]
files = []
for name in names:
    path = root / name
    try:
        first_stat = path.stat()
        first = first_stat.st_size
        second_stat = path.stat()
        second = second_stat.st_size
    except FileNotFoundError:
        continue
    if first <= 0:
        continue
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    files.append({
        "name": name,
        "kind": "history" if name.startswith("wrfout_") else "restart",
        "size_bytes": second,
        "mtime_utc": datetime.fromtimestamp(second_stat.st_mtime, timezone.utc).isoformat().replace("+00:00", "Z"),
        "sha256": digest,
        "stable": first == second,
    })
if not files:
    raise SystemExit("普通 WRF 原生文件为空")
payload = {
    "schema_version": "normal-wrf-source-handoff/v1",
    "source_task_id": task_id,
    "final_domain": final_domain,
    "source_output_dir": str(root),
    "ndown_exe": ndown_exe,
    "history_file_count": sum(item["kind"] == "history" for item in files),
    "restart_file_count": sum(item["kind"] == "restart" for item in files),
    "files": files,
}
serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
payload["inventory_sha256"] = hashlib.sha256(serialized).hexdigest()
Path(manifest_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

restart_count=$(python3 - "$source_manifest" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    print(sum(item.get("kind") == "restart" for item in json.load(handle).get("files", [])))
PY
)
history_count=$(python3 - "$source_manifest" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    print(sum(item.get("kind") == "history" for item in json.load(handle).get("files", [])))
PY
)
if [[ "$restart_count" -eq 0 || "$history_count" -eq 0 ]]; then
    echo "[错误] 普通 WRF ${SOURCE_FINAL_DOMAIN} 必须同时提供 wrfout 与 wrfrst" >&2
    exit 14
fi

mkdir -p "$output_dir"
cp "$source_manifest" "$output_dir/normal-wrf-source-handoff.json"
python3 - "$contract" "$source_manifest" "$output_dir/high-resolution-stage.json" <<'PY'
import json
import sys
from pathlib import Path

contract_path, source_path, output_path = sys.argv[1:]
with open(contract_path, encoding="utf-8") as handle:
    contract = json.load(handle)
with open(source_path, encoding="utf-8") as handle:
    source = json.load(handle)
plan = contract.get("execution_plan") or {}
wrf = plan.get("wrf_les") or {}
stage = {
    "schema_version": "wrf-les-forcing-stage/v2",
    "task_id": contract.get("task_id"),
    "status": "source_handoff",
    "source": source,
    "topology_id": str(wrf.get("topology_id") or "normal-27km-to-wrf-les-10m/v1"),
    "nominal_resolution_m": int(wrf.get("nominal_resolution_m") or 10),
    "wrf_les": wrf,
    "next_stage": "ndown_d04_d05",
    "ready_for_ndown": True,
    "cfd_not_started": True,
}
Path(output_path).write_text(json.dumps(stage, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

echo "SOURCE_HANDOFF_READY|source=${SOURCE_TASK_ID}|domain=${SOURCE_FINAL_DOMAIN}|history=${history_count}|restart=${restart_count}|target_dx=${TARGET_DX_M}|topology=normal-27km-to-wrf-les-10m/v1"
if [[ "${WRF_DOWNSCALE_PREFLIGHT_ONLY:-false}" == "true" ]]; then
    echo "PREFLIGHT_OK|ndown=${ndown_exe}|bridge=${BRIDGE}"
    exit 0
fi

# 生产运行继续进入真实的 d04→d08 分级链。WPS/real 只准备子域初始场，
# 普通 WRF d04 原生历史场和重启场始终是唯一允许的高分父域。
chain_script="${SCRIPT_DIR}/wrf_hpc_downscale_chain.sh"
chain_python="${SCRIPT_DIR}/wrf_hpc_downscale_chain.py"
if [[ "${WRF_DOWNSCALE_RUN_CHAIN:-false}" == "true" ]]; then
    [[ -x "$chain_script" && -r "$chain_python" ]] || { echo "[错误] 缺少高分链编排器: ${chain_script}" >&2; exit 19; }
    echo "WRF_LES_CHAIN_START|next=wps_prepare"
    exec "$chain_script"
fi

# 仅保留手动预检/交接模式；它不会把交接成功冒充 forcing_ready 或 CFD。
echo "HANDOFF_ONLY|next=ndown_d04_d05|cfd=not_started"
exit 0
