"""Tests for the Cloudflare TURN config and options flows."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.cloudflare_turn.api import (
    CloudflareTurnAuthError,
    CloudflareTurnConnectionError,
    CloudflareTurnKeyNotFoundError,
    CloudflareTurnRateLimitError,
    CloudflareTurnResponseError,
)
from custom_components.cloudflare_turn.const import (
    CONF_API_TOKEN,
    CONF_KEY_ID,
    CONF_TRANSPORTS,
    DASHBOARD_URL,
    DOMAIN,
    VALIDATION_TTL,
)
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult, FlowResultType

from .conftest import API_TOKEN, KEY_ID, FakeCloudflare, setup_integration, turn_servers

USER_INPUT = {CONF_KEY_ID: KEY_ID, CONF_API_TOKEN: API_TOKEN}


def suggested(result: FlowResult, key: str) -> Any:
    """Return the value a form suggests for a field."""
    for field in result["data_schema"].schema:
        if field == key:
            return (field.description or {}).get("suggested_value")
    raise KeyError(key)


@pytest.fixture
def mock_setup_entry() -> Any:
    """Keep the entry from being set up after the flow."""
    with patch(
        "custom_components.cloudflare_turn.async_setup_entry", return_value=True
    ) as mock:
        yield mock


async def test_user_flow(
    hass: HomeAssistant, cloudflare: FakeCloudflare, mock_setup_entry: AsyncMock
) -> None:
    """A valid key creates the entry with the default transports."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}
    assert result["description_placeholders"] == {"dashboard_url": DASHBOARD_URL}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_KEY_ID: f"  {KEY_ID} ", CONF_API_TOKEN: f"{API_TOKEN}\n"},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Cloudflare TURN"
    assert result["data"] == USER_INPUT
    assert result["options"] == {CONF_TRANSPORTS: ["udp_3478", "tls_443"]}
    assert cloudflare.calls == [VALIDATION_TTL]
    assert len(mock_setup_entry.mock_calls) == 1


@pytest.mark.parametrize(
    ("error", "errors"),
    [
        (CloudflareTurnKeyNotFoundError("no key"), {CONF_KEY_ID: "key_not_found"}),
        (CloudflareTurnAuthError("bad token"), {CONF_API_TOKEN: "invalid_auth"}),
        (CloudflareTurnRateLimitError(None), {"base": "rate_limited"}),
        (CloudflareTurnConnectionError("offline"), {"base": "cannot_connect"}),
        (CloudflareTurnResponseError("odd"), {"base": "unexpected_response"}),
        (RuntimeError("boom"), {"base": "unknown"}),
    ],
)
async def test_user_flow_errors(
    hass: HomeAssistant,
    cloudflare: FakeCloudflare,
    mock_setup_entry: AsyncMock,
    error: Exception,
    errors: dict[str, str],
) -> None:
    """Errors are shown on the form, and the flow recovers."""
    cloudflare.error = error
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == errors
    assert suggested(result, CONF_KEY_ID) == KEY_ID

    cloudflare.error = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize("key_id", ["short", "has space 0123456789", "a/b0123456789"])
async def test_user_flow_invalid_key_id(
    hass: HomeAssistant,
    cloudflare: FakeCloudflare,
    mock_setup_entry: AsyncMock,
    key_id: str,
) -> None:
    """A malformed key ID is refused before asking Cloudflare."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_KEY_ID: key_id, CONF_API_TOKEN: API_TOKEN}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_KEY_ID: "invalid_key_id"}
    assert cloudflare.calls == []


async def test_single_instance(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Only one entry is allowed."""
    mock_config_entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_reauth(
    hass: HomeAssistant, cloudflare: FakeCloudflare, mock_config_entry: MockConfigEntry
) -> None:
    """Reauthentication stores the new key and reloads the entry."""
    await setup_integration(hass, mock_config_entry)

    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert suggested(result, CONF_KEY_ID) == KEY_ID
    assert suggested(result, CONF_API_TOKEN) is None

    cloudflare.error = CloudflareTurnAuthError("still bad")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_KEY_ID: KEY_ID, CONF_API_TOKEN: "wrong"}
    )
    assert result["errors"] == {CONF_API_TOKEN: "invalid_auth"}

    cloudflare.error = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_KEY_ID: KEY_ID, CONF_API_TOKEN: "new-token"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data == {CONF_KEY_ID: KEY_ID, CONF_API_TOKEN: "new-token"}
    assert mock_config_entry.state is ConfigEntryState.LOADED


async def test_reconfigure(
    hass: HomeAssistant, cloudflare: FakeCloudflare, mock_config_entry: MockConfigEntry
) -> None:
    """Reconfiguring switches to another TURN key."""
    await setup_integration(hass, mock_config_entry)
    new_key = "fedcba9876543210fedcba9876543210"

    result = await mock_config_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert suggested(result, CONF_KEY_ID) == KEY_ID

    cloudflare.error = CloudflareTurnKeyNotFoundError("unknown key")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_KEY_ID: new_key, CONF_API_TOKEN: "new-token"}
    )
    assert result["errors"] == {CONF_KEY_ID: "key_not_found"}

    cloudflare.error = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_KEY_ID: new_key, CONF_API_TOKEN: "new-token"}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert mock_config_entry.data == {CONF_KEY_ID: new_key, CONF_API_TOKEN: "new-token"}
    assert mock_config_entry.state is ConfigEntryState.LOADED


async def test_options_flow(
    hass: HomeAssistant, cloudflare: FakeCloudflare, mock_config_entry: MockConfigEntry
) -> None:
    """Choosing transports applies to the next session without a reload."""
    await setup_integration(hass, mock_config_entry)

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert suggested(result, CONF_TRANSPORTS) == ["udp_3478", "tls_443"]

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_TRANSPORTS: []}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_TRANSPORTS: "no_transports"}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_TRANSPORTS: ["tls_443", "tcp_80"]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_config_entry.options == {CONF_TRANSPORTS: ["tcp_80", "tls_443"]}

    (server,) = turn_servers(hass)
    assert server.urls == [
        "turn:turn.cloudflare.com:80?transport=tcp",
        "turns:turn.cloudflare.com:443?transport=tcp",
    ]
    assert len(cloudflare.calls) == 1


async def test_options_flow_without_saved_options(
    hass: HomeAssistant, cloudflare: FakeCloudflare
) -> None:
    """An entry without options suggests the defaults."""
    entry = MockConfigEntry(domain=DOMAIN, data=USER_INPUT)
    await setup_integration(hass, entry)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    assert suggested(result, CONF_TRANSPORTS) == ["udp_3478", "tls_443"]
