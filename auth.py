from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from collections.abc import Iterable

from fastapi.responses import JSONResponse


JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret-change-me").strip()
WHITELIST = {"/", "/api/health", "/docs", "/openapi.json"}


def _decode_segment(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def decode_hs256(token: str) -> dict:
    try:
        header_segment, payload_segment, signature_segment = token.split(".")
        header = json.loads(_decode_segment(header_segment))
        payload = json.loads(_decode_segment(payload_segment))
        signature = _decode_segment(signature_segment)
    except (ValueError, TypeError, json.JSONDecodeError):
        raise ValueError("invalid token") from None
    if header.get("alg") != "HS256":
        raise ValueError("unsupported algorithm")
    message = f"{header_segment}.{payload_segment}".encode("ascii")
    expected = hmac.new(JWT_SECRET.encode("utf-8"), message, hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("invalid signature")
    if payload.get("exp") is not None and float(payload["exp"]) <= time.time():
        raise ValueError("expired token")
    return payload


def install_auth(app, rules: Iterable[tuple[str, int]]) -> None:
    ordered = list(rules)

    @app.middleware("http")
    async def check_token(request, call_next):
        path = request.url.path
        if request.method == "OPTIONS" or path in WHITELIST:
            return await call_next(request)
        required = next((role for prefix, role in ordered if path.startswith(prefix)), None)
        if required is None:
            return await call_next(request)
        header = request.headers.get("authorization", "")
        token = header[7:] if header.startswith("Bearer ") else ""
        if not token and path.startswith("/data/WRF/"):
            token = request.query_params.get("token", "")
        try:
            payload = decode_hs256(token)
        except (ValueError, TypeError):
            return JSONResponse({"code": 401, "detail": "token 无效或已过期"}, status_code=401)
        if int(payload.get("role", 0)) < required:
            return JSONResponse({"code": 403, "detail": "权限不足"}, status_code=403)
        request.state.user = payload
        return await call_next(request)
