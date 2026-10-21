## 2026-10-21 独立WRF/WPS源锚点迁移（已安全切换，04包已退役）

原ordinary-20261008-04是完整部署快照，其WRF6073/WPS1800源文件现完整复制到source-store/wrf-4.7.1-wps-4.6.0-160901b42cdc1424，source稳定入口已原子切换，配置源码和5个二进制哈希保持。原v1锚点及原manifest保留为只读来源证明，10个旧瘦包冻结清单不改写；校验器严格兼容既有绑定，新瘦包记录独立bundle。33项回归、7项真实代码盘测试及04消失后的当前19完整生产校验通过。两API经原维护入口加载独立路径；221文件/分片/引用、56资料日志历史和任务记录保持。原外部论文WRF自然SUCCESS COMPLETE WRF。确认无运行引用后已删除原04包7888文件，必要源码仍保留，不能把删除旧包大小当净释放量。21日两源仍等待发布；20日GFS完整，ECMWF24/25且f012既有完整性失败未改。无新计算/下载请求、远端任务信号或自动检查。详见backend_wrf/docs/INDEPENDENT_SOURCE_ANCHOR_ACCEPTANCE_20261021.md；证据.run/source-anchor-20261021/。下文原04依赖说明仅为历史状态。

## 2026-10-20 下载历史与主动暂停交接（已安全加载）

两源下载逐次追加历史，区分低速/总时限/连接/HTTP429，成功后保留；旧版未保存的原因不补造。原文件边界事务经自身收尾退出，确认无ECMWF动作后，独立计划仅暂停精确curl/父进程，fsync原41,463,808字节f012分片，核验旧进程退出及锁释放，由19版接管原队列；未知不重放、无强杀。现GFS29589/ECMWF4643均19版，原文件/分片inode与引用保持，两源真实推进。实际盘7项暂停、44项资料回归及17项历史聚焦复验、5项浏览器、构建和真实认证组件读取通过；原维护入口加载8007/8012，其他服务、7张任务表及8份旧日志保持。原上游低速仍在，不宣称周期完整或吞吐已修复。详见backend_wrf/docs/DOWNLOAD_ATTEMPT_HISTORY_ACCEPTANCE_20261020.md；证据.run/download-history-20261020/。无新增自动检查。

## 2026-10-19 GFS发布探测公平调度（服务器及API已安全加载）

已复现旧周期预取饿死新周期发布探测；到期探测现优先于批量预取，任务优先级/重试间隔/文件边界保持。50项聚焦、实际盘6项调度和3项原生交接保护通过。仅GFS17→18交接，新21226；原ECMWF20383/curl4135保持。19日自动published并下载f000，43.5秒增长约33.18MiB；ECMWF同期增长320KiB。260完整文件/8分片/引用、任务及旧日志保持。服务器负载/容量/网卡线速未饱和，无明确HTTP限速响应，外部路径性能仍不确定，不宣称永久解决网络吞吐。此前API重载受聊天连接门禁保护；用户释放后已通过原入口温和加载8007/8012，健康200，任务/旧日志、260完整文件/11分片及引用保持。详见backend_wrf/docs/GFS_THROUGHPUT_ACCEPTANCE_20261019.md；证据.run/gfs-throughput-20261019/。

## 2026-10-19 NOMADS TLS备用节点与发布探测修复（已加载）

服务器特定CDN节点TLS失败，另一节点返回19日f072真实GRIB；非整站未发布。GFS仅零进度连接失败时在原预算内尝试fresh DNS同域节点，HTTPS/续传/引用/周期锁保持；展示网络故障不再断言未发布。67项业务发布、原生实际盘12项及忙碌槽保护1项通过。独立17版，仅队列脚本变更；仅GFS文件边界交接，新13272，原ECMWF20383未动。API经原维护入口温和加载，任务/旧日志/257完整文件/9分片及引用保持。两源实际字节增长，GFS仍慢、19日0/73未完整，不宣称正常吞吐或周期完成。详见backend_wrf/docs/NOMADS_NETWORK_ACCEPTANCE_20261019.md；证据.run/gfs-network-20261019/。

当前代码结构与服务器部署入口见 [WRF 代码结构索引](../docs/WRF_CODE_STRUCTURE.md)。历史验收记录保留如下。

## 2026-10-15 参数组合校验补齐（已安全切换）

配置页沿既有只读namelist解析即时检查，最终提交及手动重跑复用同一规则；已知冲突直接阻断并显示实际值／调整方式，资料覆盖、地形与稳定性明确待核验，不自动修改科学参数。垂直分层按生产WRF源码的两种算法检查可达性和层厚，20层/5000Pa默认组合被拒绝，其他分层参数不变时算法最低33层；合法显式20层仍允许，这不证明预报精度。补齐物理跨域／陆面土壤／湍流浅对流／辅助通道、输出窗口、整数步长与MPI分解等确定性约束，保留几何、引用、运行身份和资源保护。

65组原始Fortran对照、164项后端、7项维护、48项原浏览器及7项新浏览器通过，最终构建通过。原维护入口温和加载8007/8012，实际健康200，5177供应新模块；仅两API身份变化，Redis与其他服务未变。三条真实任务、尝试／恢复记录和旧日志哈希逐项不变，aedb4f66仍第2次20层失败待处理，无真实重算／改参／删除。共享组件2.2.4和不可变计算发布15保持，自动检查保持停用。详见backend_wrf/docs/CONFIGURATION_COMBINATION_ACCEPTANCE.md；证据.run/wrf-refactor/config-validation-20261015/。

## 2026-10-15 GFS原参数重跑实际核验（已完成）

用户确认本次没有修改内容。aedb4f66第2次请求与第1次相同，新task.env及namelist.input实际20层（e_vert=[20,20]、p_top_requested=5000Pa）；10:19左右real.exe再次因not enough eta levels to reach p_top失败，本地waiting_restart，未擅自改参/取消/再提交。只有2个历史启动记录、1个accepted手动计划，无重复启动。旧5MiB服务日志与参数完整归档，旧日志哈希不变，原详细rsl根因保留；页面选旧尝试和认证日志接口验证通过，但大日志从头分块，末尾失败不会首次立即显示。

真实页面运行中/终态后各刷新2次，写请求0，attempt始终2；独立浏览器已关闭。原进程/会话无存活成员，无可读取同账户未登记相关进程；第2次1CPU/2GiB准入预留于10:19:43.659444自动释放，本任务活动预留0。仅只读业务核验和证据保存，无真实重算、生产源码修改、重启或自动检查恢复。详见backend_wrf/docs/GFS_RESTART_ACTUAL_ACCEPTANCE.md及.run/wrf-refactor/restart-check-20261015/。

## 2026-10-15 修改后直接重跑（已前端热更新）

“修改后重跑”一次点击直接沿原manual计划/执行接口受理，不再弹出第二次确认，提交期间禁用表单并显示状态。旧尝试参数、failure/error及service.log沿原归档保存，现代远端attempt日志保留，运行页可选择第N次查看。原权限、终态、身份、引用、路径复核及尝试锁不变，智能体ASK及其他操作确认不变。按账户/任务/原attempt保存原计划编号，响应丢失刷新只查询，unknown不重放。

33项后端回归（新增旧失败日志/参数归档及历史读取）、6项隔离浏览器和前端构建通过，真实5177模块热更新及8007健康200核验。没有替用户重算实际任务、改动后端运行代码或重启服务，共享组件仍2.2.4。详见webpage_sys/docs/EDITED_RESTART_ACCEPTANCE.md。

## 2026-10-14 四任务资源预留调度（已安全切换及真实并发验收）

当前普通WRF最多4任务，按实际MPI/保守内存预算受理，预留2个逻辑CPU和2GiB系统余量；资源不足持久排队并显示具体原因。原compute.lock下记录任务目录device/inode、attempt、互斥CPU集合及内存预留，未知尝试不重放；可靠终态且进程/会话退出后释放，取消/删除/引用保护保持。内存为准入预留而非cgroup硬上限。新监督器绑定与冻结计算包分开，已核验的同资源池协议版本可以并发，旧单租约任务保守等待。

最终不可变发布ordinary-20261014-15，指纹2ac87217785d92fd2d38f4bdcf645793bb2e536c7a245aa5aa2d3abdccf1042e，实际就绪。126项后端聚焦、7项维护、5项浏览器及最终构建通过；最终发布原生Linux6项进程保护和实际fuseblk盘终态审计/缺失证据拒绝验证通过，专用样本已收尾。此前实际盘4进程/16独立CPU/第5等待/自然退出释放及CPU、内存不足排队通过；OpenMPI默认绑定越界已修正。受管资源查询保存已退出旧租约终态证据，后续授权目录删除不会因已确认终止的旧目录消失而阻塞；未知/冲突仍拒绝，不归档或改写原租约。

真实4任务重叠运行（原ECMWF+3私有GFS），CPU0–3互不重叠，8GiB预留，最大同时3个wrf.exe；第5个任务等待并在释放后自动启动。4个私有6小时小GFS任务全部succeeded，每个7变量×3时次，共84张WebP认证读取/哈希/解码通过，匿名401、发布disabled。4条预留释放、对应进程/会话均空；原ECMWF仍正常运行并保留自己的1CPU/2GiB。验收计算采用发布13；最终15只补旧租约终态审计，未覆盖运行13/原计算12，最终15独立实际数据盘和原生进程复验通过。小算例不证明气象精度。

原GFS aedb4f66始终保持发布12、原PID11658/attempt，21:43:46自然失败：冻结20层配置不足以到达p_top，错误证据已保留，未重算/改参。原ECMWF b98e6426保持科学参数、计算发布12及原attempt/PID。现有维护入口显式绑定精确运行任务，温和重载API并核验前后身份；默认启停门禁保持，Redis及其他服务未变。证据：backend_wrf/docs/CONCURRENT_RESOURCE_ACCEPTANCE.md及.run/wrf-refactor/concurrency-20261014/。未删除真实任务/结果，自动heartbeat保持停用，有限验收观察已结束。

## 2026-10-14 小区域 MPI 准入修复（已安全切换）

当前 CPU MPI 配置上限为4。准入复用现有不可变运行脚本的小网格单进程回退规则，按所有嵌套域选择实际进程数，10×10保持原网格并使用1进程；大域仍使用配置上限。WRF原构建源码的最小分块判断为 e_we/nproc_x、e_sn/nproc_y 至少10，已去除错误的减一。显式分解参数继续检查乘积和各域块尺寸，冲突返回422及具体进程数、分解参数、块尺寸和自动分解调整提示。实际选定进程传入两源受管启动入口，并记录资源预算及回退日志，不修改远端不可变包或历史任务。

74项后端聚焦回归、4项维护回归通过，实际生产只读准入10×10为ready、MPI=1；原维护入口温和加载8007/8012，其他服务与Redis保持。未启动真实计算；测试验证两源启动命令携带MPI=1，不以准入通过宣称数值计算完成。内存、CPU、运行身份、引用及清理保护保持。详见 backend_wrf/docs/SMALL_DOMAIN_MPI_ACCEPTANCE.md。

## 2026-10-14 两源预取摘要按实际资料状态汇总（已安全切换）

修复GFS实际downloading但后台预取action/总摘要仍queued的遗漏：入队返回仅是受理回执，最终展示使用原同步流程末尾的最新资料池观测。GFS和ECMWF共同调用`apply_forcing_observations`，一致覆盖downloading/queued/waiting_publish/ready及原失败/等待状态；同步真实文件计数、当前文件和分片字节。下载在入队检查期间完成时更新ready；缺少观测继续保留旧回执，不编造状态，不新增入队调用。

99项后端聚焦回归、4项维护回归通过，原维护入口温和加载；真实认证API的GFS14日action/总摘要和资料池卡片均为downloading，24/73、f025，分片增长；ECMWF14日0/25仍waiting_publish。共享槽由GFS正常持有，12日两源备用目录仍保护、未进入清理，待各源14日完整后继续自动保留/清理验收。原30分钟只读后续检查继续，不以本次显示修复宣称下载或旧周期空间验收完成。

