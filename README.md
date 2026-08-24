# backend_wrf

智慧气象的独立 WRF 微服务。后端保留在本机，GFS 数据与 WPS/WRF 计算通过 SSH 直连 `tx-lab@10.255.248.88:1301` 完成，结果拉回本机后发布 WebP。

## 当前运行拓扑

- 本机：FastAPI、SQLite 任务状态、结果下载、渲染与 `/data/WRF` 发布。
- tx-lab NVMe：`/home/tx-lab/WRFwork/RUNTIME` 存任务运行目录，`/home/tx-lab/WRFwork/DATA/GFS_CHINA` 存中国区域 GFS 数据池。
- 不自动挂载或写入旧 `/mnt/wrf-data`；该设备恢复前保持原状。
- WPS：`/home/tx-lab/WRFwork/WPS/WPS-4.6.0-nvhpc`。
- CPU WRF：`/home/tx-lab/WRFwork/WRF_BUILD/WRF_CPU`。
- WPS_GEOG：`/home/tx-lab/WRFwork/DATA/WPS_GEOG/WPS_GEOG`。
- NVHPC 环境：`/home/tx-lab/WRFwork/env_wrf_nvhpc.sh`。

服务按任务动态创建独立执行线程，不设置应用层并发上限；任务目录、状态和输出彼此隔离，共享 GFS 周期准备与 SSH 会话操作仍通过锁协调。现阶段新建、未启动和重跑任务统一使用 CPU 与 6 小时 spin-up，CPU 默认 4 个 MPI 进程；已经在远端运行的历史任务保留原运行配置并继续对账。

## 首次启用前的人工配置

服务不接收、不保存也不自动填写 tx-lab 密码。密码仅用于管理员人工 SSH 登录和首次安装公钥。

### 1. 系统盘数据池检查

中国区域数据池使用 tx-lab 系统盘。服务启动前确认目标目录可写，且所在文件系统至少有 120 GB 可用空间：

