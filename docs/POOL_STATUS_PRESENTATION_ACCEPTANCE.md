# 资料池状态展示验收（2026-10-14）

## 根因与变更

`_queue_cycle_details`先看到queued文件便汇总downloading，覆盖周期waiting_publish/probing；前端cycleRetentionLabel又把waiting_publish/限流统一写为下载中。新汇总先识别真实downloading，再识别expired和发布等待，再区分限流、queued与失败。完整目录ready保持权威，不受陈旧队列状态降级。GFS/ECMWF使用同一汇总；provider、总状态、sync_latest动作和后台预取摘要同时识别queued/waiting_publish。

周期响应兼容增加`publication_status`和`wait_reason`，旧字段保留。等待发布文案只说明上游完整周期/f072确认，probing为正在检查；queued说明等待工作器或共享槽，不把共享槽忙当作已经观测到的事实。脱敏原因不转发原始错误中的路径或Bearer。无活动文件时清除陈旧current_file并返回0速率；前端等待/排队不显示速率。

前端明确显示等待发布、排队、下载中、已就绪，并显示实际已完成文件数、总数、累计大小和实际分片字节。进度条不再按平均文件大小估算分片等价进度，只按完成文件占比；后端正报速优先，缺失报速仅使用实际字节变化采样，支持低于1KiB/s的速度。不会把“下一文件”写成正在下载。

新增queued在两个provider的原活动清理排除集合中，与以前被汇总为downloading时一样保持保护。未改预取触发次数、引用释放、周期保留规则、目录清理桥、任务状态机、数据库表、下载脚本、远端不可变发布或两源共享传输槽。`webpage_sys/src/api.js`沿用原data-status请求，无参数变更。

## 验证

- 后端95项聚焦批次及新增非目标排队保护加验共96项不同回归通过。新增5项方法含两源矩阵：queued占位不能盖过waiting/probing；真实传输胜过陈旧publication；完整目录不降级；陈旧文件名/速度隐藏；等待原因脱敏；非目标queued仍禁止清理。覆盖既有引用、队列、删除和HTTP权限回归。
- 4项维护回归通过。4项隔离浏览器通过：单刷新入口、删除队列时钟、隔离后空间清理/刷新恢复，以及四种资料状态/等待原因/0与3/5/100%真实完成比例/分片/低速显示。首次新增浏览器夹具漏username导致导航初始化错误，补齐与现有登录合同一致的夹具后重跑全部4项通过；未改产品绕过错误。
- 前端构建通过；保留既有第三方PURE及大chunk提示、Starlette/anyio弃用提示。未声称零警告。首次失败的截图和构建产物与成功浏览器输出都在TemporaryDirectory内自动清理，无保留构建/截图。

## 切换和实际结果

默认原维护入口只读预检通过，随后`--execute --maintenance-window`温和重载8007/8012。实际运行健康入口均200，其他六项服务及Redis身份和切换前一致。任务数、资料策略全行和清理计划数量逐项一致；没有手动调用下载、刷新或删除真实资产，也未申请/创建新的登录凭据。

真实远端资料经新HpcClient读取：14日GFS0/73、ECMWF0/25均waiting_publish，current_file=null、rate=0、cleanup_allowed=false；13日分别73/73与25/25 ready，12日完整备用保持保护。运行API `/api/health?cached=true` 中实际预取汇总及两源action均为waiting_publish，返回上游完整周期/f072确认原因。5177的实际Vue模块HTTP200，确认供应wait_reason、完成文件比例与分片展示代码。

完整四状态界面交互为隔离浏览器验证，实际生产核验为远端数据、新运行API摘要及5177供应模块；没有绕过认证读取私有data-status，也不冒充已经逐个点击真实账号UI。当前仍等待上游发布，未将等待修复解释为已完成14日下载或新周期清理验收。

已核对start.sh服务与监听端口、八仓库分支以及前端data-status调用；根AGENTS/README及后端/前端README同步兼容状态字段。无服务结构或部署方式变化，无提交/推送。

## 保留产物

仅保留0600私有验收证据 `/private/tmp/wrf-pool-status-switch-d9okd625.json`，1文件、25062字节；包含切换服务身份、资料摘要和不变量，不含凭据，待用户确认不再需要后精确清理。维护中的测试源码保留，不当作临时垃圾。
