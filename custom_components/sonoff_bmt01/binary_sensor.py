"""Connection status for the Sonoff BMT01."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BMT01ConfigEntry
from .client import BMT01Client
from .entity import BMT01Entity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BMT01ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the connection sensor."""
    async_add_entities([BMT01ConnectedSensor(entry.runtime_data)])


class BMT01ConnectedSensor(BMT01Entity, BinarySensorEntity):
    """On while Home Assistant holds an authenticated connection."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, client: BMT01Client) -> None:
        """Initialise."""
        super().__init__(client, "connected", "Connected")

    @property
    def is_on(self) -> bool:
        """Connection state."""
        return self._client.connected
