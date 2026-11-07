"""Sensor for Cloudflare TURN: when the credentials in use expire."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DASHBOARD_URL, DOMAIN, TITLE
from .coordinator import CloudflareTurnConfigEntry, CloudflareTurnCoordinator

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CloudflareTurnConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the sensor."""
    async_add_entities([CloudflareTurnExpirySensor(entry.runtime_data)])


class CloudflareTurnExpirySensor(
    CoordinatorEntity[CloudflareTurnCoordinator], SensorEntity
):
    """When the credentials handed to new sessions expire.

    It moves forward with every refresh, so a value in the past or close to now
    means refreshing has been failing.
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "credentials_expire"

    def __init__(self, coordinator: CloudflareTurnCoordinator) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        entry_id = coordinator.config_entry.entry_id
        self._attr_unique_id = f"{entry_id}_credentials_expire"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name=TITLE,
            manufacturer="Cloudflare",
            model="Realtime TURN",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=DASHBOARD_URL,
        )

    @property
    def available(self) -> bool:
        """Stay available while a refresh fails: the expiry still applies."""
        return True

    @property
    def native_value(self) -> datetime:
        """Return the expiry of the current credentials."""
        return self.coordinator.data.expires_at
