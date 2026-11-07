"""Cloudflare TURN: relay Home Assistant's WebRTC streams through Cloudflare.

The integration hands short-lived Cloudflare TURN credentials to every WebRTC
session through Home Assistant's ICE server registry. The frontend, the mobile
apps and go2rtc all read that registry, so both ends of a camera stream can fall
back to Cloudflare's relay when no direct path exists.
"""

from __future__ import annotations

from homeassistant.components.web_rtc import async_register_ice_servers
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import CloudflareTurnClient
from .const import CONF_API_TOKEN, CONF_KEY_ID, DOMAIN, ISSUE_REFRESH_FAILING
from .coordinator import CloudflareTurnConfigEntry, CloudflareTurnCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(
    hass: HomeAssistant, entry: CloudflareTurnConfigEntry
) -> bool:
    """Fetch the first credentials and offer them to new WebRTC sessions."""
    client = CloudflareTurnClient(
        async_get_clientsession(hass),
        entry.data[CONF_KEY_ID],
        entry.data[CONF_API_TOKEN],
    )
    coordinator = CloudflareTurnCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    coordinator.async_schedule_refresh()
    entry.runtime_data = coordinator
    entry.async_on_unload(
        async_register_ice_servers(hass, coordinator.async_get_ice_servers)
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: CloudflareTurnConfigEntry
) -> bool:
    """Stop offering the relay and stop refreshing."""
    ir.async_delete_issue(hass, DOMAIN, ISSUE_REFRESH_FAILING)
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
