"""Base entity for the Sonoff BMT01."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity import Entity

from .client import BMT01Client
from .const import DOMAIN


class BMT01Entity(Entity):
    """Shared device info and push updates from the client."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, client: BMT01Client, key: str, name: str) -> None:
        """Initialise."""
        self._client = client
        self._attr_name = name
        self._attr_unique_id = f"{client.address}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, client.address)},
            connections={(CONNECTION_BLUETOOTH, client.address)},
            name="BMT01",
            manufacturer="SONOFF",
            model="BMT01 BBQ Thermometer",
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to client updates."""
        self.async_on_remove(self._client.async_add_listener(self.async_write_ha_state))