预取触发、引用、周期锁及清理规则未变，任务记录/策略/清理记录与切换前一致；其他服务与Redis身份保持。复用固定operator-login.json并每次核验身份，不额外导出凭据、不删除持久文件。无端口、服务职责、部署方式或数据库结构变化；仅现有action兼容补充观测字段。详见 `backend_wrf/docs/PREFETCH_SUMMARY_OBSERVATION_ACCEPTANCE.md`。以下较早阶段为历史证据。

## 2026-10-14 资料池真实状态展示修复（已安全切换）

资料周期明确区分waiting_publish（等待发布）、queued（排队）、downloading（有在下文件）、ready（已就绪）。周期发布等待优先于占位排队文件，真实downloading证据仍优先；已完整周期不被陈旧发布/队列状态降级。两源/provider/总状态和后台预取动作保持一致。接口兼容增加publication_status和脱敏wait_reason，现有字段保留；前端分别显示等待原因，不把等待状态标作下载中或显示虚假速度。进度条仅按已完成文件数/总文件数，实际分片字节另列，低于1KiB/s的真实速度也可显示。

queued仍按原先downloading的活动保护处理，目标、任务/下载/保留周期与清理保护不变；未改调度、下载器、远端发布或数据库结构。96项后端聚焦回归、4项维护回归、4项浏览器回归及前端构建通过；测试输出自动收尾。原维护入口温和切换，8007/8012健康200，其他服务与Redis身份保持，任务数/策略/清理记录逐项未变。真实运行API的14日两源预取状态已为waiting_publish，原因“上游完整周期尚未发布，等待f072发布确认”；13日完整、12日完整备用仍保护。5177实际供应更新的Vue模块，完整四状态交互通过隔离浏览器验证。详见 `backend_wrf/docs/POOL_STATUS_PRESENTATION_ACCEPTANCE.md`。以下较早阶段为历史记录。

## 2026-10-14 私有任务与计算租约维护

任务请求 `publish_to_catalog: false` 禁止自动目录发布及重试，私有渲染/认证读取保持；缺省true兼容旧任务。不修改旧任务、共享DB或远端不可变包。

`scripts/reclaim_compute_lease.py --deletion-id <原编号> --digest <原digest>` 默认只读；显式 `--apply` 在本地维护锁与远端compute.lock保护下，将已完成删除且原目录消失、原attempt终态与无活进程证据一致的精确租约原子归档到数据根 `.compute-lease-audit`。原租约与审计保留；未知/活跃/冲突/响应不明不重放，不直接删租约或改库。仅管理员运维，不装配给Agent。

## 2026-10-14 资料周期锁清理修复（已切换及实际数据盘验收）

资料整体移除现复用不可变发布中已有的周期锁和队列状态锁，不再要求整个源的下载工作器退出。目录归属/device/inode、任务/下载/预取引用、最新目标与完整备用周期及运行保护保留。兼容旧下载器时，工作器门锁空闲才直接进入；忙碌时必须核验同UID、原marker/root/module命令、实际持有同一继承内核门锁且正在另一个周期持锁。未知/孤儿写入、同周期下载、可能尚未落盘的排队选择仍拒绝，不能只凭PID存在放行。未修改远端不可变发布、下载器或两源单传输槽。

明确由周期锁/引用保护阻止且尚未移动目录的清理，在同操作员下一次请求时复用原编号，自动系统请求也复用原编号；保留有限历史错误、重新核验归属/引用，不新增重复失败事务。活动/unknown仍绑定原编号，移动或响应不明禁止自动重放；旧历史记录不批量删除。原SQLite JSON记录增加内部重试证据，无新表或对外请求合同变化。

102项后端聚焦回归、4项维护回归及实际fuseblk盘64/16/8MiB隔离并发清理验证通过，测试目录、合成工作器和专用锁文件已收尾；原维护入口温和切换，8007/8012健康200，其他服务与Redis身份保持。管理员真实刷新202后scheduled，刷新/切换前后清理记录均246条，没有新增失败记录；临时凭据已清除。

6日、7日GFS旧残缺及11日两源旧完整周期在本轮切换前已被原逻辑清理，当前原目录/资料待删除区无残留；不能把此前16.44GiB释放归因于本次补丁。本次并发空间释放通过实际数据盘专用样本验收。当前目标14日、13日00Z；14日两源f072发布探测404并等待发布，共享传输槽空闲，13日和12日完整资料保留为可用备用。未把上游未发布写成下载完成或传输死锁。详见 `backend_wrf/docs/CYCLE_LOCK_CLEANUP_ACCEPTANCE.md`。以下同日以前阶段为历史记录，以本节为准。

## 2026-10-13 GFS 70566ff1 历史尝试冲突修复（已切换、删除及空间验收完成）

取消的是第二次尝试，任务级service.pid/process.json/exit.code及active-launch属于第一次失败尝试。终态核验现在只从WRF独立库只读绑定同任务较早尝试，逐一核对远端attempt、控制目录、PID/start_ticks/boot ID、退出码及取消证据，并在共享launch锁下检查原PID/会话与未登记任务进程均已退出；未知、缺失、重复、冲突或存活身份仍拒绝。不改写启动记录、不逐文件哈希、不放宽引用和目录保护，无新表或接口。

原进程5278及会话已退出，第一次退出码141，第二次为已核验启动前取消。用户授权后仅受理一次删除 `delete_d3860bd99b924d5da1506b4ea71c576a`，终态complete、removed=true、cleanup_status=complete；排队4秒、执行及清理5秒。原远端任务目录及对应隔离操作目录、本地全部批准根与隔离目录均消失，墓碑deleted，无剩余普通任务及活动/unknown删除。远端原目录占25856528384字节，可用空间净增25855819776字节（约24.08GiB）；并发写入下净增量不要求与目录占用完全相等。本地原任务目录占5214208字节。可读取进程中未见目标删除文件占用；4个同账户进程的fd不可读，不能声称全机句柄扫描完整。

131项后端聚焦回归及4项维护回归通过；旧夹具补齐现有删除队列表，并明确普通资料预览默认不释放失败引用。最终源码通过原维护入口温和重载，8007/8012健康200，其他服务和Redis身份不变；本次管理员临时凭据已清除。未手动删除共享资料、重新计算、强杀或直接改库。服务职责、端口、启动方式和调用链不变。详见 GFS_CANCELLED_DELETION_ACCEPTANCE.md（backend_wrf/docs）。以下较早日期和同日先前阶段是历史记录，以本节为当前状态。

## 2026-10-13 fuseblk 删除权限兼容及原事务对账（原事务已完成并验收）

当前实际工作区实体目录为 `/Users/shaoyongjin/code/zhihuiqixiang`（不是符号链接）；旧中文路径当前不存在，历史迁移说明不能代替现况。

生产数据盘的fuseblk挂载提供default_permissions，目录权限掩码显示0775。待删除区仍要求当前UID所有、固定目录设备/inode与父目录一致、禁止符号链接及所有人可写；仅在已打开描述符绑定的fuseblk挂载启用default_permissions、GID为当前账户且组成员只有当前UID、父目录同属主/组/设备且无所有人写权限、无非空扩展ACL时，允许组写掩码。ntfs-3g的空ACL与ENODATA按无扩展ACL处理；其他文件系统、未知挂载、额外组成员或ACL仍拒绝。没有改挂载参数、用户组或批量chmod；引用、运行身份和正式资源访问保护不变。

原任务目录删除unknown只新增显式、认证、同编号的对账路径：墓碑必须属于原事务；所有原目录保持原设备/inode且待删除目标不存在；重新检查任务指纹、计算终态、引用、产品已撤销及认证静态访问拒绝后，保留原移动清单及编号继续处理。部分已移动、丢失、被替换或已移除事务仍拒绝重放。原reconcile接口对此类安全续办返回202，状态查询和后台空间清理不变；审计存在原WRF JSON记录，无新表。

原维护入口增加精确的 `--allow-unmoved-directory-deletion <operation_id>`：只为加载修复允许一个unknown目录事务，逐次只读证明任务/墓碑绑定、原固定目录与移动意图未变、所有目录尚未移动、原计算已终止。重载前后复核相同证明，不对账、不移目录、不解除墓碑；其他活动/未知删除继续阻断，环境变量不能隐式启用例外。默认启停保护不变。

本轮实际fuseblk盘独立合成目录验证了0775目录移除、后台清理、引用原件保留、原PoolQueue下载锁和64MiB数据清理；所有合成目录已收尾。最终空ACL权限兼容复测通过。真实目标仅 `wrf_ecmwf_20261006T000000Z_c335fc6d` 的原操作 `delete_a1ecd8abe10e4e2b8a85c10b832624c5`，用户已授权解决；其他任务、资料及未知事务不扩大处理范围。140项后端聚焦回归、13项维护回归、6项浏览器回归和前端构建通过。原维护入口已温和切换8007/8012，健康均200；其他服务及Redis身份未变，原事务逐项一致，没有自动恢复。有效管理员认证后，仅续办原ECMWF操作，原编号终态complete/removed=true/cleanup_status=complete，本次执行2.7秒；本地和远端原目录及对应待删除目录均消失，认证静态访问404，未见该目标已删除打开文件，其他任务保持。核验窗口远端可用空间净增1760780288字节、本地11079680字节；并发写入下不能以净增量精确推算目录释放量。受理后响应500由漏导入JSONResponse导致，未重发删除，只查询原编号确认终态；已补齐导入和实际路由响应回归，并通过默认维护保护重新切换。临时凭据已清除，其他服务和Redis未变；记录见 `backend_wrf/docs/FUSEBLK_DELETION_REPAIR.md`。

## 2026-10-13 固定目录移除与后台空间清理（已安全切换）

本节替代下文新删除请求的逐文件哈希流程；已保存的旧清单仍保留原校验/对账合同，退役清单不变。工作台直接删除任务、管理员资料周期删除及原自动旧周期候选使用固定根目录身份快照，不遍历或读取文件内容。保留所有权、原尝试终态、任务/下载引用、目录设备与inode、正式产品撤销及认证访问拒绝检查。任务根由原配置和受管任务编号推导，现代发布移除整个tasks/<task>；历史路径保持原精确白名单，不能扩大到共享根。

受理/查询入口不变，返回兼容增加removed、removed_at、cleanup_status。固定目录先在同一父目录原子移入`.wrf-trash/<operation_id>/<原名称>`，操作意图先持久化；任务产品登记撤销且全部目录移走后才移除任务记录，保留提交幂等墓碑。一个串行队列处理校验/移除，另一个共用串行工作器仅清理已隔离目录；清理缓慢不阻塞后续目录移除。资料桥接原不可变发布的PoolQueue工作器/状态锁，移走后撤销该周期队列登记，释放下载锁后才清理空间，不覆盖远端发布脚本。路径根禁止符号链接或其他用户所有，待删除区禁止不安全权限；后台清理不跟随内部链接，不碰原位置后来创建的新资产。

界面原卡在确认移除后消失，独立状态区显示“已移除 · 空间清理中/完成/状态待确认”。刷新按账户和原操作编号查询，未发出新删除。移动响应不明或清理失败保留unknown和精确意图，服务重启不自动重放；不能将已移除误报为仍保留，也不能将移走误报为磁盘空间已释放。目录控制命令30秒；远端清理单次最多1800秒，写操作不重试。生产Python 3.6使用固定父目录描述符的procfs路径兼容安全rmtree；本机使用dir_fd。无新数据库表、无共享DB迁移、无端口/服务职责/启动入口变化。

