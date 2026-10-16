# 两源预取摘要实际状态验收（2026-10-14）

## 根因与修复

两源sync_latest先调用既有幂等触发方法，通常得到queued受理回执，再读取资料池。原代码只覆盖失败、等待发布、queued和限流，未覆盖真实downloading，也遗漏请求期间完成的ready。因此即使pool卡片有当前文件，预取action和总摘要仍可能长期queued。

新增共享纯函数`apply_forcing_observations`，两源复用同一最新观测映射，不再复制遗漏状态列表。complete优先ready，可信downloading覆盖queued回执；同步completed_files、total_files、size_bytes、partial_size_bytes、current_file、download_rate_bps及wait_reason。下载详情说明实际文件，完成详情说明实际计数，等待/失败沿用原信息。缺失或旧服务无法识别的观测不编造状态，保留原回执。

没有新增资料读取命令、入队调用、刷新请求、取消/删除或下载重试。只替换已有同步流程第二份资料观测的展示映射，不改预取调度、引用锁、周期锁、目录归属、两周期保留与清理保护，不改DB或远端不可变发布。总摘要仍为原300秒观测周期，资料卡片约30秒刷新；不同采样时刻可有短暂进度差，不承诺每秒同步。原操作记录和幂等键不改变。

## 回归

99项聚焦后端回归及4项维护回归通过。新增场景覆盖两源交叉downloading/waiting_publish、queued、全部ready、最新观测覆盖回执、请求期间完成、缺失观测兼容、文件进度与原入队次数、无隐式清理。保留既有Starlette/anyio弃用提示。没有前端源码变化，不重复前端构建/浏览器验收；原前端读取/api/health?cached=true和/data-status入口已核对。

## 真实切换与后续验收

切换前已用固定凭据及/me核验认证，真实data-status显示GFS14日24/73、downloading，而运行摘要queued，完成实际复现。维护只读预检空闲后，通过原--execute --maintenance-window温和加载。切换后同一认证读取：

- GFS14日资料池和后台action均downloading，24/73、f025；后台观测分片218278158字节，随后卡片226470158字节，符合不同采样时间的增长，未以静态存在证明活跃。
- ECMWF14日两处均waiting_publish、0/25；13日两源分别73/73、25/25 ready，12日完整备用仍被保护。
- 总摘要downloading，action含实际当前文件/计数；共享传输槽busy、GFS工作器存活，ECMWF无在下工作器，符合槽由GFS占用。两源资料待删除区空，12日原目录存在，无12日清理事务。
- 8007/8012原维护入口成功，其他六项服务及Redis身份与基线一致；任务id/status/runtime记录摘要、资料策略全行、清理计划数量逐项一致。没有重启远端下载器、强杀、重放启动、人工刷新或清理资料。

继续原wrf-14后续只读验收；14日未完整，本轮不能验收12日自动清理和磁盘释放。新周期完整后按原目录、隔离目录、原事务complete/cleanup_status、可用空间与删除文件占用分层验收，不把并发下载净占用当作未释放。固定文件凭据保留，不重复登录或每轮删除。

服务集合、八仓库分支、启动端口和前端调用已核对。根AGENTS.md/README、backend_wrf/webpage_sys README同步，仅响应观测字段补充，无服务结构或数据库迁移。未提交/推送。

## 产物

回归样本和SQLite均自动收尾，无新增构建或截图。保留唯一0600私有证据 `/private/tmp/wrf-prefetch-summary-698wkews.json`，1文件、50974字节，包含前后API/服务身份和不变量，不含Bearer；用于后续比较，待用户确认不再需要时精确清理。测试源码继续维护。
