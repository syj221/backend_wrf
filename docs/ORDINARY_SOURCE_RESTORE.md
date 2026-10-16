# 普通 WRF 旧源码恢复与参数移植（2026-10-12）

## 当前结论

磁盘源码恢复、参数移植和离线兼容验证已完成；生产后端切换和实际登录页面验收未完成。不能宣称整项计划已验收。

工作台以 `webpage_sys@c177766` 的 `WrfStudio.vue`、`WrfTaskRun.vue` 重建普通界面，恢复预报数据池、历史任务、正在进行及原结果工具栏。参数区域继续使用当前 `WrfTaskConfig.vue` 和锁定的共享组件 **2.2.2**。普通工作台不依赖 8012。

后端从 `backend_wrf@1ee4c7b` 提取普通方法，保留必要兼容覆盖，不是整文件或整仓逐字节回退。六个核心文件的来源清单见 [ordinary-baseline-origins.json](ordinary-baseline-origins.json)：162 个定义保持旧源码，3 个旧执行方法仅外加保护装饰器，164 个定义保留参数、安全、持久化及智能体兼容实现。该数量说明源码来源，不等于独立业务能力数量或行为覆盖率。

## 备份与保留边界

本轮恢复前备份位于工作区 `.run/wrf-rollback/20261012T031657Z/`。`source/` 共 1,466 文件、20,997,186 字节；`manifest.json` SHA-256 为 `cda2dffb4303a957931b1e621729c0808c006f766df9979ebdfb19530c80a7d5`。另保留三个仓库的工作区/暂存差异、状态和选定旧提交源码。没有执行 reset、clean、stash、提交或推送。

来源 JSON 同时列出保留模块白名单。保留 `config/database` 的独立运行根及现有持久化，`schemas/configuration_profile/domain_preview/planning` 的 ui2 合同，`recovery/task_deletion/terminal_evidence` 的授权、并发及终态保护，以及只读高分退役边界。保留固定发布、受管进程、资料引用与幂等提交；不恢复向共享目录覆盖脚本、旧 cron 调度或仅删除本地记录的入口。

参数编辑、显示投影、地图底图和结果认证组件复用当前实现；工作台运行与结果宿主恢复旧结构，智能体仍使用原共享运行/结果组件。认证、网络与确认由宿主管理。未重新安装环境、迁移业务数据库、修改保留策略或清理历史资产；高分功能继续退役。

## 行为对照与安全差异

| 能力 | 恢复后的工作台入口及唯一执行方法 | 明确保留的差异 |
| --- | --- | --- |
| 手动新建 | 参数页常驻“开始模拟”；既有 create | ui2 校验、预检、原幂等键对账；不经过智能体 |
| 取消 | 运行页确认；TaskManager.cancel | cancel_pending 直到全部进程退出，不能把 TERM 当完成 |
| 恢复连接 | 运行页具体操作确认；resume_external | 对账原尝试，不启动第二次计算 |
| 结果重试 | 运行页确认；retry_outputs | 不重算 WRF，未知执行结果禁止重放 |
| 部分渲染 | 运行页缺损提示与确认；render_partial | 部分质量标记及完整产品发布门禁保持 |
| 原参数/修改后重跑 | 运行页操作，修改时回参数页；restart | 同任务递增尝试、归档旧记录；原参数无修改也可申请；最新终态仍须证明 |
| 历史尝试 | 运行页尝试选择、参数与日志 | 缓存和请求绑定任务/尝试，缺失垂直层显示“未记录” |
| 复制新建 | 运行页/历史任务复用参数 | 新任务身份，不改变来源任务 |
| 重试发布 | 运行页独立确认；retry_catalog_publish | 不重下载/渲染/计算，质量门禁与幂等保持 |
| 归档/彻底删除 | 历史任务管理弹层；元数据/精确删除事务 | 归档与删除不同；本轮未实际删除任何历史任务 |
| 资料准备/补缺 | 旧资料池分源展示、检查并准备 | 原下载器及队列、8007 唯一调度；准备资料不隐式清理 |

`resume_external/retry_outputs/render_partial` 的旧方法正文以哈希核验，外层 `guard_legacy_action` 复用任务锁、删除/恢复冲突检查与确认策略。手动操作继续使用现有 `recovery-plans(purpose=manual)`，界面只展示具体操作与影响，不强制先诊断。智能体保持原生 ASK。归档过滤和所有权使用现有 store.page；不另建任务管理器、下载器或调度服务。

ui2 的 spin-up、垂直层、真实父子树、后端投影预览与 namelist 链路保留；展示投影不写入计算网格。草稿按账户存储，不保存 Token。提交未知时仅使用原键对账。旧冻结摘要、历史请求及尝试不批量改写。

