# 普通 WRF 四任务资源预留调度验收

日期：2026-10-14。当前阶段：最终发布安全切换完成，真实四任务并发及四个私有验收任务资源释放通过。

## 行为与保护

- 部署 `WRF_MAX_CONCURRENT_TASKS=4`，源码默认1，硬上限4。任务工作线程包含准备、计算和结果处理，最多4；未知远端计算也占名额。
- 每个任务沿用小域 MPI 判定，CPU预算取实际 MPI 数；内存预算 `max(2GiB, sum(e_we*e_sn)*vertical_levels*1200)`。默认额外保留2个逻辑CPU和2GiB内存。内存是保守准入预算，不是预测峰值或cgroup硬限制；当前MemAvailable还会扣除全部活动预算，偏保守。
- 每5秒尝试调度，约30秒更新资源观测。无有效观测、CPU/内存不足、4名额占满均持久排队，显示具体原因。等待不制造新的计算尝试。
- 远端在原 `compute.lock` 下保存 `compute-pool.json`，先落盘再启动。绑定任务控制目录device/inode、attempt和预算，CPU集合不重叠。监督器及其WPS/WRF子进程继承CPU亲和性，OMP=1；OpenMPI禁止默认重新绑定逃出已分配CPU集合。
- 同一持久预留不自动重放未知启动。未知进程/会话/目录替换继续阻断。只有可信终态且原进程和专属会话均退出，才能释放预留并向本地确认终态；启动前取消必须有核验证据。取消/删除/资料引用保护保留。
- 旧任务保持冻结的计算包。未启动旧任务可单独绑定已验证的新监督器；旧计算脚本、WRF/WPS二进制及科学配置保持原版本。正在运行的旧监督器不替换，原单租约退出前保持串行兼容等待。
- 不新增数据库表、端口、服务职责或启动方式。

## 变更位置

`config.py`、`runtime_release.py`：配置、统一预算与瞬时资源不足排队。
`task_manager.py`：名额、持久状态、资源预算、版本兼容及周期调度。
`hpc_transport.py`：任务冻结计算包与监督器分开绑定、远端资源受理。
`scripts/managed_process.py`：资源池、CPU集合、终态释放及原进程生命周期。
`main.py`：兼容健康字段；主站 `WrfStudio.vue`：容量及排队原因。
根 `scripts/wrf_maintenance.py`：显式精确运行任务证明，仅重载API，不停止远端计算；默认启停活动保护不变。

## 验证

- 126项后端聚焦回归通过，覆盖4并发/第5等待、CPU/内存/未知观测、旧发布兼容、重启未决尝试、取消后会话退出、启动前取消证据、未知启动不重放、目录替换及原删除/引用保护。
- 7项维护回归通过，覆盖显式运行任务/队列证明、未知和取消状态拒绝、默认仍拒绝活动任务、活动计数/schema门禁。原SQLite夹具两条ResourceWarning保留，不隐瞒。
- 5项浏览器回归通过：容量和真实等待原因，以及历史结果竞态、播放、D01–D04时间轴和失败恢复。初轮容量断言失败是配置页不渲染底部摘要；改为实际运行侧栏显示容量并验证，未削弱断言。最终前端构建通过，共享组件保持2.2.4。
- 实际fuseblk数据盘隔离目录 `.compute-pool-test-426qhy5k`：4个受管进程同时运行、16个互不重叠逻辑CPU、真实affinity匹配，第5等待；自然退出后4条释放、活动0；19CPU及1000GiB请求明确等待。专用目录已移除。全盘可用空间前3913311576064、后3913198141440字节，并发生产写入下净减少，不能据此推断样本释放量或泄漏。
- 实际OpenMPI第一次验证发现默认绑定落在[0,10]，超出预留[16,17,18,19]；设置hwloc binding policy none后4个MPI进程均继承正确集合。Linux受管生命周期/身份/取消保护测试在发布13上通过，上传的专用测试脚本已移除。

## 不可变部署与切换

发布 `ordinary-20261014-13`，200文件，指纹 `2be5e37a3b2317a136064f98b86eac46adfbe5b191ec7a12e22fe29df24d9b4b`。
首次部署因ecCodes库检查失败，目标目录未安装、current仍12；显式指定原有ecCodes根后通过。失败staging保留，未清理非本轮资产。

切换前保存 `.run/wrf-refactor/concurrency-20261014/before.json` 和0600 `runtime.env.before`，不含token。显式核验原GFS `wrf_gfs_20261013T060000Z_aedb4f66` 的attempt `b808f4e67f074875a95f3be437c402ad`、PID11658、start_ticks140659816、boot_id及原发布12指纹；原排队ECMWF `b98e6426` 的请求和版本绑定完整保存。原维护入口仅温和重载8007/8012，前后证明一致，其他服务和Redis身份保持。首次因临时API子进程拒绝且未停止服务，保留门禁重查后成功。

