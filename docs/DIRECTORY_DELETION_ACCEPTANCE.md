# 固定目录删除验收（2026-10-13）

已通过原维护入口安全切换；真实多任务删除仍由用户发起验收。本轮没有重发失败清单，没有调用真实任务/资料删除接口进行测试。

## 实现与边界

- 新任务删除计划、直接删除、单周期删除及新资料批量计划只读取固定根的设备/inode/所有权，不遍历数据文件、不读取数据内容、不计算逐文件哈希。SHA仅用于任务状态/计划JSON摘要。已经保存的旧清单保留原校验合同；退役清单及共享源码保护不变。
- 根目录、受管任务编号、原尝试终态及任务/下载引用继续校验；正式产品撤销且认证静态访问拒绝后才能移走目录。现代任务移除整个任务根，历史目录保持原精确路径白名单。配置别名规范化后去重，不能删除共享资料根。
- 同一父目录下原子移入`.wrf-trash/<operation_id>/<原名称>`，先持久化移动意图，再执行不重试的写操作。根符号链接、其他用户所有、不安全待删除区或替换inode均拒绝；Linux通过mountinfo拒绝挂载子树。后台清理使用固定父目录描述符，不跟随内部链接，不碰原路径新建资产。
- 原移除FIFO保持容量8；独立共用串行空间清理工作器避免磁盘争抢，并允许后续任务先移走目录。资料复用不可变发布中PoolQueue原worker/state锁及队列登记，不部署或覆盖远端脚本；每个周期独立保留移动审计。
- 移除全部目录、撤销产品和移除任务记录后标记removed，提交幂等墓碑保留；空间清理终态另行记录。重启/未知响应不重放；已移除而空间清理异常不能报告为原任务仍在。原GET/POST兼容增加removed、removed_at、cleanup_status，任务另含task_label；无新表或共享DB迁移。
- 控制命令30秒；远端清理单次最多1800秒，清理预算在实际清理开始时创建，不受排队消耗。只读检查允许原有限重试；写命令不重试。生产Python 3.6使用procfs固定父目录描述符路径调用安全rmtree，本机新Python使用dir_fd。

## 验证

最终后端136项通过：directory_delete、deletion_queue、workbench_simplification、task_deletion、forcing_cleanup、retirement、manual_operations、forcing_reference_protection、forcing_policy、pool_resilience、studio_lifecycle、task_manager_safety、wrf_access。维护入口11项通过；浏览器6项通过，涵盖原队列耗时、页面刷新恢复、旧服务兼容、结果页恢复以及已移除任务/资料的后台清理状态。前端构建通过，保留现有第三方PURE注释及大chunk提示。

新增10项目录回归覆盖：不读文件内容、内部链接原件保护、原位置重建、替换目录/链接/不安全待删除区、远端桥接与禁止重放、缺失父目录、资料原下载器锁和队列审计、慢清理不阻塞后续移除、新批量计划、完整任务事务及重启已移除状态。测试输入和SQLite均隔离，不接入真实运行数据库。

远端两次合成验证均通过并在finally清理专用目录：

- `/tmp/wrf-quarantine-validation-1viap50r`
- `/tmp/wrf-quarantine-final-validation-vhg_9org`

最终版本实际验证了Python 3.6目录移动/安全清理和不可变发布原PoolQueue锁桥接。未验证真实资产删除的整体性能，不能将202受理或目录移走称为空间释放完成。

## 切换证据

首次只读维护因旧API有执行中的SSH子进程而拒绝，未强停或改记录。原用户删除队列自然结束后，只读预检通过；`scripts/wrf_maintenance.py --execute --maintenance-window`温和重载完成。新WRF PID 30028、Agent API PID 30054；8007/8012正确健康入口200。普通活动0、计算队列0、高分活动false；其他服务及Redis身份保持。实际5177返回含空间清理状态的当前Vue源码；浏览器交互验收是隔离测试，未宣称完成真实管理员删除操作。

私有证据 `/private/tmp/wrf-directory-switch-_uk7c42w.json` 保存切换前后服务身份、任务/删除事务与源码指纹。JSON序列化规范化后任务记录及删除事务逐项一致。既有自动策略产生 `cycle_delete_4a222f73347442aa94080b5534ba7009`（system、GFS 2026100600），保护检查阻止，未移除周期。未启用新策略或改写冲突启动证据。

## 产物与文档

以下仅本次生成的验收产物，保留供核对；确认不再需要后按精确路径清理，不批量扩大范围。隔离测试TemporaryDirectory与远端合成目录已自动收尾。

| 路径 | 文件数 | 字节 | 用途 |
| --- | ---: | ---: | --- |
| `/private/tmp/wrf-quarantine-build-20261013` | 10 | 3869799 | 首轮构建 |
| `/private/tmp/wrf-quarantine-final-build-20261013` | 10 | 3870311 | 最终构建 |
| `/private/tmp/wrf-quarantine-browser-20261013` | 1 | 45 | 首轮浏览器状态 |
| `/private/tmp/wrf-quarantine-final-browser-20261013` | 1 | 45 | 最终浏览器状态 |
| `/private/tmp/wrf-directory-switch-_uk7c42w.json` | 1 | 7685946 | 0600私有切换证据 |

根AGENTS.md、README.md及backend_wrf、webpage_sys README已同步新目录路径、状态字段、兼容链路和实际切换结果。核对了八仓库分支、原启停服务清单及监听入口；服务职责、端口和启动方式未变。本轮结构性变化为待删除目录及兼容API状态扩展。
