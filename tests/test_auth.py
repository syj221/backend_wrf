from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from auth import JWT_SECRET, decode_hs256
from config import settings


def _segment(value: dict) -> str:
    raw = json.dumps(value, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def test_backend_auth_hs256_token_is_accepted() -> None:
    header = _segment({"alg": "HS256", "typ": "JWT"})
    payload = _segment({"sub": "admin", "role": 2, "exp": time.time() + 60})
    message = f"{header}.{payload}"
    signature = base64.urlsafe_b64encode(
        hmac.new(JWT_SECRET.encode(), message.encode(), hashlib.sha256).digest()
    ).decode().rstrip("=")
    assert decode_hs256(f"{message}.{signature}")["role"] == 2


def test_default_cors_origins_cover_local_wrf_workbench(monkeypatch) -> None:
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    assert "http://127.0.0.1:5178" in settings.cors_origins
    assert "http://localhost:5178" in settings.cors_origins


def test_wrf_api_reports_dynamic_scheduling_without_fixed_limit(monkeypatch) -> None:
    import main

    monkeypatch.setattr(main.task_manager, "_health", {"status": "ready", "message": "test"})
    health = main.health()["data"]
    capabilities = main.options()["data"]["capabilities"]
    options = main.options()["data"]

    assert health["scheduling_mode"] == "dynamic"
    assert capabilities["scheduling_mode"] == "dynamic"
    assert capabilities["runtime_profiles"] == ["cpu"]
    assert capabilities["default_runtime_profile"] == "cpu"
    assert capabilities["fixed_spinup_hours"] == 6
    assert "forecast_focuses" not in options
    assert "spinup_hours" not in options
    assert "max_concurrent_tasks" not in health
    assert "max_concurrent_tasks" not in capabilities
