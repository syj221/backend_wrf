# backend_wrf

智慧气象的独立 WRF 微服务。后端保留在本机，GFS 数据与 WPS/WRF 计算通过 SSH 直连 `tx-lab@10.255.248.88:1301` 完成，结果拉回本机后发布 WebP。

## 当前运行拓扑

- 本机：FastAPI、SQLite 任务状态、结果下载、渲染与 `/data/WRF` 发布。
- tx-lab 数据卷通过 NTFS UUID `7EEE64E3EE6494E5` 定位，并在远端用户目录建立稳定别名 `/home/tx-lab/.wrf-data`。任务运行目录为 `/home/tx-lab/.wrf-data/WRFwork/RUNTIME`，GFS 与 ECMWF 预报数据池分别位于 `/home/tx-lab/.wrf-data/WRFwork/DATA/GFS` 和 `/home/tx-lab/.wrf-data/WRFwork/DATA/ECMWF`。
- 不依赖 `/media/tx-lab/新加卷N` 这类桌面自动挂载路径；服务每次远端命令执行前按 UUID 更新稳定别名。远端数据卷的 `WRFwork/.wrf-data-root` 标记文件必须存在，未挂载或别名冲突时直接停止。
- WPS：`/home/tx-lab/WRFwork/WPS/WPS-4.6.0-nvhpc`。
- CPU WRF：`/home/tx-lab/WRFwork/WRF_BUILD/WRF_CPU`。
- WPS_GEOG：`/home/tx-lab/WRFwork/DATA/WPS_GEOG/WPS_GEOG`。
- NVHPC 环境：`/home/tx-lab/WRFwork/env_wrf_nvhpc.sh`。

服务按任务动态创建独立执行线程，不设置应用层并发上限；任务目录、状态和输出彼此隔离，共享 GFS 周期准备与 SSH 会话操作仍通过锁协调。现阶段新建、未启动和重跑任务统一使用 CPU 与 6 小时 spin-up，CPU 默认 4 个 MPI 进程；已经在远端运行的历史任务保留原运行配置并继续对账。

## 首次启用前的人工配置

服务不接收、不保存也不自动填写 tx-lab 密码。密码仅用于管理员人工 SSH 登录和首次安装公钥。

### 1. 系统盘数据池检查

服务启动前确认目标数据卷可写，且所在文件系统至少有 120 GB 可用空间：

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

系统 cron 应调用 `scripts/wrf_server_cron.sh`，由它按 UUID 更新 `/home/tx-lab/.wrf-data`，并把数据、运行目录与共享 WRF/WPS 程序分开指定。

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
python -m pip install -r requirements.txt
export JWT_SECRET="与 backend_auth 一致的密钥"
python main.py
```

默认监听 `http://127.0.0.1:8007`，接口文档位于 `/docs`。完整重启仍使用项目根目录的 `./stop.sh` 和 `./start.sh`；不要停止归属不明的端口进程。

## 主要配置

