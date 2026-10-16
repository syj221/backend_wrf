# WRF 工作台与智能体重构验收门禁

## 当前生效与本轮验收

验收索引：`.run/wrf-refactor/20261009-management/verification-summary.json`；逐文件计算证据为同目录 `manual-computations-artifacts.json`，保留产物的精确数量、大小及原因见 `retained-artifacts-summary.json`。两前端生产构建通过；保留现有大 bundle 提示。隔离临时夹具由 teardown 清理，真实两源计算和部署审计不作为测试垃圾删除。

2026-10-09 当前生效：普通发布 `ordinary-20261009-11`，指纹 `a34363cbd75e379a3ee65977447bbb967065b515f7f965d2c1952936c7b51125`；8007/8012 已在计算空闲窗口温和切换，管理接口已加载。Vue/React 使用共享组件 2.2.0，工作台适配宿主容器，不修改系统主题、底图和全局滚动。旧 WRF cron 唯一条目已备份停用；backend_wrf 是唯一调度入口。管理员已保存自动预取和无引用过期资料清理策略：两源 00 UTC f000–f072、保留最近两个完整周期、120 GB 空间下限。两源工作器共用一个继承式传输锁，任务所需资料优先；排队未绑定周期、失败待恢复、未知尝试和下载请求均保护引用。旧资料池、队列和部分文件保留。

本轮 GFS、ECMWF 工作台手动提交各完成一例真实短程计算，共 6 份原始输出、42 张正式 WebP，绑定计算发布 10；11 仅更新资料调度与引用保护，不重写旧计算配置。后端 368 项通过、1 项跳过，Vue 浏览器 16 项与 React 共享组件浏览器 7 项通过，两宿主覆盖七种窗口。已完成实际 MPI 两进程取消与 API 09→10 回退恢复；8012 一次温和退出超过期限，后续自行退出，重新核验后恢复，未强杀。四个历史 waiting_restart 保留兼容值，界面显示失败待处理，不自动恢复或删除。专用已取消任务 `wrf_gfs_20261007T030000Z_d3a27011` 已经用户精确确认后完成彻底删除验收：2 文件、1,711 字节，清单 digest 不变；本地/远端专属目录和任务记录已移除，删除审计及幂等墓碑保留，其余 15 条任务逐行摘要不变。本轮工作台交互、任务与资料管理的工程验收已完成；预报准确率及不同产品使用同一色表不属于该结论。证据位于 `.run/wrf-refactor/20261009-management/`；跨产品单位/色标核对见 `backend_wrf/docs/PRODUCT_PRESENTATION_AUDIT.md`，历史产品色表保持。

以下保留各阶段时点记录，发生冲突时以本节及绑定审计为准。


## 2026-10-09 工作台管理增量与旧服务兼容

5177 为 Vite 开发服务，修改源码会热更新页面；未重启进程不能证明页面未变化。现场 8007 的 OpenAPI 有普通 options 接口，但尚无新增资料策略、任务元数据接口，页面此前请求策略时收到 404 `Not Found`。调用方现以 `options.workbench_management.id=wrf-workbench-management-1` 启用管理界面；无声明不请求缺失接口，保留普通配置、历史查看及未保存输入。策略未知不显示为已关闭。该声明已加入后端源码，运行实例未重载。

当前源码共享组件及 Vue/React 消费锁为 2.2.0。Vue 构建通过；16 项隔离 Chrome 回归全部通过，包括旧后端兼容、提交结果未知时对账及七种窗口尺寸的主操作可达性。相关后端 HTTP 权限、元数据并发版本、删除墓碑、发布互斥等 34 项回归通过；此前全套隔离回归为 350 通过、1 跳过。浏览器测试使用模拟接口，不能作为新增工作台手动提交真实计算、跨服务故障或彻底删除验收。

管理增量未部署至 8007/8012，旧资料 cron 未迁移，自动清理未启用。未新增、恢复或删除现有普通任务。删除对账、下载请求引用、精确资料清理计划及真实故障矩阵仍须完成；不得将 UI 兼容修复表述为原计划全部完成。

2026-10-09 资料链路已切换：默认普通发布为 `ordinary-20261009-09`，指纹 `4f67652f350fa75f4abb20a91a0252e850373236714823d0bb5092267a73e711`，370 项就绪门禁全部通过，8007/8012 温和重载完成。114 项后端聚焦回归、4 项维护回归及 6 项实际 Linux/数据盘合成故障检查通过。切换前后普通任务逐行摘要、四个 waiting_restart 的远端终态证明、其他六服务与 Redis 身份一致。08 保留用于回退，旧任务计算与 08 输出校验器绑定均未改写。本机预取开关仍关闭；远端另有旧 WRF cron 指向历史独立资料池，尚未迁移或停用，不能说全局自动下载已关闭。八案例功能计算及产品矩阵已于 2026-10-09 完成 8/8：264 份原始输出、1,848 张 WebP；最后 ECMWF 案例经新原生 ASK 完成仅输出恢复，原计算版本、尝试和配置保持。管理员已重新登录并核验身份。v4 精确清单及额外 197 个展示缓存文件已执行退役：两任务专属数据和环境、精确数据库记录、旧代码及批准的缓存/验证目录均已清理；197 个静态资源认证访问均为 404。8007/8012 已恢复，普通任务摘要、其他六服务及 Redis 身份保持；临时管理员令牌已清除。完整故障注入、回退演练及跨产品色标统一验收仍未完成。

2026-10-09 主题边界修正：当前共享组件为 2.1.1，撤销新增的底图暗色着色，工作台遵循框架原有地图设置。下文 2.1.0 为此前阶段证据；2.1.1 已通过黑白主题/八投影场景及底图原色像素回归，未改后端、计算或资料生命周期。

最后增量核对：2026-10-09。本文记录已取得的证据和待完成门禁，随实际验收更新；当前不具备“整体验收通过”结论。工作区尚有未提交改动，证据须绑定下面的包、配置、发布和产物指纹，不能仅以分支名判断版本。

## 2026-10-09 批准退役范围完成

[完成回执](../../.run/wrf-refactor/20261009-lifecycle/retirement-completion.json) 绑定原 v4 digest 与 197 文件增量 digest。主数据清理包括 97,790 个常规文件、13,891 个符号链接（不跟随共享原件），逻辑大小 39,724,412,320 字节、2 条任务谱系与 1,353 条精确记录。先撤销登记并验证 197 个认证静态请求均为 404，再删除文件；清理后再次验证全部 404。此数字不等于物理磁盘释放量。

六组余项回执均 complete：9 项远端旧代码、3 个区域缓存、5 个失败 staging、8 个批准临时验证目录、3 个旧 vendor 包，以及 37 项后端源码删除/适配。最终前端与文档回执另验证 10 个旧文件删除、16 个命名 API 函数移除及 6 处指定文档片段适配；保留原源码备份和现有无关改动。新增审计/构建不在删除范围内。

8007/8012 已恢复；普通记录摘要及其他六项服务/Redis 身份不变。认证就绪 ready、09 发布、issues=[]；活动和队列均为 0。实际工作台显示服务已连接、13 条保留普通任务、ECMWF 四帧结果，并通过下一帧操作；主题仍适配框架。管理员临时登录文件已在访问核验结束后清除。

清理后实际源码 183 项隔离回归通过，首次回归因沙箱禁止回环 bind 失败，保留原报告；仅调整执行权限后同一源码/断言通过。Vue 生产构建通过，包完整性验证为 2.1.1；存在既有大 bundle 提示，不是构建失败。审计目录权限不足的首次余项执行在任何该组删除前退出，已保留失败回执；后续使用 0700 新回执目录重新核验完成，未降低保护条件。

本轮新增并有意保留的审计/源码备份/验证产物见 [精确保留清单](../../.run/wrf-refactor/20261009-lifecycle/retirement-retained-artifacts.json)：144 个文件、7,496,119 字节（清单自身另计）。包括隔离测试快照及独立前端构建，等待整个重构验收完成后另行确认清理；不混入真实运行数据。`schemas.py` 末尾冗余空行已按 `git diff --check` 修正，AST 完全一致，变更前后 SHA 单独保留。

此结论仅关闭批准退役与八案例功能矩阵；完整故障注入、回退演练、旧独立资料池 cron 迁移及跨产品色标标准仍未完成，不能宣称整个重构计划或预报准确率验收完成。

## 2026-10-09 八案例闭环与退役增量授权

最新功能验收汇总为 [eight-case-acceptance.json](../../.run/wrf-refactor/20261009-lifecycle/eight-case-acceptance.json)：8/8、264 份原始输出及 1,848 张 WebP，绑定原计算发布版本及逐行记录摘要。EC-N 的新计划 `repair_e9053d91b9ec444ca2459c32c05f55ac` 经一次原生 ASK 接受，仅恢复结果处理；任务 `wrf_ecmwf_20261006T030000Z_0d2e5ad3` 已 succeeded，attempt 1、原 04 计算及 08 输出校验器绑定保持。其 28 张 WebP 在 8012、8007、8002 共 84 次认证读取 SHA 一致，匿名 401、旧版本 409；实际工作台变量、时间帧和播放验证已记录。此前一次恢复被“原受管会话仍有活跃进程”门禁拒绝，随后只读检查未见对应会话；未移除门禁，历史瞬时阻断的根因未证实。

v4 原清单保持冻结。用户另行仅批准退役谱系新增展示缓存：`.run/wrf-workbench/data/WRF/runs/wrf_gfs_20260918T120000Z_4501020c`，197 文件、520,857 字节、无符号链接；增量 digest `cf09add476bd7543a4d0c8417ccbbbef24452298f59cc1efe42379c02b8bdfe6`。新清单仅此差异，其他任务、远端资产、登记、引用和保护范围不变。新增验证产物不属于删除授权。

03:24 UTC 的维护尝试在 8012 已建立聊天连接检查处退出，尚未发送 SIGTERM，未进入退役事务；`primary-retirement-receipt.json` 未生成。保留 `primary-retirement-maintenance.json` 失败审计，禁止盲目重放；释放聊天连接后须重新检查身份、空闲与全部文件哈希，使用独立执行审计。管理员认证已刷新，但资源撤销后的认证访问拒绝尚待执行核验。下文 7/8、待 ASK 与凭据过期叙述属于此前时点记录，不代表当前状态。

## 2026-10-09 退役准备复验

v4 审批 digest 保持 `484bc604043723202aeed1757d0684ddbc8919c3e94c3eaba68bc0317b575d51`，删除范围没有扩大。对旧执行准备的 178 项绑定核对发现 12 项绑定发生变化，涉及 10 个唯一文件；旧协调包不能直接执行。三个后端共享源码文件与一份 Agent README 的退役改动已重新合并到当前版本的私有副本，原始清单、旧备份和生产源码不变。完整退役后源码临时副本的 183 项测试通过，涵盖原退役合同及新的资料生命周期回归；临时副本已清理。最初验证中发现临时副本缺少配套测试/依赖，已补齐；另将发布绑定测试改为显式合成路径，避免依赖生产 `.env`。没有降低测试断言。

证据：`.run/wrf-refactor/20261009-lifecycle/retirement-source-rebase-tests-v3.json`。这仅证明源码预演通过，尚未生成可直接应用的新完整协调包，也不替代远端删除清单、引用、进程与文件指纹的最终重验。ECMWF 的 `repair_28f30496d5434b08bd0654f11cf4ee88` 于 2026-10-09 03:01:27 UTC 到期且未被批准，UI 已禁用确认；任务未恢复。新的管理员登录文件尚未生成；401 不作为退役访问证明。物理删除保持未执行。

## 2026-10-09 资料链路续建

2026-10-09 资料链路实现记录（随后切换结果见文首）：新增本机资料引用文件锁，选周期与持久化绑定、显式清理串行；共享资料清理仅管理员可用。下载工作器使用系统盘内核锁并传给 curl，未知/旧锁不自动删除，队列损坏不重置；清理进入受管队列，逐路径响应不完整标记未知且不自动重放。任务资料等待默认 21600 秒，截止时间持久化，单次 curl 最长 3600 秒。资料脚本随当前不可变发布版选择，避免继续调用旧 04 脚本。维护入口对 waiting_restart 增加新鲜远端退出证明。已随文首所列 09 发布生效；功能矩阵后续完成 8/8，批准范围的物理退役已完成，局部故障注入证据不等于完整链路验收。

本机聚焦回归与真实 Linux 数据盘故障夹具分别记录；远端 6 项通过：工作器骤停后子进程持锁、子进程退出后接管、未知标记保护、活动工作器阻断清理、精确删除不影响其他周期、审计完整。输入为合成小文件，不下载真实资料、不启动 WRF；夹具已清理。证据位于 `.run/wrf-refactor/20261009-lifecycle/remote-pool-fault-tests.json`，生产切换另记。

## 2026-10-09 显示与资料审计增量

此前 2.1.0 显示阶段：source SHA `05ea2fd476542967ba2444f6b35f5d1f8c46e5fea2b48a3e07bdf51b1c1d39d5`，bundle SHA `868c569071b519d03cd13b18bba00e3a62c88b857ad2aebace1909e79930b012`，两端实际安装一致。下文 2.0.6 指纹是历史验收版本，不代表当前消费锁。Vue 29 共享单测、6 主站聚焦、13 Chrome 场景，React 26 组件测试、6 Chrome 宿主场景、TypeScript 与两个前端隔离生产构建通过。Chrome 验证包括八种显示投影、主题、单位、域树、迟到预览、非法输入阻断；只代表覆盖场景，不等于全部地图投影的测绘精度认证。真实登录工作台默认等经纬度、空地图、黑白主题、历史成功产品 °C/K 显示、Lambert 显示切换与换帧已复验。验证产物见工作区 `.run/wrf-refactor/20261009-display/`，待精确清理许可，未纳入 v4。

计算矩阵仍为 7/8，七案例既有逐文件证据 260 原始输出/1820 WebP 保留。本轮只读任务快照为 14 个：9 succeeded、1 failed、4 waiting_restart；1 个 failed 的 ECMWF 非默认案例已完成原计算、等待授权输出恢复，历史 waiting_restart 不自动运行。未接受恢复 ASK，未新增模拟、下载、发布或删除。

资料代码加固 43 项聚焦回归通过：取消预取和手动下载的隐式旧周期清理，保护暂停、失败、部分成功和等待恢复任务引用。该显示阶段尚未重载 8007；后续已随文首 09 发布生效。本机预取开关关闭，界面将完整周期计数与任务资料窗口区分，并把实际两源预取按钮改成相应名称。

该显示阶段发现的下载锁、引用/删除竞态与任务资料等待期限已在 09 中加固并完成聚焦测试。仍未关闭：旧 cron 迁移、跨服务重启/SSH 丢响应/MPI 取消/资源耗尽的完整真实故障注入矩阵，以及回退演练。不得凭历史任务终态或局部单测宣称运行链路已全面健壮。

