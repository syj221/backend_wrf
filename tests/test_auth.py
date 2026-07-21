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
