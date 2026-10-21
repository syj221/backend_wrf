# WRF/WPS 独立源锚点迁移验收（2026-10-21）

状态：独立源迁移、API安全加载及原04包退役完成。当前运行发布保持 ordinary-20261020-19，指纹 a8e4b47a0ad6719496456af1fe32a0ea4ffd319a4211967a79366a95caa0a73d。没有重启远端计算或下载工作器，也未提交计算、刷新、下载、取消或资料删除请求。

## ordinary-20261008-04 是什么

它是早期普通WRF服务的完整不可变部署快照，不是任务或资料周期。原包包含WRF 4.7.1和WPS 4.6.0完整源码树、当时的可执行文件、运行表和13个服务脚本。后来的瘦发布自带运行所需文件，但其来源校验曾依赖该完整包。

原04包7888文件、1,118,158,465逻辑字节；其中源锚点实际使用下表7873文件、1,117,054,363字节。原包删除不等于净释放同等空间，因为必要源码已完整保留到独立目录。

| 原依赖 | 文件数 | 内容举例 |
| --- | ---: | --- |
| `ordinary-20261008-04/wrf/` | 6073 | `Registry/*`中的编译配置声明，`inc/version_decl`、`inc/namelist_statements.inc`，`phys/`、`dyn_em/`等Fortran源码，`main/wrf.exe`、`main/real.exe`及`run/`运行表 |
| `ordinary-20261008-04/wps/` | 1800 | `geogrid/src/`、`metgrid/src/`、`ungrib/src/read_namelist.F`、版本与namelist说明，三个WPS可执行文件、GRIB链接脚本、Vtable和地理/插值表 |

精确的相对路径和SHA-256清单保存在根 `.run/source-anchor-20261021/source-files.json`；原完整manifest及初始引用扫描在同目录 `before.json`。独立源是只读权威快照，不能直接原地编译；重新编译应复制到独立构建工作区并重新配置，不改该源快照。

## 新的目录与验证合同

稳定入口仍是 `/home/tx-lab/WRF_WORKBENCH/source/wrf-4.7.1` 和 `source/wps-4.6.0`，现在分别指向物理目录：

`/home/tx-lab/WRF_WORKBENCH/source-store/wrf-4.7.1-wps-4.6.0-160901b42cdc1424/{wrf,wps}`

独立目录有自己的 `wrf-source-bundle-1` manifest，文件清单与完整源逐项相同，实际复制而非硬链接；所有源文件和目录封为只读。`provenance/release-manifest.json`和`provenance/source-anchor-v1.json`逐字保留原始来源证明，不读取已删除的04目录。独立源、其provenance和manifest是代码/科学资产，不是可清理缓存。

当前 `source/anchor.json` 为 `wrf-source-anchor-2`，SHA-256为5efe32b3e5e732cf31673307f333041d12d0571698fe795509f7643025a2271b；bundle manifest SHA-256为871f75d3b0a607d9ba8a206669e518097dd2ba510dab5275a77431d2ba2b04c6。源配置摘要 e86c064c555d50c308b2cb5fef128541b8bf46749ae5fe4b51326b4f87c4ee2b 及5个二进制摘要逐项保持，没有自动修改科学参数或重新编译。

原10个瘦发布的清单没有重写。更新后的部署校验器只接受两种精确绑定：当前独立bundle绑定，或从只读provenance完整验证出的原v1冻结绑定。其他绑定拒绝。原清单中的04字符串仅表示来源，不再是执行读取路径。未来新瘦发布记录独立bundle绑定，不再新增旧包运行依赖；原生运行文件选择仍为184个。

## 安全切换与退役

操作员入口 `backend_wrf/scripts/migrate_source_anchor.py` 分为plan、prepare、switch、verify，使用原维护锁和远端deployment.lock。计划37e327bd…绑定原目录device/inode、锚点SHA、完整源和二进制/配置摘要。复制后逐文件校验并fsync文件与目录，用Linux `renameat2(RENAME_EXCHANGE)`原子交换完整锚点目录；服务器旧glibc无包装函数时使用同一个内核系统调用，不降级为非原子交换。switch_requested或已有审计不重放。

已更改的本机配置仅为 `.runtime.env` 两个源目录，均改指稳定独立入口。通过 `scripts/wrf_maintenance.py --execute --maintenance-window` 温和加载8007/8012；门禁未放宽，Redis及其他服务身份保持。