色标按发布元数据一致显示，温度 K→°C、气压 Pa→hPa 仅转换标签。原 renderer 的时序色标范围按帧百分位包络固定，但跨域/跨任务不可直接按颜色比较；U/V 发散色表还需显式零中心规范。WRF 总览的旧独立硬编码 palette 已移除；其他来源产品尚未完成全系统 registry 和阈值审计。CF 定义单位语义与可换算要求，不能据此称自定义 palette 为国际统一标准；参考 [CF conventions](https://cfconventions.org/cf-conventions/cf-conventions.html)、[ECMWF contour styles](https://confluence.ecmwf.int/spaces/MAGP/pages/14158128/Contouring)、[Matplotlib scientific colormaps](https://matplotlib.org/stable/users/explain/colors/colormaps.html)。

本轮源码、文档和消费锁已变化，旧冻结退役协调准备不再满足指纹门禁。v4 删除范围授权仍有效，但执行前必须生成新的只读绑定并复核范围不扩大；不重放旧准备，高分物理删除保持未执行。

状态约定：`PASS` 为本层证据已通过；`PARTIAL` 为只完成部分范围；`IN_PROGRESS` 为维护进行中；`PENDING` 为尚未执行；`QUEUED` 为已持久化排队、尚未远端 launch；`FAILED` 为该次尝试未通过门禁，保留失败证据；`WAITING_RESTART` 为本次尝试失败、业务任务等待另行授权恢复，不能据此自动重启。代码存在、构建成功、环境就绪、真实计算成功和产品通过验收分别记录，不相互替代。

## 1. 分层门禁与当前状态

| 层次 | 门禁 | 当前状态 | 已有证据与限制 |
| --- | --- | --- | --- |
| 实现 | 工作台与智能体共用一个组件包，宿主掌握网络、鉴权和执行 | PASS（实现） | 四种 Vue Custom Elements；2.0.6 已冻结，两宿主实际安装版本、bundle SHA 核对一致。2.0.5 数值门禁与真实编辑证据保留，2.0.6 为运行错误摘要/折叠详情的最小修复，React 新专项27单测/2Chrome/TypeScript及构建通过，真实6登录错误呈现与编辑门禁已独立核验。智能体最终计划、原生 ASK 属于 AgentScope 宿主，组件不修复环境、不自行提交计算。 |
| 实现 | 新工作台直接编辑、校验、幂等创建 | PASS（实现） | 默认空中国亮色地图；无区域推荐、多个物理预设、计划/逐组勾选页；支持四域树、兄弟域、稳定节点键、增删子树、撤销及后端投影预览；spin-up 和统一垂直层数可改；支持逐域积云显式覆盖/恢复按网格距默认、子树删除影响提示和撤销、投影预览防抖/取消/重试。非法数值缓冲阻断全部执行入口，合法值实时同步。 |
| 实现/聚焦回归 | 智能体计划逐项修改与原生 ASK 授权顺序 | PASS（实现/41项相关回归及真实ASK拒绝/修改/接受） | 计划每项可点击修改；只读历史无修改回调时不显示按钮。底部为修改配置/暂不运行/确认并开始模拟，全部操作共用同步锁。修改必须先完成原生拒绝，再使匹配草稿的冻结计划失效，成功后才打开共享编辑器；拒绝失败可重试，草稿失效失败不会打开。该41项为隔离增量回归；随后真实8012会话已完成原生ASK拒绝→共享编辑器将输出60改30分钟→新计划→接受，旧计划无提交、新计划唯一任务，证据另列。 |
| 实现 | 高分功能退出普通工作台 | PASS（活动入口） | 旧路由显示退役说明。专属源码、数据与环境按用户已确认 v4 清单等待执行门禁；尚未物理删除，不能把入口退出写成已清理完成。 |
| 单元/构建 | 主站及共用组件 | PASS | 2.0.6 `npm test` 73/73，其中共用模型、运行展示与数值门禁 26 项；主站生产构建及共用包可复现构建通过；重新构建与锁定 SHA/integrity 一致。构建仍有现有大 chunk/纯注释提示。 |
| 隔离浏览器 | 真实 Chrome、合成会话、全部业务 API 拦截 | PASS（5版完整；6版重点） | 使用后端真实投影算法；覆盖地图、四域树/兄弟域/孙域、非法父域缩放拒绝、撤销、连续参数编辑、input-only 快连几何编辑、未失焦提交、晚到初始预览不覆盖新值、中文运行状态与未知码、WPS 无虚构百分比、WRF 实测时间/进度、终态覆盖旧阶段、提交响应不明仅对账、日志折叠、认证 WebP、结果播放、延迟能力加载与旧合同。新增覆盖逐域积云及恢复默认、子树删除提示、503 原配置重试、180 ms 几何防抖、空/非法/越界数值保留与全部提交阻断、同草稿保留/新草稿清理缓冲，以及侧栏真实模拟进度。2.0.6 另有错误摘要/有界脱敏与 transport/JSON503 提交不明各两轮，共6 Chrome PASS；不将它与历史完整用例相加为独立总数。没有访问真实 8007，也没有启动 WRF。 |
| 单元/隔离部署 | 发布 CLI、事务恢复与固定来源入口 | PASS（18/18） | 私有 TemporaryDirectory、假二进制文件与受控依赖响应，原 11 项覆盖完整门禁、只读旧 release 复制、源权限/内容保持不变、无跟随解封、固定 Python、CLI 指纹、失败不切换及恢复重新封口；新增 7 项覆盖固定来源入口。没有执行真实 WRF。固定来源入口真实只读核验与维护负责人 apply 均完成，见固定来源入口审计。 |
| 真实登录 UI | 当前登录用户、实际 8007/8012、两个宿主 | PARTIAL | 首次失败后，实际登录工作台已完成 GFS 1 小时与 ECMWF 3 小时短程，几何及中文状态正确，结果页分别显示 2/2 与 4/4 帧。阶段/模型时间已在真实混合树运行页观察；新 monitor 已温和加载。2.0.5及2.0.6两前端已温和切换、其他六服务身份不变；真实登录 Chrome 编辑门禁 PASS，工作台及 Agent iframe 均验证非法数值保留/禁提交、合法恢复。2.0.6另验证真实EC-T错误摘要固定中文、技术详情默认折叠且旧task/attempt不变。两次编辑检查没有执行save/plan/submit；随后08激活，真实模型ASK拒绝/修改/新计划/接受已完成并生成唯一短程任务，该额外Agent任务已succeeded及发布21帧资产；该Agent短程聊天结果已实际切帧/切U10/播放并暂停；随后发现并修复同产品版本刷新重置时次，薄宿主隔离回归及真实登录刷新保持U10/00:30/地图ready已独立PASS；原矩阵七case本地完整产品已通过，两混合树真实结果切域/变量/播放已通过；EC-N输出恢复与完整八case门禁仍待授权和取证。 |
| 真实 HPC 环境 | release03/04 时点协议、独立校验 release05/06/07/08 | PASS（各版本时点） | 03 首次通过当时 8055/8055；04 通过 8055/8055，历史计算绑定04保留；05 通过 8056/8056；薄发布 06 于 10:54:27 UTC 通过 369/369（按实际选定文件检查，不能与全源码版本的检查数量直接比较）。07 于 11:25:53 UTC 通过 370/370，加入强地理资料合同；08 于12:58:14 UTC通过370/370，含五处大头部解析修复；固定源码入口仍为04；13:06 UTC activation v2完成，当前API默认08、五个既有04计算任务已绑定08输出校验器，计算绑定不变。安装/检查不改变既有计算绑定或证明产品验收。 |
| 真实 HPC 输入 | 两数据源解码与隔离副本 | PARTIAL | 两源短程全套 2+2 文件语义检查及 WPS 已通过，原4文件复制SHA一致；各矩阵task/attempt实际输入、语义报告与冻结配置已分别绑定，七case最终本地证据复核原输入/配置/程序一致。GFS07完整窗口f000–030和ECMWF06 f000–030步长3的候选复制记录另行保留，候选文件存在不替代实际任务的输入门禁。 |
| 真实 HPC 计算 | 短程、24小时单域、混合四域树、非缺省设置 | PARTIAL（7/8本地完整产品通过） | 两源S、两源D、EC-T、GFS-N及同配置替代GT08共7case succeeded，本地260native/1820WebP/目录绑定均通过；六个既有计算仍为04，四个输出重试仅08 validator，不改attempt/request/config/程序；GT08为新08计算，旧GT04退出141尝试保留。EC-N已完成原生积分及08只读复验4native，仍待人类本次明确授权的新诊断/计划/ASK，28帧产品未恢复，不能声明8/8或生成完整矩阵门禁。额外Agent08 3native/21WebP属于独立入口验收，不计入原矩阵；HTTP资产权限、预报技巧和完整资源峰值分别核验。 |
| 产品/数值 | 原生输出、渲染、发布登记与宿主展示 | PARTIAL（7/8本地完整产品通过） | 固定collector七case全部products_ready=true，共260native/1820WebP，完整时序/必需变量/finite/冻结投影与拓扑、逐文件SHA、meta/正式catalog绑定均通过。工作台两源短程及两混合树已实际登录查看、切域/变量/帧和播放；两树每域首末U10帧经三个入口抽样认证HTTP/SHA及拒绝门禁已独立PASS。EC-N4native已原生复验，但28WebP尚待授权恢复。额外Agent08的3native/21WebP、聊天播放与三个认证资产入口另行PASS，不计入原矩阵。matrix_complete=false；预报技巧、全生命周期资源峰值和生产稳定性未验收。 |
| 维护回退 | 本机资产迁移、API 切换、完整回退演练 | PASS（切换）/ PENDING（回退演练） | 迁移和切换完成：8007 使用 backend_wrf/.venv，8012 温和重载，SQLite 备份记录/配置一致并启用工作区 .run/wrf-workbench，旧资产精确映射；Redis 及其他服务身份不变。旧 CPU 构建、旧 API 解释器和旧数据库保留，完整回退流程尚未演练。 |

持续回归源码：[共享模型测试](../../webpage_sys/packages/wrf-ui/tests/model.test.js)、[Chrome 回归](../../webpage_sys/tests/browser/wrf-shared.spec.mjs)、[发布 CLI 回归](../test_runtime_deployment.py)、[固定来源入口回归](../test_source_anchor.py)。

## 2. 冻结版本与证据

### 共用组件

| 字段 | 冻结值 |
| --- | --- |
| 包 / 展示合同 | `@zhihuiqixiang/wrf-ui@2.0.6` / `wrf-ui-2` |
| 源码 SHA-256 | `c9883b469b5c007b44e8c75a4ae08a09ee071b31f3bc051d42eecbf612ba7f69` |
| bundle SHA-256 | `4471f2ede2358de1f11e002dabba5cc3ecfeef0daa95cfb03563780dd2d86818` |
| tarball SHA-256 / 大小 | `51661a82e0bc59d9f85637a1675ebbfba1ac5385f86259eaf173af829a86af22` / 162963 B |
| tarball integrity | `sha512-fBmQeE6jDNGpeCy44r9cGKewrJy6XzJI1CcuIdS+HuZYO5BY1TYWsSSE5i+G/InDCAG4WsP8A9uogZpcE4T8ZQ==` |
| 构建工具 | Node.js `24.14.1`、npm `11.11.0` |
| 安装核对 | Vue 与 React 两宿主均实际安装2.0.6，bundle SHA 一致；Vue新专项与可复现构建PASS，React6专项27单测/2Chrome/TypeScript及构建PASS。两前端2.0.5及2.0.6温和切换、真实登录错误呈现/编辑门禁PASS；真实模型ASK执行闭环已补证；07未运行计算，08独立四域计算及700帧本地产品/登录结果已PASS，完整八case门禁仍未完成。 |

权威锁：[wrf-ui.lock.json](../../webpage_sys/wrf-ui.lock.json)。发布 tarball：[2.0.6 包产物](../../webpage_sys/.artifacts/wrf-ui/2.0.6/zhihuiqixiang-wrf-ui-2.0.6.tgz)。包与宿主锁必须一起更新；后续内容变更使用新版本，不覆盖冻结产物。数值校验事件为同步原生 `validation-change:{valid,errors}`；`editorKey` 使用稳定 draft/task ID，同草稿轮询和预览保留内部非法文本缓冲，切换身份才清理。缓冲及界面逐域积云元数据不进入计算请求。

[2.0.6 运行错误呈现专项](../../.run/wrf-refactor/20261008T082008Z/wrf-ui-2.0.6-validation.json)，19248 B，文件 SHA `2ff37167c9885631c5f1e53d75ac50e834e53a00b917cdda57ebcdf671185c2e`。73单测PASS（新增5个纯呈现回归）、6次Chrome PASS（错误摘要/unknown/namelist/脱敏有界及两类不明提交各两轮）、主站生产构建及同锁可复现共享构建PASS。输出校验已识别签名显示事实摘要，只有严格 `runtime.remote_wrf_succeeded===true` 才表示计算已完成；未知错误不猜根因。原始堆栈仅在默认折叠的技术详情中显示，清控制字符、脱敏Bearer/password/token/API key/URL口令，并限制12000字符；不改历史DB。46个专用临时文件/22688638 B 已由TemporaryDirectory finally全部清理，release6仅保留8必要文件/762619 B与保护审计；历史5包及旧报告不覆盖、不删除。随后维护负责人已独立完成6真实登录错误呈现验证，见后文；该专项报告自身仍为隔离fixture证据。

[2.0.6 双宿主安装绑定](../../.run/wrf-refactor/20261008T082008Z/wrf-ui-2.0.6-dual-host-validation.json)，4987 B，文件SHA `09ddd3380021dfbaed96ed842ce0a727d876da93e3dff75ffb5dccb8512c40b1`，现场核对Vue/React实际安装6及同bundle，绑定两宿主锁、vendor与冻结专项审计。Agent [模型错误边界与React6专项](../../.run/wrf-refactor/agent-failure-boundary-ui206.json)，10336 B，文件SHA `1ec98fb39b2258839d233dfe146ebba81fb3d4a858329b97e68999f0ebfeb578`：模型仅接收固定failure_class、已识别字面evidence_signals和固定中文摘要，不接收原始task.error/failure.message/log；科学参数错误与恢复授权绑定保留。55项Python聚焦（含7新边界、5个Mock原生ASK）及另12项提交CAS/503、React27单测/3文件、1场景重复两轮Chrome、标准TypeScript与隔离构建393文件/15844548 B PASS。这是增量专项，不是139/96全量重跑；所有模型/业务HTTP/ASK均fixture，未验证真实8012会话、未重载后端。已安全化Agent任务的CE可能显示一般“结果处理失败”摘要，细类仍由宿主固定direct_answer说明；不伪造日志签名或runtime报告。临时输出与独立Redis进程均按fixture清理，旧4/5/503报告保留。该报告时点尚未覆盖真实登录；随后已完成6完整刷新和独立验证，不能沿用已注册5的CE实例。

智能体计划交互另有独立[逐项修改/授权顺序补证](../../.run/wrf-refactor/agent-plan-row-edit-regression.json)，83633 B，文件SHA `f249ab95b04648f4a009a7d8914c6d739208864b4f37d5ed298f0a2434726bf1`，12:18:46 UTC。仅修改 `WrfPlanSummary.tsx`、`WrfRenderer.tsx` 两生产文件并维护两份既有确认/对话测试；每行可访问“修改{项目}”按钮与底部修改配置共用同步ref/state锁，冻结历史未传onEdit时保持只读。异步原生拒绝完成前不读写草稿、不打开编辑器；匹配草稿失效完成后才打开，重复点击不重复拒绝，拒绝失败恢复可重试，草稿失效失败保持关闭。4文件41项相关React回归、`tsc -b`及隔离生产构建393文件/15845318 B PASS，包含恢复ASK与非WRF renderer相关回归；这是局部增量，未重跑139项Python或96项React全量，也不代表真实模型ASK通过。402个临时regular文件/15875895 B由TemporaryDirectory finally清理；只保留该保护审计。共用2.0.6 tgz、实际bundle、package.json、pnpm锁SHA前后一致，冻结前端退役prep未改；未重载8007/8012、未启动计算。

[2.0.5 工作台专项审计](../../.run/wrf-refactor/20261008T082008Z/wrf-ui-2.0.5-validation.json)，7462 B，文件 SHA `182b55ebac6a3ba788bf241e49411627fa3a24fddedced7e8944e5e429a8c340`，绑定 68 单测、9 个完整 Chrome 场景、8 次重点重复、隔离构建、同锁可复现构建和源码。首次 2.0.5 测试期间调整状态子路径造成的两项失败诊断单独保留，修正安装后的完整与重复回归均通过。Chrome 使用真实 CE 和真实后端投影算法，但所有业务 HTTP 均拦截，没有发起真实计算；随后 2.0.5 两前端温和切换及真实登录编辑门禁已完成，下文保留独立证据。

[2.0.5 双宿主安装及门禁绑定](../../.run/wrf-refactor/20261008T082008Z/wrf-ui-2.0.5-dual-host-validation.json)，3953 B，文件 SHA `989edec56c60958760e27af18336412b1dbbd3ce6f45403b8b7f0d64e34419f6`，现场读取两个 node_modules 版本/bundle，并绑定 Vue npm 锁、React pnpm 锁/vendor 和宿主源码。React 96 单测/14 文件、标准 `tsc -b`、隔离构建 393 文件/15842416 B、5 场景重复两轮共 10 Chrome PASS；React 构建临时目录由其 fixture 清理。报告明确 React 测试结果来源为该宿主负责人本轮实际执行记录，路由/模型/ASK 均 fixture，未访问真实 8012，不能据此升级真实模型 ASK 门禁。

历史 2.0.4 已双端核对相同 bundle，源码 SHA `bcaa32cc38355e0f6213a0bed71dd0b626dbbcb7ef6a2a7b55b70f286c02e41a`，bundle SHA `612760a90336ac2fbfd624f8849ae5f97c563023c8430cad696219caf3d234c3`；[工作台回归](../../.run/wrf-refactor/20261008T082008Z/wrf-ui-2.0.4-validation.json) 保留其 62 单测/7 Chrome/3 次积云重点重复的时点记录。2.0.3 源码 SHA `3f33ce2f8ccf9f40c67fabc2df2e60521ea5ea47c352c5413cd86e37084b46b4`，bundle SHA `fb218bc6fd126f8bd53e2c2b00e4f703572747bc4643c935a08286181bbc5122`，对应历史 56 单测/5 Chrome 及真实维护刷新记录。

5177 曾继续服务内存缓存的 2.0.0；维护负责人核验 PID 身份后仅 SIGTERM 温和刷新前端，实际 import 先读取 2.0.2、再读取 2.0.3，其他服务身份不变。[2.0.3 刷新审计](../../.run/wrf-refactor/20261008T082008Z/frontend-dev-refresh-2.0.3.json)、[几何修复刷新审计](../../.run/wrf-refactor/20261008T082008Z/frontend-dev-refresh.json) 保留时点证据。浏览器更新须完整刷新，不能沿用旧 Custom Elements 注册实例。

2.0.5 维护负责人已完成两个前端温和切换：[身份及切换审计](../../.run/wrf-refactor/20261008T082008Z/frontend-205-live-switch.json)，5426 B，文件 SHA `a8bb88c642634cd9cc885fa95c19bb868d15e0b46490e65d16ba9b2bf9bf2dd0`，phase=complete，主站 PID2875、AgentUI PID2950，其余六个服务身份保持，未重载 API/Redis 或修改远端计算。真实 Chrome 登录编辑验证另有下述报告；切换记录本身不升级模型 ASK 或产品门禁。

2.0.5 真实登录 Chrome 编辑验收：[双宿主行为与截图绑定](../../.run/wrf-refactor/20261008T082008Z/wrf-ui-2.0.5-live-browser-validation.json)，2939 B，文件 SHA `f7f491cdecb9caf70019be9f848b533a8ff860662d3903d459a6ae675d1d7a6e`，2026-10-08T11:39:12Z。既有认证页通过语义 UI 操作验证：新建为空中国地图、无默认 D01；合法配置按钮可用，spin-up 清空、vertical501、域尺寸清空均保留原输入并禁止开始；D01 显式积云0与 D02 按网格距默认3并存，删除父树展示具体影响，撤销恢复域及显式值；真实运行卡与侧栏模拟进度均72.5%，历史 waiting_restart 中文。Agent 真实 iframe 的 spin-up 清空同时禁用外部保存/计划，恢复0后计划按钮重新可用。该次未生成计划、保存草稿或提交新计算；Agent center/provenance/CAS 新源码尚未在活跃队列期间重载，真实模型原生 ASK 和07计算仍未验收。

2.0.6真实维护与登录补证：[前端温和切换](../../.run/wrf-refactor/20261008T082008Z/frontend-206-live-switch.json)，5440 B，文件SHA `9612f72a10a4633aa0b03b8fe97a31059fe0c4563158aa91e85142c8a5bb751c`，phase=complete，frontend PID9619、AgentUI PID9699，其余六服务身份不变，未重载API/Redis或改远端计算。[真实Chrome行为及截图绑定](../../.run/wrf-refactor/20261008T082008Z/wrf-ui-2.0.6-live-browser-validation.json)，1457 B，文件SHA `c5ba934f9ac345f571fd9a70aa73b3d725e6718af717ffb794936151ad133eb0`，12:07:19UTC：工作台同一EC-T任务显示“远端 WRF 计算已完成；输出校验失败，结果尚未通过完整性检查。”，技术详情默认closed、原task.error/attempt未改；Agent同既有会话spin-up清空同时阻断SAVE/PLAN，恢复0后PLAN可用。未执行save/plan/submit，后台center/provenance/错误模型边界补丁仍待API安全重载，真实模型原生ASK和07计算未验。报告绑定两张新增jpg119008/145477 B与切换记录，作为保护验收证据保留，不加入临时清理范围。

随后维护负责人在真实登录的同一GFS-T尝试核验WPS呈现：[2.0.6真实WPS进度补证](../../.run/wrf-refactor/20261008T082008Z/workbench-ui206-real-wps-progress-verification.json)，926 B，文件SHA `b9072280e6f89cdc0332458747e481c13f7bd0ebf8f1c7f290d98c5c6924b9d3`，明确无数值模拟百分比、无已观测模型时间，运行日志默认折叠；报告绑定已查看的76801 B截图。它只证明该次登录UI行为，不代表WPS、模型积分或产品门禁完成；同次尝试继续观察，不触发提交/恢复/维护。

### 08真实激活、原生ASK和复用配置补证

[activation v2执行审计](../../.run/wrf-refactor/20261008T082008Z/activation-preparation-v2/activation-audit.json)，19216 B，文件SHA `265ecc842b06e63f8fd2b705ac0076ce9f89010b920e01ca1a03ae8c0d0b4a44`，13:06:38 UTC phase=complete，plan digest `c861a253ad1f5953536d8b130e2b5a4ccd1c79f33bea3511c5dbf2f8df297a72`。8007/8012温和切换为PID19123/19149，其他服务及Redis身份不变；默认release08，五个原04任务仅应用已通过的08输出校验器绑定，原compute/attempt/request不改，activation自身recovery_started=false。旧GT04整行/配置及精确failed141会话证明前后保持，未自动恢复。[认证就绪与空闲检查](../../.run/wrf-refactor/20261008T082008Z/activation-preparation-v2/authenticated-readiness.json)，760 B，文件SHA `e53d039e23755209aeb2f3be7a3c0c84bc820befdc02af42ebab6440aad0f1f9`，13:07:08 UTC返回08指纹ready、issues为空；activity为0任务/0队列/downscale_active=false，这是切换时点快照，后续任务另记。

维护负责人已在实际登录Agent会话完成原生ASK拒绝→修改配置→输出周期60改30分钟→新计划→接受。界面原始证据：[修改前计划文字](../../.run/wrf-refactor/20261008T082008Z/agent-native-ask-plan-before-edit.txt) / [截图](../../.run/wrf-refactor/20261008T082008Z/agent-native-ask-plan-before-edit.jpg)，[修改阶段文字](../../.run/wrf-refactor/20261008T082008Z/agent-native-ask-modified-plan.txt) / [截图](../../.run/wrf-refactor/20261008T082008Z/agent-native-ask-modified-plan.jpg)，[接受后运行文字](../../.run/wrf-refactor/20261008T082008Z/agent-native-ask-accepted-run.txt) / [截图](../../.run/wrf-refactor/20261008T082008Z/agent-native-ask-accepted-run.jpg)。其中修改阶段文字快照本身是连接后的首页，不能单独证明30分钟新计划；授权顺序、草稿和执行身份由下述消息/持久化只读审计核验，六个UI原文件全文SHA另绑定于产物附件。

[真实原生ASK消息/草稿/计划/幂等提交证据](../../.run/wrf-refactor/20261008T082008Z/agent-native-ask-live-evidence-v1.json)，7623 B，文件SHA `ec6ef404a6d2499d15ce5ddae391b02e32d6be105ef67de748dc9249e16b1e82`，complete=true：旧60分钟计划 `wrf_8a816d417d4143dba41e8ad72b9f1970` 的两次调用均denied、无submitted claim、submission=not_found；草稿版本7→9，新30分钟计划 `wrf_c4048b2446a74752b0d9551635d6f2bd` 只有一次成功执行调用、一个持久化claim，并精确绑定任务 `wrf_gfs_20261006T000000Z_cd18efb7`。旧冻结计划未重写；拒绝后草稿变更使旧确认失效。该只读collector仅TYPE/GET/EXISTS和有界GET消息/查询，不持久化凭据或原始日志；“一次成功调用/一个reservation”不等于计数所有传输重试。

额外Agent08验收任务cd18efb7 / attempt `b4fad164f655493a95980d2d5d1096f6` 于13:16:50.800565 UTC succeeded，严格remote_wrf_succeeded=true、模型时间01UTC。正式00→01UTC、spin-up0、88层、27km/41×41、输出30分钟，闭区间00/00:30/01的3个原生文件共15407380 B；本机只读完整时序、必需变量、finite、冻结投影/parent/start/垂直/中心复验PASS。7显示变量×3时次共21张WebP，本地与发布副本每帧SHA均与meta一致、图片解码通过，catalog精确1个主记录/21assets，发布文件含meta共45804 B。[程序/输入/配置/native/产品/catalog/资源完整附件](../../.run/wrf-refactor/20261008T082008Z/agent-native-ask-08-product-annex-v1.json)，68785 B，文件SHA `e7c48507716a840a2d39854cabb5341ba562472e3641786ed28c97c73b686f32`；2输入/1028702732 B、plan digest `4c7b603b72d926559da690985f7876a0b616ab0ad38b0f59db62e2e959ab8875`，release08指纹和5程序SHA均冻结。资源既有sampler5文件manifest全SHA匹配，156样本/155.016 s，launch后12.07 s开始；PSS观测峰2126536704 B、RSS2296393728 B、CPU191.37 core-s、同样本allocated盘峰3345314816 B，partial_sample=true、managed wall估计166.351 s。此为额外Agent入口及30分钟输出验收，不能替代原矩阵两源S/D/T/N，该项后续聊天结果/鉴权查看补证如下；它仍不替代原矩阵。

维护负责人后续已在真实登录的Agent08结果卡点击下一帧由00→00:30、切换U10、播放到01:00/3of3并暂停：[有界DOM记录](../../.run/wrf-refactor/20261008T082008Z/agent-08-live-result-playback.txt)，1836 B，文件SHA `a13acf926dfd8142bce566dc15ed17fe87ff41aa462d7208e0f4ae974cf15da2`。三个实际认证入口（8012代理、8007私有版本资产、8002已登记目录）的21帧共63次GET均200、image/webp且SHA与本地一致；三个入口匿名均401、旧product_version为409。[认证HTTP完整证据v2](../../.run/wrf-refactor/20261008T082008Z/agent-08-product-authenticated-http-v2.json)，24764 B，文件SHA `f677786dd974819b2951e7d3814a01e7b2aacc8d2aa5e6bedfb50c7bf05019d2`，绑定上述产物附件SHA，未保存令牌/凭据/原始响应。首次[v1探针诊断](../../.run/wrf-refactor/20261008T082008Z/agent-08-product-authenticated-http-v1.json)因错误将relative path传给要求manifest完整URL的8007 read_frame而21次404；v2仅按源码合同修正探针，无产品或生产代码变更，v1保留历史诊断。

四项输出恢复授权另有真实[原生ASK/原attempt与配置不变的只读审计](../../.run/wrf-refactor/20261008T082008Z/agent-output-recovery-live-evidence-v1.json)，46422 B，文件SHA `e73aa8e695d11ada2cffe9f18ef680c2bd65494a6ea5310f3974c7a4385c88aa`：EC-D/GFS-D/EC-T/GFS-N各一次accepted/success、action=retry_outputs、计划changes为空；原attempt/process/compute04指纹、request/config字节和namelist digest均保持，校验target08。接受仅代表输出重试已入队，不能代替产品完成；不启动新WPS/WRF。该审计时点EC-N仅有asking调用、successful_execute_call_count=0，原failed状态未改；自动审批拒绝代点击原生ASK后，维护负责人已向用户请求授权。随后该ASK过期，维护负责人正常点击“暂不处理”，UI关闭且成员DENIED、无执行；用户授权仍未回复，后续须重新diagnosis→plan→原生ASK，绝不从其他四项接受推定该项授权，也不复用过期计划。

后续真实结果刷新暴露独立缺口：同一product_version选中00:30/2of3后点击“刷新状态”，时次回到00:00/1of3。[刷新前有界DOM](../../.run/wrf-refactor/20261008T082008Z/agent-result-same-version-refresh-before.txt)，1832 B、SHA `d9bcdf593e274206a8cd8c448baf1cb314fd885451abaa17a62a19c1440e61d2`；[刷新后有界DOM](../../.run/wrf-refactor/20261008T082008Z/agent-result-same-version-refresh-after.txt)，1834 B、SHA `cb6413958467f96243da50feb5c609fd8900fc37d831cbb58a0e624c6033a633`。React tracker每次新value对象触发CE重置/Blob释放是已定位的宿主问题；下述最小宿主修复及独立真实回归现已通过，共享6包、21帧内容及上述鉴权/播放证据不改，原缺陷快照继续保留。

同版本刷新修复补证：[React宿主增量回归](../../.run/wrf-refactor/20261008T082008Z/agent-ui2-summary-and-product-refresh-regression-v1.json)，5023 B、文件SHA `415df4b86f83bbd2c1092e40aa8fbd9e8488cf30b0ddf9b7d799e1b6ab7e3caf`：旧源码隔离复现D02→D01失败；修复仅memo稳定product与实际preferred字符串，并同步ui-2配置摘要文案。26项既有组件回归、标准tsc-b、隔离生产构建393文件/15845510 B PASS，覆盖域/变量/时次/Blob保持、不重取以及版本替换/卸载释放；668 regular /18417851 B与55专属links由TemporaryDirectory finally清理。共享2.0.6包/锁、10个Vue protected源及后台源码未改。维护负责人随后真实登录重新打开该会话，选择U10+00:30/2of3，刷新后checkedAt更新而U10/00:30/slider1/地图ready保持，ui-2新摘要已显示：[真实刷新/源与截图绑定](../../.run/wrf-refactor/20261008T082008Z/agent-result-refresh-fixed-live-validation-v1.json)，2370 B、文件SHA `a8725b97e63747cf1e6ab3922ec7825b36e401de335f8ee1a37d0402cc40c3b8`。网络不重取和Blob寿命由隔离真实CE回归证明，不能从登录截图推定；实际登录仅证明所选变量/时次和地图保持。

过期恢复指引最小补证：[本地两段prompt修复及既有26项回归](../../.run/wrf-refactor/20261008T082008Z/expired-recovery-guidance-validation-v1.json)，1991 B、文件SHA `c1e92e7792ff14d21d93fc3e02741fa0470ab5075c3566d24c7943d7202a1f00`；仅澄清未过期pending复用、过期后重新诊断/计划/ASK，不修改hard expiry执行门禁。middleware/工具相关12项与backend14项既有聚焦回归PASS，提示源码尚待最终安全维护加载；当前API未因该修改重载。EC-N原过期ASK的[只读身份/状态证据](../../.run/wrf-refactor/20261008T082008Z/agent-ec-n-pending-expiry-evidence-v1.json)绑定于此报告，拒绝本身不授予新恢复执行权限。

工作台真实“复用参数新建”先暴露Vue proxy structuredClone错误，随后发现旧request仅namelist.geogrid包含投影时会被默认投影覆盖；两项最小宿主修复及隔离回归均已冻结，未改共享6包。维护负责人已复测四域、Lambert30/30/116、spin-up6/88层/24小时/60分钟保留：[复用配置文字](../../.run/wrf-refactor/20261008T082008Z/workbench-gt08-reused-configuration.txt) / [截图](../../.run/wrf-refactor/20261008T082008Z/workbench-gt08-reused-configuration.jpg)。初始快照中，新GT08任务 `wrf_gfs_20261007T060000Z_1fe03ede` 为running、attempt `990514f8af4c47159b4e9cb98c520248`；SQLite新旧request全字段相等，旧GT04 failed141尝试原样保留。[新GT08初始程序/配置/输入/预期/catalog附件](../../.run/wrf-refactor/20261008T082008Z/matrix-GFS-T08-initial-expectations-annex-v1.json)，153713 B，文件SHA `3b90239a119f58a4cd5532ad9043fef3b162e3cc9f7c61708cce99f993cf2aa8`。31输入/16620234127 B、原namelist plan digest不变；该初始附件列出四域各25时次预期100native，7显示变量全部有效时条件预期700WebP，当时尚不能把预期写为已产出；最终产物门禁见下文。[30秒只读状态记录](../../.run/wrf-refactor/20261008T082008Z/matrix-GFS-T08-status-observations.jsonl)保存了阶段/模型时间并在终态自然exit0，不重复资源采样、不发提交/恢复/上传/下载。

GT08另按授权做一次同attempt有界只读SSH：[13:48:07 UTC WPS目录存在观测](../../.run/wrf-refactor/20261008T082008Z/gfs-t08-bounded-wps-presence-20261008T134807Z.json)，1457 B、文件SHA `e959772d4dcf9fd14966117501091fe8e0c2ca922b7aaaf6091f81c7536a4404`。PID21854/start_ticks89016644/boot ID核验一致、managed running、last stage=wps/running；独立wps目录136entries，当前四域met_em均0。只列该目录及读取有界state/stage元数据，不读正在写的大文件、不hash/递归共享目录、不发信号；文件存在数量不能判断完整性、停止推进或积分百分比。自有SSH已finally关闭，之后仅隔至少5分钟或阶段变化时复看；SQLite observer及root既有资源sampler不变。

间隔5分钟33秒后的[13:53:40 UTC有界复看](../../.run/wrf-refactor/20261008T082008Z/gfs-t08-bounded-wps-presence-20261008T135340Z.json)，1487 B、文件SHA `67dbb834041c3629561c6a0ab9bf1b7686f2e6717a61e878be330bd52e4bbc13`：同attempt/PID/start_ticks/boot均吻合，met_em由0增至21，D01已存在2026-10-07 00→20 UTC，其他三域尚0，最近mtime=13:53:34 UTC；stage仍WPS。此变化证明目录有新文件产生，但未读内容、未完成时序/数值/几何门禁，不能表示WPS完成或模拟进度；该次SSH也已结束。

[14:00:04 UTC有界复看](../../.run/wrf-refactor/20261008T082008Z/gfs-t08-bounded-wps-presence-20261008T140004Z.json)，1522 B、文件SHA `8fd9c684c5bdab4848ac7dc61b23b48438bf69a313766e0db124f3ad26cc85b1`：身份一致，met_em增至61，D01=31至2026-10-08 06 UTC、D02=30至05 UTC，D03/D04尚0；stage仍WPS，最新mtime=13:59:52 UTC。仍仅表示同尝试目录中持续产生文件，未验内容或完成；自有SSH已关闭。

[14:05:42 UTC有界复看](../../.run/wrf-refactor/20261008T082008Z/gfs-t08-bounded-wps-presence-20261008T140542Z.json)，1591 B、文件SHA `77781ab25d1ae6ce91551bf786923c24590ca54067e4dfbd7f6ac0cacd2ce546`：同一受管身份，met_em=98，D01–D03各31至2026-10-08 06 UTC、D04=5至2026-10-07 04 UTC，最近mtime=14:05:39 UTC。仍在WPS，仅文件存在持续推进，自有SSH已关闭。

14:09:49 UTC的既有SQLite只读observer首次记录`current_stage=wrf`、`current_stage_status=running`（后端updated=14:09:42 UTC），同`990514f8af4c47159b4e9cb98c520248`/release08。此时尚未观测Timing，模型时间/模拟比例仍null；进入WRF阶段不代表积分完成。后续停止WPS目录SSH复看，仅继续原30秒本地状态observer和维护负责人已运行的资源sampler。

维护负责人真实登录已看到GT08的WRF积分测量值：模拟进度4.6%、已观测模型时间2026-10-07 01:22:26 UTC，进度分母为含6小时spin-up的完整30小时；日志保持折叠。[实际DOM](../../.run/wrf-refactor/20261008T082008Z/workbench-gt08-live-wrf-progress.txt)，1321 B、SHA `270e3ce836d2380df0f250741a54edeee9447684a24366c661e3af55eaffca26`；[实际截图](../../.run/wrf-refactor/20261008T082008Z/workbench-gt08-live-wrf-progress.jpg)，78676 B、SHA `7f04cc8405e402e140b73c9a9ccef84a57cc7126dc445f49bfa0192f9cb19f42`，已独立查看。该快照仅证明真实运行进度展示，不预估剩余时间或证明最终输出通过。

既有30秒SQLite只读observer在14:18:49 UTC记录同attempt的已观测模型时间2026-10-07 05:00 UTC、`model_progress=16.67`，后端updated=14:18:36 UTC。该时点仍为WRF/running，尚未进行终态原生输出/产品完整门禁；不重复SSH或资源采样。

GT08最终于14:56:55.087274 UTC后端`succeeded/done`；同attempt/release08，严格`remote_wrf_succeeded=true`、模型时间2026-10-08 06 UTC/100%。后端输出报告100 valid、complete=true、invalid[]/missing{}；此报告每文件仅name/size/reason，不提供native逐文件SHA，本机run也没有另存artifact hash报告，未额外SSH扫描。已下载100native共498872755 B，本机现有08验证器完整时序/必需变量/finite/冻结投影/真实parent/start/垂直/中心复验通过，7显示变量各100时次均有效；700本地与700发布WebP逐帧解码及meta/local/published SHA一致，catalog精确1主记录/700 ready assets。

[GT08终态完整本地清单](../../.run/wrf-refactor/20261008T082008Z/matrix-GFS-T08-terminal-products-annex-v1.json)，1691659 B、文件SHA `54f4c06e5002fb13368ecd3137665161c7eb29694a14f7b942086c7a5fe1519b`，绑定每native/fullSHA、request/config/forcing/program、产品及84178资源manifest。首清单额外要求私有/发布meta全文相等，因正常发布URL重写被记为annex_complete=false；此为采集断言过严的诊断历史，不改生产资产。按实际publisher源码的两纯函数独立核验后，[发布合同补证v2](../../.run/wrf-refactor/20261008T082008Z/matrix-GFS-T08-publication-contract-supplement-v2.json)，2885 B、SHA `248b43da9d25949b7e974ca99b5fd62fca90e789a239402e871f48a5c24a2785`，全部13项本地门禁true：严格仅700帧URL改为已登记workbench路径并增加source_origin/source_task_id/catalog_meta_url三个字段；重写后发布meta全文与源码预期相等，catalog.file_hash匹配发布meta SHA。私有meta SHA `6617bbde83452b3aacb6ccaf75ffa265060e3a1a85e298526f9233b66a3c02e2`、发布meta SHA `383d411613e4d55eb54dc4a29693f3e9fdcc9c6f4b2a5f4dc66afb193c1c6ff7`分别保留。当前本地完整产品门禁通过；随后维护负责人另行完成下述两树抽样HTTP权限与内容验证，不以本地SHA替代真实HTTP。

维护负责人真实登录GT08结果：默认D01可见4域/7变量/25帧，切D04(9000m)+U10、下一帧到07 UTC、播放至13 UTC后暂停8/25。[实际DOM](../../.run/wrf-refactor/20261008T082008Z/workbench-gt08-four-domain-result-live.txt)，1685 B、SHA `1e64321383bd1422eae60cb2e88e82e4ff6fb7dc9e05fc8c6e15e1786a8df6e3`；[实际截图](../../.run/wrf-refactor/20261008T082008Z/workbench-gt08-four-domain-result-live.jpg)，254861 B、SHA `0c44d702f479d3f04f371cd17c52d17cf6331a54a5567ec0f10f1d60473c85fe`，已独立查看；无额外计算或模型恢复。

GT08资源仅绑定维护负责人既有84178 sampler自然结束的5文件/8247130 B，manifest SHA `a605eedd2ba43a6c2452e7d20e2ffc36923de2f33cc3ec02c429fcf5c7b45375`、summary SHA `d252f5404bd9ffe496ec5243d771b41db5e06fe1c3468b60006c31363aac14bb`，全文件SHA及task/attempt/release身份已匹配。5775样本/5775.941 s，launch后0.46 s开始；PSS观察峰4173832192 B、RSS4369666048 B、CPU12392.32 core-s、同样本allocated盘峰50450294784 B，partial_sample=true。managed wall估计5774.922 s，任务创建至产品终态6156.148798 s含排队/准备/产品阶段，均不称WRF-only或容量保证。

四个已授权的原04attempt输出重试现已全部终态：EC-D于14:57:22.228866 UTC succeeded，25native/175WebP；GFS-D于14:57:49.382161 UTC succeeded，25/175；EC-T于14:59:37.309359 UTC succeeded，100/700；GFS-N于14:59:44.320667 UTC succeeded，4/28。各runtime均complete=true、invalid0/missing{}，catalog_publication=success；原attempt、request/config和compute04指纹不变，使用已绑定08validator，未重跑模型。[四输出终态只读快照](../../.run/wrf-refactor/20261008T082008Z/matrix-output-recovery-terminal-snapshots-v1.json)，4374 B、SHA `1a30f5705d3f1e1ae1921ce72246b8712cfa46fb675734688ef9f17d024a5e1b`；[24行阶段观察](../../.run/wrf-refactor/20261008T082008Z/matrix-output-recovery-status-observations-v1.jsonl)，20485 B、SHA `60779ec42938c7c9ba87ed8e4b68f03dad76c8f66081f7cd3a1e60dc9ad69ed6`。GT observer85425与四输出observer3566均已自然exit0，无自有SSH/资源sampler/后台观察剩余。

维护负责人执行冻结collector得到[实际七case完整本地证据](../../.run/wrf-refactor/20261008T082008Z/matrix-local-final-evidence-seven-final-v1.json)，6456503 B、文件SHA `6f03175c5f27a3bb6cdeed2549c7c04883c9996c418c499af3df84f63b4e516a`：7个products_ready全部true，共260native/1820WebP，完整时序/变量/finite/冻结几何、每native及WebP全SHA、meta/发布/catalog、原attempt/config/程序及08validator绑定通过。该收集仅本地只读、无HTTP/SSH/Redis/写生产，`local_products_complete=true`而`matrix_complete=false`；第五EC-N仍缺本次人类授权与4native对应28帧产品闭环，该本地collector不包含HTTP；后续两树抽样HTTP已独立补证，最终8case新鲜行门禁仍未满足。没有生成8casegate，也未执行任何源码/数据删除。

EC-T真实登录结果已通过默认D01与四域/7变量/25帧，切D03(3000m)+V10、下一帧07 UTC、播放至13 UTC并暂停8/25。[实际DOM](../../.run/wrf-refactor/20261008T082008Z/workbench-ec-t-four-domain-result-live.txt)，1684 B、SHA `95ebe3f06937abe0b28be2048af9791e945e508a0c1d37a74f8791c2e1074382`；[实际截图](../../.run/wrf-refactor/20261008T082008Z/workbench-ec-t-four-domain-result-live.jpg)，318628 B、SHA `538eea2ac59bb395aaa70dc11d44f9f35d5196a382f3374ee3fadf87f65dd6c2`，已独立查看。两树登录交互与下述HTTP权限/版本/内容检查分别取证，不能相互替代。

两混合树的独立真实HTTP补证：[认证资产、版本与权限验证](../../.run/wrf-refactor/20261008T082008Z/mixed-tree-product-authenticated-http-v1.json)，18769 B、文件SHA `79ff98ba4b8c56792229fdefa5b002c404548a7f1c7add50f932d7d4a61d15ba`，15:02:31 UTC。每个任务四域U10的首末时次各8帧，共16张选定图片，经8012代理、8007私有版本资产、8002正式目录三个入口的48个Bearer GET全部200、image/webp且全文SHA与本地匹配；两个私有入口均private/no-store。三入口每任务各一次匿名请求合计6次均401；两任务旧product_version请求均409。报告绑定七case本地清单全文SHA，无重定向、不落凭据、不提交计划/计算/清理。此为明确选样的资产权限与合同验收，不声称对全部1400张混合树图片逐张发起HTTP；全帧SHA/解码/catalog由本地完整清单覆盖。

EC-N的本次交接状态：[新诊断/计划/原生ASK交接](../../.run/wrf-refactor/20261008T082008Z/agent-ec-n-fresh-native-ask-handoff-v1.json)，4807 B、文件SHA `9af874db0b98c4da99a97c133ab8a6894b641744a79bc58baede6429357897ca`，15:06:51 UTC快照；[实际待确认文字](../../.run/wrf-refactor/20261008T082008Z/agent-ec-n-fresh-native-ask-live.txt)，10676 B、SHA `eaf49d1c1f452c5393348342899ea875497a8c5613c5ea39773dde5dac95c9cb`；[实际待确认截图](../../.run/wrf-refactor/20261008T082008Z/agent-ec-n-fresh-native-ask-live.jpg)，119882 B、SHA `a900c8a1b345c42d746cfa072f1216aae7ae180251983399da291b3c469943ea`。由原成员新诊断生成`repair_35ef98afe8c443679963048d4384f50f`，动作retry_outputs、changes={}，绑定原04/attempt1/204eef7acd5244328b68ff976f944229与08validator；有效期至15:13:45 UTC。该快照human_authorization_received=false、execution_or_cleanup_performed=false，task仍failed，等待人类本次明确点击；旧过期计划已DENIED且不能复用。超过有效期须再次新diagnosis→plan→原生ASK，不能把待确认写为已执行。缺第八case时不执行退役协调器或删除任何源码/数据；已批准v4和额外审批范围均未因此扩大。

[宿主复用修复增量回归及两文件严格桥接](../../.run/wrf-refactor/20261008T082008Z/workbench-reuse-reactive-request-regression-v2.json)，24427 B，文件SHA `57e2e1cf587b4ae85f6d30a366f3476368d925cb3ea91bda98c0b169d9945c90`。3个实际Studio Chrome场景PASS，含transport/JSON503只读对账与旧namelist投影/混合树复用；Vue proxy错误先复现，宿主浅ref+toRaw快照及独立性回归通过，共享6安装完整性不变，隔离生产构建10文件/3700593 B PASS。34临时文件/22111395 B finally清理，专属Vite进程退出。保护的WrfStudio最终SHA `164456b9624a3e7d2bf309da1fc9c0e90d0a7e3cddb02ebbb12e46d8376075e3`，browser spec SHA `1c07438b962f393f7e3a02ac5b873b1b7083784132c597085d0bc97e12f2cddb`；旧final-source plan-v1保留历史，实际应用须重新冻结获授权桥接的v2，不能用旧protected SHA执行，删除scope不变。

### 智能体明确配置补充回归

实际登录会话曾出现用户明确给出中心 30/116、草稿仍缺 center 的问题。Agent 专项修复将明确几何通过后端 preview 规范化写入 ui-2 草稿，保留未接受的位置建议，取消 ui-2 对旧 center_confirmed 位的依赖，并为已有官方成员提供当前合同。报告记录 Agent 116 项中 112 PASS / 4 live skipped、原生 ASK 框架模拟 4 PASS、后端 27 PASS、React 90 PASS、真实 Chrome 配合 HTTP/模型/ASK fixture 6 PASS，以及 TypeScript/隔离构建通过。它们不等于真实登录模型最终 ASK 闭环通过；报告时尚未重载生产服务，后续真实加载与验收另记。首次投影预览返回 503 后，宿主提供“重新校验投影”并保持原中心；6 项 Chrome fixture 已覆盖重新校验到唯一最终计划。

[专项回归与源码绑定 v2](../../.run/wrf-refactor/agent-explicit-center-regression-v2.json)，4422 字节，文件 SHA-256 `82ea271287a01312f26fe2b9c2b79114d0ff03459e352d119986e6ac5d42cd78`；原 [v1 报告](../../.run/wrf-refactor/agent-explicit-center-regression.json) 保留历史记录。该专项时点共用组件仍为冻结的 2.0.3，没有改共享包、锁、冻结 dist 或旧清理索引。

智能体来源闭环已新增差异字段来源、接受状态、最终计划展示及拒绝不执行回归。[2.0.4 与来源专项冻结报告](../../.run/wrf-refactor/agent-provenance-regression-ui204.json)，6696 B，文件 SHA `ea6bc3d7a04599f0741b4f598bbcebb488e520e5ab65c8be8d540caab236cbac`：129 项 WRF 中 125 PASS / 4 live skipped、原生 ASK 框架模拟 5 PASS、React 94 PASS、真实 Chrome 配合 HTTP/模型/ASK fixture 8 PASS、实际 TypeScript 与隔离构建通过。模型 Mock 和 fixture 不等于真实模型 ASK；来源元数据不进入 request/digest，报告及旧文档冻结保留。

智能体 2.0.5 的 [最终来源/提交围栏/宿主回归](../../.run/wrf-refactor/agent-provenance-regression-ui205.json)，11968 B，文件 SHA `392736416276552a14e7432eed7a86cd8424f02505d28640830ceaa4fa883e03`，绑定 28 源码与锁/vendor/实际 bundle。139 项 WRF 中 135 PASS / 4 live skipped，另记录提交/编辑竞态 10 PASS（双向租约围栏、独立无 TCP Redis 执行真实 Lua）、原生 ASK MockModel 框架 5 PASS、React 96/Chrome fixture 10/标准 TypeScript/隔离构建 PASS。测试集合可能重叠，不相加为独立用例数；未访问生产 Redis、未执行真实 8012 模型会话，未重载任何服务。真实模型配置→最终 ASK→计算/拒绝闭环仍待维护窗口验收。

代理JSON503幂等补充：[工作台专项](../../.run/wrf-refactor/20261008T082008Z/workbench-json503-idempotency-regression.json)，21545 B，文件SHA `c6cd9d10d53980278cf7f501e1a608adf1667e37d6227729798b14dbfe47ddec`。只移除503的确定拒绝分支，保留原幂等键；首次POST已接收却返回503/断连时，再次开始仅GET对账、无第二POST。旧版本对照Chrome按预期失败；修复后两场景各3轮、共6Chrome PASS，68单测及隔离生产构建PASS。43临时文件/22874860 B finally全部清理；该报告为包5之后宿主补丁的时点证据，后续6构建不覆盖它。明确400/401/403/409/422拒绝行为保留。

[智能体JSON503专项](../../.run/wrf-refactor/agent-submission-503-regression.json)，2930 B，文件SHA `aafb1d268eb4fc6aedc740d4e83abea7ab979938e4f800992677babe4922f550`。503统一unknown、保留原plan/key/submitting，仅只读对账；27项相关增量回归PASS（Submission12、Runtime10、Mock原生ASK5），不是139全量重跑，也不是实际模型ASK。旧ui205审计与包5保持不变，报告时8012尚未加载本次Agent补丁。

### 部署与运行环境

| 字段 | 记录 |
| --- | --- |
| 首次通过完整协议的 release | `ordinary-20261008-03` |
| release fingerprint | `d5b4a71af190754e002d4a236d5f221f9377305dc5e94f594673bcf3c5c92677` |
| readiness | `ready`，8055/8055；报告时间 `2026-10-08T09:18:47.719260+00:00` |
| 部署 CLI（11 项回归时）源码 SHA-256 | `10c38e6ae1ee423277fffa395141546db2ce8be9d68bd6ae145427eff7d8231b` |
| 固定来源入口 CLI（18 项回归时点）源码 SHA-256 | `45e22a456f99e6707a187ebb650a69681db0db33790b18f83d82f3237802ed57` |
| 固定来源入口回归源码 SHA-256 | `169f07da4bc862229877e43e8412b7ff15846277e80a6e677fc12341b73c6e2f` |
| 原部署回归源码 SHA-256 | `d36c452dbb141f27b349bda6b4ff78499eda41833f9e4d95e5d5511aea08b26a` |
| 当前 API release | `ordinary-20261008-08`；activation v2于13:06 UTC complete，13:07认证readiness=ready；历史矩阵compute04不改 |
| release04 fingerprint | `160901b42cdc1424ee72c14f390f0a8d6bd5836ec061a3a81db44ca5d9115111` |
| release04 readiness | `ready`，8055/8055；`2026-10-08T09:43:40.846136+00:00` |
| 独立输出校验 release05 | 已安装 `ordinary-20261008-05`；readiness `ready`，8056/8056，`2026-10-08T10:23:19.352193+00:00`；不追溯改变 04 计算绑定。 |
| release05 fingerprint | `b47403f83361998efc35dd4a9cf67d64532c4495a4d0f94d7b01c52e99a63b64` |
| 独立薄发布 release06 | 已安装 `ordinary-20261008-06`；readiness `ready`，369/369，`2026-10-08T10:54:27.690566+00:00`；199 regular files、365436855 B、0 symlink；该报告时点服务器 current 指向 06；随后已更新为 07，计算 API 和矩阵仍为 04。 |
| release06 fingerprint | `0fcd88f0730aa6fefaf28aa3681020c5fa5fa371a799aae1f864beddf53eea04` |
| 独立强地理门禁 release07 | 已安装 `ordinary-20261008-07`；readiness `ready`，370/370，`2026-10-08T11:25:53.425860+00:00`；200 manifest-bound files / 365452274 B（含 manifest 为 201 / 365537794 B），0 symlink、0 writable。该报告时点current为07；随后08已安装，API/既有矩阵仍绑定04。 |
| release07 fingerprint | `9a04d6a3c1877ae68b0e91a847ecaee91d80d8de47c6e116a59ac5ca1dab67a8` |
| 大头部解析修复release08 | 已安装 `ordinary-20261008-08`，200 manifest-bound文件；readiness `ready`，370/370，`2026-10-08T12:58:14.737170+00:00`。五赋值改here-string；当前API默认08、五个04任务输出校验器绑定已apply；新08四域树running，额外Agent08真实ASK短程已succeeded，完整矩阵仍待完成。 |
| release08 fingerprint | `d4502f6b2af5f5b2979e7032c80891bfd38cd23a8e91bdcad0d2805e44206db4` |
| 本地维护状态 | 本机资产迁移与 8007/8012 温和切换已完成，备份记录/配置一致；真实工作台两源短程及额外Agent08原生ASK短程已通过对应层门禁，完整矩阵与回退演练仍待完成。 |

新增报告：[release05 readiness](../../.run/wrf-refactor/20261008T082008Z/isolated-release-05-readiness.json) 文件 SHA `ac1853c278b8d14d0b0838f66daa1e99182a483715fcb41b341577f6d2ff6589`；[release05 部署](../../.run/wrf-refactor/20261008T082008Z/isolated-release-05-deployment.json) 文件 SHA `1a3ad2d0c5aac2d410a2e8c6a9976bd5ffdd7bcc546bf85748cdc5f16b2eaebc`。

薄发布报告：[release06 readiness](../../.run/wrf-refactor/20261008T082008Z/isolated-release-06-readiness.json)，43820 B，文件 SHA `53c08f18127ebcebebaf583c196e70f32fd70ea9942789f6036e5c73d75a9b34`；[release06 部署](../../.run/wrf-refactor/20261008T082008Z/isolated-release-06-deployment.json)，169 B，文件 SHA `1df0036df27076ffdef086fd4cf480fc157662c4b325b5aca3c875bd972ee02b`。薄发布物化运行所需文件，完整源码查询绑定固定 04 anchor，anchor digest 未变；不把文件减少当作数值、吞吐或生产稳定性验收。

强地理门禁报告：[release07 readiness](../../.run/wrf-refactor/20261008T082008Z/isolated-release-07-readiness.json)，43306 B，文件 SHA `3ce01e6fb0e4ad257bbebfef462aa40b1c56554ba9d6be9ba0729215bbf494ab`；[release07 部署](../../.run/wrf-refactor/20261008T082008Z/isolated-release-07-deployment.json)，168 B，文件 SHA `015697281dab5014d47d9e773efe43f8d3f5302e7cf3a57a1b0c03bb9d7aeb73`。地理合同覆盖 19 mandatory、38 optional 声明、3450 tiles / 16640082432 B，按实际 default 优先级、全球 regular_ll 覆盖、tile 名字/精确字节与 index SHA 核验；optional 缺失不当作 mandatory。helper 不读取 tile payload，不能据此声称逐 tile 内容 SHA 或地理数值质量通过。[地理开发门禁](../../.run/wrf-refactor/20261008T082008Z/geography-validation-development-evidence.json) 2169 B、文件 SHA `ad6244905b1a7d2202dd60d53d32b071ee0f501c34e0ab7dec83073f057c1a91` 记录 61 聚焦测试；[独立只读资料核验](../../.run/wrf-refactor/20261008T082008Z/geography-helper-readonly-06-v3.json) 28397 B、文件 SHA `3ce576b8056671d57c4fb87db62986c2ee1ef30abbd229339732e1caf7df0fb9` 未执行 geogrid。真实 07 短程/ASK 尚未运行，04/06 保留同指纹。

release08部署与门禁：[安装记录](../../.run/wrf-refactor/20261008T082008Z/isolated-release-08-deployment.json)，169 B，文件SHA `e1169cebb06f25e282a1123fd7d9c63d9fed6e13d47261c12c08b50a53339fc6`；[完整readiness](../../.run/wrf-refactor/20261008T082008Z/isolated-release-08-readiness.json)，43307 B，文件SHA `fee4cc870104c17ed7b8208c98e07141469ab88288783fd5fb70fd220a2724ab`。该安装报告只证明当时370项就绪检查；后续API切换、五输出绑定和真实08任务均采用下述独立证据，不能从安装报告推导。

受管进程 Linux toy 的 [v2 生命周期审计](../../.run/wrf-refactor/20261008T082008Z/managed-process-linux-toy-audit-v2.json)，9207 B，文件 SHA `c4437d46279ab95d64bd1ef484e4115ec385f0b125862a5868cd49bc9a91c18b`：独立 06 只读程序、专属临时资源根，核验同 token 一次、第二根租约拒绝、独立重连、篡改 fixture start_ticks 后 unknown/取消拒绝、恢复真实身份后的 cancel_pending→cancelled 和全 session 退出。14 临时文件 / 2584 B 已按 fixture finally 清理，4 个精确身份无 live/zombie；无生产资源、MPI 或真实 WRF。清理代码对存在 claim 而缺进程身份的未知状态拒绝删除，只有已核验的 cancelled_before_launch tombstone 例外；该 toy 不替代真实 WRF/MPI 取消验收。

原始报告：[release04 readiness](../../.run/wrf-refactor/20261008T082008Z/isolated-release-04-readiness.json)、[release04 部署](../../.run/wrf-refactor/20261008T082008Z/isolated-release-04-deployment.json)、[release03 readiness](../../.run/wrf-refactor/20261008T082008Z/isolated-release-03-readiness.json)。报告位于本机私有审计目录，可能不随 Git 分发；链接缺失时应读取实际保留的审计副本，不能凭本文重建通过记录。

本地迁移证据：[SQLite 备份、记录/配置摘要与旧资产映射](../../.run/wrf-refactor/20261008T082008Z/local-runtime-assets-migration.json)，状态为 `complete`；服务切换和其他进程身份保持不变由本轮维护操作记录确认。工作台两源短程已另行验收，后续完整矩阵与智能体仍须实际登录及计算证据。

`ordinary-20261008-01`、`ordinary-20261008-02` 仅为历史成功安装记录。新增当前门禁后，它们尚未通过当前完整协议，不能称为“已验证可用回退版”。保留它们有助于核查与维护，但选择回退目标仍须核验协议、配置、数据兼容性并实际演练。release03/04/05/06/07/08 及其运行环境、Python、地理资料和指纹清单均属于保护项；旧版本是否符合之后新增的协议须另验，不根据历史 readiness 推定。

固定来源入口状态：已验收版本 CLI 的 `--anchor-source` 默认只读；维护负责人已执行 apply，审计返回 `status=anchored`、退出码 0，实际 04 文件 SHA、88 个依赖、官方版本及只读权限已核验。入口固定绑定 04 的封口源码树，物理源码复制字节数为 0，current link 未改；anchor digest 为 `0e826277d5054d64437550e325159471643c901acb3b39b949cf01dff29e80f6`。[apply 审计](../../.run/wrf-refactor/20261008T082008Z/source-anchor-apply.json) 文件 SHA-256 为 `6d54384d0ee4df0422cf5fc955daf53a0af246d1266a427fede73d6ce4f57689`。入口完成不意味着旧环境可以直接删除。

首次发布和 `--resume-staging` 使用同一完整验证器。仅在文件完整集合、依赖 SHA、Python 包集合与模块导入、构建 snapshot、程序/CLI 的 ldd、运行表和地理资料检查全部通过后，才写入 `validation.complete=true`。标记绑定内容 SHA，总 fingerprint 绑定含标记的 manifest；恢复时重新核验并封口，未完成门禁的旧暂存目录不能直接恢复激活。远端事务明确使用配置的专用 Python，不依赖共享 shell 的系统 Python。

## 3. 真实输入证据与范围

已完成的两源短程使用 `2026100600` 周期；以下是短程隔离副本的逐文件记录。24 小时候选输入已另按实际资料池状态准备，见后文。源资料保留，副本不是可随测试产物清理的垃圾。

| 来源 | 文件 | 字节 | SHA-256 |
| --- | --- | ---: | --- |
| GFS | `gfs.t00z.pgrb2.0p25.f000` | 500132137 | `ef078ed0922259726055252371c2ac2bb1dafcb4424a3178eef5225c0393f6fb` |
| GFS | `gfs.t00z.pgrb2.0p25.f001` | 528570595 | `c0e4b7f507945463d598db10c7024e610c89300fe19d26eaff3d0b76f68b47bf` |
| ECMWF | `ecmwf.t00z.ifs.0p25.f000.grib2` | 136645456 | `4f020db07ff6b0a1cef3f96a8363e2f1bc958c20b74e2f8899dddb6e214cd630` |
| ECMWF | `ecmwf.t00z.ifs.0p25.f003.grib2` | 143756129 | `56bfc2dee9f4253894755cf4c9922b2a8098b831f52d1929579867a7e79198ac` |
| 合计 | 4 文件 | 1309104317 | 逐文件匹配；不以总大小代替 SHA |

| f000 语义检查 | GRIB messages | 共同压力层（操作核验） | 共同土壤层 | 结果 |
| --- | ---: | ---: | ---: | --- |
| GFS | 696 | 41 | 4，按实际米制层界解码 | PASS，issues 为空 |
| ECMWF | 187 | 14 | 4，按实际 soil index 层界解码 | PASS，issues 为空 |

报告：[两源 f000 语义证据](../../.run/wrf-refactor/20261008T082008Z/real-forcing-semantic-probe-corrected.json)、[四个隔离输入副本及精确路径](../../.run/wrf-refactor/20261008T082008Z/short-acceptance-input-copies.json)。压力层数量采用本轮操作核验记录；JSON 列出压力层并记录共同必需场和模式顶门禁通过。本表保留最初仅核验 f000 的时点记录；实际两源短程运行前，f000/f001 与 f000/f003 的整套语义门禁均通过，完整逐文件结果已绑定下文短程证据。后续每个新任务仍须检查其全部实际使用文件。

短程实际任务的 input manifest、forcing.expected.tsv 和语义门禁已冻结于下文证据。后续边界缓冲、spin-up 和运行窗口仍以各任务 expected 清单为准；资料不完整即停止，不能用修改时长、精度或参数掩盖缺口。

首次真实失败记录：`wrf_gfs_20261006T000000Z_24f22fba`，未远端 launch，因此没有受管 attempt 或远端 PID。保存的 [task.request.json](../../.run/wrf-workbench/runs/wrf_gfs_20261006T000000Z_24f22fba/task.request.json) SHA-256 为 `576e6ec81c706ea73e9b07d1e78b93a27aae3b4b2c161f9c0c7a897e13ee984a`，中心仍为 35/104、网格为 100×79，spin-up=0；这证明旧 UI 没有把当时输入的 30/116、41×41 保存进请求。新 2.0.2 在隔离 Chrome 已通过同一失败序列及未失焦提交回归；真实复测另记新 task，不重写此失败请求。资料窗口错误已由后端独立修复，不能把它作为该次几何丢失的解释。

两源真实短程均已完成。GFS `wrf_gfs_20261006T000000Z_cacb399f`，attempt 1 / launch `121ecb463080426699414afa98fb8f9a`，于 `2026-10-08T09:55:10.681798Z` succeeded，2 个原生输出完整、14 张 WebP 登记；ECMWF `wrf_ecmwf_20261006T000000Z_c335fc6d`，attempt 1 / launch `4a492648405749b8b2e8ae639c2cfd87`，于 `2026-10-08T09:58:02.558168Z` succeeded，4 个原生输出完整、28 张 WebP 登记。两次实际 release 均为 04，完整 fingerprint 见下表。真实登录截图：[GFS 2/2 帧](../../.run/wrf-refactor/20261008T082008Z/gfs-short-real-result.jpg)、[ECMWF 4/4 帧](../../.run/wrf-refactor/20261008T082008Z/ecmwf-short-real-result.jpg)。

只读从 SQLite 当前任务、保存请求/配置、6 个本地原生输出及 42 张登记 WebP 和 meta 生成 [短程完整证据](../../.run/wrf-refactor/20261008T082008Z/short-acceptance-evidence.json)，文件 SHA-256 为 `6aa831358615209c48a12050fa3ca0279f641a110ec4cc48778c2df2e480025c`。该私有证据保留每个输入/原生输出/产品的 SHA、发布登记、语义/输出校验、计划摘要和截图摘要。manifest SHA 统一按 UTF-8 JSON `sort_keys=True, separators=(',', ':')` 生成；文件 SHA 与 canonical manifest SHA 区分。原生输出检查 `complete=true`，无 invalid/missing_times。GFS 短程未取得有效内存/CPU 采样，EC 短程已有部分生命周期资源观测，见资源表；不能据小网格短程推断资源上限或稳定性。

补充输出时序复验：发现 release04 对 history_end=0 的时序解读错误后，使用修正后的本机只读验证器重新检查这两个短程的 6 个既有原生文件，GFS 2/2、ECMWF 4/4 均 complete=true，invalid 为空且无缺时次。[复验报告及验证器源码 SHA](../../.run/wrf-refactor/20261008T082008Z/short-output-schedule-revalidation.json) 文件 SHA-256 为 `4f5f5f4a43070c10bf69764fee6836315d41afa75bf5d62d397345d243f7ac6c`。本次仅读取现有文件，不重跑计算、不改任务数据库；修正源码不追溯改变原 release04 指纹，历史运行报告继续保留。

24 小时候选输入范围已经明确：

| 数据源 | 实际周期 / forecast hours | 精确数量 / 逻辑字节 | 清单 / 摘要与限制 |
| --- | --- | ---: | --- |
| GFS | `2026100700`，f000–f030，步长 1 小时 | 31 文件 / 16620234127 | [副本清单](../../.run/wrf-refactor/20261008T082008Z/full-gfs-acceptance-input-copies.json)，manifest SHA `7413c32726b0dca4d09eb45a7d8ebe8fd19215052140ace3155647a87f751e71`；源未修改。原 06 周期在权威资料池已按既有生命周期过期，证据为 [周期过期记录](../../.run/wrf-refactor/20261008T082008Z/authoritative-gfs-cycle-expiry.json)，不能凭旧 source 路径继续复制 06 全时序。 |
| ECMWF | `2026100600`，f000–f030，步长 3 小时 | 11 文件 / 1587521777 | [副本清单](../../.run/wrf-refactor/20261008T082008Z/full-ecmwf-acceptance-input-copies.json)，after manifest SHA `1aa81b2e12d8332b55741c7ca1baab37d5f7d2b98271119fcd482453a731b233`；源未修改。 |

以上为候选输入复制及首次04尝试时点证据，不能以复制本身推断24小时计算或完整时序语义验收。各任务后续按实际周期、spin-up、expected文件及语义门禁冻结，06短程隔离副本与历史证据仍受保护。新阶段/模拟时间观察绑定各自task/attempt；七case最终状态与独立08替代混合树的产物证据见下列矩阵，不改写旧失败记录。

## 4. 真实 HPC 验收矩阵

下列为两数据源各四类用例的验收矩阵，状态随实际证据更新。运行前保存配置与输入 manifest，明确资源预算并固定 release；一个 task/attempt 的结果不能填入其他用例。短程先过门禁，再开展较长与较复杂用例；此表不自动授予运行或删除权限。

| 用例类别 | 设计与主要门禁 |
| --- | --- |
| S：真实短程单域 | GFS 1 小时、ECMWF 3 小时作为最小资料周期对齐的烟雾用例，显式 spin-up=0；冻结实际 D01/网格/输出周期。逐阶段检查 geogrid、ungrib、metgrid、real、wrf，实际使用的输入须完整，不能仅凭当前两个时次假定边界缓冲足够。该用例不评估预报技巧。 |
| D：24 小时单域 | 一套通用物理默认、默认 6 小时 spin-up / 88 垂直层，冻结周期与模拟时段，检查 spin-up 与产品窗口分离及完整输出。至少覆盖工作台直接幂等创建和智能体最终原生 ASK 两条真实入口；拒绝 ASK 不得创建计算。 |
| T：混合四域树 | 本轮实际拓扑为 D01→D02、D01→D04、D02→D03，比例采用服务允许的 3 或 5，兄弟域位置由后端预览验证；本轮实际四域树使用连续 3:1，5:1 不作为该真实用例已验结论；固定时段后验证实际 parent_id、网格距、父子覆盖、各域输出时序与渲染定位。不能用链式四域替代本项。 |
| N：非缺省设置 | 本轮按授权执行组合兼容短例：spin-up=3 小时、统一垂直层=60、FDDA 弱网格松弛，产品窗口 3 小时；实际 namelist 与输出须证明采用这些设置。此组合用例验证参数兼容与流程，不分离单因素因果或证明物理技巧改善；若研究参数效果，另做控制变量实验。 |

### 执行与指纹登记

`待填` 表示尚未运行，不能替换成默认值或猜测。release 列须填写该任务实际绑定的 release ID 和完整 fingerprint，不能只抄当前环境值。N 类每个子用例完成时增行，保留原记录。

| ID | 数据源 / 类别 | 状态 | task_id | attempt | input manifest SHA-256 | config / namelist SHA-256 | 实际 release ID / fingerprint | product manifest SHA-256 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| GFS-S | GFS / 短程单域 | PASS（短程） | `wrf_gfs_20261006T000000Z_cacb399f` | 1 / `121ecb463080426699414afa98fb8f9a` | `e216e57d091bf5c576f6f0f88c427ac321a916bf1e22a01cd030b5befcbef6f5` | plan `fd64aa210defefae442280244b06746f1413414e686d12f50f838df9f345bbac`；request/config/preview SHA 见完整证据 | 04 / `160901b42cdc1424ee72c14f390f0a8d6bd5836ec061a3a81db44ca5d9115111` | `a4c0403808885bcb5e1a5334b02e4b379537954ce460c0a21a18df8956cfecde` |
| EC-S | ECMWF / 短程单域 | PASS（短程） | `wrf_ecmwf_20261006T000000Z_c335fc6d` | 1 / `4a492648405749b8b2e8ae639c2cfd87` | `5171cd13e02ffe8e749faa1d02e6d220fdd0d05e879d0fefff1a875bafb00465` | plan `2d811ef4680f76df05bff81afe86a5285f911cc738d64ceceff3bfa252e080be`；request/config/preview SHA 见完整证据 | 04 / `160901b42cdc1424ee72c14f390f0a8d6bd5836ec061a3a81db44ca5d9115111` | `b783191a938f1b682fbe102847ad7e7d1a98d6be4810f564cb870de38dc7b395` |
| GFS-D | GFS / 24 小时单域 | PASS（原生ASK输出重试完成；04compute/08validator） | `wrf_gfs_20261007T060000Z_f8e52c77` | 1 / `512c44705afb458585973119a9e7b443` | `3dd323eaab0781d6353b24bf817c63026c1037cc0d6bafb07756e8542e0d2c77` | plan `21c8e70fc21719a8dab4ea349f2fb242995de2d164ab527d2ebca33383919ae2`；config `0485938e176328977d6d60c29500b348e73ec42e670c14b832816704bcc54328` | 04 / `160901b42cdc1424ee72c14f390f0a8d6bd5836ec061a3a81db44ca5d9115111` | 25native/175WebP/catalog；product_version `124b89326327d0cddd7b6eb5c6df84fb9c8d21a1aaa77ce3fc27751e82e3ceb5` |
| EC-D | ECMWF / 24 小时单域 | PASS（原生ASK输出重试完成；04compute/08validator） | `wrf_ecmwf_20261006T060000Z_3134eb5a` | 1 / `403cda75fba444f48aaa5ec6fbd2da92` | `51da0895c24c4094693566b1a009d125f689e32e2829e2046f363fed05c7c9c0` | plan `3958e35a8b94d9903e740048371cea7755676b90f8a5acb5a60137b73d911c4d`；config `1f3258817e793ba8f64b31deee10562c94dbc55e699602cd8cd53a7561c66592` | 04 / `160901b42cdc1424ee72c14f390f0a8d6bd5836ec061a3a81db44ca5d9115111` | 25native/175WebP/catalog；product_version `fd43dedc347591abc667c41a9d1772cb9e63548732806a69852a73dfa4ac8550` |
| GFS-T | GFS / 混合四域树 | WAITING_RESTART（本次尝试退出141） | `wrf_gfs_20261007T060000Z_70566ff1` | 1 / `1459e6a07e0e4bed8b5b3b632f1ea439` | `3dd323eaab0781d6353b24bf817c63026c1037cc0d6bafb07756e8542e0d2c77` | plan `0fe22798fa300b3de35b4aceddc0c4f0a9038d3274936c671263370173bcc680`；config `cd8e422e02c5c07135f5f46924d5ae7af8c1523fab391b0a9f66c2f1417fb5ef` | 04 / `160901b42cdc1424ee72c14f390f0a8d6bd5836ec061a3a81db44ca5d9115111` | 未生成；catalog精确任务记录/asset均0 |
| EC-T | ECMWF / 混合四域树 | PASS（原生ASK输出重试完成；04compute/08validator） | `wrf_ecmwf_20261006T060000Z_a6256269` | 1 / `4df58e7bd3544d46807fb32a3bc37d53` | `51da0895c24c4094693566b1a009d125f689e32e2829e2046f363fed05c7c9c0` | plan `7129a7b3746c74a896af55e6a8f927760e4c365358d42c501a269ec831071a6a`；config `f9ac0862afa09ceebf6949aa6fcb70e028c8b65aec484294ca9becb5f16e72a0` | 04 / `160901b42cdc1424ee72c14f390f0a8d6bd5836ec061a3a81db44ca5d9115111` | 100native/700WebP/catalog；product_version `8b125d9cc69d7754f369af63ad698ded1ac2a16a53410f93b2c904f4239c2f7f` |
| GFS-N | GFS / 非缺省组合短例 | PASS（原生ASK输出重试完成；04compute/08validator） | `wrf_gfs_20261007T030000Z_5c70db3a` | 1 / `f7d6a8bd69f245748c464d24a6c0b358` | `031a934b8aff56178df26dc22b9b7a43036fa50b6c10932298d2181a4ff65697` | plan `b463ef290fb75bbefe48c472a3af29a82f0f72d3bc180befb0e67b9697f9dc82`；config `3288a8fa1326f68f2c0ff7c1d0ff988a15309b6682b5ed50b9b3ad8c95dfc705` | 04 / `160901b42cdc1424ee72c14f390f0a8d6bd5836ec061a3a81db44ca5d9115111` | 4native/28WebP/catalog；product_version `286c5ff8cd60552a3c8f56c8524a88591ff7fd09063dc29e95cc6168e9c445a9` |
| EC-N | ECMWF / 非缺省组合短例 | FAILED（04历史终态）/ PARTIAL（08原生复验） | `wrf_ecmwf_20261006T030000Z_0d2e5ad3` | 1 / `204eef7acd5244328b68ff976f944229` | `173ab8c13597e024c47be1b6337767074ed88b049b05328fb62162ef6b5fc33e` | plan `6f8170bae73dfaa3cd35556939741c3b9dfa9f505e66698d7cdea13a5fff71cf`；config `17f5e1e3ac1ed74bb71d00c2d701ca587134023e2c04c8f0a70a46daff4d82ef` | 04 / `160901b42cdc1424ee72c14f390f0a8d6bd5836ec061a3a81db44ca5d9115111` | 未生成；catalog任务/asset均0 |
| GFS-T08 | GFS / 同配置混合四域树独立验收 | PASS（完整本地输出/发布/catalog/登录UI及抽样认证HTTP） | `wrf_gfs_20261007T060000Z_1fe03ede` | 1 / `990514f8af4c47159b4e9cb98c520248` | `76353d98952552eb7c0a16aefd5ac89b19614c9ea77c16a432c420177b5b6295`（本附件canonical字段schema） | plan `0fe22798fa300b3de35b4aceddc0c4f0a9038d3274936c671263370173bcc680`；request全字段与旧GT相等；文件SHA见初始附件 | 08 / `d4502f6b2af5f5b2979e7032c80891bfd38cd23a8e91bdcad0d2805e44206db4` | 100native/700WebP；发布清单manifest `ae60c78ed29fa8a9d3b5c35519cb76169358abd90c04ff41224afb823a5786a8`；完整本地SHA见v1清单/v2合同补证 |
| Agent-A08 | GFS / 额外真实Agent ASK、30分钟输出 | PASS（真实ASK/计算/完整输出/发布/切帧播放/认证资产）；同版本刷新保持已补真实PASS | `wrf_gfs_20261006T000000Z_cd18efb7` | 1 / `b4fad164f655493a95980d2d5d1096f6` | `1ec30ee0889f1e0fa3b66bfa8fac91ad0f21557cd6f586f6393ab43cb7b76c6d`（本附件canonical字段schema） | plan `4c7b603b72d926559da690985f7876a0b616ab0ad38b0f59db62e2e959ab8875`；完整文件SHA见附件 | 08 / `d4502f6b2af5f5b2979e7032c80891bfd38cd23a8e91bdcad0d2805e44206db4` | 3native/21WebP，逐文件SHA与catalog见附件；不能替代原矩阵 |

### 资源、输出与界面登记

峰值内存记录整个实际进程组的测量口径，不能把单个 rank 的 RSS 当作全任务峰值；磁盘区分目录绝对占用、已测基线及增量，未测基线不把 du 绝对值称为增量；wall time 记录起止时刻、秒数和阶段分解。无采样时填“未测”，不以准入预算代替实测。

资源门禁仍有明确缺测：[原始两源短程证据](../../.run/wrf-refactor/20261008T082008Z/short-acceptance-evidence.json)中，两源`peak_memory`和`peak_disk_increment`均为`not_measured`，不能填成完整峰值已验收。EC-S总wall为160.946469 s、GFS-S为180.217683 s，均包含资料准备、计算、下载、渲染及登记，不能称WRF-only耗时。下表EC-S另有部分生命周期采样，其他case采样也均为`partial_sample=true`的观察下界，未覆盖完整生命周期/共享forcing与release/本地渲染；它们不补齐原短程缺测，不构成容量保证或全矩阵完整峰值验收。此限制保留在工程验收范围内，原始证据不重写，未为补性能另建算例。

| ID | 峰值内存 / 口径 | 观测磁盘占用 / 卷 | wall time / 阶段 | 输出校验报告 SHA | 登录 UI / 产品证据 | 结论与限制 |
| --- | --- | --- | --- | --- | --- | --- |
| GFS-S | 未测 | 未测 | 180.217683 s，attempt 起止包括资料/计算/渲染/登记，非 WRF-only | `4c7d879a178830ff4f006b0085b30cab94cb7625d26e5d6bd58ea17b4fd7f835` | 实际登录工作台 2/2 帧；14 WebP 登记 | PASS（该短程工程与产品闭环）；不含 24h、数值技巧或智能体 ASK |
| EC-S | 全生命周期峰值未测；另有观测 PSS 下界 2154324992 B，75 样本/74.009 s | 基线和总增量峰未测；UUID 数据盘 attempt 的 WPS/WRF/logs 分目录异时 allocated 峰分别 1565885952/737769984/115712 B，不相加 | 160.946469 s，attempt 起止包括资料/计算/渲染/登记，非 WRF-only | `546da3048e490080e2e8be7da323567b2605ffe85a3bd883af71defdf886db9e` | 实际登录工作台 4/4 帧；28 WebP 登记 | PASS（该短程工程与产品闭环）；不含 24h、数值技巧或智能体 ASK |
| GFS-D | 观测 PSS 峰值下界 2389850112 B；2542 样本/2546.477 s，partial_sample | 同一样本 allocated 总峰 50547214336 B，非增量；不含 forcing 池、release、本地渲染 | attempt 3300.264158 s（准备至失败）；managed wall 估计 2545.705 s，非精确 rusage | 04原门禁失败；08只读完整报告 `9def6e101cd936a98cc47608f03a9a474e2ce6847cb787cf3d38143f2adbe669` | 本地25native/175WebP/catalog完整；七case只读证据已绑定 | 04失败历史保留；08输出重试产品闭环PASS；资源仅partial观察下界 |
| EC-D | 观测 PSS 峰值下界 2235916288 B；699 样本/698.077 s，非全生命周期峰值 | 基线和总增量峰未测；UUID 数据盘 attempt 的 WPS/WRF/logs 分目录异时 allocated 峰分别 10483001856/1499859968/410624 B，不相加 | 745.427735 s，attempt 起止，包括准备和失败门禁，非 WRF-only | 04原门禁失败；08只读完整报告 `cc7589b95564742a0330eb7ded631081ced871aeb4e3afd34dc3e934b812f167` | 本地25native/175WebP/catalog完整；七case只读证据已绑定 | 04失败历史保留；08输出重试产品闭环PASS；资源仅partial观察下界 |
| GFS-T | 观测PSS峰值下界1122813952 B；3018样本/3020.804 s，partial_sample | 同一样本allocated总峰50592442368 B，非增量；不含forcing池、release、本地渲染 | attempt9075.151237 s（含排队/准备）；managed wall估计3020.4 s，非rusage | 未进入输出验证；无有效报告 | 本地native0/WebP0；远端wrfout0/rsl_error0，未进入real；catalog均0 | 本次尝试失败、waiting_restart；无Timing，不将残留wrf/running阶段当作模型完成 |
| EC-T | 观测PSS峰值下界3966013440 B；2856样本/2855.351 s，partial_sample | 同一样本allocated总峰10622498816 B，非增量；不含forcing池、release、本地渲染 | attempt6136.581435 s（准备至失败）；managed wall估计2855.547 s，非rusage | 04原门禁失败；08只读完整报告 `9a4c21b732187e598e9fd35a0836a5ace3bcd4044f4b06f66466fd7e9bf6f979` | 本地100native/700WebP/catalog完整；七case只读证据已绑定 | 04失败历史保留；08输出重试产品闭环PASS；资源仅partial观察下界 |
| GFS-N | 观测PSS峰值下界2108254208 B；469样本/468.049 s，partial_sample | 同一样本allocated总峰11618043904 B，非增量；不含forcing/release/本地渲染 | attempt9526.145988 s（含排队/准备）；managed估计467.852 s | 04原门禁失败；08只读完整报告 `02733ce0a3298d96a4fc0f6dfe2e5affadf95569e1db65372eab73975ce4d486` | 本地4native/28WebP/catalog完整；七case只读证据已绑定 | 04失败历史保留；08输出重试产品闭环PASS；资源仅partial观察下界 |
| EC-N | 观测PSS峰值下界2084187136 B；171样本/170.018 s，partial_sample | 同一样本allocated总峰2780403712 B，非增量；不含forcing/release/本地渲染 | attempt9698.733647 s（含排队/准备）；managed估计170.836 s | 04原门禁失败；08只读完整报告 `2053f432ad7659e89694dfd1a238bbef187a79172fa3356ebdb2d331ce5befdc` | 远端4/4原生完整；尚无本地下载/WebP/登记 | 04失败历史证据保留；08原生层PASS且校验器已绑定；尚未授权输出重试，原生复验不等于28帧产品闭环 |
| GFS-T08 | 观测PSS下界4173832192 B；5775样本/5775.941 s，partial_sample | 同样本allocated总峰50450294784 B，非增量；不含forcing/release/本地渲染 | managed估计5774.922 s；创建至产品终态6156.148798 s，含排队/准备，非WRF-only | 本机完整报告canonical SHA `c9b68f01325db49583480030b25db56bff601a61e8fa1fd945971d9e8141fdae` | 100native/700WebP/catalog700，真实D04/U10/播放及抽样认证HTTP PASS | 本地工程与产品门禁PASS；资源仅观测下界，不能替代容量或数值技巧结论 |

短程资源附证：[已核验 SHA 的资源绑定](../../.run/wrf-refactor/20261008T082008Z/short-resource-evidence-annex.json)，文件 SHA-256 `20f2790af31b92a837fbeab5c872a69108c2d77f2cb420fa66811beb83839ff9`。EC 短程观测 PSS 峰值下界 2154324992 B、RSS 2315177984 B、观测 CPU 增量 222.9 core-s；仅覆盖启动后 74.009 s。GFS 短程只有终态 1 个样本，内存/CPU 仍记未测；该样本的目录占用不能当峰值。采样绑定 SID/PID/start_ticks/boot ID 与 release；峰值均为 partial_sample，遗漏采样前准备、forcing 池、封口 release 与本地渲染，不以各目录异时最大值之和冒充总峰。

历史04首次尝试只读观察：[状态变化记录](../../.run/wrf-refactor/20261008T082008Z/matrix-status-observations.jsonl)。EC-D 真实观测 WPS→WRF→积分结束，最终模型时间 2026-10-07T06:00:00Z / 100%，分母为含 6 小时 spin-up 的 30 小时积分；顶层 68 仅流程值，UI 不把它作为模型进度。后端于 10:16:14.528489 UTC 记录 FAILED，输出校验抛出 empty_output_schedule:d01，未下载原生文件、渲染或登记产品。GFS-D 于 10:19 UTC 已绑定 launch，10:20 UTC 为 WPS/running；10:56:08 UTC 已进入 WRF/running，随后有真实 Timing，11:00:52 UTC 积分结束后被同一旧输出门禁记为 FAILED。EC-T 于 11:01 UTC launch，11:02 UTC 为 WPS/running，11:09 UTC 后进入 WRF；11:49:13.432118 UTC 模型推进到2026-10-07T06:00:00Z /100%，随后旧输出门禁FAILED。GFS-T随后绑定1459e6a07e0e4bed8b5b3b632f1ea439，11:53UTC为WPS；12:41:44.780196UTC远端退出141，业务状态waiting_restart/stagefailed，未观测Timing。日志最后包含切入wrf/running与metgrid层数34，不能据此认为模型已积分。GFS-N随后于12:43UTC以f7d6a8bd69f245748c464d24a6c0b358进入WPS，12:49进入WRF、12:50模型07日06 UTC/100%，后端于12:50:22.672852UTC被旧04输出门禁记为FAILED。EC-N以204eef7acd5244328b68ff976f944229于12:51进入WPS，12:53进入WRF并推进到06日06 UTC/100%，于12:53:38.370717UTC同样FAILED。两N的runtime.remote_wrf_succeeded为严格true，表示计算已结束；observer自然退出，无提交/恢复/维护。资源采样由独立只读观察器记录。

EC-D 失败证据：[任务/配置/输入及失败冻结](../../.run/wrf-refactor/20261008T082008Z/matrix-EC-D-wrf_ecmwf_20261006T060000Z_3134eb5a-attempt1.json)，文件 SHA-256 `b26513b390dcd11ee80e64ba796afad9dee0eb0d4d03c9f3588919e6354cb8f7`；11 文件输入 manifest 完整，语义报告 SHA `6c1fdb1508cdb276af0351fd64827852e4c8ede3c48715bdeb00deea2e7e2376`。实际配置 history_begin=360 分钟、history_end=0，该任务绑定的 release04 输出验证器将后者按 model_start+0 解读，导致计划时序为空；这是已复现的门禁计算错误，不据此假定原生输出完整。该失败不自动恢复、不覆盖旧尝试。

EC-D 原生输出补充验收：维护侧使用独立 release05 校验器，只读核验原 attempt 的 25 个远端 wrfout，总逻辑大小 129164313 B，覆盖 2026-10-06 06:00 至 2026-10-07 06:00 UTC 的 25 个整点；全时次、必需变量、有限数值及几何检查通过，invalid/missing_times 为空。[完整逐文件 SHA、任务/计算与校验 release 绑定及报告](../../.run/wrf-refactor/20261008T082008Z/output-validator-ecmwf24h-plan.json)，9008 B，文件 SHA `22ea534c5c29018014f215d5374dfcf4fdc5fcbbd535c777e55dfeea3d57d40f`，output inventory digest `bbaf99b8cd638e4bd4ca96ed2a551c66cb111c3433baa0377a34a8d89dc5cb20`。原计算仍是 04/原 request/原 attempt；此只读计划未绑定新校验器、未恢复或重算、未渲染登记，不能把业务任务或完整产品行改为 PASS。

新增 06 原生复验：[完整只读计划](../../.run/wrf-refactor/20261008T082008Z/output-validator-ecmwf24h-06-plan.json)，9008 B，文件 SHA `671173b011514b935afc01c7c407648f5cd5862f54902852218c9fb0567d6577`，计划 digest `89ce89e08033017c21f36f8d07fbebf116852fbab9e37cc3f086ca3fda377658`。同一 attempt、25 native / 129164313 B 再次 complete=true、invalid=[]、missing_times={}，本次还包含冻结投影、真实 parent/start、垂直层及根中心对照。compute 仍为 04，validation 为 06；没有绑定、重试、渲染或登记，历史 FAILED 保留。

输出时序修复按封口 WRF 4.7.1 源码的 history_end=0 规则统一规划、渲染和验证；[修复与回归证据](../../.run/wrf-refactor/20261008T082008Z/output-schedule-fix-evidence.json) 文件 SHA `f120b17db050584aaddf045ee5e5be663d0650ad46ca1a10f99fa22a4c21b683`，分别记录 52 项和 61 项聚焦回归 PASS（两组可能重叠，不相加为独立用例数），记录时 live_binding_applied=false、live_recovery_executed=false。

EC-D 资源附证：[已核验逐文件 SHA 的资源绑定](../../.run/wrf-refactor/20261008T082008Z/matrix-EC-D-resource-annex-attempt1.json)，文件 SHA-256 `ad047294d3178fb70459f55047290b4638d7fbb318a5cadab8a338b7c8935f87`；PSS 2235916288 B、RSS 2397716480 B、观测 CPU 增量 1433.38 core-s，均为 partial_sample。采样记录的受管脚本 succeeded 只表示远端脚本退出，最终业务状态以输出门禁后的 FAILED 为准。

GFS-D 失败及资源附证：[任务/配置/输入冻结](../../.run/wrf-refactor/20261008T082008Z/matrix-GFS-D-wrf_gfs_20261007T060000Z_f8e52c77-attempt1.json)，文件 SHA `cd09f41f0323d1934c86881b8221713d4b49c8de965d01dc32011566a5a1cd9d`；31 文件输入 manifest 完整，语义报告 SHA `836b8b43d20a10c82cb052d29f2f5dd8972337cc1312b366b9b4450a0b8f7995`。[资源绑定](../../.run/wrf-refactor/20261008T082008Z/matrix-GFS-D-resource-annex-attempt1.json)，4325 B，文件 SHA `f4cfd9a9c4cda5f109759683ef3ee5ac4f8aad0f2c7295fbb003ca0c2d99064e`。2542 样本覆盖 2546.477 s，launch 后 0.98 s 开始；观测 PSS 峰 2389850112 B、RSS 2552979456 B、CPU 增量 2356.93 core-s、同一样本 allocated 总峰 50547214336 B，全部为 partial_sample 下界。attempt wall 3300.264158 s 包含准备/复制至失败门禁；managed wall 2545.705 s 仅估计。积分结束和脚本退出不替代原生完整性、下载、渲染或登记。

EC-T终态与原生补证：[任务/配置/输入冻结](../../.run/wrf-refactor/20261008T082008Z/matrix-EC-T-wrf_ecmwf_20261006T060000Z_a6256269-attempt1.json)，文件SHA `509204016fba2cb646a99792d55f561d760afdf3c19038f79d4a9e1c0a30c74f`。04业务终态FAILED，原因为旧empty_output_schedule:d01，尚无产品登记。[独立06完整原生复验计划](../../.run/wrf-refactor/20261008T082008Z/output-validator-ecmwf-mixed24h-06-plan.json)，30284 B，文件SHA `dca3d4539f39db901a9001ebacb9765bc858951adc025175797bc714ac9b2071`、digest `765782c2635ca18b15765527200b38d7abf9929f7c08de504243a847ff7c32db`：同一04 attempt的100 native /498057176 B，四域各25整点时次，必需变量、finite、冻结投影/拓扑/层数/中心全部通过，complete=true、invalid=[]、missing_times={}、blockers=[]。只读复验不绑定新校验器、不恢复、不渲染登记；历史FAILED保持。

EC-T资源补证：[逐文件SHA绑定](../../.run/wrf-refactor/20261008T082008Z/matrix-EC-T-resource-annex-attempt1.json)，4407 B，文件SHA `d5630fed4ff53352e020585c5150aab917b11e9c97d4f38e9abf6fcae4ddb095`，核对5个资源文件/4820240 B及attempt/release身份。2856样本覆盖2855.351 s，launch后1.04 s起采；观测PSS3966013440 B、RSS4155248640 B、CPU增量9878.91 core-s，同一样本allocated总峰10622498816 B，全部为partial_sample观测下界。远端脚本succeeded只表示该受管计算退出，不能替代最终输出/产品门禁。

GFS-T失败尝试及预期对照：[本次尝试冻结](../../.run/wrf-refactor/20261008T082008Z/matrix-GFS-T-wrf_gfs_20261007T060000Z_70566ff1-attempt1.json)，117222 B，文件SHA `8890fe95c7a3067ab0c4e4191d21c0307672c384ac866b2ee3c47b550ebf612c`；31个输入/16620234127 B，完整manifest及语义报告保持。另有[程序指纹/输出预期/catalog只读附件](../../.run/wrf-refactor/20261008T082008Z/matrix-GFS-T-final-expectations-annex-attempt1.json)，45328 B，文件SHA `ac7571604ff62baf137ce1467f7343286cc58d5bb4d6b8d9a7b6f32cc41ec6c7`，绑定冻结配置中的5二进制SHA、source_hash、计划和preview SHA，以及request/config/forcing.expected的实际文件SHA。按冻结namelist及WRF history_end=0语义，正式24小时各域25时次、frames_per_outfile=1，列出100个预期native文件名；仅当7个显示变量各时次齐全时预期700个WebP，PBLH等显示变量是否存在须由实际输出证明，不能把条件预期当作已产出。当前本地native收集为0、WebP为0；未由此附件额外扫描远端native，不能单凭本地0断言远端不存在。业务catalog以SQLite mode=ro单事务、精确file_uuid及task_id核验public_info/wrf_info均0，同库仍有两短程14/28条产品登记。退出141的根因尚未在此记录中证实；保留原attempt，不重放或恢复。

GFS-T资源附证：[逐文件SHA绑定](../../.run/wrf-refactor/20261008T082008Z/matrix-GFS-T-resource-annex-attempt1.json)，4506 B，文件SHA `e1ce2c89aa5406c2feb0ae85711053c779191d92e13279dcfc56ac3d5c761211`，复核5个资源文件/3291851 B及同attempt/release身份。3018样本/3020.804 s，launch后1.27 s开始；PSS观测峰1122813952 B、RSS1138593792 B、CPU增量1588.07 core-s，同一样本allocated总峰50592442368 B，全部partial_sample观测下界。末样本managed状态failed，残留stage-record wrf/running不代表模型完成；managed wall估计3020.4 s，业务attempt从创建准备至停止9075.151237 s含排队，两者口径不同。

GFS-N最终附证：[本次尝试冻结](../../.run/wrf-refactor/20261008T082008Z/matrix-GFS-N-wrf_gfs_20261007T030000Z_5c70db3a-attempt1.json)，27439 B，文件SHA `8264a70b295b0b4bfc1e571bb55ef35ab235866998622dabddbae69043fa1b9f`；[程序/配置/预期/catalog附件](../../.run/wrf-refactor/20261008T082008Z/matrix-GFS-N-final-expectations-annex-attempt1.json)，7661 B，文件SHA `b87a78cfab2de9656f885ac6754eb78a23f3945048e0b8e23a20d09c3c6110b1`。7个输入/3685250912 B，语义报告SHA `de25adea954456483edc2e52e6a927b4a91f97753cb6a8d75286dab0066dd65d`；冻结spin-up3小时、60垂直层及FDDA弱，模型00→06、正式03→06 UTC，预期4个native时次（03/04/05/06）及条件28个WebP，后者须实际证明7显示变量齐全。source/5binary、request/config/forcing.expected、plan/preview指纹均绑定，catalog精确任务public/asset均0。本地native收集0不表示远端缺失；旧04业务FAILED保留，不以runtime计算成功改写为产品PASS。

GFS-N资源附证：[逐文件SHA绑定](../../.run/wrf-refactor/20261008T082008Z/matrix-GFS-N-resource-annex-attempt1.json)，4567 B，文件SHA `449f0850dda51391a4a92ca004d8b5e76788fcee8ff1e188005aeef5a20a0373`；5个资源文件/560131 B已复核manifest及task/attempt/release身份。469样本/468.049 s、launch后1.11 s开始；PSS2108254208 B、RSS2265243648 B、观测CPU492.16 core-s、同样本allocated总峰11618043904 B，均partial_sample观测下界；managed wall估计467.852 s，业务attempt9526.145988 s含排队/准备。

EC-N最终附证：[本次尝试冻结](../../.run/wrf-refactor/20261008T082008Z/matrix-EC-N-wrf_ecmwf_20261006T030000Z_0d2e5ad3-attempt1.json)，10394 B，文件SHA `1e40d4ddb03b1097a71dbd83d9d04914acf336f314779511a63d237802a64296`；[程序/配置/预期/catalog附件](../../.run/wrf-refactor/20261008T082008Z/matrix-EC-N-final-expectations-annex-attempt1.json)，7670 B，文件SHA `01ab403f819d01b899efb8dda97e31f6996c42cbef522a933b05c1cc2a6c17f7`。3个输入/424898400 B，语义报告SHA `5948f8aeb00470c20ef59c11e27eb435454aef3b2d46bfd45ee7104610e9ce5c`；冻结spin-up3小时、60垂直层及FDDA弱，模型00→06、正式03→06 UTC，预期4个native时次（03/04/05/06）及条件28个WebP，后者须实际证明7显示变量齐全。source/5binary、request/config/forcing.expected、plan/preview指纹均绑定，catalog精确任务public/asset均0。本地native收集0不表示远端缺失；旧04业务FAILED保留，不以runtime计算成功改写为产品PASS。

EC-N资源附证：[逐文件SHA绑定](../../.run/wrf-refactor/20261008T082008Z/matrix-EC-N-resource-annex-attempt1.json)，4580 B，文件SHA `929dc60466c20cc5dea36d42f0f53cc7bbb31494dce9fe9232c6ff1d59ccf741`；5个资源文件/234010 B已复核manifest及task/attempt/release身份。171样本/170.018 s、launch后0.99 s开始；PSS2084187136 B、RSS2242117632 B、观测CPU330.82 core-s、同样本allocated总峰2780403712 B，均partial_sample观测下界；managed wall估计170.836 s，业务attempt9698.733647 s含排队/准备。

GFS-N原生历史复验：[06只读完整计划](../../.run/wrf-refactor/20261008T082008Z/output-validator-gfs-nondefault-06-plan.json)，3063 B，文件SHA `1168c1037f1099e1c10781f5b971ebf1f7ac0d2c780ec76cff2ebf3830e824fd`，digest `e7ff930c467bb667f9af17a4f61b38f8717e0060dfe56a1f945c0a6aeb9930cc`；4个原生文件/17359720 B，complete=true、invalid/missing_times/blockers为空。该计划尚未绑定、恢复、下载、渲染或登记。随后本地wrf.sh五处大头部解析修复改变生成器指纹，当前严格就绪门禁拒绝06；EC-N06计划未成功生成。维护侧目标改为独立08发布后重新冻结只读校验计划，继续绑定原04计算/request/attempt，不放宽指纹门禁，不apply旧06计划；此处保留06历史时点；随后08已部署并通过就绪门禁，五份08完整计划均生成PASS并于activation v2应用绑定，旧06计划未apply。

GFS-T远端失败补证：[精确attempt只读取证](../../.run/wrf-refactor/20261008T082008Z/gfs-t-exit141-readonly-evidence-v2.json)，21313 B，文件SHA `9e8fd2a2f32cffee151926f6c8b025bc45f76bf5fec4d7472543df04ce8fd47e`，精确会话已无存活，wrfout/wrfrst/rsl_error均0，日志未进入real完成；124个met_em的维度/Times头部检查完整。其首次系统ncdump格式读取失败是探针环境问题，后续相同04 runtime.env下的真实header复现另行保留，不能据探针报错认定met_em损坏。

GFS-T141候选机制补证：[同04环境实际头部2500次解析](../../.run/wrf-refactor/20261008T082008Z/gfs-t-exit141-header-repeat-v1.json)，1647 B，文件SHA `01354ff702c505a8900df3014b6f9d12d95a7507e2951a5fe80997d61a609914`，25926 B头部SHA `0f6916af3f37944fe510b0dd522e8adf0c7f48851f5fc6f292be34cb59110789`。west_east出现1次、south_north出现3次printf/awk管道状态[141,0]，证实早退出与pipefail存在竞态机制；三个层数解析各500次未复现141，生产精确失败行未由现有证据证实。维护侧进一步核验GT04原attempt无real启动、无wrfout，原attempt保持失败/waiting_restart。五个实际header赋值改为awk here-string，修复脚本SHA `ae7f9d8c55b7a2d61f8f16cacae1a096608e7f8415975bffd564da98967b9160` 已纳入08；08就绪通过仍不证明此矩阵计算通过。08同科学配置的新完整混合树为独立任务1fe03ede/attempt990514f8af4c47159b4e9cb98c520248，后续已完成计算、100native/700WebP/catalog及真实登录结果门禁，证据见前文；旧GT request逐字段相等，原失败attempt/digest保留，不能把新任务写成原失败恢复。

五份08原生输出只读复验均PASS，合计158个native文件/791345602 B，complete=true、invalid/missing_times/blockers均为空。它们分别绑定原04计算任务、原attempt/request/配置/namelist及逐文件SHA；校验release统一为08 / `d4502f6b2af5f5b2979e7032c80891bfd38cd23a8e91bdcad0d2805e44206db4`，校验脚本SHA `3b224895e51978e0ddad9703b6df083fc4c677d425aeb3ed8d3a5472dba80714`、schedule SHA `132e521e31f924fbca1319eecce9013b322fef91a068808a1cca8739eb215da2`。上述五份计划于13:06 UTC activation v2完成08校验器绑定，当前API默认08；activation审计明确recovery_started=false，绑定本身没有启动重算/下载/渲染。随后维护负责人经原生ASK逐项接受输出重试，13:33只读快照EC-D/GFS-D/EC-T/GFS-N为queued/retrying_outputs，EC-N仍failed；原ASK过期后维护负责人正常点击“暂不处理”，成员DENIED/UI关闭且无执行，用户授权未回复，后续必须新diagnosis→plan→原生ASK，不能复用过期计划或绕过授权；产品层须逐项实际核验。GFS-T04退出141不在五份输出绑定范围，旧attempt/digest保留；新GT08独立同配置任务及四个已授权输出重试的后续产品终态见前文，EC-N仍未获本次恢复执行授权；15:06 UTC已生成下述新的待人类ASK，未点击执行。

| 用例 | 08只读原生文件 / 逻辑字节 | 精确计划 / 文件字节 | 文件SHA-256 | canonical plan digest |
| --- | --- | --- | --- | --- |
| GFS-D | 25 / 129386574 B | [output-validator-gfs24h-08-plan.json](../../.run/wrf-refactor/20261008T082008Z/output-validator-gfs24h-08-plan.json)；9006 B | `07de3969d2903cac8adf6ea9c1a26f02c6242197ef6cca637bf66fd9a4a752a2` | `d99d66ebd48b22812d0d5da364cd03fd81173eb8f18a0c9fac245ad2e53133b0` |
| EC-D | 25 / 129164313 B | [output-validator-ecmwf24h-08-plan.json](../../.run/wrf-refactor/20261008T082008Z/output-validator-ecmwf24h-08-plan.json)；9008 B | `3aacdf52b47080fce46aefce8a7ccd164a8a435f53a4d313b11058ac34f3a6a9` | `36b28741204f7120d0ac5dfe7d549e0fb44c3302f7edfdfe7fd2b314a52a019d` |
| EC-T | 100 / 498057176 B | [output-validator-ecmwf-mixed24h-08-plan.json](../../.run/wrf-refactor/20261008T082008Z/output-validator-ecmwf-mixed24h-08-plan.json)；30284 B | `3da7defd62fc83dc83a10fe62da0b44d580b94056da4a17ba3a56ca57d0030b2` | `4cee515cfc95cabbc1dd19d86ea445448ad300ef0b303ed1965c0a56528948c2` |
| GFS-N | 4 / 17359720 B | [output-validator-gfs-nondefault-08-plan.json](../../.run/wrf-refactor/20261008T082008Z/output-validator-gfs-nondefault-08-plan.json)；3063 B | `0271996fa61d0d9e43f92eeda2527df7a28e09cc2d96f416552d06f26021640a` | `6c69028823fa8fc28f82e4a39f332c407d45052f834d84109ffb1918e3118e8d` |
| EC-N | 4 / 17377819 B | [output-validator-ecmwf-nondefault-08-plan.json](../../.run/wrf-refactor/20261008T082008Z/output-validator-ecmwf-nondefault-08-plan.json)；3066 B | `03311448e1ae25c669f7880fbb65498b501f404df5e722eba1b752da0d3c0c86` | `745a766902cf2ab290efbd1b4decfb050cdd1ebb8010117a9314769c665252b2` |

每项通过至少需要：

1. 维护切换完成，实际 API 的配置合同、鉴权、发布与本地资产路径正确；失败、401 或响应未知保留输入，未知提交用原幂等键只读对账。
2. 输入清单逐文件 SHA、有效时间、共同必需字段/层、模式顶及空间覆盖通过；任务实际 namelist 与配置一致，运行前后的构建/布局摘要门禁通过。
3. 记录受管尝试的 PID、start_ticks、boot ID 与终态；WPS/WRF 阶段成功且实际输出符合所有域的计划时序。RC=0、排队或部分积分不能代替完整验收。
4. 原生 NetCDF 必需变量、全部时次、投影元数据和数值有效性通过，记录缺帧、非有限值和 CFL 等诊断；不能把部分产品写成完整成功。
5. 真实登录工作台和智能体正确读取同一任务/产品，域、变量、时间播放、图例、范围、质量提示和权限符合实际元数据；精确地图叠加绑定通过定位校验的 WebP。
6. 需共享的产品必须完成 backend_system 正式登记并通过权限验证；文件存在、8007 私有预览或图片可打开不等于已正式发布。

上述门禁是工程与数据完整性验收；若要给出数值等价、预报效果或性能优越性的结论，须另冻结参考程序/个例、误差指标与容差、重复次数和公平资源条件，不能从本表推导。

## 5. 失败暂存、测试产物与保护项

v4 精确清单及所列六个受限进程身份排除项已获用户确认，执行门禁待满足，本轮尚未物理删除。[用户确认记录](../../.run/wrf-refactor/20261008T082008Z/retirement-v4-user-approval.json)，861 B，文件 SHA `6aa5e4ed91880b3b65ee83b64282c04ec0a6cabf40a2f1a26b71d778747a72ae`，绑定 v4 索引文件 SHA `8e811d257953b3416c41e09de8751e6f9e686d7b0a888f551f1bf57944546937`。执行仍要求普通计算空闲、现场逐文件 SHA/身份/引用复验和合法认证的静态访问检查。新增 release05 失败 staging、2.0.4/2.0.5 产物及任何漂移或额外范围均不在本次授权内。私有审计文件、输入、任务数据库和真实输出不是默认临时垃圾。

| 清单 | 精确范围 / 数量 / 逻辑大小 | 当前动作 |
| --- | --- | --- |
| [失败 staging 候选 v3](../../.run/wrf-refactor/20261008T082008Z/failed-staging-candidates-v3.json) | 5 个精确 token；35807 regular files、316 links、3502639828 字节 | 已纳入用户确认的 v4；执行门禁待满足，仅独立失败副本，不包含成功 release。 |
| [前端验证与发布资产](../../.run/wrf-refactor/20261008T082008Z/frontend-validation-artifacts-v3.json) | v4 精确范围：主站 dist 10 文件/3689942 字节；本轮专用 npm-cache 251 文件/152885488 字节；6 个专属 Chrome 目录合计 20 文件/2033280 字节 | 用户已确认 v4，尚未删除；现场摘要/引用与其他执行门禁须再次通过。 |
| 同上清单的保护项 | 共用组件 dist 2 文件/577466 字节；2.0.0/2.0.1/2.0.2/2.0.3 的 tgz+manifest 合计 8 文件/625309 字节；高分源码私有备份 10 文件/137341 字节 | 保留发布、复现及回退用途。 |
| [前端高分专属源码候选及备份](../../webpage_sys/.artifacts/retirement/frontend-2026-10-08T08-42-49-205Z/candidates.json) | 精确文件/符号范围及逐项 SHA 见清单；混合 api.js 仅退役指定符号，不整文件删除 | 用户已有修改保留；尚未物理删除。 |
| [2.0.4 新增验证/发布资产，独立未批准](../../.run/wrf-refactor/20261008T082008Z/frontend-2.0.4-additional-artifacts.json) | 未批准候选 33 regular / 5485995 B：4 个专属 Chrome 目录 16 文件/1627869 B、隔离主站构建 10 文件/3693938 B、版本专属 npm-cache 7 文件/164188 B；保护的 package stage 4 文件/587493 B、tgz 158482 B、manifest 497 B | 保留验证/复现用途，不属于 v4；文件 SHA、精确路径和本用户 lsof 零可见引用见清单。未来删除前须重新核验/授权。 |
| [2.0.5 新增验证/发布资产，独立未批准](../../.run/wrf-refactor/20261008T082008Z/frontend-2.0.5-additional-artifacts.json) | 未批准候选 41 regular / 6332603 B：3 个 Chrome 目录 19 文件/2141194 B、隔离主站构建 10 文件/3698241 B、版本专属 npm-cache 12 文件/493168 B；保护 package stage 6 文件/596187 B、tgz 161672 B、manifest 497 B | 保留诊断、验证和复现用途；不属于 v4，不删除。 |

审计绑定：失败 staging 候选 v3 SHA-256 为 `6635a47a85a64bbdeda38a610a3ea07d7370bed327583dbc4a32a8a0362dc5df`；前端验证与发布资产清单 SHA-256 为 `57e2647e5399eb454bb2e024316cf01527f280a5048e1992c5e5d58c4a3b5837`。五个失败暂存目录的逐文件 SHA 与 symlink target 另绑定 [完整清单](../../.run/wrf-refactor/20261008T082008Z/failed-staging-file-inventory.json)，SHA-256 为 `ba9a0696f408474baf7d0e90d5d7a939b2410bd136675cf802b7f852c3efffbb`。总审批索引由运行侧合并生成，必须绑定这些完整文件摘要；链接或摘要改变须重新核对审批范围。

2.0.4 独立产物清单为 17333 B，文件 SHA `66e51427a771936dc44d602ef3b1a2ec516150bbd6226d7c94fbc9a37a6f2f66`；生成时已只读复验旧前端 v3 清单绑定的全部 301 文件，SHA 无差异。旧 dist、旧共用 dist、旧缓存、旧 tarball 和 v4 索引未修改；新增构建使用版本专属隔离目录。私有审计报告本身属于保护证据。

2.0.5 独立清单 19222 B，文件 SHA `2f8f6e914e074444770325d3196fc8edd58bf0cbeffe048ed849daed5073510c`；生成时已逐文件复验旧 v3 全部 301 文件及 2.0.4 清单全部 39 文件，SHA 无差异。本用户 lsof 可见范围零引用、无 stderr，不能替代未来删除时的现场占用检查；新增5范围全部未批准，审计报告受保护。

新增独立 [额外清理 review 索引](../../.run/wrf-refactor/20261008T082008Z/retirement-additional-review-index-v1.json)，55689 B，文件 SHA `9a506006279fa2ee62d47b09bede761591544fdbecb45c705e80f88f6fdac270`，只包含上述新 05 失败 staging、2.0.4/2.0.5 临时范围及 Agent 旧 2.0.4 vendor 单副本：13 精确根、7963 regular、0 links、1129196957 逻辑 B。全部尚未批准，维护负责人已向用户异步请求确认；没有扩大 v4 授权或执行删除；物理回收量未估。Agent 旧4副本的 [独立候选](../../.run/wrf-refactor/20261008T082008Z/agentscope-ui204-duplicate-candidates.json)，3219 B、文件 SHA `515c80af37097614b8d9d96b4ef0ec54d0f64a0ce457c05754479ddf18a88f77`，仅 158482 B、SHA `6279b710b99608dc96c2859322b8b7ec7983dcec575749fbb2586739063fc382` 的精确 tgz；当前依赖/锁/manifest 不引用4，权威 Vue4同SHA包继续保留。新索引保护当前5包、所有真实矩阵/证据及成功01–07/current/sourceanchor，05既有新 opaque 身份阻断及未来 ordinary idle、fresh全SHA/占用、合法认证门禁保持。

| 失败 token | 失败阶段（部署操作报告） |
| --- | --- |
| `a770c6a845d740baaab3348728ab0d9d` | WRFDA external `var/run` link 拒绝。 |
| `02b16d5bf51a4414b508f376c81f49b8` | 源复制排除回调发生 `set += list` TypeError。 |
| `b8495f10c9e24740bee22c7da058466e` | WRFDA `var/test` link 拒绝。 |
| `b9e62a914a77448fa805da429f6efb96` | 从只读版本复制后覆盖 `wrf/main/wrf.exe` 发生 PermissionError。 |
| `00db543b9327454da27e2f4a883eb293` | 系统 Python/Linux 不支持 `chmod(follow_symlinks=False)`，解封发生 NotImplementedError。 |

追加未纳入旧清单的保护项：release05 首次部署暂存 `3628c17da2a24491ad87441f067402ae` 因操作传入错误 ecCodes 前缀而失败，未激活并保留。独立只读盘点为 7888 regular files、0 links、465 directories、1117219877 逻辑字节；[全文件 SHA/链接/占用快照](../../.run/wrf-refactor/20261008T082008Z/failed-staging-05-file-inventory.json) 2672406 B，文件 SHA `2ff0693c1f581867e7827e8954ac768f38d98e10690706eb87981b15e5864146`；[独立未批准候选](../../.run/wrf-refactor/20261008T082008Z/failed-staging-05-candidates.json) 10175 B，文件 SHA `50b8866af1836a1733d06b4aaed74e262a14465cc2d92fd7d4f803839a166a7f`。可读进程无引用，但 7 个受限身份中新增 PID 5637 不匹配旧 6 个精确例外，未来删除门禁明确阻断；不能按 sshd 名称忽略。该目录不属于上述五 token 清单或既有总审批索引，全部未批准，不改旧索引、不删除。成功 04/05/06/07、current 及固定源码入口受保护；逻辑字节不表示可保证物理回收量。

五个失败副本均无完整 manifest，未激活。可读的同 UID 进程范围中没有观察到引用；六个受限进程的部分句柄无法读取，已核对 PID/start_ticks/boot ID/argv0 SHA 与现有私有清单一致。它们只能作为同一次用户审批中明确批准的精确身份例外；名称相同不构成豁免，新身份、身份变化或实际引用均须阻止删除。现场 stderr 的独立审计文件尚未找到，失败阶段来源明确为部署操作报告，不能伪称存在独立日志文件。

保护范围还包括：历史成功安装的 01/02、首次通过完整门禁的 03、计算绑定 04、独立校验 05/06/07、当前08与 release manifest、独立普通运行环境、CPU/WPS 二进制、地理资料、共享编译器/MPI/NetCDF/ecCodes、WRF_GPU、四个真实输入及源资料、任务/审计数据库与备份、原生输出、发布产品、Redis 持久化资产、用户已有代码改动。`node_modules`、持续回归测试源码及本轮以外的历史临时目录不在本次前端临时清理候选中。

最终保留范围与未完成项统一记录于[15:08 UTC交接清单](../../.run/wrf-refactor/20261008T082008Z/implementation-handoff-20261008T1508Z.json)，8763 B、文件SHA `2b89b6e31f9865d6b5215b9973b80e7daca212aff92665de384addd8f9434f99`。统计时点为该JSON创建前：专属审计根617 regular/203792394 B，新GT08资源根5 regular/8247130 B，均为需保留的验收与诊断证据。已批准v4中的8个临时验证根与3个旧vendor根共11 roots/284 regular/159075428 B仍保留，等待精确8case、空闲维护及现场nofollow/fullSHA/身份门禁；额外13 roots/7963 regular/1129196957逻辑B尚未批准，继续保护。清单逐根保存精确路径、计数、保留原因及后续时机，不估计物理回收量，不含令牌。physical_deletion_performed、source_cleanup_applied、matrix_acceptance_file_created、coordinator_execution_claim_created均false；七case成功不自动解除第八case与执行授权门禁。

## 6. 更新规则

- 每次维护或验收追加时间、操作者、具体任务/尝试和证据路径；变更 release、输入或配置后新增记录，不覆盖旧任务结论。
- 报告缺失、身份/摘要不一致、响应未知或输出不完整时记录实际状态和阻塞原因，保留证据，不自动重放或写成 PASS。
- 真实登录 UI、真实 HPC、产品与回退演练逐层完成后才升级对应状态；全表通过前，交付报告继续明确未完成项。
- 本文是验收记录，不修改服务职责、数据库、计算配置或执行授权。根文档与子仓库运行说明由结构性变更负责人同步维护。

前端源码退役已完成私有准备、未应用：[精确备份/变更范围](../../.run/wrf-refactor/20261008T082008Z/frontend-source-cleanup-preparation-v1/preparation.json)，39107 B，文件SHA `abb6a6617cb17acdc77e2c014f5c65c5911c4fe7951b1a22b39a1575efde86c7`。逐个核对获批10个whole文件和api16个函数的旧SHA/备份，当前源码123文件/1637278 B另行完整快照，保留6包源码、503宿主修复及有价值退役fixture；API其余导出函数逐AST SHA不变。[私有补丁](../../.run/wrf-refactor/20261008T082008Z/frontend-source-cleanup-preparation-v1/frontend-source-cleanup.patch)114638 B、SHA `6bc73b784edbf85b10d121d9679f1b8cecfa962fbc299281c8fc88bc5cc2b075`仅删除上述10文件及16函数。[临时副本验证](../../.run/wrf-refactor/20261008T082008Z/frontend-source-cleanup-preparation-v1/isolated-validation-v1.json)，13219 B，文件SHA `a16c9b5bce40862fa394f09d953c5aee7010eb586d707c567c3dca6c322ffecc`：精确补丁apply只发生在TemporaryDirectory，73单测、生产构建、4Chrome普通树/提交对账/退役路由/错误呈现PASS，live源码所有SHA未变。151临时文件/23883119 B finally全部清理；私有prep127文件/1845989 B受保护。实际apply须在main data退役完成后由维护负责人执行并现场复验beforeSHA；没有扩大v4删除范围或把额外未批产物加入授权。


### 2026-10-09 工作台管理切换

普通发布已切换为 `ordinary-20261009-10`，指纹 `e772c0e4cb0cb93f40482bb96dd516f8375c2f6629f496ab13ac42b9285a12b3`。8007/8012 已温和重载并加载管理能力；任务元数据、恢复确认、删除计划、幂等下载及绑定摘要的资料清理接口已生效。旧 WRF cron 的唯一条目已精确备份并停用，三个已核验预取下载器温和退出；旧资料池、下载队列和部分文件保留。其他服务身份及原任务行摘要一致。自动策略仍须管理员显式保存启用，不将旧队列视为已清空。审计位于 `.run/wrf-refactor/20261009-management/management-activation.json`，远端调度审计位于配置发布根的 `manifests/management-scheduler-20261009.json`。

验证：后端隔离回归 363 通过、1 跳过；远端 Linux 六项合成资料安全检查通过并移除夹具；Vue 构建通过；浏览器窗口主操作可达性复验通过。真实工作台两源手动提交、故障接管/回退及跨产品色标核对仍在验收，不能将原计划标为完成。历史失败任务不自动恢复或删除。
