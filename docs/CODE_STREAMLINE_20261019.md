# WRF 入口与重复实现复核（2026-10-19）

状态：源码及聚焦回归完成；用户释放聊天连接后，已通过原维护入口安全加载。

## 本次精简

- gfs.GfsManager.normalize_source 与 HpcClient._normalize_forcing_source 原先包含相同实现。统一为 gfs.normalize_source，两原调用入口保留静态别名，默认 GFS、ec 别名、大小写/空白及非法源拒绝合同保持。
- deploy_namelist_generator.py 是历史可变服务目录的三模块原地部署工具。当前不可变发布模式下 `--apply` 在任何数据库/网络/上传前明确拒绝，并提示 deploy_runtime_release.py；不改写封存发布。未配置不可变发布的兼容分支及只读检查保留，原备份/回滚测试保持。

## 调用核验及保留项

25 个 backend_wrf/scripts 顶层脚本的文本引用已建立索引，见根目录 `.run/code-streamline-20261019/script-references.json`。没有文本引用不能证明外部运维入口未使用。

- runtime_release.SCRIPTS 明确列出不可变包脚本；它们不是本地重复副本。
- migrate_runtime_assets.py 被根维护入口调用；upgrade_output_validator.py 是受管终态修复工具，均保留。
- wrf_server_worker.sh 为退出码 3 的禁用墓碑，防止旧文件系统调度误启，保留。
- wrf_server_cron.sh 当前远端 crontab 中已注释；forcing_policy.py 仍检测它防止双调度。保留兼容入口，不复活 cron。
- retire_downscale.py 和退役查询边界用于旧任务可读及禁止写入，不按“旧代码”删除。
- 当前远端 current 为 ordinary-20261016-16，两源实际工作器 19169/20383 均使用16版。源锚点与运行环境旧版本依赖保持。
- 服务器旧 backend_wrf_service 存在旧下载/运行脚本及备份，但尚未逐条排除历史任务、外部运维调用与配置引用；本轮未删除或搬移。
- backend_model 恢复状态保持；不触及 WRF_GPU 科研运行。

## 验证与切换

53 项资料源、生成器部署、时次、策略、源选择、状态和引用保护测试通过；18 项维护/服务生命周期测试通过。新增不可变原地写入零 I/O 拒绝及共享规范化回归。

原维护入口只读检查通过；执行时连接门禁提示 `Chat connections remain open; leave /agent before API maintenance`，未停止服务，未绕过。待用户释放智能体连接后方可继续安全切换，不能声称生产已加载。

切换尝试前后只读快照完全一致：7 个任务/提交/恢复相关表摘要、7 份 service.log 完整哈希、远端 current/源锚点/16版清单哈希及两源工作器进程命令均保持。其余7个Git仓库状态保持；backend_wrf 仅本轮预期源码/回归改动（原 README 修改保留）。快照位于根 `.run/code-streamline-20261019/`。

本轮未删除代码、任务、资料、日志或发布包；未发刷新、重算、下载、取消或删除请求。没有证据支持继续批量删除旧服务目录，也不把本轮两项精简称为全面消除语义重复。

## 后续安全切换与实际复核（已完成）

用户释放所有智能体页面后，原维护入口通过完整门禁并温和重载8007/8012，远端任务不重启，Redis及其他服务身份保持。两API健康200；AgentScope status/checks全部ok，icing/nowcast/wrf注册保持。切换后16项新增/相关聚焦回归再次通过。

switch-before/switch-after快照完全一致：7个任务/尝试/提交/恢复/元数据/删除/墓碑表摘要、7份service.log哈希、current与源锚点/16版清单哈希以及两源工作器PID/命令保持。两源工作器仍为GFS19169、ECMWF20383，分别有自己的curl子进程。

实际下载不能笼统报告全部正常：ECMWF18日25/25，19日2/25，f006原inode分片在18.50秒内增长229376字节；GFS18日59/73，f059分片未增长，因NOMADS TLS连接SSL_ERROR_SYSCALL进入原自动重试，后续已选择f060；此前失败时间早于本次切换。GFS19日发布探测超时、0/73，当前只能确认发布状态待核验，不能认定上游确实尚未发布。没有中断或手动重放下载，没有修改重试/清理规则。

固定凭据当前不存在，认证资料池接口未能验收；上述下载核验来自远端只读队列、进程和实际分片stat，而非浏览器界面。原始两次采样与切换快照保存在根.run/code-streamline-20261019/。此前连接门禁阻断为历史阶段。