## 本轮验证

| 验证 | 结果与范围 |
| --- | --- |
| 后端聚焦 pytest | **115 通过**：来源哈希、手动计划、状态/锁、ui2、资料策略/队列/引用、元数据、删除/清理、恢复及访问权限；隔离数据库和夹具 |
| Vue Node | **77 通过**，包括错误脱敏与共享组件；既有失败进度断言修正为要求的“最后观测流程进度” |
| Vue 浏览器 | **26 通过**：实际恢复页面、七种尺寸/黑白主题、可点击边界、旧组件拦截、草稿、未知提交对账、跨任务晚响应、历史尝试、各手动操作、归档/发布和取消中状态 |
| Agent Python | **51 通过**：HTTP/业务/故障边界、提交竞争与编辑；使用隔离 Redis Unix socket，未修改生产 Redis |
| React 单元/浏览器 | **39 / 7 通过**：共享编辑/产品、ASK 拒绝、恢复卡及窗口主题兼容 |
| 两前端生产构建 | 通过；保留现有大包提示，Vue 构建也有第三方 PURE 注释提示 |
| 既有真实产品只读复验 | GFS 14、ECMWF 28 个既定产品文件的大小及 SHA-256 均保持，共 **42 个**；未重新计算 |

后端命令：`.venv/bin/python -B -m pytest -q -p no:cacheprovider test_ordinary_baseline.py test_manual_operations.py test_task_manager_safety.py test_ordinary_ui2.py test_forcing_policy.py test_pool_resilience.py test_forcing_reference_protection.py test_studio_lifecycle.py test_task_metadata.py test_task_deletion.py test_forcing_cleanup.py test_recovery_diagnosis.py test_forcing_pool_queue.py test_wrf_access.py`。

浏览器业务接口采用隔离 fixture；通过不代表本轮实际远端 WRF 重跑已验收。预览拒绝非法父域缩放的预期用例会打印 ValueError，此用例通过。当前未扩展其他业务智能体。

## 部署门禁与后续切换

5177 已只读确认提供恢复后的工作台源码和当前参数适配器，但不能等同于实际登录浏览器中的 Custom Element 构造器及指纹复验。实际 Chrome 连接超时，本轮该项未通过。

维护入口只读检查返回 `Read-only task/remote identity check failed; no authorization to stop`。历史任务 `wrf_gfs_20261007T060000Z_70566ff1` 有启动尝试标记，却缺少持久化 `remote_process` 等身份与终态观测。没有猜测身份、补写数据库或绕过保护。公开本地活动接口虽返回活动/队列为 0、高分活动为 false，不能替代远端终态证明。**生产 8007/8012 尚未加载本轮后端源码**，没有执行维护重载。

下一步先依据可追溯的同一次尝试控制记录核验并修复身份缺口；不能把历史退出码或缺少 exit.code 当作充分证明。仅在证据完整和独占维护窗口，通过原维护入口温和切换，再复验实际 5177、API 版本、组件指纹与手动接口。失败按本轮精确备份恢复受影响源码/构建，不回滚业务数据库、不覆盖运行资产。

用户暂缓的 `wrf_gfs_20261007T030000Z_c964e293` 真实第二次尝试继续不执行，也未清理其文件。生产切换、实际浏览器和此真实验收门禁未完成前，不宣布完整计算验收通过。

## 验证产物与文档

精确新增产物数量、大小及保留原因见备份目录的 `artifact-inventory.json`；源码指纹见 `restored-source-fingerprints.json`。最终 Vue 浏览器证据位于 `browser-accepted-v2/`（8 文件、1,068,883 字节）；较早失败/中间验证证据保留用于定位与审查，未获清理授权。两个 dist 为当前构建产物，待切换使用，不提交。隔离 Redis/服务夹具由测试收尾，不影响生产服务。

本轮涉及工作台界面组织和旧接口承接的结构性变化，已同步根 AGENTS/README、三个受影响仓库 README，并在旧管理验收文档标注历史范围。分支职责和根 start.sh 端口仍为 auth 8005、system 8002、upload 8003、WRF 8007、Redis 6379、Agent API 8012、Agent UI 5173、主前端 5177；模型 8006 仍独立启动。

## 后续交互补丁

2026-10-12 交互精简是在恢复基线上追加的兼容变更，不把上文恢复阶段验收视为最新补丁已部署。取消归档界面不改变历史元数据；隐藏失败页旧手动操作不删除后端旧方法或智能体 ASK 保护。`HpcClient._queue_cycle_details` 单独列为兼容覆盖：取消/过期引用不能被误报为活动下载。最新逐模块验收见 `WORKBENCH_SIMPLIFICATION_ACCEPTANCE.md`。