实际验收：136项后端聚焦回归、11项维护回归、6项隔离浏览器回归及前端构建通过；最终远端Python 3.6目录清理与原下载器锁桥接通过，合成目录已清理。首次维护因旧服务正在执行用户已提交删除而拒绝；等待原队列自然结束后，原维护入口温和重载8007/8012成功，两者健康200，普通活动/队列0、高分活动false，其他服务和Redis身份未变。切换前后任务与原任务删除事务记录相同，未重发失败删除。既有自动策略新产生的GFS 2026100600操作被保护检查阻止、未移除资料。新任务/周期真实删除由用户操作验收，不能以合成测试替代。新建任务/资料批量计划同样使用目录身份快照，资料批量执行返回202并沿原状态接口查询；旧已保存哈希计划不迁移。证据与保留的测试产物见 `backend_wrf/docs/DIRECTORY_DELETION_ACCEPTANCE.md`（子仓库中按相对文档路径读取）。

## 2026-10-13 删除共用串行队列（已受管切换，真实多任务删除待用户验收）

实际切换：8007/8012温和重载完成，新资料周期删除入口与5177排队源码已加载；计算活动/队列为0，其他服务与Redis身份保持。切换前后任务记录和原任务删除事务完全一致，没有重放失败清单。此前已启用的两周期自动策略产生两条system资料操作：ECMWF 2026100600已清理，GFS 2026100600被下载占用明确阻止、未删除。没有启动计算或改写冲突启动证据。

任务删除、资料周期删除及已授权的自动旧周期清理共用一个FIFO删除工作器，容量为8个在途操作。排队状态为`checking + phase=queued`，兼容状态查询增加`queue_position`（前方操作数）、`started_at`、`wait_seconds`、`execution_seconds`；保留`elapsed_seconds`和原操作编号。任务与资料卡分别展示等待/执行耗时，刷新只查询原事务，不重发。失败不阻塞后续任务，未知写操作不自动重放，服务重启将未执行排队标为中断、执行中标为未知。

新增管理员入口`POST /api/wrf/forcing/{source}/cycles/{cycle}/delete`返回202，状态沿用既有资料清理计划GET。仅允许独立资料池中不属于最新目标、完整保留窗口、显式保留、任务或下载引用的单个旧周期；引用与目录在哈希前后复核，hash阶段释放引用锁。旧残缺周期不再等待两个完整周期才进入既有保留规则的候选集。远端明确BLOCKED表示未删除，响应不明则保留unknown，不能当作成功或自动再删。新能力为`workbench-simplification-3`及`forcing_cycle_deletion`。

执行预算从出队开始：控制命令30秒，先读取轻量文件统计，再按字节量分配哈希60–900秒，整个操作1800秒；本地哈希/删除也检查截止时间。仅写操作之前的临时连接/超时允许2秒、5秒两次后台重试；撤销登记或开始物理删除后不重试。维护入口增加未决删除保护，不只检查计算队列。端口、运行入口、共享数据库和远端运行包不变，新增字段保存在既有WRF独立JSON记录，无新表。

同日先前阶段曾因70566ff1启动记录冲突保守拒绝；后续已按本文件顶部最新验收记录完成历史尝试绑定修复及授权删除。其他未知身份和真实冲突继续拒绝，现有失败删除不自动重放。

2026-10-13 启停身份修复及实际切换：确认登记的英文 `ps lstart` 与中文终端输出不一致导致误报PID复用。`service_lifecycle.command`对子进程固定 `LC_ALL=en_US.UTF-8`，不改终端环境、不编辑现有PID/身份记录。中文环境下全部八项受管身份及只读preflight-stop通过；温和重载仅8007/8012，其他服务与Redis PID保持。8007活动数/队列数为0、高分活动false；8007/8012健康200，新接口已加载，5177提供当前源码。前端新旧API混用时保留资料池，缺少新能力则提示服务待更新并禁止不支持的写请求。未执行真实资料刷新、删除或计算。

## 2026-10-13 工作台单按钮刷新与直接删除（API已温和切换）

按本轮用户要求，资料区仅保留“刷新”，不再展示补缺、进度核验或首份清理清单确认入口。新增 `POST /api/wrf/forcing/refresh` 立即返回202，`GET` 同路径读取本地受理状态；管理员刷新在后台使用原策略校验启用双源两周期预取/清理、保留显式保留周期，并记录两源保留规则授权。普通用户可刷新已由管理员启用的策略，不能自行扩大共享清理权限。仍由8007原预取循环及远端资料队列执行；最新目标固定每源两个00Z周期，旧周期删除须保留最新两个完整周期及活动、未知身份、下载和显式保留引用。新周期尚未齐全时继续下载并保留必要旧周期，不以受理202表示下载或删除完成。

新增 `POST /api/wrf/tasks/{id}/delete`：所有权/权限校验后立即返回202，既有单工作器删除事务在后台依次准备并执行；工作台不再打开核验/清单/确认弹窗，任务项显示处理中，完成后移除。同任务请求去重，Bearer仅保留于后台闭包内存，状态查询不重新执行删除。活动计算、未知进程身份、共享引用和文件变化仍阻断物理删除，错误在列表简短提示，详细证据保留后台。旧清单接口保留兼容，不修改共享数据库、Agent工具或远端运行脚本。

能力标记 `workbench-simplification-2`，增加 `direct_deletion` / `forcing_refresh`。端口、服务职责、启动入口和依赖不变。8007/8012已通过原维护入口温和重载，新刷新/删除接口已在实际OpenAPI中确认；管理员实际点击刷新后才启用两源规则。没有在测试中触发真实资料预取、清理或历史任务删除。

2026-10-12 切换补充：8007/8012经空闲身份预检温和重载，真实5177已核验2.2.2及启动栏可达。固定普通发布ordinary-20261012-12就绪；两周期扩大清理仍需首份精确清单确认，未删除真实资料。详情以 `docs/WORKBENCH_SIMPLIFICATION_ACCEPTANCE.md` 最新补充为准。

# backend_wrf

## 当前工作台交互补丁（2026-10-12，源码待切换）

本次继续复用旧普通 TaskManager、资料队列和既有删除事务。源码已移除重复侧栏关闭及归档交互，新增包含既有归档记录的分页；失败任务提供删除与返回修改，结果处理失败另有重新生成结果。同任务修改后新增尝试；明确确认重新计算已完成的 WRF 时归档原始输出和展示产品。任务/日志/缓存健康每5秒独立刷新，资料每30秒刷新，隐藏页面暂停普通轮询。资料补缺、进度核验和取消自身引用使用原接口。

删除计划/执行可选 `asynchronous=true` 返回202，状态查询仅读本地持久化记录；未知事务保留原操作编号，不自动重放。仅 WRF 独立 SQLite 增加 `wrf_forcing_retention_approvals`，共享业务数据库不变。扩展的“每源最新两个完整周期＋可靠终止失败引用释放＋旧残缺周期清理”必须先由管理员确认首份精确清单；当前尚未启用或删除旧资料。远端 `expire-prefetch` 队列能力还须随新的不可变普通发布包部署，不能覆盖运行包脚本。

只读维护复核仍因历史任务 `wrf_gfs_20261007T060000Z_70566ff1` 缺少持久化进程身份而拒绝切换。8007/8012 未重载，不把磁盘实现当作生产生效；暂缓的真实第二次尝试保持未执行，历史任务/资料/产品均未修改。逐模块证据与剩余门禁见 `docs/WORKBENCH_SIMPLIFICATION_ACCEPTANCE.md`。


## 当前 WRF 源码恢复状态（2026-10-12）

工作台普通界面以 `webpage_sys@c177766` 恢复，后端以 `backend_wrf@1ee4c7b` 的普通方法为基线，逐符号保留参数、运行安全及智能体兼容覆盖；不是整仓回退。参数编辑器继续使用同版 `wrf-ui 2.2.2`，任务、资料和结果恢复旧界面组织，工作台不依赖 8012。来源、白名单与验收见 `../backend_wrf/docs/ORDINARY_SOURCE_RESTORE.md` 和 `ordinary-baseline-origins.json`。

当前磁盘实现与前端构建已更新。5177 已只读核验提供恢复后的页面源码；生产 8007/8012 **尚未重载**：历史等待任务缺少持久化受管进程身份，维护检查拒绝切换。不可将本地活动数为零当作远端终态证明。实际浏览器登录复验尚未完成；暂缓的真实第二次尝试继续不执行。不安装环境、不修改历史任务/资料/产品或保留策略，不恢复高分。以下较早日期的 WRF 状态与验收数字为历史证据，不能代替本轮部署状态。



智慧气象的独立 WRF 微服务。后端保留在本机，GFS 数据与 WPS/WRF 计算通过 SSH 直连 `tx-lab@10.255.248.88:1301` 完成，结果拉回本机后发布 WebP。

普通 WRF 的私有 `/api/wrf/display` 和任务 `/result` 读取旧版产品时，可从本地原始 `wrfout` 重投影并校验，生成独立缓存 `data/WRF/runs/<task_id>/geolocated/<version>/`。缓存版本由原始文件状态和任务请求确定；响应返回新图片 URL 与 EPSG:3857 定位元数据。旧文件、运行数据库和已发布目录登记保持原样；缓存沿用任务所有者/管理员的静态资源权限，不因生成文件而共享。缺少原始输出或校验失败时返回 `geolocation_repair` 的状态和原因，不盲目标记为已重投影，不重新执行 WRF 或自动下载远端数据。

## ui-2 隔离工作台迁移（2026-10-08）

`WRF_HPC_FORCING_WAIT_TIMEOUT_SECONDS` 为任务等待上限（默认 21600、最小 60），记录为 `runtime.forcing_wait_deadline`；重启不续期，输入已经齐全可直接通过。超时保留资料和任务，按数据故障进入受控恢复。锁文件 `.forcing-references.lock` 位于 WRF 独立数据库旁；远端内核锁位于系统 `/tmp/wrf-forcing-locks-<uid>`，禁止在运行期间删除锁 inode。旧 PID 标记无法证明无残留下载子进程时，保留并阻止第二工作器，交由运维核验。旧发布不支持新 cleanup 命令时拒绝清理，不回退 shell 删除。清理审计保存在对应资料池 `logs/cleanup_<cycle>_<uuid>.json`，保留原队列条目。生产下载脚本必须来自当前发布版，旧显式路径只供非隔离历史模式兼容。

地图显示默认等经纬度属于前端 2.1.0，后端计算投影和发布栅格 EPSG:3857 不变。前端只转换显示单位，旧产品、palette 和原始 NetCDF 不重写。原生恢复授权、七个已通过案例与第八个待恢复案例状态保持；本轮不部署远端发布包、不恢复任务、不执行退役删除。新的源码修改使旧退役执行准备指纹失效，执行前应重新核验。

新配置使用 `wrf-ordinary-ui-2`：六组仅组织字段，不作为提交门槛。GFS、ECMWF、CPU，spin-up 默认 6 小时且可设非负整数，垂直层默认 88 且可设 10–500，全域一致。公开字段贯通规范化、计划摘要、digest、任务及 namelist；与额外覆盖冲突即拒绝。spin-up 改变模式起始、资料窗、history_begin、FDDA 和资源预算，正式模拟时段不变。spin-up=0 禁用依赖它的 FDDA。

ui-2 驱动资料覆盖 `[产品起报时间 − spin-up, 产品结束时间]`：首端向下、末端向上对齐资料周期，不再额外添加 6 小时边界缓冲。周期选择、完整池回退、对话评估、任务清单和运行环境采用同一窗口。旧 ui-1 及无 profile 的历史请求仍保留额外 6 小时缓冲；输出周期独立于资料周期。

任务 `runtime.current_stage/current_stage_status` 来自同一次尝试的 `[stage-status]` 记录；`current_model_time` 为 WRF `Timing for main` 观测的 UTC 时间，`model_progress` 按实际含 spin-up 的模型起止时间计算并限制在 0–100。WPS 无可测模型时间时不生成模拟百分比；任务顶层 `progress` 仍表示准备、传输和渲染等流程阶段，不能作为 WRF 积分进度。旧日志缺少阶段时保留已有观测，错尝试和已终态记录不回写。

