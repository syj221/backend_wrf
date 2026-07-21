from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent


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
    max_concurrent_tasks: int = max(1, min(4, int(os.getenv("WRF_MAX_CONCURRENT_TASKS", "3"))))
    hpc_host: str = os.getenv("WRF_HPC_HOST", "chaosuan").strip()
    hpc_user: str = os.getenv("WRF_HPC_USER", "xjm_shaoyongjin").strip()
    hpc_remote_dir: str = os.getenv("WRF_HPC_REMOTE_DIR", "~/WRFwork").strip().rstrip("/")
    hpc_gfs_dir: str = os.getenv("WRF_HPC_GFS_DIR", "~/Data/gfsdata").strip().rstrip("/")
    hpc_gfs_download_script: str = os.getenv(
        "WRF_HPC_GFS_DOWNLOAD_SCRIPT", "~/Data/gfsdata/download_gfs_00z.sh"
    ).strip()
    hpc_gfs_wait_seconds: int = max(60, int(os.getenv("WRF_HPC_GFS_WAIT_SECONDS", "5400")))
    hpc_gfs_poll_seconds: int = max(5, int(os.getenv("WRF_HPC_GFS_POLL_SECONDS", "30")))
    hpc_gfs_full_min_bytes: int = max(
        10 * 1024 * 1024,
        int(os.getenv("WRF_HPC_GFS_FULL_MIN_BYTES", str(100 * 1024 * 1024))),
    )
    hpc_wps_source_dir: str = os.getenv("WRF_HPC_WPS_SOURCE_DIR", "~/Model/WPSV4.2").strip().rstrip("/")
    hpc_wrf_source_dir: str = os.getenv("WRF_HPC_WRF_SOURCE_DIR", "~/Model/WRFV4.5.2").strip().rstrip("/")
    hpc_geog_dir: str = os.getenv("WRF_HPC_GEOG_DATA_PATH", "/share/data/WPS_GEOG").strip().rstrip("/")
    hpc_auth_mode: str = os.getenv("WRF_HPC_AUTH_MODE", "key").strip().lower()
    hpc_connection_mode: str = os.getenv("WRF_HPC_CONNECTION_MODE", "bastion").strip().lower()
    # 堡垒机默认沿用 wrfautosystem 的同一 TTY/base64 传输通道，避免先走
    # user@host SFTP 而触发堡垒机不支持的 subsystem 认证流程。
    hpc_transfer_mode: str = os.getenv("WRF_HPC_TRANSFER_MODE", "pty").strip().lower()
    hpc_transfer_retries: int = max(0, min(10, int(os.getenv("WRF_HPC_TRANSFER_RETRIES", "5"))))
    hpc_transfer_chunk_kb: int = max(64, min(1024, int(os.getenv("WRF_HPC_TRANSFER_CHUNK_KB", "256"))))
    hpc_transfer_chunk_timeout: int = max(30, min(300, int(os.getenv("WRF_HPC_TRANSFER_CHUNK_TIMEOUT", "90"))))
    hpc_download_chunk_mb: int = max(1, min(32, int(os.getenv("WRF_HPC_DOWNLOAD_CHUNK_MB", "8"))))
    hpc_import_legacy_gfs: bool = _flag("WRF_HPC_IMPORT_LEGACY_GFS", False)
    hpc_key_file: str = os.getenv("WRF_HPC_KEY_FILE", "").strip()
    hpc_password: str = os.getenv("WRF_HPC_PASSWORD", "")
    hpc_server_index: str = os.getenv("WRF_HPC_SERVER_INDEX", "4").strip()
    hpc_account_index: str = os.getenv("WRF_HPC_ACCOUNT_INDEX", "2").strip()
    hpc_poll_seconds: int = max(5, int(os.getenv("WRF_HPC_POLL_SECONDS", "15")))
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