- `WRF_PORT`：默认 `8007`。
- `WRF_CATALOG_PUBLISH_ENABLED`：默认开启；完整成功任务会把 WebP 与 `scene.meta.json` 发布到统一数据目录，部分结果不会登记。
- `WRF_CATALOG_DATA_ROOT`：默认指向工作区 `backend_system/data`。统一目录发布失败只记录在任务的 `catalog_publication` 子状态，可通过 `POST /api/wrf/tasks/{task_id}/publish` 重试，不会重跑 WPS/WRF。
- 任务调度固定为 `dynamic`，不再读取 `WRF_MAX_CONCURRENT_TASKS`。
- `WRF_HPC_HOST` / `WRF_HPC_PORT` / `WRF_HPC_USER`：默认 `10.255.248.88` / `1301` / `tx-lab`。
- `WRF_HPC_CONNECTION_MODE` / `WRF_HPC_AUTH_MODE`：默认 `direct` / `key`。
- `WRF_HPC_KEY_FILE`：默认 `~/.ssh/id_ed25519_wrf_txlab`。
- `WRF_HPC_KNOWN_HOSTS_FILE`：默认 `~/.ssh/known_hosts_wrf_txlab`，严格校验主机密钥。
- `WRF_HPC_STORAGE_UUID` / `WRF_HPC_STORAGE_ALIAS`：默认 `7EEE64E3EE6494E5` / `/home/tx-lab/.wrf-data`；服务按 UUID 解析当前挂载点并更新别名。
- `WRF_HPC_REMOTE_DIR`：默认 `/home/tx-lab/.wrf-data/WRFwork/RUNTIME`。
- `WRF_HPC_GFS_MOUNT` / `WRF_HPC_GFS_DIR`：默认 `/home/tx-lab/.wrf-data` / `/home/tx-lab/.wrf-data/WRFwork/DATA/GFS`。
- `WRF_HPC_GFS_REGION_WEST/EAST/SOUTH/NORTH`：默认 `65` / `145` / `5` / `60`，任务 D01 加 1° 缓冲必须完全落在该范围内。
- `WRF_HPC_GFS_PREFETCH_ENABLED`：默认开启；8007 存活期间持续检查并触发 GFS、ECMWF 最新 00Z 的超算后台预取。
- `WRF_HPC_GFS_PREFETCH_INTERVAL_SECONDS` / `WRF_HPC_GFS_PREFETCH_START_DELAY_SECONDS`：默认 `300` / `15` 秒；分别控制后台复查间隔和服务启动后的首次检查延迟。
- `WRF_HPC_GFS_RETAINED_CYCLES`：默认 `2`；最近两个名义周期、最近两个完整回退周期与活动任务周期受保护，不进入清理候选。
- `WRF_HPC_GFS_REQUEST_INTERVAL_SECONDS`：默认且最小 `10` 秒，遵守 NOMADS Grib Filter 脚本请求间隔。
- `WRF_HPC_GFS_MIN_SPEED_BPS` / `WRF_HPC_GFS_SLOW_SECONDS`：默认 `65536` B/s / `120` 秒；持续低速时保留断点并重试。
- `WRF_HPC_GFS_POLL_SECONDS`：默认 `30` 秒；任务等待共享队列的必需文件，不再按固定分钟数超时失败。
- `WRF_POOL_RETRY_MAX_ATTEMPTS` / `WRF_POOL_RETRY_MAX_SECONDS`：默认 `8` 次 / `900` 秒；临时网络错误采用退避重试，达到单轮上限后延迟开启下一轮。
- `WRF_POOL_HISTORY_RETENTION_DAYS`：默认 `30` 天；失败和清理摘要保留期限，GRIB、`.part` 和失败周期目录不在此期限内长期保留。
- `WRF_HPC_GFS_DOWNLOAD_WORKERS`：兼容配置，区域下载固定串行，默认 `1`。
- `WRF_HPC_GFS_FULL_MIN_BYTES`：单个区域 GRIB 最小 `1 MiB`。
- `WRF_HPC_GFS_MIN_FREE_GB`：GFS 数据卷最低可用空间，默认 `120` GB。
- `WRF_HPC_RUNTIME_MIN_FREE_GB`：启动任务前 NVMe 最低可用空间，默认 `110` GB。
- `WRF_HPC_RUNTIME_PRE_WRF_MIN_FREE_GB`：进入 real/wrf 前最低可用空间，默认 `40` GB。
- `WRF_HPC_DOWNSCALE_INTERFACE_ROOT`：CFD 接口工具目录，默认 `/home/tx-lab/WRFwork/CFD/interface`。
- `WRF_HPC_DOWNSCALE_CFD_TEMPLATE`：受管 OpenFOAM 最小模板，默认 `/home/tx-lab/WRFwork/CFD/templates/thermal-rans-graded-v2`。
- `WRF_HPC_DOWNSCALE_OPENFOAM_ACTIVATE`：统一 OpenFOAM 环境入口，默认 `/home/tx-lab/WRFwork/CFD/activate.sh`；OpenFOAM 和 `foamgeo` 环境位于 `/home/tx-lab/WRFwork/ENV`。
- `WRF_HPC_DOWNSCALE_PYTHON`：CFD 后处理解释器，默认 `/home/tx-lab/WRFwork/ENV/wrfpost/bin/python`。
- `WRF_HPC_CPU_MPI_PROCESSES`：默认 `4`。
- CPU 构建根目录中的可执行文件统一取自标准位置 `main/real.exe` 与 `main/wrf.exe`，不依赖 `run/` 软链接。
- 新建和重跑接口不再接收运行配置、预报关注点与 spin-up 选项；后端统一持久化为 CPU、通用预报和 6 小时 spin-up。
- 普通 WRF 业务任务默认每 60 分钟写出 `wrfrst_d*`，并保留同一时段的 `wrfout_d*`。高分任务提交时按权限核对普通任务的最细域为 1 km，再通过远端原生文件清单完成交接；仅有 WebP、`scene.meta.json` 或任务数据库记录不能作为高分初始化场。
- 高分任务由本机 `downscale/runner.py` 串行调度，远端入口为 `scripts/wrf_hpc_downscale.sh`。完整链路提交时自动创建或复用固定 27 km→9 km→3 km→1 km 普通 WRF 上游，并先检查 d04 原生 `wrfout/wrfrst`；随后由 `wrf_hpc_downscale_chain.py` 按 `WPS → real → d04→d05→d06→d07→d08 的 NDOWN/WRF-LES` 重建 12.345679 m forcing。forcing 时次由任务起止时间按小时闭区间生成（默认 48 小时即 49 个时次），每一步在任务专属目录写入绑定合同、拓扑、输入清单和二进制校验的 `.done` 标记，服务重启后只恢复完全匹配的阶段；历史 34.293556 m forcing 仅保留为兼容资产。
- 第二阶段从已通过质量门的 forcing 任务结果页创建，使用 `upstream_mode=reuse_10m` 和 `source_forcing_task_id`。`wrf_hpc_cfd_10m_chain.py` 会按同一任务的动态时间窗口重新核对 d08 manifest、文件大小/SHA-256/修改时间、TKE/QKE 和数据盘路径，再按 RANS 独立快照或 LES 有限统计时段建立独立 OpenFOAM case；产品只发布 1 m 采样的 CF-NetCDF、GeoTIFF、VTK 和轻量浏览图层，不声明连续 1 m 预报。旧 34 m CFD 链路继续由 `wrf_hpc_cfd_chain.py` 兼容处理。
- `WRF_DOWNSCALE_RUN_CHAIN` 由后端远端启动命令固定设置为 `true`；仅做源文件预检时不设置该变量，入口会在交接检查后退出，不会启动高分计算。

