# Small-domain MPI acceptance — 2026-10-14

## Cause and implementation

Configured CPU MPI count is 4; the existing pinned wrf.sh selects 1 for domains smaller than its conservative 10*ceil(sqrt(ranks)) rule. Admission previously rejected the configured count before this fallback and incorrectly subtracted one from domain dimensions. Production original source /home/tx-lab/WRFwork/WRF_BUILD/WRF_CPU/share/module_check_a_mundo.F:2981 checks integer e_we/nproc_x and e_sn/nproc_y against 10. frame/module_dm.F handles automatic and partially explicit decomposition, with a required product equal to MPI ranks.

runtime_release.mpi_budget mirrors the existing runner fallback, checks one common decomposition across every domain, honors explicit/partial overrides and preserves request geometry. Invalid decomposition reports configured and actual ranks, dimensions and corrective parameter settings. main maps these request errors to 422. hpc_transport passes the selected count to both source launch commands; task_manager records the budget and fallback log. No remote immutable script, generator fingerprint, database schema or endpoint changes. No historical plan mutation.

## Verification

74 focused tests: test_small_domain_mpi, test_runtime_environment, test_task_manager_safety, test_http_safety, test_ordinary_ui2, test_namelist_config. Includes 10x10, 19/20 boundary, elongated and nested domains, original shell function agreement, explicit/partial conflicts, blocked submission with no task, no cache contamination, memory rejection and both source launch environments. 4 maintenance tests passed. Initial added HTTP assertion used an unsupported test-store offset argument; corrected to the existing list API, then reran the suite successfully. A first test-file creation used the wrong working-directory prefix; no file was written, and the final test file and suite were verified. Existing rasterio PendingDeprecationWarnings remain.

Production read-only require_ready: release ordinary-20261012-12, ready; 10x10/88 levels selected MPI=1, decomposition1x1, configured4, conservative RAM2GiB. Original source read verified actual WRF cell rule. No calculation started or task created. Existing shell function tested in isolation; no frontend source changed.

Original wrf_maintenance.py preflight passed, followed by --execute --maintenance-window: both APIs warm reloaded, same remote attempts verified, other services and Redis untouched. No transfer, reference or cleanup rule changes. This validates admission and launch wiring, not a completed scientific simulation.
