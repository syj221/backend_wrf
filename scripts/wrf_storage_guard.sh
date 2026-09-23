#!/usr/bin/env bash
# Resolve the tx-lab data volume by UUID and refuse to run on an unmounted
# system-disk directory. This file is safe to source from other entrypoints.
set -euo pipefail

wrf_require_storage() {
    local storage_uuid="${WRF_STORAGE_UUID:-${WRF_HPC_STORAGE_UUID:-7EEE64E3EE6494E5}}"
    local storage_alias="${WRF_STORAGE_ALIAS:-${WRF_HPC_STORAGE_ALIAS:-/home/tx-lab/.wrf-data}}"
    local mount_target_escaped mount_target marker

    mount_target_escaped="$(findmnt -rn -S "UUID=${storage_uuid}" -o TARGET 2>/dev/null | head -n 1 || true)"
    mount_target="$(printf '%b' "$mount_target_escaped")"
    if [[ -z "$mount_target" || ! -d "$mount_target/WRFwork" ]]; then
        echo "WRF_STORAGE_LAYOUT_MISSING|uuid=${storage_uuid}" >&2
        return 23
    fi

    marker="$mount_target/WRFwork/.wrf-data-root"
    if [[ ! -f "$marker" ]]; then
        echo "WRF_STORAGE_MARKER_MISSING|path=${marker}" >&2
        return 24
    fi

    if [[ -L "$storage_alias" ]]; then
        ln -sfn -- "$mount_target" "$storage_alias"
    elif [[ -e "$storage_alias" ]]; then
        echo "WRF_STORAGE_ALIAS_CONFLICT|path=${storage_alias}" >&2
        return 25
    else
        ln -s -- "$mount_target" "$storage_alias"
    fi

    export WRF_STORAGE_MOUNT="$mount_target"
    export WRF_STORAGE_ALIAS="$storage_alias"
    printf 'WRF_STORAGE_READY|uuid=%s|mount=%s|alias=%s\n' \
        "$storage_uuid" "$mount_target" "$storage_alias"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    wrf_require_storage
fi