真实8007健康上限4、CPU余量2、内存余量2GiB；5177真实模块含容量/排队原因。原GFS仍同PID/attempt，metgrid持续推进，不把等待视为失联或重算。

## 真实计算验收范围（初始阶段记录）

仅通过既有认证提交接口、每任务固定幂等键，新增以下4个私有任务，结果不发布到正式目录：

- `wrf_gfs_20261013T120000Z_0cbb57db`
- `wrf_gfs_20261013T120000Z_0c319cf2`
- `wrf_gfs_20261013T120000Z_844c6cbd`
- `wrf_gfs_20261013T120000Z_c1819620`

统一冻结：GFS 2026101300，2026-10-13 12Z–18Z，3小时驱动/输出，spin-up0，55层，D01 24×19@27km，中心32.06/118.79，默认物理，MPI1/内存2GiB每任务。原GFS和排队ECMWF不取消、不重算、不修改科学参数。验证文件 `validated-request.json`；提交响应和原幂等键 `acceptance-submissions.json`。记录保留，测试后不自动删除任务/产品。

受理初始阶段原GFS仍运行，4验收任务及原ECMWF等待旧发布租约自然释放。后续只读观察记录 `watch-*-accept.json`、`remote-*.json`，核验实际重叠进程、任务目录、CPU集合、终态及预留释放。尚不能将合成进程测试作为真实WRF并发/成功输出证据。自动heartbeat保持停用，本轮只做有限实时验收。

## 原任务自然退出证据

原GFS aedb4f66于21:43:46自然失败，原attempt/PID及发布12保持。原冻结请求vertical_levels=20；rsl.error.0000明确 `not enough eta levels to reach p_top`，原MPI1分解1×1，无新监督器/亲和性介入。证据保留original-real-error.txt；未重算或修改原任务。原退出后调度自动放行4个准备任务，第5等待名额，进一步真实远端并发仍以进程和资源池证据为准。

## 最终补丁及验收完成

旧单租约终止证据保存为资源池中的审计记录，绑定原租约摘要、任务根、attempt、device/inode及核验时间。缺少证据的目录消失、目录替换、租约更换或未知运行身份仍拒绝。不会归档/删除/改写原compute-lease.json；原取消与删除门禁不变。受管resources查询可在不启动新计算时保存该证据，已对实际原终止GFS保存，legacy_blocked=false。

中间14已安装且只做隔离测试，未激活API；最终发布15指纹 `2ac87217785d92fd2d38f4bdcf645793bb2e536c7a245aa5aa2d3abdccf1042e`，200文件，原生Linux6项保护与实际fuseblk样本 `.compute-legacy-test-8ucongr1` 通过：2条预留释放、活动0，已核验终止旧根移除后可继续受理、无证据更换租约拒绝；样本目录及上传脚本已移除。当前任务仍固定13监督器/原计算包，未覆盖它们；真实四任务计算证据来自13，15新增审计路径通过独立实际盘验证。宿主调度另保留同资源池协议版本并发，缺少监督器指纹继续串行保守等待。

126后端聚焦回归、7维护、5浏览器和最终构建通过。真实最大活动4、最大同时3个wrf.exe，所有观测CPU集合互斥、子进程affinity不超预留；原ECMWF与3私有GFS真实重叠，第5等待并自动接续，未超过4。

4个私有验收任务均succeeded，7变量×3时次每任务；84张WebP实际认证读取、sha256与解码通过，匿名401，catalog_publication=disabled。4条资源预留已有released_at，原进程/会话均空；原ECMWF继续正常运行，其1CPU/2GiB预留保留。未删除输出，也不以全盘净变化或系统MemAvailable精确计算任务释放量。原GFS/ECMWF请求逐字与切换前一致；原GFS20层自然失败保留证据，未自动重算。

最终15通过原维护入口温和加载，原ECMWF PID15423/start_ticks141012485/attempt3314b57e98e54863a7ddf66009a914d8不变，原计算包12、监督器13保留。API上限4、队列0；Redis和其他服务身份保持。原heartbeat继续停用，本轮有限watch已自然退出。

最终证据：`final-acceptance.json`、`final-managed-resource-report.json`、`concurrency-proof.json`、`asset-validation.json`、`product-validation.json`、`release15-validation.txt`及各只读远端快照，位于根`.run/wrf-refactor/concurrency-20261014/`。私有产物保留，不等同于气象精度或性能基准。
