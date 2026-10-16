# GFS aedb4f66 原参数重跑实际核验

2026-10-15，实际终态和资源释放核验完成。用户确认本次没有修改内容，直接提交原参数。仅检查已有第2次尝试，不发起重跑、取消、刷新资料或资源写回命令。

## 已核实

- 原任务 `wrf_gfs_20261013T060000Z_aedb4f66`，第2次尝试2026-10-15 09:19:53（北京时间）受理。请求逐字段与第1次相同，vertical_levels=20、Spin-up6小时、D01 24×19@27km、D02 28×25@9km；当前远端task.env的WRF_E_VERT=20，网格数相符。没有修改内容丢失的证据。
- 新token `e33e253815db44539bca78059a9871dc`，PID30866，start_ticks145206856，boot_id56365d4c-453f-478a-9a77-49332b34707d。仅一个新启动记录和一份1CPU（CPU0）/2GiB预留；旧token b808f4e67f074875a95f3be437c402ad失败，原PID11658/会话已空。当前运行包和监督器均固定ordinary-20261014-15。
- 本任务只有1个manual恢复计划，编号repair_2bce4e20a4344cdfa7fbfe99271eb628，状态accepted、原attempt_no=1、changes={}；API访问日志仅见一次执行POST。数据库data_json内的旧pending快照不能作为当前状态，实际status列和API均为accepted。
- 第1次本地service.log归档5264328字节，SHA256=2ccd273a639e473cb27307f8e451cf09953608626c520e07db7e37f2d117cff9；attempt.json保存旧参数、runtime、failure/error。历史日志认证接口读取末尾成功，包含real.exe失败；原远端attempt的namelist.input和rsl.error.0000保留，FATAL明确not enough eta levels to reach p_top。
- 独立认证浏览器读取真实5177：选择第1次显示历史20层/Spin-up6小时及旧日志，切换第2次正常；刷新页面两次，写请求0，attempt仍2、token及PID保持，无确认弹窗。所有写方法拦截防误操作，实际没有触发写请求；未操作用户原浏览器页面。

## 开始检查时的历史状态

第2次仍在WPS ungrib，CPU计数连续增长，不是进程静止；预留资源应保持。原参数20层未修改，旧根因条件仍在，不能将重跑受理写成模型成功或根因已解决。继续有限只读观察终态、进程/会话退出及资源记录released_at，不创建自动检查。

## 日志显示边界

旧service.log约5MiB。页面从头分块加载，首次显示旧启动日志，末尾失败内容不会立即出现；认证日志接口按offset读取末尾可用，远端详细rsl根因文件也保留。本次首轮浏览器断言错误地要求立即看见末尾失败，未通过；调整为验证实际历史日志来源，并另用真实接口读取末尾通过。没有将首轮失败隐去，也没有改动产品显示逻辑。

证据位于根`.run/wrf-refactor/restart-check-20261015/`：task.json、attempts.json、plans.json、initial-proof.json、browser-readonly.json、attempt-1-history.png、attempt-1-log-tail.json、attempt-1-rsl.error.0000.txt及remote/watch快照。凭据只在内存取用，无导出或打印。

## 最终结果

第2次于10:19左右自然失败，退出码1；本地已为waiting_restart，attempt_no仍2。实际新namelist.input的e_vert=[20,20]、p_top_requested=5000Pa，rsl.error.0000再次报not enough eta levels to reach p_top，与原20层失败条件一致。用户确认未改内容，因此参数原样生效，不是修改丢失；本次未改参/取消/再提交。

原token/PID/start_ticks全程保持，仅有原第1次和第2次两个启动目录，1个accepted恢复计划。旧日志和attempt.json完整，旧日志SHA256前后相同，参数一致；第2次日志开头为2026-10-15T01:19:53，与旧尝试日期区分。旧详细rsl失败和本次新rsl失败均另存于只读验收目录。

进程/专属会话无存活成员，同账户可读取范围未发现脱离会话的相关进程，无法读取进程数0。原第2次资源记录released_at=2026-10-15T10:19:43.659444+08:00，1个逻辑CPU/2GiB准入预留自动释放，本任务活动预留0；没有手动调用资源释放入口。此结论不以系统全盘/内存净变化估算实际释放量。

真实运行页面在任务运行中和终态后各刷新2次，共4次：写请求0，attempt始终2、token/PID记录一致，无第3次。终态后仍可切换第1次旧参数/日志和第2次新日志。运行中的截图与浏览器证据分别保存为attempt-1-history-running.png、browser-readonly-running.json，终态证据为attempt-1-history.png、browser-readonly.json。原浏览器未被刷新，独立验收浏览器已关闭。

最终证据final-proof.json、final-task.json、attempt-2-rsl.error.0000.txt、attempt-2-log-start.json及终态remote/watch快照位于根.run/wrf-refactor/restart-check-20261015/。有限观察在终态/预留释放后自然退出，没有新建或恢复自动检查，也没有生产源码、服务或真实数据清理变更。
