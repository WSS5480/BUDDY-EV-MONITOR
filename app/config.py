"""Settings, all from environment variables (set in Render -> Environment)."""

from __future__ import annotations

import os
from urllib.parse import urlparse


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


TESLA_CLIENT_ID = _env("TESLA_CLIENT_ID")
TESLA_CLIENT_SECRET = _env("TESLA_CLIENT_SECRET")
OWNER_PASSWORD = _env("OWNER_PASSWORD")
# Encrypts the Tesla tokens and the car key in the database. Any long random string
# (Render's "Generate" button makes one). Changing it means signing in with Tesla again.
SECRET_KEY = _env("SECRET_KEY")
TESLA_REGION = _env("TESLA_REGION", "na") or "na"
TESLA_VIN = _env("TESLA_VIN").upper()

# Public base URL. Render sets RENDER_EXTERNAL_URL automatically.
APP_URL = (_env("APP_URL") or _env("RENDER_EXTERNAL_URL") or "http://localhost:8000").rstrip("/")
APP_DOMAIN = urlparse(APP_URL).hostname or "localhost"

TESLA_AUTH_URL = "https://auth.tesla.com/oauth2/v3/authorize"
TESLA_TOKEN_URL = "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
TESLA_API_BASE = {
    "na": "https://fleet-api.prd.na.vn.cloud.tesla.com",
    "eu": "https://fleet-api.prd.eu.vn.cloud.tesla.com",
    "cn": "https://fleet-api.prd.cn.vn.cloud.tesla.cn",
}.get(TESLA_REGION, "https://fleet-api.prd.na.vn.cloud.tesla.com")
TESLA_REDIRECT_URI = f"{APP_URL}/auth/callback"
TESLA_SCOPES = "openid offline_access user_data vehicle_device_data vehicle_location vehicle_cmds vehicle_charging_cmds"

# Scope the Claude connector's access tokens carry.
CONNECTOR_SCOPE = "car"
CONNECTOR_ACCESS_TTL = 60 * 60  # 1 hour
CONNECTOR_REFRESH_TTL = 60 * 60 * 24 * 90  # 90 days
APPROVAL_TTL = 10 * 60  # owner approvals for unlock-type actions
MIN_PASSWORD_LEN = 12


def missing_settings() -> list[str]:
    missing = []
    for name, value in (
        ("TESLA_CLIENT_ID", TESLA_CLIENT_ID),
        ("TESLA_CLIENT_SECRET", TESLA_CLIENT_SECRET),
        ("OWNER_PASSWORD", OWNER_PASSWORD),
        ("SECRET_KEY", SECRET_KEY),
        ("DATABASE_URL", os.environ.get("DATABASE_URL", "").strip()),
    ):
        if not value:
            missing.append(name)
    return missing
