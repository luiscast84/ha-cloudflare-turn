"""Tests for the Cloudflare TURN diagnostics."""

from __future__ import annotations

from datetime import timedelta
import json

from freezegun.api import FrozenDateTimeFactory
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.cloudflare_turn.const import CONF_API_TOKEN, CONF_KEY_ID
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .conftest import API_TOKEN, DEFAULT_URLS, KEY_ID, FakeCloudflare, setup_integration

REDACTED = "**REDACTED**"


async def test_diagnostics(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    cloudflare: FakeCloudflare,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Diagnostics explain the state without revealing any secret."""
    # The clock stays frozen at the real time: the test client's token must be valid.
    now = dt_util.utcnow()
    await setup_integration(hass, mock_config_entry)

    result = await get_diagnostics_for_config_entry(
        hass, hass_client, mock_config_entry
    )

    assert result["entry"] == {
        "data": {CONF_KEY_ID: REDACTED, CONF_API_TOKEN: REDACTED},
        "options": {"transports": ["udp_3478", "tls_443"]},
    }
    assert result["refresh"] == {
        "last_update_success": True,
        "last_error": None,
        "auth_failed": False,
        "failures": 0,
        "failing_since": None,
        "next_refresh": (now + timedelta(hours=12)).isoformat(),
    }
    assert result["credentials"]["issued_at"] == now.isoformat()
    assert (
        result["credentials"]["expires_at"] == (now + timedelta(hours=48)).isoformat()
    )
    (server,) = result["credentials"]["servers"]
    assert server["username"] == REDACTED
    assert server["credential"] == REDACTED
    assert len(server["urls"]) == 6
    assert result["ice_servers_offered"] == [
        {"urls": DEFAULT_URLS, "username": REDACTED, "credential": REDACTED}
    ]
    dump = json.dumps(result)
    for secret in (KEY_ID, API_TOKEN, "user-1", "cred-1"):
        assert secret not in dump