## GFS 与数据保留

工作台使用 NOAA NOMADS GFS 与 ECMWF Open Data 的全球 0.25° 资料。GFS 每个完整 00Z 周期包含逐小时 `f000–f072` 共 73 个 GRIB；ECMWF Open Data 仅发布 3 小时边界场，即 `f000、f003…f072` 共 25 个 GRIB。每个数据源各有一个持久化串行队列，保留 `.part` 断点、逐文件进度和重试状态；校验 GRIB 首尾标记和最小大小后再原子落盘。

后端默认每 5 分钟持续检查最近两个 00Z；每轮都会重新检查 tx-lab 连接，因此夜间网络恢复或新周期发布后会自行继续。队列先探测 `f072`，仅对已发布周期补齐完整 `f000–f072`；活动 WRF 任务的必需时次始终优先于后台预取。已经触发的下载由 tx-lab 队列持有，关闭网页或短暂重启 8007 不会丢失进度；8007 恢复后会继续发现和触发后续新周期。可通过健康接口的 `gfs.prefetch` 查看最近一次后台检查状态。

当 GFS 或运行区剩余空间低于安全阈值时，健康状态标记为 `storage_low`，后台预取自动暂停并继续周期性复查；接口仍会展示已有周期和实际可用空间，不会把空间不足误报为缺少环境变量。扩容或释放空间达到阈值后，预取会自动恢复。

远端共享数据池默认保留两个最新完整回退周期；GFS 任务可选择 `1/3/6` 小时边界场间隔，ECMWF 任务固定使用 3 小时边界场。任务只校验和链接所选间隔对应的预报时次，`interval_seconds` 与该选择保持一致。最新周期尚未补齐时，只要仍有完整回退周期，超出保留窗口的旧完整周期仍会自动清理。

生成 WRF namelist 时，外层 `time_step` 会在保守的 `5×dx(km)` CFL 上限内自动选择能够整除驱动资料间隔的最大值。这样 GFS 1 小时资料在 27 km 外层网格上使用 120 秒时间步，避免 FDDA 在 `01:00:45` 等非整点寻找 `01:00:00` 输入而退出；ECMWF 3 小时资料仍可使用 135 秒时间步。

同步最新 00Z 或任务准备数据时，系统会立即清理不再可恢复的失败/空周期残留；完整旧周期在完整回退窗口之外自动清理。目标周期、下载中的周期、完整回退周期以及活动任务正在使用的周期不会自动删除；不满足自动清理条件的路径仍需在界面人工确认。远端任务断点、wrfout、历史运行结果和本地已发布产品始终保留，取消或重跑任务也不会删除这些数据。

生产反向代理应将 `/api/wrf` 与 `/data/WRF` 转发到本服务。GFS、wrfout、WebP 结果及密钥不得提交到 Git。