`domains[] + parent_id` 是唯一拓扑，最多 4 域，支持兄弟、孙域，每条边 3/5 比；父先于子，禁止环、孤立、越界和兄弟重叠。预览使用真实投影边界，移动父域仅带动后代，缩放保留子域位置和精度，不合法拒绝。最细网格 min(dx) ≥1 km，D01 ≤50 km，结果默认 D01。树沿祖先累乘时间步；垂直网格和模拟时段全域一致。

一个通用物理默认模板采用 Thompson/RRTMG/YSU/Revised MM5/Noah，城市冠层及 FDDA 关闭。积云按域网格默认 ≤3 km 为 0、>3 km 为 Grell–Freitas（3），显式用户值不覆盖。旧 ui-1 冻结计划、已提交任务及 digest 保持 6 小时/88 层旧语义；旧编辑草稿保留实际参数升级。

本机运行环境是 `.venv`，用 `python3.11 -m venv .venv` 后 `.venv/bin/python -m pip install -r requirements.lock` 建立；根 start.sh 使用此解释器并检查依赖，缺失即退出，不自动安装或回退。部署私有 `.runtime.env` 不提交，配置优先级为进程环境、.runtime.env、.env。

本机资产隔离由 `WRF_RUNTIME_ASSET_ROOT` 显式启用，目标通常为工作区 `.run/wrf-workbench`，新普通数据库为 `wrf_tasks.sqlite3`、日志和原始输出为 `runs/`、私有 WebP/重投影缓存为 `data/WRF/`。未启用时继续读取原配置，不能提前切空库。`scripts/migrate_runtime_assets.py --target <new-root>` 默认只读预检；`--apply --maintenance-window` 才用 SQLite backup 复制普通库，并校验前后逐表记录及配置 digest 一致。目标必须不存在、8007/8012 均无监听，且必须持有根 `.run/wrf-maintenance.lock`；维护脚本可通过 `--maintenance-lock-fd <fd>` 传递同一已持有锁，不使用环境变量冒充维护锁。迁移失败保留部分目标与失败审计，旧库和旧资产不改写、不删除，不自动重试。

成功迁移的 `legacy-assets.json` 将任务身份、尝试和归档日志绑定到精确旧根；私有资源认证及产品版本/checksum 门禁不变。旧日志和新日志可顺序读取，旧原始输出用于只读重投影，新缓存始终写新根；禁止遍历、未映射任务和子路径符号链接回退。高分库继续使用原路径，清理清单需同时核验 ordinary 的 source/target 库映射；不修改既有行的路径。迁移目录权限 700、数据库和清单/审计 600。原有 `WRF_DATABASE_PATH/WRF_RUN_DATA_DIR/WRF_DATA_DIR` 显式配置优先于根配置，维护切换必须检查这些覆盖。

发布配置需明确 `WRF_RUNTIME_RELEASE_ROOT`（系统盘）、`WRF_WORKBENCH_DATA_ROOT`（UUID 数据盘）、`WRF_RUNTIME_RELEASE` 和 `WRF_RUNTIME_PYTHON`。前者保存 source/releases/env/manifests，后者保存 forcing/gfs、forcing/ecmwf、tasks/<task>/attempts/<attempt>、products。已部署 `source/wrf-4.7.1`、`source/wps-4.6.0`，固定链接到 `releases/ordinary-20261008-04/{wrf,wps}` 的只读完整树，不指向 current、不复制源码；`source/anchor.json` 绑定官方版本、两树完整文件清单 SHA、发布 manifest SHA 和 fingerprint，04 因此是不可删除的源码锚。01–07 历史发布暂留；服务器 current 为 ordinary-20261009-11，任务仍固定原发布及指纹；历史八案例已完成，当前补丁验收另列文首。`scripts/deploy_runtime_release.py --anchor-source` 沿用 system-root/data-root/python/release-id 参数，默认只读复验完整发布门禁和源码入口；额外 `--apply` 才在 deployment.lock 下独占创建并封存，已有入口一致则幂等返回，不一致拒绝覆盖，中途失败保留未封存入口供核查。普通部署仍在显式 --apply 时复制核验 CPU/WPS、运行表与生成器至 staging，记录文件、源码、二进制及固定依赖指纹，完整校验后原子切换。实际准备在任何远端资料/文件操作之前强制复验并持久化 release_id 与 release_fingerprint，已有固定发布尝试不随当前版本漂移；提交仅上传 task.json、task.env、forcing.expected.tsv，不覆盖发布脚本。固定编译器/MPI/NetCDF/ecCodes/地理资料只读复用；不加载 CFD 环境。历史任务按持久化路径映射读取。

`GET /api/wrf/readiness` 带认证，返回缓存的脱敏状态、检查时间、发布版和问题代码；refresh=true 供用户主动重新核验。提交与实际启动强制复验。缺 Vtable.GFS、动态库、校验器、挂载、权限或资源时拒绝，绝不换用其他 Vtable 或将非空文件视为合格。资料门禁用 ecCodes 核实实际气象字段、共用压力层、土壤层和时空覆盖；输出门禁使用 NetCDF 校验所有域/时次、必需变量、有限值、投影及稳定性，不完整不得登记为完整产品。

普通持久化队列默认并发 1（WRF_MAX_CONCURRENT_TASKS），下载有独立有界队列。`managed_process.py` 在远端使用启动 claim、全局计算租约、PID/start_ticks/boot ID 和独立会话；启动意图先写 SQLite，SSH 不确定仅对账，不重放。取消确认所有 MPI 子进程退出后完成；未知状态保留资源预约，API 重启接管队列与尝试。新发布尝试重跑保留旧尝试目录。

工作台直接编辑、校验、预检、幂等提交，计划表单只在智能体显示并经官方专用成员原生 ASK 执行；组件位于 webpage_sys/packages/wrf-ui，认证和网络由宿主控制。旧 confirm 恢复政策继续要求后端恢复计划与 ASK，拒绝不执行、结果不确定不重放。智能体没有安装、Shell、部署、清盘或重启工具。

高分默认退役（WRF_DOWNSCALE_RETIRED=1，runner 关闭）：禁止新建、重试、自动上游和发布；运行服务仅保留独立只读查询，写接口返回 410。既有高分会话已核验停止。一次性 `scripts/retire_downscale.py` 默认盘点；精确清单 digest 确认后才撤销登记、校验已认证资源不可访问并删除专属路径/记录，保留最小审计。共享权威驱动、原有普通任务、编译依赖、WRF_GPU、Redis 和符号链接原件均不删除。普通独立环境验收前不删除共享环境。

退役 apply 另要求8007温和退出、持有公共 `.run/wrf-maintenance.lock`，并在整次交易中保留 IPv4/IPv6 loopback 端口。`--maintenance-lock-fd` 可复用外层维护窗口已经持有、经设备和inode核验的同一锁，不释放重取。所有计算必须已终止或确证尚未启动；历史 `waiting_restart` 仅在新鲜远端核验绑定原尝试终态且整个会话退出，或精确证明不存在启动及远端控制目录时允许通过。不会修改这些受保护任务的状态，任何未知证据继续阻断。

当前状态：04 与专用 Python 已通过 8055 项门禁，8007/8012 已温和切换，普通资产已迁至 `.run/wrf-workbench`。两源短程已真实完成 WPS→WRF→渲染→登记，修正后的时序检查再次验证 GFS 2/2、ECMWF 4/4 帧完整。原八个案例已全部终态：两短程成功；五个完整案例积分完成，但 04 校验器错误处理 history_end=0 导致输出门禁失败、未发布。修复进入 05/06，06 增加冻结投影、父子拓扑和垂直网格核对，07 加入默认地理资料门禁。GFS 混合树案例在 WPS 后以 141 退出，未进入 real/WRF；真实资料头复现管道早退/SIGPIPE 机制，五处 awk 输入改为 here-string 并封入 08，不能据此断定旧日志未记录的具体生产失败行。08 已通过370项就绪检查并切换服务器current，五份旧成功积分的08输出校验绑定计划通过只读复验并在空闲维护窗口应用。认证 API 就绪为08，新任务默认08，旧五个尝试仅升级校验器，输出恢复仍须各任务原生 ASK；不重算或改写计算发布版。失败 GFS 混合树行、配置与尝试保留，同科学配置新 08 四域任务已完成全部积分，100 份原始输出、700 张 WebP 与正式登记复验通过。真实智能体 ASK 已核验拒绝旧计划后修改、重新冻结并单次接受，新 08 短程成功；3 时次/21 WebP、正式登记和聊天播放通过。63 次认证资产读取 SHA 一致，匿名拒绝与旧版本 409 通过。四个旧案例的仅输出恢复已各经一次原生 ASK 后完成，第八个 ECMWF 非默认案例待用户明确授权；原计算版本、尝试及配置均保留。七个已完成案例逐文件验收通过，共 260 份原始输出和 1,820 张 WebP；两源混合树共 48 次三端认证资产读取 SHA 一致，匿名拒绝及旧版本 409 通过。完整八案例矩阵和回退演练仍未完成；高分已停、新写入已阻断；v4 精确清单及所列排除项已获用户确认，物理删除仍待独立普通计算验收、全队列空闲及新鲜身份和资产门禁通过。旧资产和失败发布事务继续保护。下文带旧日期的高分和 ui-1 记录仅为兼容/历史来源。

## 当前运行拓扑

### 独立 GPU 构建边界

官方WRF v4.7.1干净构建的固定GPU/CPU对照入口已交付（2026-10-20）：服务器 `WRF_BUILD/WRF_GPU/main/wrf.exe` 为bfc2fc4d…，`WRF_BUILD/WRF_GPU_REFERENCE/main/wrf.exe` 为原生CPU e9786365…。每端wrf/real/ndown/tc四程序与验收指纹一致，源码及运行资料逐文件校验通过；运行前加载原 `env_wrf_nvhpc.sh`。WSM3/WSM5+RRTMG-fast24/24三域一小时对照及两卡目标核函数通过，CPU零GPU核函数通过；各三对性能数值分别1326/1404项逐值一致。整体积分加速1.610×/1.599×，微物理2.895×/3.233×，辐射7.617×/7.371×，仅适用于该MPI2/双2080Ti/44质量层固定个例。完整证据及限制见 `WRF_GPU/STATUS.md`、`WRF_GPU/PERFORMANCE.md`、`WRF_GPU/reports/fixed_entry_delivery/manifest.json`。这是独立工程交付，生产WRF_CPU/WPS、服务配置和调度保持；真实数据/失败诊断保留，不自动切换业务任务。此前旧指纹阶段记录属于历史，不能替代当前结果。 2026-10-21只读最终复核：20/20覆盖通过，现有单域五组各三对及三域两组各三对数值全部通过；单域WSM3/WSM5组合整体积分1.716×/1.672×、微物理3.808×/4.527×、辐射6.723×/6.572×。单域MPI1/单卡与三域MPI2/双卡仅作配置收益比较，不作扩展效率结论；独立辐射组CPU微物理0.648×退化及资源限制详见WRF_GPU/PERFORMANCE.md。本轮未新增计算或生产切换。

普通新任务已切换至独立 CPU/WPS 发布包，历史任务仍按原路径读取；独立 GPU 工程及其任务不变。

### 版本化 WPS/WRF namelist

新配置能力为 `wrf-composer-2`，目录协议为 `wrf-namelist-1`。`namelist_catalog.json` 从当前配置环境 WRF 4.7.1 / WPS 4.6.0 的 Registry、编译后的 namelist 声明与 WPS 读取代码生成（不是根据安装目录名推测）。`catalog_source.py` 对全部编译声明执行缺失检查；`scripts/namelist_source_probe.py` 只读采样源码摘要和 wrf/real/geogrid/ungrib/metgrid 五个程序指纹。

