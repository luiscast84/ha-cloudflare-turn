"""Shared fixtures for the Cloudflare TURN tests."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from webrtc_models import RTCIceServer

from custom_components.cloudflare_turn.api import (
    CloudflareTurnClient,
    TurnCredentials,
    _parse_credentials,
)
from custom_components.cloudflare_turn.const import (
    CONF_API_TOKEN,
    CONF_KEY_ID,
    CONF_TRANSPORTS,
    DEFAULT_TRANSPORTS,
    DOMAIN,
)
from homeassistant.components.web_rtc import async_get_ice_servers
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

KEY_ID = "0123456789abcdef0123456789abcdef"
API_TOKEN = "test-api-token-abcdef0123456789"
START = "2026-11-07 12:00:00+00:00"

# What Cloudflare's generate-ice-servers endpoint returns, plus a port 53 URL.
TURN_URLS = [
    "turn:turn.cloudflare.com:3478?transport=udp",
    "turn:turn.cloudflare.com:53?transport=udp",
    "turn:turn.cloudflare.com:443?transport=udp",
    "turn:turn.cloudflare.com:3478?transport=tcp",
    "turn:turn.cloudflare.com:80?transport=tcp",
    "turns:turn.cloudflare.com:5349?transport=tcp",
    "turns:turn.cloudflare.com:443?transport=tcp",
]
DEFAULT_URLS = [
    "turn:turn.cloudflare.com:3478?transport=udp",
    "turns:turn.cloudflare.com:443?transport=tcp",
]


def cloudflare_response(
    username: str, credential: str, urls: list[str] | None = None
) -> dict[str, Any]:
    """Return a credential response like Cloudflare's."""
    return {
        "iceServers": [
            {"urls": ["stun:stun.cloudflare.com:3478", "stun:stun.cloudflare.com:53"]},
            {
                "urls": list(TURN_URLS if urls is None else urls),
                "username": username,
                "credential": credential,
            },
        ]
    }


class FakeCloudflare:
    """Stand-in for Cloudflare's credential API."""

    def __init__(self) -> None:
        """Start with no calls and no error."""
        self.calls: list[timedelta] = []
        self.error: Exception | None = None
        self.urls: list[str] | None = None
        self.gate: asyncio.Event | None = None

    async def generate(
        self, _client: CloudflareTurnClient, ttl: timedelta
    ) -> TurnCredentials:
        """Return numbered credentials, or raise the configured error."""
        self.calls.append(ttl)
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        number = len(self.calls)
        now = dt_util.utcnow()
        return _parse_credentials(
            cloudflare_response(f"user-{number}", f"cred-{number}", self.urls),
            now,
            now + ttl,
        )


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Let Home Assistant load the integration from custom_components."""


@pytest.fixture
def cloudflare() -> Generator[FakeCloudflare]:
    """Replace Cloudflare's API with a fake."""
    fake = FakeCloudflare()
    with patch.object(
        CloudflareTurnClient,
        "async_generate_credentials",
        autospec=True,
        side_effect=fake.generate,
    ):
        yield fake


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a configured entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Cloudflare TURN",
        data={CONF_KEY_ID: KEY_ID, CONF_API_TOKEN: API_TOKEN},
        options={CONF_TRANSPORTS: list(DEFAULT_TRANSPORTS)},
    )


async def setup_integration(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Add the entry and set it up."""
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def turn_servers(hass: HomeAssistant) -> list[RTCIceServer]:
    """Return the TURN servers Home Assistant hands to new WebRTC sessions."""
    return [server for server in async_get_ice_servers(hass) if server.username]
