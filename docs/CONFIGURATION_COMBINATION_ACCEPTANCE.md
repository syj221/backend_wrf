# WRF configuration combinations — 2026-10-15

## Behavior and authority

Configuration preview and final submission now share the source-backed `resolve` checks. The workbench debounces the existing compute-free `/namelist/resolve` call; errors disable submission and show actual values and adjustments. Input-dependent findings are explicitly `pending`. Preview never applies its returned effective values to the editor. Request sequence, abort and full configuration signatures reject stale success and error responses; final submission still validates its frozen snapshot. A failed preview can be retried and final validation remains mandatory. No additional confirmation dialog.

New tasks and manual recovery use the same deterministic validation. Legacy HTTP submission now also goes through this path without converting its generated-file contract. Existing task requests, attempts, logs and immutable compute packages are not rewritten.

## Vertical initialization

Authority: installed WRF V4.7.1 source at `ordinary-20261008-04/wrf/dyn_em/module_initialize_real.F`, `compute_eta` and `levels`, with constants in `share/module_model_constants.F` and base-state values from `const_module_initialize`.

- Automatic option 2 replays default-REAL arithmetic with float32 intermediates. It checks the actual early-loop exit needed to reach the lid, the `1.5 * max_dz` upper-layer bound, and generated eta ordering.
- Automatic option 1 checks the initial PBL/top estimate and the subsequent five-by-ten convergence thickness check. Its fixed indexing requires at least 12 full levels.
- Explicit eta tables retain their supplied values: count equals e_vert, endpoints 1/0, strict monotonicity including single-precision distinguishability. The automatic thickness checks do not apply to explicit tables.
- Model top must be positive and below the reference surface pressure; invalid base pressure/temperature and zero base lapse are rejected. Automatic thickness/stretching inputs must be positive and representable.
- Errors distinguish unreachable top, excessive upper thickness and invalid eta. No scientific parameter is automatically changed. Increasing pressure lowers the model lid; decreasing pressure raises it.

The real failed GFS combination is 20 levels, 5000 Pa, max_dz=1000 m, dzbot=50 m, dzstretch_s/u=1.3/1.1. Keeping these other values, the first algorithmically passing count is **33**. This is an initialization result, not a recommendation about forecast skill. Twenty levels remain admissible with a valid explicit eta table or another compatible lid/thickness combination. Actual meteorological coverage remains pending even then.

## Configuration audit

| Area | Deterministic checks | Conditions still requiring actual inputs |
| --- | --- | --- |
| Types / choices | Existing source catalogue and candidates; finite values; 32-bit INTEGER bounds | External table/file availability |
| Time / spin-up / FDDA | Existing UTC, boundary alignment, spin-up duration and FDDA interval/end checks | Actual forcing timestamps and completeness |
| Geometry / projection | Existing parent tree, grid ratios, alignment, siblings, boundaries and projection checks | Actual terrain, map factors and field coverage |
| Vertical grid | Both automatic algorithms, explicit eta and base-state guards | Forcing highest pressure level, surface pressure and valid input layers |
| Time step / output | Integer-second service contract, explicit upper limit and common-divisor message; per-domain output alignment; nonempty output window | CFL and numerical stability |
| Physics | PBL/surface compatibility with actual values and allowed choices; native cross-domain restrictions; soil count versus LSM; SMS-3DTKE, shallow convection, cloud ice, irrigation, urban mosaic, TEMF, KF diagnostics, eclipse, wind farms, aerosol update dependencies | Enabled urban, irrigation, aerosol and wind-farm input requirements are labelled individually |
| MPI / resources | Existing `mpi_budget` reused in parameter preview; explicit decomposition/product/block size errors and small-grid one-rank policy retained | Available CPU/memory and admission queue at execution |
| Managed / unsupported modules | Existing managed-field, unsupported module/input-channel and four-soil-layer restrictions retained; unsupported vertical refinement, diagnostic/auxiliary channels rejected | No claim that arbitrary advanced physics combinations are scientifically validated |

