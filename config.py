"""
config.py — Load, save, and validate the YAML configuration file.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from models import UserAlert

DEFAULT_CONFIG_PATH = Path("config.yaml")


# ---------------------------------------------------------------------------
# Default config skeleton written on first run
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict[str, Any] = {
    "settings": {
        "poll_interval_minutes": 15,
        "log_level": "INFO",
        "db_path": "price_history.db",
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    },
    "platforms": {
        "seatgeek": {
            "enabled": True,
            "client_id": "",   # fill in your SeatGeek API client_id
            "client_secret": "",
        },
        "stubhub": {
            "enabled": True,
        },
        "vividseats": {
            "enabled": True,
        },
    },
    "notifications": {
        "email": {
            "enabled": False,
            "smtp_host": "smtp.gmail.com",
            "smtp_port": 587,
            "username": "",    # your Gmail address
            "password": "",    # Gmail App Password (not your regular password)
            "from_address": "",
        },
        "telegram": {
            "enabled": False,
            "bot_token": "",   # from @BotFather
        },
    },
    "alerts": [],
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class Config:
    """Thin wrapper around the parsed YAML config."""

    def __init__(self, path: Path = DEFAULT_CONFIG_PATH) -> None:
        self.path = path
        self._data: dict[str, Any] = {}
        self.load()

    # ------------------------------------------------------------------
    # Load / Save
    # ------------------------------------------------------------------

    def load(self) -> None:
        if not self.path.exists():
            self._data = DEFAULT_CONFIG.copy()
            self.save()
            print(f"[config] Created default config at {self.path}. "
                  "Please fill in your API keys and notification credentials.")
        else:
            with open(self.path) as f:
                self._data = yaml.safe_load(f) or {}
            # Merge missing keys from defaults (non-destructive upgrade)
            self._data = _deep_merge(DEFAULT_CONFIG, self._data)

    def save(self) -> None:
        with open(self.path, "w") as f:
            yaml.dump(self._data, f, default_flow_style=False, sort_keys=False)

    # ------------------------------------------------------------------
    # Settings accessors
    # ------------------------------------------------------------------

    @property
    def poll_interval_minutes(self) -> int:
        return int(self._data["settings"]["poll_interval_minutes"])

    @property
    def db_path(self) -> str:
        return self._data["settings"]["db_path"]

    @property
    def log_level(self) -> str:
        return self._data["settings"].get("log_level", "INFO")

    @property
    def user_agent(self) -> str:
        return self._data["settings"]["user_agent"]

    # ------------------------------------------------------------------
    # Platform accessors
    # ------------------------------------------------------------------

    def platform_enabled(self, name: str) -> bool:
        return bool(self._data.get("platforms", {}).get(name, {}).get("enabled", False))

    def seatgeek_credentials(self) -> tuple[str, str]:
        sg = self._data["platforms"]["seatgeek"]
        return sg.get("client_id", ""), sg.get("client_secret", "")

    # ------------------------------------------------------------------
    # Notification accessors
    # ------------------------------------------------------------------

    @property
    def email_config(self) -> dict:
        return self._data["notifications"]["email"]

    @property
    def telegram_config(self) -> dict:
        return self._data["notifications"]["telegram"]

    # ------------------------------------------------------------------
    # Alert CRUD
    # ------------------------------------------------------------------

    def get_alerts(self) -> list[UserAlert]:
        return [UserAlert.from_dict(a) for a in self._data.get("alerts", [])]

    def add_alert(self, alert: UserAlert) -> None:
        alerts = self._data.setdefault("alerts", [])
        alerts.append(alert.to_dict())
        self.save()

    def update_alert(self, alert: UserAlert) -> None:
        alerts = self._data.get("alerts", [])
        for i, a in enumerate(alerts):
            if a["id"] == alert.id:
                alerts[i] = alert.to_dict()
                break
        self.save()

    def remove_alert(self, alert_id: str) -> bool:
        alerts = self._data.get("alerts", [])
        original_len = len(alerts)
        self._data["alerts"] = [a for a in alerts if a["id"] != alert_id]
        if len(self._data["alerts"]) < original_len:
            self.save()
            return True
        return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base. override wins on conflicts."""
    result = base.copy()
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result