```bash
mkdir -p /home/tx-lab/WRFwork/DATA/GFS_CHINA
findmnt -T /home/tx-lab/WRFwork/DATA/GFS_CHINA
df -h /home/tx-lab/WRFwork/DATA/GFS_CHINA
test -w /home/tx-lab/WRFwork/DATA/GFS_CHINA
```

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
- 任务调度固定为 `dynamic`，不再读取 `WRF_MAX_CONCURRENT_TASKS`。
- `WRF_HPC_HOST` / `WRF_HPC_PORT` / `WRF_HPC_USER`：默认 `10.255.248.88` / `1301` / `tx-lab`。
- `WRF_HPC_CONNECTION_MODE` / `WRF_HPC_AUTH_MODE`：默认 `direct` / `key`。
- `WRF_HPC_KEY_FILE`：默认 `~/.ssh/id_ed25519_wrf_txlab`。
- `WRF_HPC_KNOWN_HOSTS_FILE`：默认 `~/.ssh/known_hosts_wrf_txlab`，严格校验主机密钥。
- `WRF_HPC_REMOTE_DIR`：默认 `/home/tx-lab/WRFwork/RUNTIME`。
- `WRF_HPC_GFS_MOUNT` / `WRF_HPC_GFS_DIR`：默认 `/` / `/home/tx-lab/WRFwork/DATA/GFS_CHINA`。
- `WRF_HPC_GFS_REGION_WEST/EAST/SOUTH/NORTH`：默认 `65` / `145` / `5` / `60`，任务 D01 加 1° 缓冲必须完全落在该范围内。
- `WRF_HPC_GFS_PUBLICATION_LAG_HOURS`：默认 `8`；UTC 00:00–07:59 使用前一日 00Z，避免选择尚未完整发布 f072 的周期。
- `WRF_HPC_GFS_PREFETCH_ENABLED`：默认开启；后端启动后持续检查并触发最新 00Z 的超算后台下载。
- `WRF_HPC_GFS_PREFETCH_INTERVAL_SECONDS` / `WRF_HPC_GFS_PREFETCH_START_DELAY_SECONDS`：默认 `300` / `15` 秒；分别控制后台复查间隔和服务启动后的首次检查延迟。
- `WRF_HPC_GFS_RETAINED_CYCLES`：默认 `2`；最近两个 00Z 与活动任务周期受保护，不进入清理候选。
- `WRF_HPC_GFS_REQUEST_INTERVAL_SECONDS`：默认且最小 `10` 秒，遵守 NOMADS Grib Filter 脚本请求间隔。
- `WRF_HPC_GFS_MIN_SPEED_BPS` / `WRF_HPC_GFS_SLOW_SECONDS`：默认 `65536` B/s / `120` 秒；持续低速时保留断点并重试。
- `WRF_HPC_GFS_WAIT_SECONDS` / `WRF_HPC_GFS_POLL_SECONDS`：默认 `5400` / `30` 秒。
- `WRF_HPC_GFS_DOWNLOAD_WORKERS`：兼容配置，区域下载固定串行，默认 `1`。
- `WRF_HPC_GFS_FULL_MIN_BYTES`：单个区域 GRIB 最小 `1 MiB`。
- `WRF_HPC_GFS_MIN_FREE_GB`：系统盘最低可用空间，默认 `120` GB。
- `WRF_HPC_RUNTIME_MIN_FREE_GB`：启动任务前 NVMe 最低可用空间，默认 `110` GB。
- `WRF_HPC_RUNTIME_PRE_WRF_MIN_FREE_GB`：进入 real/wrf 前最低可用空间，默认 `40` GB。
- `WRF_HPC_CPU_MPI_PROCESSES`：默认 `4`。
- CPU 构建根目录中的可执行文件统一取自标准位置 `main/real.exe` 与 `main/wrf.exe`，不依赖 `run/` 软链接。
- 新建和重跑接口不再接收运行配置、预报关注点与 spin-up 选项；后端统一持久化为 CPU、通用预报和 6 小时 spin-up。

## GFS 与数据保留

工作台使用 NOAA NOMADS Grib Filter，按 `65–145°E、5–60°N` 直接获取 0.25°、全部层次和变量的 f000–f072，共 73 个区域 GRIB。请求严格串行且间隔至少 10 秒，使用 `.part.nomads` 断点文件；校验 GRIB 首尾标记和最小大小后再原子落盘。同一时刻只允许一个共享下载进程。

后端默认每 5 分钟检查最近两个可用 00Z，并按从旧到新的顺序在 tx-lab 端以 `nohup` 补齐。已经触发的下载由 tx-lab 进程持有，关闭网页或短暂重启本地后端不会中断；本地后端恢复后会继续发现和触发后续新周期。任务处于 GFS 准备阶段时，后台周期预取暂停让路；任务历史周期只下载实际需要的时次，后台保留周期则完整预取 f000–f072。系统盘低于保护阈值时停止新下载。可通过健康接口的 `gfs.prefetch` 查看最近一次后台检查状态。

远端共享数据池始终保留上述 73 个逐小时文件；单个 WRF 任务可独立选择 `1/3/6` 小时边界场间隔，默认 `1` 小时。任务只校验和链接所选间隔对应的预报时次，`interval_seconds` 与该选择保持一致。

同步最新 00Z 或任务准备数据时，系统会自动清理严格早于目标周期的受管 GFS 周期目录，然后重试一次下载。目标周期、更新周期、下载中的周期以及活动任务正在使用的周期不会自动删除；不满足自动清理条件的路径仍需在界面人工确认。远端任务断点、wrfout、历史运行结果和本地已发布产品始终保留，取消或重跑任务也不会删除这些数据。

生产反向代理应将 `/api/wrf` 与 `/data/WRF` 转发到本服务。GFS、wrfout、WebP 结果及密钥不得提交到 Git。
