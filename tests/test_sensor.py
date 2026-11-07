"""Tests for the Cloudflare TURN sensor."""

from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.cloudflare_turn.api import CloudflareTurnConnectionError
from custom_components.cloudflare_turn.const import DOMAIN
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from .conftest import START, FakeCloudflare, setup_integration

ENTITY_ID = "sensor.cloudflare_turn_credential_expiry"


async def test_expiry_sensor(
    hass: HomeAssistant,
    cloudflare: FakeCloudflare,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
) -> None:
    """The sensor shows the expiry, and stays available while refreshes fail."""
    freezer.move_to(START)
    await setup_integration(hass, mock_config_entry)

    state = hass.states.get(ENTITY_ID)
    assert state is not None
    assert state.state == "2026-11-09T12:00:00+00:00"
    assert state.attributes["device_class"] == "timestamp"

    entity = entity_registry.async_get(ENTITY_ID)
    assert entity is not None
    assert entity.entity_category is EntityCategory.DIAGNOSTIC
    assert entity.unique_id == f"{mock_config_entry.entry_id}_credentials_expire"
    (device,) = dr.async_entries_for_config_entry(
        device_registry, mock_config_entry.entry_id
    )
    assert device.identifiers == {(DOMAIN, mock_config_entry.entry_id)}
    assert device.entry_type is dr.DeviceEntryType.SERVICE
    assert device.manufacturer == "Cloudflare"
    assert entity.device_id == device.id

    cloudflare.error = CloudflareTurnConnectionError("offline")
    freezer.tick(timedelta(hours=12))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    state = hass.states.get(ENTITY_ID)
    assert state is not None
    assert state.state == "2026-11-09T12:00:00+00:00"

    cloudflare.error = None
    freezer.tick(timedelta(minutes=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    state = hass.states.get(ENTITY_ID)
    assert state is not None
    assert state.state == (dt_util.utcnow() + timedelta(hours=48)).isoformat()
