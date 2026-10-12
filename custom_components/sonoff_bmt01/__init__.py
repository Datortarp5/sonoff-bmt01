"""The Sonoff BMT01 BBQ Thermometer integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant

from .client import BMT01Client
from .const import CONF_KEY

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]

type BMT01ConfigEntry = ConfigEntry[BMT01Client]


async def async_setup_entry(hass: HomeAssistant, entry: BMT01ConfigEntry) -> bool:
    """Set up a BMT01 from a config entry."""
    client = BMT01Client(hass, entry.data[CONF_ADDRESS], entry.data[CONF_KEY])
    entry.runtime_data = client
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    client.start(entry)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: BMT01ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.stop()
    return unloaded