原04退役前再次验证：无任务运行绑定，WRF相关可读进程的命令、环境、文件描述符及映射无引用、无其他包运行依赖或活动符号链接引用，且完整文件清单、原inode和manifest相符。受/proc保护的SSH/SFTP、PAM、密钥环和ssh-agent按实际完整命令及父进程识别，无WRF执行器豁免。仅对原04自有只读目录赋予删除所需属主写权限后删除，保留独立源及原manifest/锚点历史。最终04路径不存在，退役审计complete。

## 验证结果及边界

- 33项源迁移/旧瘦包/部署回归通过；7项服务器真实代码盘原生测试通过，包括旧源目录消失后的旧清单验证、新瘦发布创建、内容/权限异常拒绝、原子交换失败和禁止重放。测试的原生库包装缺失、模块路径问题修正后复验；3个失败测试临时目录精确核验后已清理，测试日志保留。
- 04删除后重新完成当前19包的完整生产部署校验；全部10个旧瘦包的冻结来源/原生清单兼容检查通过。没有用模拟构建结果代替生产校验。
- API真实认证历史可读、匿名401，WRF活动接口及AgentScope健康HTTP200，AgentScope checks均ok。本次无前端源码变更，无需重建前端。
- 221个原资料文件/分片逐项同inode、未缩小、引用相同。56份资料日志与尝试历史逐项字节、SHA及inode相同；任务相关表和已有任务日志快照相同。没有清理业务日志、分片或数据。
- 外部论文任务使用 `/home/tx-lab/WRFwork/WRF_BUILD/WRF_CPU`，源路径和映射均无04依赖。本次未发送远端进程信号或改动其运行目录；它在验收期间自然到达6小时时段终点，`rsl.error.0000`和`rsl.out.0000`都有SUCCESS COMPLETE WRF。不据此宣称性能未受任何共享资源波动影响。
- 资料状态与迁移前相同：20日GFS73/73且manifest完整，21日两源等待发布；20日ECMWF24/25，f012原先已data_integrity失败，无活动工作器。未重试/修复该下载问题，不宣称ECMWF完整或两源正在传输。
- 顺带验证真实历史中GFS f057/f058、ECMWF f009/f015同一文件先失败后成功的记录并存，下载历史保留功能有自然生产样本。

证据：根 `.run/source-anchor-20261021/` 的plan.json、prepared/switch记录、retirement.json、live-source-after-retire.json、comparison.json、state-after.json、scientific-completion.json、tests-final.log、native-tests-final.log、maintenance-apply.log和api-after.json；远端maintenance下保留原子迁移及退役审计。未新增自动检查或定时任务。


## Git 交付补充：隔离验证

本次将已上线的源锚点 v2 支持补交到 `backend_wrf/master`，以此前 WRF 工作台交付 `0ce8e69` 为基线。范围仅部署校验器、操作员迁移工具、迁移测试、本记录和 README 的源锚点说明；不夹带独立 GPU 说明、DB 提交、环境文件或运行数据。

在新建隔离目录和独立 Python 虚拟环境内安装 requirements、pytest、httpx 后：

```bash
python -m unittest test_source_anchor_migration test_source_anchor test_thin_runtime_deployment test_runtime_deployment -q
python -m pytest -q test_guidance_candidates.py test_guided_region.py test_forcing_source_normalization.py test_generator_deployment.py test_forcing_pool_queue.py test_download_attempt_history.py test_nomads_network.py test_wrf_planning.py
```

源锚点/部署回归40项通过，WRF功能聚焦79项及2项子测试通过。提交后从该提交执行 `git clone --no-local`，不复制 `.env`、原 node_modules、数据库、运行输出或虚拟环境，再使用本轮独立安装的依赖复验同一组测试。最终推送以远端分支 SHA 与该验证提交一致为准。

边界：本轮macOS测试通过专用fixture模拟目录交换，未重新运行Linux原子交换；上文7项原生测试和生产迁移验收属于此前记录，不作为本次重新执行的结果。没有调用生产迁移CLI、维护入口或任务接口，没有重载服务；现有运行目录不就地更新。框架、WRF业务及前端文件相对已交付基线保持不变；安装成功不替代科学计算或生产迁移验收。
