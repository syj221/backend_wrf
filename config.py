from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


@dataclass(frozen=True)
class Settings:
    host: str = os.getenv("WRF_HOST", "127.0.0.1")
    port: int = int(os.getenv("WRF_PORT", "8007"))
    data_dir: Path = Path(os.getenv("WRF_DATA_DIR", BASE_DIR / "data")).expanduser().resolve()
    run_dir: Path = Path(os.getenv("WRF_RUN_DATA_DIR", BASE_DIR / "data" / "runs")).expanduser().resolve()
    database_path: Path = Path(os.getenv("WRF_DATABASE_PATH", BASE_DIR / "data" / "wrf_tasks.sqlite3")).expanduser().resolve()
    hpc_host: str = os.getenv("WRF_HPC_HOST", "10.255.248.88").strip()
    hpc_port: int = max(1, min(65535, int(os.getenv("WRF_HPC_PORT", "1301"))))
    hpc_user: str = os.getenv("WRF_HPC_USER", "tx-lab").strip()
    hpc_remote_dir: str = os.getenv(
        "WRF_HPC_REMOTE_DIR", "/home/tx-lab/WRFwork/RUNTIME"
    ).strip().rstrip("/")
    hpc_gfs_dir: str = os.getenv(
        "WRF_HPC_GFS_DIR", "/home/tx-lab/WRFwork/DATA/GFS_CHINA"
    ).strip().rstrip("/")
    hpc_gfs_download_script: str = os.getenv(
        "WRF_HPC_GFS_DOWNLOAD_SCRIPT",
        "/home/tx-lab/WRFwork/RUNTIME/backend_wrf_service/download_gfs_00z.sh",
    ).strip()
    hpc_gfs_wait_seconds: int = max(60, int(os.getenv("WRF_HPC_GFS_WAIT_SECONDS", "5400")))
    hpc_gfs_poll_seconds: int = max(5, int(os.getenv("WRF_HPC_GFS_POLL_SECONDS", "30")))
    hpc_gfs_download_workers: int = max(
        1, min(8, int(os.getenv("WRF_HPC_GFS_DOWNLOAD_WORKERS", "1")))
    )
    hpc_gfs_mirror_probe_seconds: int = max(
        3, min(30, int(os.getenv("WRF_HPC_GFS_MIRROR_PROBE_SECONDS", "12")))
    )
    hpc_gfs_publication_lag_hours: int = max(
        0, min(24, int(os.getenv("WRF_HPC_GFS_PUBLICATION_LAG_HOURS", "8")))
    )
    hpc_gfs_prefetch_enabled: bool = _flag("WRF_HPC_GFS_PREFETCH_ENABLED", True)
    hpc_gfs_prefetch_interval_seconds: int = max(
        60, int(os.getenv("WRF_HPC_GFS_PREFETCH_INTERVAL_SECONDS", "300"))
    )
    hpc_gfs_prefetch_start_delay_seconds: int = max(
        0, int(os.getenv("WRF_HPC_GFS_PREFETCH_START_DELAY_SECONDS", "15"))
    )
    hpc_gfs_retained_cycles: int = max(
        1, min(14, int(os.getenv("WRF_HPC_GFS_RETAINED_CYCLES", "2")))
    )
    hpc_gfs_region_west: float = float(os.getenv("WRF_HPC_GFS_REGION_WEST", "65"))
    hpc_gfs_region_east: float = float(os.getenv("WRF_HPC_GFS_REGION_EAST", "145"))
    hpc_gfs_region_south: float = float(os.getenv("WRF_HPC_GFS_REGION_SOUTH", "5"))
    hpc_gfs_region_north: float = float(os.getenv("WRF_HPC_GFS_REGION_NORTH", "60"))
    hpc_gfs_request_interval_seconds: int = max(
        10, int(os.getenv("WRF_HPC_GFS_REQUEST_INTERVAL_SECONDS", "10"))
    )
    hpc_gfs_min_speed_bps: int = max(
        1024, int(os.getenv("WRF_HPC_GFS_MIN_SPEED_BPS", str(64 * 1024)))
    )
    hpc_gfs_slow_seconds: int = max(
        30, int(os.getenv("WRF_HPC_GFS_SLOW_SECONDS", "120"))
    )
    hpc_gfs_full_min_bytes: int = max(
        64 * 1024,
        int(os.getenv("WRF_HPC_GFS_FULL_MIN_BYTES", str(1024 * 1024))),
    )
    hpc_ec_dir: str = os.getenv(
        "WRF_HPC_EC_DIR", "/home/tx-lab/WRFwork/DATA/ECMWF_CHINA"
    ).strip().rstrip("/")
    hpc_ec_download_script: str = os.getenv(
        "WRF_HPC_EC_DOWNLOAD_SCRIPT",
        "/home/tx-lab/WRFwork/RUNTIME/backend_wrf_service/download_ecmwf_00z.sh",
    ).strip()
    hpc_ec_wait_seconds: int = max(
        60, int(os.getenv("WRF_HPC_EC_WAIT_SECONDS", str(hpc_gfs_wait_seconds)))
    )
    hpc_ec_poll_seconds: int = max(
        5, int(os.getenv("WRF_HPC_EC_POLL_SECONDS", str(hpc_gfs_poll_seconds)))
    )
    hpc_ec_full_min_bytes: int = max(
        64 * 1024,
        int(os.getenv("WRF_HPC_EC_FULL_MIN_BYTES", str(1024 * 1024))),
    )
    hpc_wps_source_dir: str = os.getenv(
        "WRF_HPC_WPS_SOURCE_DIR", "/home/tx-lab/WRFwork/WPS/WPS-4.6.0-nvhpc"
    ).strip().rstrip("/")
    hpc_wrf_source_dir: str = os.getenv(
        "WRF_HPC_WRF_SOURCE_DIR", "/home/tx-lab/WRFwork/WRF_BUILD/WRF_CPU"
    ).strip().rstrip("/")
    hpc_wrf_gpu_source_dir: str = os.getenv(
        "WRF_HPC_WRF_GPU_SOURCE_DIR", "/home/tx-lab/WRFwork/WRF_BUILD/WRF_GPU"
    ).strip().rstrip("/")
    hpc_geog_dir: str = os.getenv(
        "WRF_HPC_GEOG_DATA_PATH", "/home/tx-lab/WRFwork/DATA/WPS_GEOG/WPS_GEOG"
    ).strip().rstrip("/")
    hpc_runtime_env: str = os.getenv(
        "WRF_HPC_RUNTIME_ENV", "/home/tx-lab/WRFwork/env_wrf_nvhpc.sh"
    ).strip()
    hpc_gfs_mount: str = os.getenv("WRF_HPC_GFS_MOUNT", "/").strip().rstrip("/") or "/"
    hpc_gfs_min_free_gb: int = max(1, int(os.getenv("WRF_HPC_GFS_MIN_FREE_GB", "120")))
    hpc_runtime_min_free_gb: int = max(1, int(os.getenv("WRF_HPC_RUNTIME_MIN_FREE_GB", "110")))
    hpc_runtime_pre_wrf_min_free_gb: int = max(
        1, int(os.getenv("WRF_HPC_RUNTIME_PRE_WRF_MIN_FREE_GB", "40"))
    )
    hpc_cpu_mpi_processes: int = max(1, int(os.getenv("WRF_HPC_CPU_MPI_PROCESSES", "4")))
    hpc_gpu_mpi_processes: int = max(1, min(2, int(os.getenv("WRF_HPC_GPU_MPI_PROCESSES", "1"))))
    hpc_auth_mode: str = os.getenv("WRF_HPC_AUTH_MODE", "key").strip().lower()
    hpc_connection_mode: str = os.getenv("WRF_HPC_CONNECTION_MODE", "direct").strip().lower()
    # 堡垒机默认沿用 wrfautosystem 的同一 TTY/base64 传输通道，避免先走
    # user@host SFTP 而触发堡垒机不支持的 subsystem 认证流程。
    hpc_transfer_mode: str = os.getenv("WRF_HPC_TRANSFER_MODE", "auto").strip().lower()
    hpc_transfer_retries: int = max(0, min(10, int(os.getenv("WRF_HPC_TRANSFER_RETRIES", "5"))))
    hpc_transfer_chunk_kb: int = max(64, min(1024, int(os.getenv("WRF_HPC_TRANSFER_CHUNK_KB", "256"))))
    hpc_transfer_chunk_timeout: int = max(30, min(300, int(os.getenv("WRF_HPC_TRANSFER_CHUNK_TIMEOUT", "90"))))
    hpc_download_chunk_mb: int = max(1, min(32, int(os.getenv("WRF_HPC_DOWNLOAD_CHUNK_MB", "8"))))
    hpc_import_legacy_gfs: bool = _flag("WRF_HPC_IMPORT_LEGACY_GFS", False)
    hpc_key_file: str = os.getenv(
        "WRF_HPC_KEY_FILE", str(Path.home() / ".ssh" / "id_ed25519_wrf_txlab")
    ).strip()
    hpc_known_hosts_file: str = os.getenv(
        "WRF_HPC_KNOWN_HOSTS_FILE", str(Path.home() / ".ssh" / "known_hosts_wrf_txlab")
    ).strip()
    hpc_password: str = os.getenv("WRF_HPC_PASSWORD", "")
    hpc_server_index: str = os.getenv("WRF_HPC_SERVER_INDEX", "4").strip()
    hpc_account_index: str = os.getenv("WRF_HPC_ACCOUNT_INDEX", "2").strip()
    hpc_poll_seconds: int = max(5, int(os.getenv("WRF_HPC_POLL_SECONDS", "15")))
    hpc_reconcile_interval_seconds: int = max(
        5, int(os.getenv("WRF_HPC_RECONCILE_INTERVAL_SECONDS", "30"))
    )
    hpc_reconcile_timeout_seconds: int = max(
        60, int(os.getenv("WRF_HPC_RECONCILE_TIMEOUT_SECONDS", "1800"))
    )
    hpc_connect_timeout: int = max(5, int(os.getenv("WRF_HPC_CONNECT_TIMEOUT", "20")))
    hpc_shell_ready_timeout: int = max(10, int(os.getenv("WRF_HPC_SHELL_READY_TIMEOUT", "60")))
    hpc_connect_retries: int = max(1, min(10, int(os.getenv("WRF_HPC_CONNECT_RETRIES", "3"))))
    hpc_connect_retry_delay_seconds: float = max(
        0.0,
        min(30.0, float(os.getenv("WRF_HPC_CONNECT_RETRY_DELAY_SECONDS", "2"))),
    )

    @property
    def output_dir(self) -> Path:
        return self.data_dir / "WRF"

    @property
    def cors_origins(self) -> list[str]:
        raw = os.getenv(
            "CORS_ORIGINS",
            "http://localhost:5177,http://127.0.0.1:5177,"
            "http://localhost:5178,http://127.0.0.1:5178,"
            "http://localhost:5173,http://127.0.0.1:5173",
        )
        return [item.strip() for item in raw.split(",") if item.strip()]

    def ensure_directories(self) -> None:
        for path in (self.data_dir, self.run_dir, self.output_dir, self.database_path.parent):
            path.mkdir(parents=True, exist_ok=True)


settings = Settings()