- `GET /api/wrf/namelist/catalog`：鉴权后的完整目录，含类型、默认值、维度、来源、生效条件、能力分类和限制。
- `POST /api/wrf/namelist/resolve`：解析配置，返回规范化配置、两份文件预览、当前确认范围和错误；无任务副作用。
- `WrfTaskCreate.namelist`：`{schema_version,source_hash,wps:{group:{name:value}},input:{group:{name:value}}}`。逐域单值广播，数组长度须匹配域数，管理项/资料派生项不可直接覆盖。`null` 表示显式保留默认，完整配置仍须通过合法性校验。

`namelist_config.py` 使用f90nml生成与回读，校验时间/域、物理、垂直层、时间步及输出组织。目录仍返回生效字段，但普通界面不逐项询问FDDA/Noah-MP内部细项，使用合法默认。特殊编译与额外输入模块分别标明能力；完整目录不代表全部可执行。地理资料仅开放default，米制Lambert/Mercator/Polar支持预览，lat-lon角度网格未接入。

生成器显式使用 OrderedDict：WPS 固定 `share → geogrid → ungrib → metgrid`，WRF 从 time_control、domains 起按规范顺序输出，条件组按同一顺序插入。普通 dict 会被 f90nml 字母排序，Python 回读成功不代表 WPS 顺序读取成功。`scripts/namelist_layout.py` 在生成和运行期分别检查必要段、顺序、重复及结束符；该独立模块由发布包固定保存，运行准备不覆盖脚本。布局版本 `wrf-namelist-layout-2` 纳入计划摘要，旧摘要需重新校验确认，不改写旧冻结计划或任务。哈希正确但顺序错误的历史预览也会在写入前拒绝。

`test_namelist_layout.py` 覆盖上述门禁和旧计划保护；安装 gfortran 时还编译独立读取器，按 Registry 类型读取两份真实生成文件（含双层、FDDA、Noah-MP），并复现旧排序的 EOF。它不执行 geogrid、real 或 wrf，不代表真实数值运行验收。配置读取故障分类为 `configuration_generation`，仅允许受控 `regenerate_plan`：保留用户配置，只清除旧派生摘要，重新生成并校验；不能用非空或原样替代配置绕过修复门禁。

历史部署状态（2026-10-06，当时尚未重载）：顺序修复已完成本地测试，但因高分活动未重载、未重跑失败任务。此记录不描述当前部署；当前隔离发布及验收边界见上文。

`GET /api/wrf/options.configuration_profile` 返回版本化 `wrf-ordinary-ui-2` 的字段、合法候选、默认与服务限制；不再以 group_approved 作为执行前提。ui-1 冻结计划保留旧摘要及语义。物理/输出别名及公开字段与 namelist 覆盖冲突时拒绝；投影同时用于预览和 namelist。

该profile及其地图预览统一当前普通配置服务范围：最细网格不小于1 km、D01不大于50 km。层数与3/5比例的组合还须满足此范围；不静默改变精度。此项是当前服务能力限制，不是WRF官方通用约束；未指定profile的旧请求不收窄。

`POST /api/wrf/domains/preview` 的 edit 支持 draw/move/resize 和树拓扑，返回规范化投影、网格、polygon、四角及调整。稳定节点 ID 由宿主维护，提交转为父先于子连续编号。父域移动带动真实后代，缩放保留子域位置与精度；兄弟完整在父内且不重叠。

validate 对新请求只读复验当前构建并返回规范化 request 和 plan_digest；工作台直接提交，智能体冻结后经 ASK 单次幂等提交。运行准备只上传任务配置与输入清单，生成器来自不可变发布包，实际 met_em 维度读入前复验；构建、预览或布局冲突即停止。历史请求不改写，旧原生输出按路径映射读取。

配置前校验不能替代真实资料、静态地理数据、数值稳定性及实际计算验收；真实矩阵结果单独记录，不能以配置回归代替。

### 普通 WRF 建议与确认恢复（兼容接口）

下述ordinary-wrf-dialogue-3保留原建议/恢复能力；新分组问答核对 `configuration_schema=wrf-composer-2`、profile ID和构建摘要。2026-10-05 已在取消并核验高分任务空闲后安全重载8007；生成器/Stub验证不能替代真实计算验收。

配置能力标识为 `ordinary-wrf-dialogue-3`。`options.physics_options` 来自与 `PhysicsConfig` 相同的允许编号集合，不是运行时编译能力证明。`planning/assess` 支持 `duration_hours`、`domain_count`、`nesting_ratio` 和 `assimilation_scheme`，明确时长优先，未指定才用 24 小时。`domains/preview` 的 AOI 候选支持 1–4 层及 3/5 嵌套比，保持目标最细网格，父域逐级变粗并留余量；超出规模上限报错，不静默减层或变粗。新增 `finest_aoi_covered`，与外层 `aoi_covered` 区分。FDDA 映射仍由现有执行器控制，未新增 WRFDA 或观测同化。

`POST /api/wrf/planning/assess` 返回 `suggestions`、`choice_constraints`、`rules`、`data_window`、`resources` 等结构化字段，包含时间建议、资料覆盖、资源快照、兼容选项和规则依据。`validate` 返回 `ruleset` 和结构化规则；硬约束复用 `WrfTaskCreate`，经验建议不承诺预报精度。可选 `time_step_seconds` 必须满足保守外层网格时间步上限并整除边界与输出周期。运行脚本检查 met_em 维度与可读性并记录二进制哈希，实际输入及编译支持仍需运行时验证。

新任务默认 `recovery_policy=confirm`，诊断自动但恢复须确认，历史无字段任务保持兼容。新增仅所有者/管理员可访问的接口：

- `GET /api/wrf/tasks/{task_id}/events`：持久化故障事件、有置信度的诊断。
- `GET /api/wrf/tasks/{task_id}/diagnosis?event_id=...&attempt_no=...`：仅所有者/管理员可读的 `wrf-diagnosis-1` 快照。同次尝试重新读取有界日志，返回分类、已知事实、未知项、白名单错误特征、证据摘要、阻塞原因及可用动作，不返回原始日志/私有路径，不改写历史事件 ID。重复读取使用 20 秒缓存与单飞；准备/执行恢复强制复核。历史尝试只读，不提供恢复动作。
- `POST /api/wrf/tasks/{task_id}/recovery-plans`：生成计划，不启动计算；动作包括 `resume`、`retry_outputs`、`restart`、`reduce_time_step`、`regenerate_plan`。未知故障不默认提供原配置重跑。
- `GET /api/wrf/tasks/{task_id}/recovery-plans/{plan_id}`：查询计划与执行状态。
- `POST /api/wrf/tasks/{task_id}/recovery-plans/{plan_id}/execute`：要求 `confirmed=true`，计划 10 分钟有效，执行前复验状态、路径、语义诊断摘要、运行能力及规范化配置。SQLite 写事务保障同一事件防重，执行异常保留 `unknown`，不得自动重试。旧待执行计划没有新摘要时拒绝执行，需要重新制定；已接受/未知计划保持原结果，不重放。

`request_validation.py` 是新任务与恢复重算共用的规范化、namelist 回读/布局和运行构建校验路径。`regenerate_plan` 另须核验已部署的 `apply_namelist_plan.py`、`namelist_layout.py`、`wrf.sh` 指纹与当前修复代码一致，未部署或构建无法核实即阻塞，不声称已修复。`options.recovery_capability` 标明诊断/恢复协议支持，不代表实时部署门禁已通过；真实状态由诊断与计划生成核验。原始日志和尝试归档保持不变，语义相同的日志新增或来源信息变化不单独作废计划，新的错误特征、能力或任务变化会要求重新确认。

历史维护记录（2026-10-08，退役及隔离切换前）：曾通过根窄范围维护入口重载 8007/8012，并保留同一远端高分尝试、Redis 和其他服务进程。真实登录旧会话能够读取该次尝试并显示配置读取错误诊断。随后部署远端普通生成器修复，核验源码、五个二进制与生成器指纹；通过真实任务打包器及远端生成器完成 WPS/WRF 内存生成、布局校验和 f90nml 回读，资料维度及周期为合成验证值。该时点未执行恢复或启动真实 WRF；不能替代上文随后完成的真实短程及仍在进行的完整矩阵验收。高分现已停止且新写入被阻断。

2026-10-08 后续 I/O 修复：普通生成器显式输出 `&namelist_quilt`，固定 `nio_tasks_per_group=0`、`nio_groups=1`、`poll_servers=.false.`，计算进程负责 I/O。此段由 WRF MPI 初始化直接读取，不在 Registry 参数目录内；不开放为用户额外选项。生成及运行期检查均要求该段，新布局版本进入计划摘要，缺段旧预览即使哈希正确也拒绝写入，旧计划必须重新生成并确认。独立 Fortran 读取验证新配置成功、删段配置失败；真实失败任务的内存重生成仅增加 I/O 段，其余 WRF 参数和 WPS 文件不变。本次仅本地修复，未重载8007、未同步远端校验模块或恢复计算；前述部署证据不包含本修复。

### 普通生成器显式维护（旧布局兼容）

此入口保留旧共享布局的维护审计。当前不可变隔离发布须通过 `deploy_runtime_release.py` 构建新版本；不对已封存的 04/08 或固定源码锚就地替换生成器。

在本仓库环境运行 `python -B scripts/deploy_namelist_generator.py` 默认仅检查；`--apply` 才上传 `namelist_layout.py`、`namelist_source_probe.py`、`apply_namelist_plan.py`。部署检查本地普通任务和队列空闲、远端源码/二进制及未改变的 wrf.sh；远端文件锁、旧值比较、暂存哈希/语法/布局验证后逐文件替换，依赖先安装。保留 `.generator-deployments/<id>/backup` 和 `deployment.json`；部分失败尝试恢复旧文件，响应不确定须核查审计，不自动重放。此入口不是模型工具或公开 API，不修改高分脚本或启动计算。不能代替恢复计划和原生 ASK。

诊断接口支持 `refresh=true`：任务可见性校验后，最多 20 秒重新核验远端构建并绕过诊断缓存；用于主动重新核验处理条件。默认请求继续使用有界缓存，避免轮询反复 SSH；探测失败仍返回未核验，不降级为可执行。

`wrf_incidents`、`wrf_recovery_plans` 和 `wrf_diagnosis_snapshots` 是本服务 SQLite 增量表，不涉及共享 DB。脱敏诊断快照独立保存并按语义去重，不改写事件身份，也不保存原始日志。证据有长度限制与凭据脱敏；段错误、无进度仅作低置信度诊断。新确认策略任务质检覆盖缺失域、T2/U10/V10/PSFC 缺失帧与非有限值，保留失败证据而不伪报完整成功。

普通 WRF 的 `summary`、`result`、`display` 返回相同的 `attempt_no` 与 `product_version`；版本是当前尝试及持久化产品构建元数据的摘要，不随轮询时间变化，同次尝试重新生成产品后会更新。新渲染清单记录每帧 SHA-256。聊天端通过 `GET /api/wrf/tasks/{task_id}/assets?path=...&attempt_no=...&product_version=...` 读取私有 WebP，只允许当前清单列出的帧；读取前后均检查身份、终态、尝试及版本，内容校验失败或版本变化拒绝返回，不缓存旧图。普通旧静态 URL 保持兼容，高分入口显示退役说明。

- 本机：FastAPI、SQLite 任务状态、结果下载、渲染与 `/data/WRF` 发布。
- tx-lab 数据卷通过 NTFS UUID `7EEE64E3EE6494E5` 和稳定别名 `/home/tx-lab/.wrf-data` 定位。新任务使用前述 `WRF_WORKBENCH` 隔离目录；`WRFwork/RUNTIME`、`WRFwork/DATA/GFS` 和 `WRFwork/DATA/ECMWF` 是历史任务/旧资料池兼容路径，不能作为新任务写入目录。
- 不依赖 `/media/tx-lab/新加卷N` 这类桌面自动挂载路径；服务每次远端命令执行前按 UUID 只读核对稳定别名；缺失或冲突由管理员维护，探针不修改别名。远端数据卷的 `WRFwork/.wrf-data-root` 标记文件必须存在，未挂载或别名冲突时直接停止。
- 历史共享 WPS 构建：`/home/tx-lab/WRFwork/WPS/WPS-4.6.0-nvhpc`；新任务执行前述不可变发布包内的程序。
- 历史共享 CPU WRF 构建：`/home/tx-lab/WRFwork/WRF_BUILD/WRF_CPU`；不作为新任务工作目录，也不属于高分专属清理目标。
- 共享只读 WPS_GEOG：`/home/tx-lab/WRFwork/DATA/WPS_GEOG/WPS_GEOG`；新发布按 geography 合同核验复用。
- 历史共享 NVHPC 环境入口：`/home/tx-lab/WRFwork/env_wrf_nvhpc.sh`；新任务使用发布绑定的独立 runtime.env 与固定依赖，不加载 CFD 环境。

