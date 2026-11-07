"""Diagnostics for Cloudflare TURN."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_API_TOKEN, CONF_KEY_ID
from .coordinator import CloudflareTurnConfigEntry

TO_REDACT = {CONF_API_TOKEN, CONF_KEY_ID, "username", "credential"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: CloudflareTurnConfigEntry
) -> dict[str, Any]:
    """Return diagnostics without the TURN key or any credential."""
    coordinator = entry.runtime_data
    credentials = coordinator.data
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "refresh": {
            "last_update_success": coordinator.last_update_success,
            "last_error": coordinator.last_error,
            "auth_failed": coordinator.auth_failed,
            "failures": coordinator.failures,
            "failing_since": _iso(coordinator.failing_since),
            "next_refresh": _iso(coordinator.next_refresh),
        },
        "credentials": {
            "issued_at": credentials.issued_at.isoformat(),
            "expires_at": credentials.expires_at.isoformat(),
            "servers": [
                async_redact_data(
                    {
                        "urls": [url.url for url in server.urls],
                        "username": server.username,
                        "credential": server.credential,
                    },
                    TO_REDACT,
                )
                for server in credentials.servers
            ],
        },
        "ice_servers_offered": [
            async_redact_data(server.to_dict(), TO_REDACT)
            for server in coordinator.async_get_ice_servers()
        ],
    }


def _iso(value: datetime | None) -> str | None:
    """Format an optional datetime."""
    return value.isoformat() if value is not None else None
