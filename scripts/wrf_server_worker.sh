#!/usr/bin/env bash
# Compatibility tombstone: do not revive the former filesystem task scheduler.
set -euo pipefail
echo 'WRF_LEGACY_WORKER_DISABLED|submit_and_recover_tasks_through_backend_wrf' >&2
exit 3