服务使用持久化普通计算队列，源码保守默认并发1，当前部署显式上限4并按任务预留CPU/内存，资源不足排队；CPU MPI进程数由部署配置决定（默认4，小域自动1）；spin-up 和垂直层数取经过校验的公开配置，旧冻结计划保留旧策略。已运行任务固定发布版并按受管身份对账。

## 首次启用前的人工配置

服务不接收、不保存也不自动填写 tx-lab 密码。密码仅用于管理员人工 SSH 登录和首次安装公钥。

### 1. 数据盘检查（旧布局兼容记录）

当前新部署使用显式 `WRF_WORKBENCH_DATA_ROOT` 和前述发布 CLI 建立隔离布局。以下命令仅保留旧共享资料池初始化来源，不是新任务写入路径；不能据此重新创建或切回旧池。旧布局当时要求目标数据卷可写且至少有 120 GB 可用空间：

```bash
findmnt -rn -S UUID=7EEE64E3EE6494E5 -o TARGET,SOURCE,FSTYPE
mkdir -p /home/tx-lab/.wrf-data/WRFwork/DATA/GFS
findmnt -T /home/tx-lab/.wrf-data/WRFwork/DATA/GFS
df -h /home/tx-lab/.wrf-data/WRFwork/DATA/GFS
test -w /home/tx-lab/.wrf-data/WRFwork/DATA/GFS
```

远端首次初始化时，在确认 UUID 已挂载且目录为真实数据卷后创建一次布局标记：

```bash
test "$(findmnt -rn -S UUID=7EEE64E3EE6494E5 -o TARGET | head -n 1)" \\
  && touch "$(findmnt -rn -S UUID=7EEE64E3EE6494E5 -o TARGET | head -n 1)/WRFwork/.wrf-data-root"
```

兼容 cron 入口 `scripts/wrf_server_cron.sh` 按 UUID 只读核验 `/home/tx-lab/.wrf-data` 后仅入队预取，不修改别名或扫描、启动 WRF；别名修复由管理员处理。新普通资料、任务与产品路径由隔离发布配置决定。

低于阈值时后台预取停止并报告存储不足，不会回退到旧外置盘。

### 2. 安装专用 SSH 密钥

在运行 `backend_wrf` 的本机账号下创建专用密钥并保存独立 known-hosts。首次复制公钥时由管理员手工输入 tx-lab 密码：

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_wrf_txlab -C backend_wrf@tx-lab
ssh-keyscan -p 1301 -t ed25519 10.255.248.88 > ~/.ssh/known_hosts_wrf_txlab
ssh-keygen -lf ~/.ssh/known_hosts_wrf_txlab
ssh-copy-id -i ~/.ssh/id_ed25519_wrf_txlab.pub -p 1301 tx-lab@10.255.248.88
ssh -i ~/.ssh/id_ed25519_wrf_txlab -o BatchMode=yes \
  -o UserKnownHostsFile=~/.ssh/known_hosts_wrf_txlab \
  -p 1301 tx-lab@10.255.248.88 'printf WRF_TXLAB_READY'
```

`ssh-keyscan` 的指纹必须通过可信渠道与服务器实际主机密钥比对后才能使用。

## 启动

```bash
./start.sh
```

此命令在项目根目录执行，由根脚本使用 `backend_wrf/.venv/bin/python` 并登记服务身份。依赖须按上文 `requirements.lock` 由管理员预先建立；WRF 启动过程只检查，不自动安装或回退到通用 Python。JWT 与私有部署配置由现有部署环境提供，不在命令行填写。默认监听 `http://127.0.0.1:8007`，接口文档位于 `/docs`。完整重启仍使用项目根目录的 `./stop.sh` 和 `./start.sh`；不要停止归属不明的端口进程。

仅维护 API 可从根目录运行 `python3 -B scripts/wrf_maintenance.py`，默认只读检查。独占、无新请求的维护窗口才能加 `--execute --maintenance-window` 重载 8007/8012。普通任务和队列必须空闲，历史等待流程须有严格的新鲜远端终态证明；高分退役查询必须可读且无活动或未知状态。旧入口的同尝试接管分支仅保留兼容，不能重新启用高分。Redis、其他服务和日志保留，任何不确定、未释放的 8012 长连接或温和退出超时都拒绝强制继续。默认全组启停保护不变；维护不是实际计算验收。

本机旧 `com.zhihuiqixiang.backend-wrf-8007` LaunchAgent 已禁用并卸载，原配置保留；普通启动统一由根脚本登记 PID 与身份。禁止同时启用该 launchd 保活项，否则停止后会自动拉起新进程、导致 8007 占用及身份记录失配。重启前根脚本检查普通与高分任务均无活动；任务状态不明时拒绝停止，不强杀未知监听者。

`GET /api/health/activity` 是无需登录的最小本地停止检查：返回 `schema=wrf-local-activity-1`、`active_task_count`、`queue_size` 和布尔 `downscale_active`，不含任务 ID 或用户数据，不调用 SSH/远端健康刷新。它表示本地执行线程的活动状态，不保证远端任务已完成或连接正常。根停止脚本优先使用该接口，5 秒超时即拒绝；只有旧进程 404 才使用 45 秒的 `/api/health` 兼容迁移。原 `/api/health` 行为保持兼容。

## 主要配置

- `WRF_PORT`：默认 `8007`。
- `WRF_CATALOG_PUBLISH_ENABLED`：默认开启；完整成功任务会把 WebP 与 `scene.meta.json` 发布到统一数据目录，部分结果不会登记。
- `WRF_CATALOG_DATA_ROOT`：默认指向工作区 `backend_system/data`。统一目录发布失败只记录在任务的 `catalog_publication` 子状态，可通过 `POST /api/wrf/tasks/{task_id}/publish` 重试，不会重跑 WPS/WRF。
- 普通计算队列读取 `WRF_MAX_CONCURRENT_TASKS`，默认 `1`；下载队列独立有界，未知尝试仍占用计算预约。
- `WRF_HPC_HOST` / `WRF_HPC_PORT` / `WRF_HPC_USER`：默认 `10.255.248.88` / `1301` / `tx-lab`。
- `WRF_HPC_CONNECTION_MODE` / `WRF_HPC_AUTH_MODE`：默认 `direct` / `key`。
- `WRF_HPC_KEY_FILE`：默认 `~/.ssh/id_ed25519_wrf_txlab`。
- `WRF_HPC_KNOWN_HOSTS_FILE`：默认 `~/.ssh/known_hosts_wrf_txlab`，严格校验主机密钥。
- `WRF_HPC_STORAGE_UUID` / `WRF_HPC_STORAGE_ALIAS`：默认 `7EEE64E3EE6494E5` / `/home/tx-lab/.wrf-data`；服务按 UUID 只读核验挂载和别名，不自动修改；不一致由管理员处理。
- `WRF_HPC_REMOTE_DIR`：默认 `/home/tx-lab/.wrf-data/WRFwork/RUNTIME`。
- `WRF_HPC_GFS_MOUNT` / `WRF_HPC_GFS_DIR`：默认 `/home/tx-lab/.wrf-data` / `/home/tx-lab/.wrf-data/WRFwork/DATA/GFS`。
- `WRF_HPC_GFS_REGION_WEST/EAST/SOUTH/NORTH`：旧区域资料兼容配置；当前 GFS 全球资料范围由 `HpcClient.gfs_bounds` 返回 `[-180, -90, 180, 90]`，不以旧区域范围限制新任务。
- `WRF_HPC_GFS_PREFETCH_ENABLED`：默认开启；8007 存活期间持续检查并触发 GFS、ECMWF 最新 00Z 的超算后台预取。
- `WRF_HPC_GFS_PREFETCH_INTERVAL_SECONDS` / `WRF_HPC_GFS_PREFETCH_START_DELAY_SECONDS`：默认 `300` / `15` 秒；分别控制后台复查间隔和服务启动后的首次检查延迟。
- `WRF_HPC_GFS_RETAINED_CYCLES`：默认 `2`；最近两个名义周期、最近两个完整回退周期与活动任务周期受保护，不进入清理候选。
- `WRF_HPC_GFS_REQUEST_INTERVAL_SECONDS`：默认且最小 `10` 秒，遵守 NOMADS Grib Filter 脚本请求间隔。
- `WRF_HPC_GFS_MIN_SPEED_BPS` / `WRF_HPC_GFS_SLOW_SECONDS`：默认 `65536` B/s / `120` 秒；持续低速时保留断点并重试。
- `WRF_HPC_GFS_POLL_SECONDS`：默认 `30` 秒；任务等待共享队列的必需文件，不再按固定分钟数超时失败。
- `WRF_POOL_RETRY_MAX_ATTEMPTS` / `WRF_POOL_RETRY_MAX_SECONDS`：默认 `8` 次 / `900` 秒；临时网络错误采用退避重试，达到单轮上限后延迟开启下一轮。
- `WRF_POOL_HISTORY_RETENTION_DAYS`：默认 `30` 天；失败和清理摘要保留期限，GRIB、`.part` 和失败周期目录不在此期限内长期保留。
- `WRF_HPC_GFS_DOWNLOAD_WORKERS`：兼容配置，全球资料下载队列固定串行，默认 `1`。
- `WRF_HPC_GFS_FULL_MIN_BYTES`：单个全球 GRIB 的最小大小门槛为 `1 MiB`；实际合格仍须通过字段、层次及覆盖校验。
- `WRF_HPC_GFS_MIN_FREE_GB`：GFS 数据卷最低可用空间，默认 `120` GB。
- `WRF_HPC_RUNTIME_MIN_FREE_GB`：启动任务前 NVMe 最低可用空间，默认 `110` GB。
- `WRF_HPC_RUNTIME_PRE_WRF_MIN_FREE_GB`：进入 real/wrf 前最低可用空间，默认 `40` GB。
- `WRF_HPC_CPU_MPI_PROCESSES`：默认 `4`。
- CPU 构建根目录中的可执行文件统一取自标准位置 `main/real.exe` 与 `main/wrf.exe`，不依赖 `run/` 软链接。
- ui-2 新建保持 CPU 和通用预报，开放经过校验的 spin-up 与垂直层；ui-1 历史冻结策略不改写。
- 普通 WRF 业务任务默认每 60 分钟写出 `wrfrst_d*`；`wrfout_d*` 的 `history_interval_minutes` 独立于资料边界周期，可选 10/30/60/180/360 分钟（默认 60）。

### 已退役高分环境（仅旧资产归属审计）

以下名称和路径仅用于识别历史专属资产、冻结退役清单及旧配置，不是当前普通 WRF 的安装依赖或执行说明。当前服务不实例化高分 runner，不自动创建上游，不加载 OpenFOAM/CFD，也不提供 forcing→CFD 创建入口；高分写接口返回 410。不能通过设置旧 `WRF_DOWNSCALE_RUN_CHAIN` 或重装旧环境来恢复产品功能。

