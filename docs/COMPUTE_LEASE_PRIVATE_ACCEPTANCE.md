# 2026-10-14 租约回收与私有任务实际验收

## 修复合同

`scripts/reclaim_compute_lease.py` 是仅管理员运维的默认只读入口，必须绑定已完成删除事务编号和原 manifest digest。只读核查 deleted 墓碑、任务记录已移除、普通计算和未决删除空闲，并保留原终态/attempt/控制路径。显式 `--apply` 才在远端原 compute.lock 下复核精确租约字节、目录消失、原PID/会话及同UID关联任务进程不存活，将原租约原子归档到数据根 `.compute-lease-audit`；先持久化核验审计，原字节保留。未知、活跃、身份变化、非空目录或不可读进程拒绝；响应不明不重放，无数据库写入，不修改不可变运行包或装配给Agent。数据根旧/新配置别名按规范物理路径比较，不能把任务路径别名变更当作合法归属。

新任务请求严格布尔 `publish_to_catalog` 默认true兼容既有行为；false在草稿/规范请求/计划摘要中保持，ASK明示禁止正式目录发布，自动发布和重试不能调用目录发布器。仅认证私有产品正常生成。共享编辑器克隆请求会保留此字段；旧冻结计划和历史任务不重写，无新数据库表。

## 实际修复与运行

- 孤儿租约绑定历史任务 `wrf_gfs_20261007T030000Z_c964e293`、attempt `b9b8aa8ad4da401e982e3f80b411cbee`、删除事务 `delete_6947dd2ca37542c0a8d522f088116b5b`、digest `77138e8c8d5974ca9fef1b3560d45fc4239c599ce1e3f7a325cfefc056d7f04b`。只读检查通过后使用入口归档；未直接删租约或改库。
- 原维护入口温和重载8007/8012，Redis和其他服务身份未变。远端运行包仍为ordinary-20261012-12，未覆盖运行脚本。
- 原生产会话内，旧ASK已拒绝并返回denied、无task_id；旧失败验收任务保持cancelled，不重放。
- 原成员用自动复制的编辑草稿保存 `publish_to_catalog=false`（user来源），新冻结计划 `wrf_8a7015af563249129453bd4940495320`。核对参数并通过生产原生确认事件接受；只创建新任务 `wrf_gfs_20261013T120000Z_23e5ff1e`。
- 参数：2026-10-13 12:00–13:00 UTC（北京时间20:00–21:00）、GFS、中心32.06N/118.80E、D01 dx/dy=27km、41×41、spin-up0、输出30分钟、默认物理、88层。配置4 MPI，最终运行日志确认进程数4。受管身份采样通过；WRF结束很快，第二次rank/线程现场采样时原进程已退出，因此不声称已连续采样所有rank线程。
- 从接受ASK到完成约181秒，低于30分钟预算。监控从接受前建立，25分钟未结束才会受管取消；本轮未触发。远端exit_code=0、SUCCESS COMPLETE WRF，后端输出校验complete，任务succeeded。
- 独立读取3份原生wrfout，Times为12:00/12:30/13:00 UTC；40×40质量点网格符合41×41的WRF配置。7变量T2/U10/V10/PSFC/PBLH/RAINC/RAINNC共21帧，全部时次一致、统计有限、认证WebP读取200，匿名读取401。
- catalog_publication为disabled/task_publication_disabled；共享正式目录只读查询该任务记录0、资产0，未发布或清理本轮产品。
- 原聊天weather_wrf_result返回SUCCESS及weather_result succeeded卡。模型曾泛列未展示变量单位，随后依据实际display元数据更正为7变量；RAINC/RAINNC明示运行累计，不能当作30分钟降水量/强度。浏览器连接不可用，实际页面播放和视觉布局不计为通过；本轮通过生产API/原生确认/真实计算/认证产品/聊天卡协议闭环，不构成气象预报精度验收。

## 验证与证据

后端66项聚焦回归通过；Agent151项中147项通过、4项真实模型默认跳过，原生ASK及聚焦35项通过。覆盖租约只读、精确归档、变化/未知/活进程拒绝、发布字段严格布尔、冻结保真、发布器零调用。远端Linux合成租约归档/拒绝测试通过，生产归档在实际数据盘执行成功。未改前端源码或共享组件版本。

私有证据位于根 `.run/wrf-workbench-review/20261013-live-agent/`，含lease-reclaim-receipt、private-acceptance-start、private-terminal-run、private-final-task、native-output-inspection、private-frame-verification、private-catalog-verification及private-final-corrected-chat。不提交令牌、运行数据库、原生输出或日志。
