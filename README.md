# backend_wrf

智慧气象的独立 WRF 微服务，负责调度超算共享 GFS 0.25° 完整文件池、超算 WPS/WRF 并行任务和 WebP 结果发布。

## 启动

```bash
python -m pip install -r requirements.txt
export JWT_SECRET="与 backend_auth 一致的密钥"
export WRF_HPC_HOST="chaosuan"
export WRF_HPC_USER="xjm_shaoyongjin"
export WRF_HPC_CONNECTION_MODE="bastion"
export WRF_HPC_AUTH_MODE="key"
export WRF_HPC_KEY_FILE="/path/to/private_key" # 使用 ssh-agent 时可省略
python main.py
```

默认监听 `http://127.0.0.1:8007`，接口文档位于 `/docs`。服务只支持 GFS 驱动与超算执行，任务采用 SQLite 持久化和最多 3 个工作线程并行调度。

未配置可用密钥时，工作台会在提交任务前提示输入超算密码。密码只保存在
`backend_wrf` 当前进程内存中，不会写入 SQLite、任务配置或日志；服务重启后需重新认证。
堡垒机模式保持一个串行复用的 `pexpect` TTY，会自动完成服务器与账号菜单选择；
根目录的 `start.sh` 会在变更任何服务进程前检查并自动安装 `pexpect`。
项目不再提供单独的 WRF 重启脚本。需要完整重启时，在根目录依次执行
`./stop.sh` 和 `./start.sh`；不要停止归属不明的端口进程。

## 主要配置

- `WRF_PORT`：默认 `8007`
- `WRF_MAX_CONCURRENT_TASKS`：完整任务并行上限，默认 `3`，允许 `1-4`
- `WRF_RUN_DATA_DIR`：任务、日志与原始 wrfout 根目录
- `WRF_HPC_REMOTE_DIR`：超算 WRF 工作根目录，默认 `~/WRFwork`
- `WRF_HPC_GFS_DIR`：超算 GFS 缓存根目录，默认 `~/Data/gfsdata`
- `WRF_HPC_GFS_DOWNLOAD_SCRIPT`：超算完整 GFS 下载脚本，默认 `~/Data/gfsdata/download_gfs_00z.sh`，调用契约为 `脚本 YYYYMMDD`；脚本自身固定下载该日 00Z 的 f000-f072
- `WRF_HPC_GFS_WAIT_SECONDS`：任务等待共享周期补齐的上限，默认 `5400` 秒
- `WRF_HPC_GFS_POLL_SECONDS`：共享数据池轮询间隔，默认 `30` 秒
- `WRF_HPC_GFS_FULL_MIN_BYTES`：拒绝旧筛选文件的单文件最小大小，默认 `100 MiB`
- `WRF_HPC_WPS_SOURCE_DIR` / `WRF_HPC_WRF_SOURCE_DIR`：超算 WPS/WRF 安装目录
- `WRF_HPC_GEOG_DATA_PATH`：超算 WPS_GEOG 目录
- `WRF_HPC_CONNECTION_MODE`：默认 `bastion`；仅直连计算节点时设为 `direct`
- `WRF_HPC_TRANSFER_MODE`：默认 `pty`，沿用 `wrfautosystem` 的堡垒机 TTY/base64 断点传输；仅在确认堡垒机开放 SFTP subsystem 时才显式设为 `auto` 或 `sftp`
- `WRF_HPC_TRANSFER_RETRIES`：PTY 传输中断后的自动重连续传次数，默认 `5`
- `WRF_HPC_TRANSFER_CHUNK_KB`：PTY 单块大小，默认 `256` KiB
- `WRF_HPC_TRANSFER_CHUNK_TIMEOUT`：PTY 单块确认超时，默认 `90` 秒
- `WRF_HPC_DOWNLOAD_CHUNK_MB`：wrfout 经堡垒机回传的断点块大小，默认 `8` MiB，可在 `1`–`32` MiB 间调整
- `WRF_HPC_IMPORT_LEGACY_GFS`：兼容保留、默认 `0`；完整文件模式不会凭文件头、大小和 SHA256 导入来源不明的旧筛选文件
- `WRF_HPC_SERVER_INDEX`：堡垒机服务器菜单选项，默认 `4`（`log04 / 172.18.1.178`）
- `WRF_HPC_ACCOUNT_INDEX`：堡垒机账户菜单选项，默认 `2`（`self`）
- `WRF_HPC_SHELL_READY_TIMEOUT`：选择计算节点后等待 Shell 就绪的秒数，默认 `60`
- `WRF_HPC_CONNECT_RETRIES`：堡垒机或计算节点 Shell 初始化总尝试次数，默认 `3`
- `WRF_HPC_CONNECT_RETRY_DELAY_SECONDS`：连接重试的基础等待秒数，默认 `2`
- `WRF_HPC_AUTH_MODE`：推荐 `key`；兼容工作台临时密码认证或服务端环境变量密码

GFS GRIB 统一保存在超算共享数据池。进入工作台并完成临时密码认证后，服务会幂等
触发 UTC 当天及前一天两个 00Z 周期补齐至 f072；任务只在超算周期目录检查 GRIB
首尾标记、完整文件最小大小、Manifest 和 SHA256，并让同周期任务共享下载进程。
取消某个 WRF 任务只结束该任务的等待，不会杀死共享下载。小型 `gfs.expected.tsv`
随任务配置提交到超算，`wrfout` 完成后再拉回本机渲染。

工作台显示超算周期的文件数、容量、状态和绝对路径。仅当最近两个目标周期均达到
73/73 时，旧周期才会成为清理候选；下载中周期和运行中任务使用的周期始终受保护。
远端清理必须由用户在界面核对精确路径并当次确认，不会在认证或服务启动时静默执行。

新任务可显式选择 `off/auto/custom` spin-up。`start_time` 始终表示产品起报时刻，
模型从更早的 `model_start` 开始，`history_begin` 保证产品只发布起报后的帧。自动
规则通常选择 6 小时；强对流、城市、降雪、≤3 km 网格或复杂地形建议 12 小时。
FDDA 仅在 spin-up 阶段作用于 ≥9 km 域，不松弛水汽和边界层，结束前 60 分钟渐退。

工作台通过 `POST /api/wrf/recommendations` 和对应 GET 接口在超算运行/复用 WPS
`geogrid`，根据地形、陆水/城市比例、经纬度、季节、关注点和网格距给出可确认的
规则建议。输出下载前会在超算执行 NetCDF 完整性检查；默认严格失败，也可由用户
确认忽略坏帧生成 `partial_success`，质量告警、排除帧和缺失时次写入 `scene.meta.json`。

生产反向代理应将 `/api/wrf` 与 `/data/WRF` 转发到本服务。GFS、wrfout 和 WebP 结果均不应提交到 Git。