- 旧 `WRF_HPC_DOWNSCALE_INTERFACE_ROOT`：`/home/tx-lab/WRFwork/CFD/interface`。
- 旧 `WRF_HPC_DOWNSCALE_CFD_TEMPLATE`：`/home/tx-lab/WRFwork/CFD/templates/thermal-rans-graded-v2`。
- 旧 `WRF_HPC_DOWNSCALE_OPENFOAM_ACTIVATE`：`/home/tx-lab/WRFwork/CFD/activate.sh`，曾引用 `/home/tx-lab/WRFwork/ENV` 下的 OpenFOAM/foamgeo。
- 旧 `WRF_HPC_DOWNSCALE_PYTHON`：`/home/tx-lab/WRFwork/ENV/wrfpost/bin/python`。
- 旧 `WRF_HPC_DOWNSCALE_MPI_PROCESSES` 及 27→9→3→1 km 上游、NDOWN/WRF-LES、12.345679 m forcing/1 m CFD 采样链路只保留历史审计语义，短时基准不构成完整链路验收。

v4 与获批增量范围的退役事务已完成，执行回执见 WORKBENCH_REFACTOR_ACCEPTANCE.md；未列入清单的资产不据此删除。运维盘点/删除仍须精确 manifest、归属、共享原件保护、空闲和新鲜身份门禁；此处列出路径不授权单独清理或重启。

## GFS 与数据保留

工作台使用 NOAA NOMADS GFS 与 ECMWF Open Data 的全球 0.25° 资料。GFS 每个完整 00Z 周期包含逐小时 `f000–f072` 共 73 个 GRIB；ECMWF Open Data 仅发布 3 小时边界场，即 `f000、f003…f072` 共 25 个 GRIB。每个数据源各有一个持久化串行队列，保留 `.part` 断点、逐文件进度和重试状态；校验 GRIB 首尾标记和最小大小后再原子落盘。

后端默认每 5 分钟持续检查最近两个 00Z；每轮都会重新检查 tx-lab 连接，因此夜间网络恢复或新周期发布后会自行继续。队列先探测 `f072`，仅对已发布周期补齐完整 `f000–f072`；活动 WRF 任务的必需时次始终优先于后台预取。已经触发的下载由 tx-lab 队列持有，关闭网页或短暂重启 8007 不会丢失进度；8007 恢复后会继续发现和触发后续新周期。可通过健康接口的 `gfs.prefetch` 查看最近一次后台检查状态。

当 GFS 或运行区剩余空间低于安全阈值时，健康状态标记为 `storage_low`，后台预取自动暂停并继续周期性复查；接口仍会展示已有周期和实际可用空间，不会把空间不足误报为缺少环境变量。扩容或释放空间达到阈值后，预取会自动恢复。

远端共享数据池默认保留两个最新完整回退周期；GFS 任务可选择 `1/3/6` 小时边界场间隔，ECMWF 任务固定使用 3 小时边界场。任务只校验和链接所选间隔对应的预报时次，`interval_seconds` 与该选择保持一致。最新周期尚未补齐时，只要仍有完整回退周期，超出保留窗口的旧完整周期仍会自动清理。

生成 WRF namelist 时，外层 `time_step` 会在保守的 `5×dx(km)` CFL 上限内自动选择能够整除驱动资料间隔的最大值。这样 GFS 1 小时资料在 27 km 外层网格上使用 120 秒时间步，避免 FDDA 在 `01:00:45` 等非整点寻找 `01:00:00` 输入而退出；ECMWF 3 小时资料仍可使用 135 秒时间步。

同步最新 00Z 或任务准备数据时，系统会立即清理不再可恢复的失败/空周期残留；完整旧周期在完整回退窗口之外自动清理。目标周期、下载中的周期、完整回退周期以及活动任务正在使用的周期不会自动删除；不满足自动清理条件的路径仍需在界面人工确认。远端任务断点、wrfout、历史运行结果和本地已发布产品始终保留，取消或重跑任务也不会删除这些数据。

生产反向代理应将 `/api/wrf` 与 `/data/WRF` 转发到本服务。GFS、wrfout、WebP 结果及密钥不得提交到 Git。

### 普通 WRF 规划、输出与权限合同（本地实现；生产计算待验收）

2026-09-28 本地回归增加四层域独立球面 Lambert 控制点、逐层拖动/重画吸附及越界拒绝、分钟级目录时间与时间步整除测试；文献证据只返回来源文件名，不返回宿主机目录。跨仓库验收证据与真实计算门禁见 `../weather_agentscope/docs/WRF_ACCEPTANCE.md`。合成 fixture 和 Stub 不代表真实预报能力。

- `GET /api/wrf/options` 返回能力版本、字段说明和固定策略；`POST /api/wrf/planning/assess` 只读评估部分需求，有 AOI/网格距时可给当前、较粗、较细最多三项规模对照，替代项必须由用户选择、不自动降低要求；可按显式 `evidence_query` 检索离线 `KnowledgeStore` 并标记文献核验状态（未导入则返回不可用）；`POST /api/wrf/domains/preview` 按 WPS Lambert、Mercator 或 Polar 投影计算域轮廓，均不启动资料下载或 geogrid。资源耗时/内存无校准数据时返回未知。
- `POST /api/wrf/tasks/validate` 仍是参数最终权威。`POST /api/wrf/tasks` 使用稳定 `Idempotency-Key`；响应不明时用本人可见的 `GET /api/wrf/submissions/{key}` 只读对账，不自动重复提交。
- 区域推荐接口已退役并返回 410，不再创建分析任务。直接 `/data/WRF/runs/<task_id>/...` 只对本人或管理员开放。正式共享产品交由 `backend_system` 已登记发布目录提供。
- 新渲染协议 `scene.meta.json` 1.3 使用 `pyproj`/`rasterio` 按 WRF 经纬度网格和投影元数据重投影至 Web Mercator；同一域/变量跨时次共享色标，原网格统计与展示重采样分离。服务显式传递 `ref_x/ref_y`（未指定时取自身既有网格中心策略），投影预览与运行使用同一参考点。`RAINC/RAINNC` 按 WRF 官方定义为累计降水，不把单帧数值当作间隔雨量。旧产品不自动重渲染，客户端须标明旧版定位局限。合成网格测试只验证实现，不证明预报准确率。

## 调度与启动保护

- 远端 `scripts/wrf_server_cron.sh` 仅调用 `download_gfs_00z.sh <今日00Z> 72 "" prefetch`。`wrf_server_worker.sh` 是退出码 3 的禁用兼容入口，不再扫描或启动任务。两个服务器 cron 入口与旧 worker 已备份替换；crontab 频率保持原样，历史数据不清理。
- 新普通任务的旧 retry/restart/resume/retry-outputs 接口返回 409，必须使用恢复计划及原生 ASK；历史未声明 confirm 的任务保持兼容。

### 已退役 Studio 高分运行历史（2026-10-05）

以下仅保留旧任务状态、数值身份和未完成验收的审计来源，不是当前 Studio 的操作说明。旧 review、retry、cancel、archive 和创建接口现均不可执行；只读列表/任务摘要保留所有者/管理员权限，旧日志和结果路由返回 410，查询状态不可读时停止保护失败关闭。历史取消已核验不等于物理退役事务已完成。

- 旧管理员审核在自动上游提交前冻结 `expected_updated_at`、显式确认和依据；响应不确定保留同一上游幂等键。当前不会重新提交上游。
- 旧 `managed_downscale_process.py` 曾使用持久化 claim、`active-launch.json` 和 `launches/<attempt>/state.json/service.log`，以 PID/start ticks/boot ID 与独立会话防重放。旧 service.pid/exit.code 及控制文件按退役清单保护，不再因 prepare 上传高分脚本。
- 旧 SSH 未知、取消待核验、tombstone、归档字段等仅用于解释历史记录；当时只有绑定会话进程全部退出才确认 cancelled，归档没有删除数据或撤销登记。当前不提供归档恢复或计算恢复入口。
- 旧 WRF-LES 阶段标记用于区别 NDOWN 和 D08 故障；不属于当前普通工作台能力。

旧 D08 个例在 0.0625 s 固定步长、4 MPI 下出现数值发散；16:15:38 日志 W 从 544 增至 2565 m/s 后段错误，栈落在地表层稳定函数查表。2026-10-05 用户授权修复和实算后，已通过原任务受管重试从 12:00 初值开始：仅 D08 将 `epssm` 调为 0.5，并通过 Registry 运行期 I/O 补 `ZNT`；原 WRF 二进制不变。数值版本、namelist 和 I/O 指纹写入运行身份；D08 阶段指纹包含完整起始 namelist，旧配置的重启文件拒绝复用。旧现场归档保留，D05–D07 和 NDOWN 经复验复用。新运行已开始积分，是否消除失稳仍待全时段及 CFD 质量门验证。

CFL 门禁识别真实 WRF 的 `points exceeded v_cfl` 格式，不将警告阈值误记为实测最大值。forcing 必需变量逐时次核验，manifest 区分 D05–D07 自适应步长与 D08 固定步长并记录实际恢复比例和运行身份。CFD GeoTIFF 修复 IFD 条目数/偏移，EPSG:4326 像元单位使用经纬度角度；NetCDF 的 y 坐标与北向上的像元顺序一致，范围不足时拒绝裁切。当前 AOI 经纬度仍采用局地米制近似，质量报告保留 georeferencing review，不将其描述为精确重投影。

2026-10-05 后续验收：4/8/10/4 MPI 隔离基准完成，8 MPI 约缩短 8% 单步耗时，9 个关键变量短时输出逐值一致。旧受管尝试已核验取消；8007 在空闲维护窗口完成重载。管理员真实工作台完成归档、恢复及原任务重试，D08 已以 8 MPI 积分，合同/科学参数/二进制指纹保持一致。71 项聚焦测试与前端内存构建通过；完整 6 小时 D08、7 个 forcing 时次及 3 个 CFD 快照尚未完成，不能宣称端到端验收通过。

### 已完成积分任务的独立输出校验器升级

`scripts/upgrade_output_validator.py` 是运维入口，不接入智能体环境修复工具。默认只读：指定 `--task-id`、`--validation-release` 和新的私有 `--output` 路径，核验目标发布完整性、原受管尝试成功终态、冻结配置和真实 namelist，逐文件校验原生输出 SHA、时次、变量及数值。已成功或部分成功任务仅生成完整复验审计，不改状态或重新渲染。

仅 `failed` 且属于输出阶段失败的任务可生成绑定计划。操作员显式使用 `--apply --plan <计划> --confirm-digest <精确摘要> --audit <新的私有审计>`，要求8007温和退出，整次交易持有公共维护锁及IPv4/IPv6端口占位；所有普通计算须终止或确证尚未启动。历史 `waiting_restart` 另须实时证明原远端尝试失败或已核验取消且会话进程全部退出；尚未启动的原生任务必须无启动标记、且远端控制和尝试目录不存在，未知状态拒绝。检查不会改写原任务状态。

根维护窗口可通过 `--maintenance-lock-fd` 传入同一锁的继承描述符；入口验证其设备和inode属于精确公共锁，复用同一open-file-description，不释放外层锁。离线重载、多个独立绑定和重启由外层窗口串联；每次绑定仍在双栈端口上listen占位但不接收请求，交易结束才关闭占位。没有继承描述符时入口自行取得公共锁。

升级只追加 `runtime.output_validation_override`，绑定原任务、请求、计划、计算发布和尝试，以及新的校验发布、脚本、配置和输出指纹；不修改原计算版本、配置、计划摘要、尝试编号或任务状态，不启动计算或恢复。结果重试仍走原恢复计划和专用成员原生ASK；`recovery_policy=confirm` 不允许直接绕过。原发布和输出保留，覆盖绑定或审计响应不确定时先只读核查，不能重放升级。

### 可选薄发布

`scripts/deploy_runtime_release.py --thin` 默认只读返回部署配置，额外 `--apply` 才构建新版本；未指定 `--thin` 时保留完整复制模式。源码通过 `WRF_HPC_WRF_SOURCE_DIR`、`WRF_HPC_WPS_SOURCE_DIR` 显式指定，不从 current 推断。薄发布要求已有固定、只读的完整 `source/anchor.json`，选中文件、源码摘要及五程序 SHA 必须与其绑定的完整树一致。