A parameter being declared in the catalogue is not a guarantee of complete initialization, stability or forecast accuracy. This work adds deterministic admission checks and explicit pending conditions; it does not launch experiments for every catalogue combination or auto-repair configuration.

## Validation evidence

Evidence directory: `.run/wrf-refactor/config-validation-20261015/`.

- Original Fortran routines were extracted read-only from the pinned source, compiled separately on Linux with `gfortran -ffree-line-length-none -fcheck=bounds -O0`, and run for **65 combinations**. Both algorithms and all observed failure/pass decisions match the Python checker. Temporary compiler/run directories automatically removed. No real WRF execution. The source snapshot, extracted routine, reproduction script and JSON observations are retained in the evidence directory; the reference matrix is also under `test_fixtures/vertical_fortran_reference.json`.
- Focused backend regression includes actual isolated ASGI routes: invalid 20-layer configuration returns 422 before dispatch or resource readiness, preview returns conflict/pending without creating records, invalid manual restart preserves its original attempt/history.
- Dedicated browser coverage: known failure, manually corrected 33-layer input, legal 20-level explicit eta, stale preview response, preview failure/retry, schema conflict versus connection failure, and actual studio desktop/mobile visibility. All task writes are intercepted. Existing geometry, undo, task/restart, result and deletion browser scenarios retain their own server fixtures; their preview is deliberately mocked separately from the real-rule suite.
- Initial regression exposed two obsolete expectations (10 default automatic levels and mixed nonzero cumulus across domains); tests now require rejection while retaining explicit choices. Other first-pass issues were fixture assertions about omitted canonical geometry defaults and an overflow fixture using a REAL field instead of INTEGER. These were corrected; no failed configuration was made to pass by loosening rules.

## Switch and preservation

Final verification and safe-switch results are appended below after the managed maintenance entry completes. No real task creation, restart, cancellation, deletion, forcing refresh, database migration, remote package overwrite or scheduled-monitor activation is part of this acceptance.

### Final acceptance and switch

- **164** focused backend tests passed; **7** maintenance tests passed; original **48** browser tests and **7** new configuration/browser cases passed; final front-end build passed. Existing bundler size/comment and rasterio deprecation warnings remain. The added actual-layout case initially reached the login screen because its isolated fixture omitted login state; after supplying the same synthetic browser-only login convention as existing tests, desktop 1366×768 and mobile 390×844 passed. Screenshot visually inspected and retained. No authentication bypass against production.
- `python3 -B scripts/wrf_maintenance.py --execute --maintenance-window` verified idle task state and the original terminal attempt, then gently reloaded only 8007 and 8012. Both health checks return 200. All eight actual service identities verified; only WRF/AgentScope API identities changed, Redis and other services unchanged.
- Real authenticated compute-free API verification: the original aedb4f66 configuration returns **422** with the specific 20-level/5000-Pa unreachable-top message and 33-level threshold; a validation-only copy set to 33 returns **200**; a validation-only copy with explicit 20-level eta also returns **200**. The latter two show execution source/binary verification and the new ruleset `ordinary-wrf-2026-10-15`. No task was submitted. The actual original request remains 20 levels.
- FastAPI can return a structured `detail` list when its model validator rejects a supplied namelist before the route. The live acceptance script initially expected only string detail; it was corrected to flatten the existing error format, without changing or relaxing production checks. The front-end already formats both forms.
- Actual 5177 Vue module contains the new assessment and stale-response protections. Shared component remains **2.2.4**. Immutable compute release remains **ordinary-20261014-15**; generator files, fingerprints, remote binaries and old compute bindings were not changed.
- Before/after read-only SQLite comparison: all three task rows, task attempts, recovery plans and incident rows exactly equal; archived service log hashes equal. GFS 6683e508 and ECMWF b98e6426 remain succeeded/attempt 1; GFS aedb4f66 remains waiting_restart/attempt 2. No third attempt, cancellation, deletion or real parameter change. No recurring monitor re-enabled.

Evidence: `live-api-proof.json`, `preservation-proof.json`, `before.json`, `test-results.json`, `actual-studio-conflict.png`, source snapshots and Fortran observations under `.run/wrf-refactor/config-validation-20261015/`.