薄包保持 wrapper 原路径：五个程序、`link_grib.csh`、WRF `run/` 的全部非任务运行表、WPS 全部 Vtable 和 GEOGRID/METGRID 表，保留 Registry 中含 rconfig 的非 `.var` 文件、编译声明、版本声明及 WPS 参数读取元数据，并上传完整运行脚本集合。全部文件物化为 regular file，不保留符号链接；越界、悬空和目录链接拒绝。`link_grib.csh` 必须可执行，固定 shebang 解释器及其动态库纳入依赖 SHA 校验。现有物理方案的运行表全部保留，不按默认方案裁剪。

manifest 的 `layout=thin` 记录精确 native 清单、摘要与固定源码锚绑定；首次激活与 `--resume-staging` 使用同一完整校验器，缺表、缺脚本、额外文件、链接或锚点漂移均拒绝。恢复使用已冻结 manifest 的布局，不再传 `--thin`。发布前封存只读并原子切换，新薄包不能作为完整源码锚。06 已部署：199 文件、365,436,855 字节、无符号链接，369 项就绪检查通过；完整源码04、历史01–05和现有计算绑定不变。此证据不能替代06真实资料与计算门禁。

### 默认地理资料完整性门禁

新部署由固定的 `scripts/validate_geography.py` 生成 `manifest.geography`（`wrf-geography-1`）。首次激活、保留事务恢复和实际提交/启动的强制 readiness 共用该发布内已绑定SHA的验证器，门禁名称仍为 `geography`。契约按官方 WPS 的 default 路径、插值映射与数值优先级记录必需源；所有非optional源都须存在，optional源只记录声明，不被冒充必需资料或全球覆盖证明。

当前支持完整全球 `regular_ll` 二进制瓦片：核验索引SHA、正/负dy覆盖、网格与tile整除关系、5/6位文件名、垂直范围、border、wordsize，以及signed/endian/category/scale/missing元数据。逐个打开文件只核验regular、可读、无链接及精确字节数，不读取瓦片内容；不支持的必需投影、局地区域或部分边缘tile布局拒绝就绪。此门禁保证结构完整性，不能证明瓦片数值内容未损坏，也不替代geogrid真实运行与结果验收。

契约保存19个必需数据集各自的相对路径、索引与tile名称/字节清单摘要、元数据、数量和逻辑大小。现有06的GEOGRID及共享资料只读核验为3450瓦片、16,640,082,432字节齐全；38个optional条目不作为必需资料。07已显式部署并通过370项就绪检查；08继承同一地理资料合同并修复五处资料头解析管道，亦通过370项检查。服务器current和认证API就绪均为08，新计算默认08、既有计算仍绑定04，08 真实短程和 GFS 四域 24 小时计算及产品已完成，完整八案例矩阵仍未完成。没有此字段的不可变04/06继续按其原冻结合同复验已有输出，不追加新脚本要求或重写旧manifest。


### 2026-10-09 工作台管理切换

普通发布已切换为 `ordinary-20261009-10`，指纹 `e772c0e4cb0cb93f40482bb96dd516f8375c2f6629f496ab13ac42b9285a12b3`。8007/8012 已温和重载并加载管理能力；任务元数据、恢复确认、删除计划、幂等下载及绑定摘要的资料清理接口已生效。旧 WRF cron 的唯一条目已精确备份并停用，三个已核验预取下载器温和退出；旧资料池、下载队列和部分文件保留。其他服务身份及原任务行摘要一致。自动策略仍须管理员显式保存启用，不将旧队列视为已清空。审计位于 `.run/wrf-refactor/20261009-management/management-activation.json`，远端调度审计位于配置发布根的 `manifests/management-scheduler-20261009.json`。

验证：后端隔离回归 363 通过、1 跳过；远端 Linux 六项合成资料安全检查通过并移除夹具；Vue 构建通过；浏览器窗口主操作可达性复验通过。真实工作台两源手动提交、故障接管/回退及跨产品色标核对仍在验收，不能将原计划标为完成。历史失败任务不自动恢复或删除。


## 历史部署快照（不描述当前补丁）

本轮 GFS、ECMWF 工作台手动提交各完成一例真实短程计算，共 6 份原始输出、42 张正式 WebP，绑定计算发布 10；11 仅更新资料调度与引用保护，不重写旧计算配置。后端 368 项通过、1 项跳过，Vue 浏览器 16 项与 React 共享组件浏览器 7 项通过，两宿主覆盖七种窗口。已完成实际 MPI 两进程取消与 API 09→10 回退恢复；8012 一次温和退出超过期限，后续自行退出，重新核验后恢复，未强杀。四个历史 waiting_restart 保留兼容值，界面显示失败待处理，不自动恢复或删除。专用已取消任务 `wrf_gfs_20261007T030000Z_d3a27011` 已经用户精确确认后完成彻底删除验收：2 文件、1,711 字节，清单 digest 不变；本地/远端专属目录和任务记录已移除，删除审计及幂等墓碑保留，其余 15 条任务逐行摘要不变。本轮工作台交互、任务与资料管理的工程验收已完成；预报准确率及不同产品使用同一色表不属于该结论。证据位于 `.run/wrf-refactor/20261009-management/`；跨产品单位/色标核对见 `backend_wrf/docs/PRODUCT_PRESENTATION_AUDIT.md`，历史产品色表保持。


### 早期阶段记录（仅供追溯，不作为当前部署说明）

2026-10-09 管理事务源码协议：POST `/api/wrf/forcing/{source}/cleanup-plans` 创建10分钟计划；GET `/api/wrf/forcing/cleanup-plans/{id}` 查询；POST `.../{id}/execute` 提交digest单次执行，仅原管理员可用。旧两种直接cleanup路由返回410。POST `/api/wrf/forcing/downloads` 必须带Idempotency-Key；GET同路径列出当前用户请求（管理员可见全部）。下载操作保留hours，按远端reference去重与释放，cancel_pending/cancel_unknown继续保护资料。API启动将中断管理事务置unknown，不重放。新队列snapshot/release/expected-digest协议须随不可变运行包部署，运行09尚未加载，自动清理仍关闭。

2026-10-09 管理增量尚未部署：新增任务元数据表、删除计划与墓碑、资料策略/下载操作和清理审计表，旧直接 DELETE 在源码中返回 410；恢复计划增加执行用户绑定。运行中的 8007 仍为 09 版本，旧 cron 仍是远端预取入口；自动清理未启用。新删除流程和预取切换须在隔离实例验证并在空闲维护窗口核验后上线，历史任务和资料不得因源码存在而删除。

2026-10-09 已应用高分退役源码：移除旧 `downscale/` 执行业务、专属脚本及区域推荐，保留独立 `downscale_retired.py`、`retirement.py` 与 `scripts/retire_downscale.py` 审计兼容工具。普通资料生命周期与受管运行器保留。两条精确谱系的专属环境、原始数据及 1,353 条对应记录已清理，数据库表结构未改动；旧普通任务和其他资产保留。清理后的 183 项隔离回归通过，API 就绪仍为 09、issues 为空，工作台认证结果切帧通过。下文旧阶段门禁与执行准备是历史记录。

2026-10-09 资料链路已切换：默认普通发布为 `ordinary-20261009-09`，指纹 `4f67652f350fa75f4abb20a91a0252e850373236714823d0bb5092267a73e711`，370 项就绪门禁全部通过，8007/8012 温和重载完成。114 项后端聚焦回归、4 项维护回归及 6 项实际 Linux/数据盘合成故障检查通过。切换前后普通任务逐行摘要、四个 waiting_restart 的远端终态证明、其他六服务与 Redis 身份一致。08 保留用于回退，旧任务计算与 08 输出校验器绑定均未改写。本机预取开关仍关闭；远端另有旧 WRF cron 指向历史独立资料池，尚未迁移或停用，不能说全局自动下载已关闭。八案例功能计算及产品矩阵已于 2026-10-09 完成 8/8：264 份原始输出、1,848 张 WebP；最后 ECMWF 案例经新原生 ASK 完成仅输出恢复，原计算版本、尝试和配置保持。管理员已重新登录并核验身份。v4 精确清单及额外 197 个展示缓存文件已执行退役：两任务专属数据和环境、精确数据库记录、旧代码及批准的缓存/验证目录均已清理；197 个静态资源认证访问均为 404。8007/8012 已恢复，普通任务摘要、其他六服务及 Redis 身份保持；临时管理员令牌已清除。完整故障注入、回退演练及跨产品色标统一验收仍未完成。

2026-10-09 资料链路实现记录（随后切换结果见文首）：新增本机资料引用文件锁，选周期与持久化绑定、显式清理串行；共享资料清理仅管理员可用。下载工作器使用系统盘内核锁并传给 curl，未知/旧锁不自动删除，队列损坏不重置；清理进入受管队列，逐路径响应不完整标记未知且不自动重放。任务资料等待默认 21600 秒，截止时间持久化，单次 curl 最长 3600 秒。资料脚本随当前不可变发布版选择，避免继续调用旧 04 脚本。维护入口对 waiting_restart 增加新鲜远端退出证明。已随文首所列 09 发布生效；功能矩阵后续完成 8/8，批准范围的物理退役已完成，局部故障注入证据不等于完整链路验收。

2026-10-09 早期资料生命周期增量（以下为当时待重载状态）：`sync_latest_gfs/ecmwf` 默认不清旧周期，自动预取、两源预取和 GFS 手动下载路由不再隐式删除资料。`active_forcing_cycles` 还保护 paused_external、waiting_restart、failed、partial_success 的资料引用。43 项资料窗、队列、引用保护与任务安全回归通过。显式清理仍是独立运维操作；当前引用快照到删除之间尚无覆盖整个任务生命周期的持久租约，下载锁仍需补启动身份和 PID 复用保护，排队等待尚需总期限，因此不得宣称下载/删除链路已完成故障注入验收。运行实例自动预取关闭；全周期 GFS 73/ECMWF 25 文件计数只描述资料池，任务按含 spin-up 的实际资料窗判定。

2026-10-09 当前生效：普通发布 `ordinary-20261009-11`，指纹 `a34363cbd75e379a3ee65977447bbb967065b515f7f965d2c1952936c7b51125`；8007/8012 已在计算空闲窗口温和切换，管理接口已加载。Vue/React 使用共享组件 2.2.0，工作台适配宿主容器，不修改系统主题、底图和全局滚动。旧 WRF cron 唯一条目已备份停用；backend_wrf 是唯一调度入口。管理员已保存自动预取和无引用过期资料清理策略：两源 00 UTC f000–f072、保留最近两个完整周期、120 GB 空间下限。两源工作器共用一个继承式传输锁，任务所需资料优先；排队未绑定周期、失败待恢复、未知尝试和下载请求均保护引用。旧资料池、队列和部分文件保留。

### 2026-10-16 两源独立下载槽

新发布16已安装并安全加载，GFS/ECMWF改为每源一个传输槽，保留同源、周期、引用和清理保护。当前旧下载工作器不停止，等待自然退出后接管；真实两源并发验收仍待完成。详见 docs/PROVIDER_PARALLEL_DOWNLOAD_ACCEPTANCE.md。

### 2026-10-16 文件边界交接

操作员精确绑定的文件边界交接已实现，实际盘七项及资料池63项通过。GFS已进入16包并与原ECMWF同时传输，ECMWF当前文件未中断，完整升级交接仍等待文件边界，预算到期只退出等待、不重放。详见 docs/POOL_WORKER_HANDOFF_ACCEPTANCE.md。

### 2026-10-16 ECMWF单源交接完成

核验旧事务未对ECMWF动作后，独立新计划以4200秒预算等待当前文件自然结束；20:42:14已交接16包，原分片、完整文件和引用保持，原事务未重放。新版真实续传推进，11项实际盘原生回归通过。GFS73/73，ECMWF9/25继续下载，上游仍较慢。详见 docs/ECMWF_SINGLE_HANDOFF_ACCEPTANCE.md。
