"""Sensors for the Sonoff BMT01: probe temperatures, probe status, battery."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import BMT01ConfigEntry
from .client import BMT01Client
from .entity import BMT01Entity
from .protocol import PROBE_COUNT, PROBE_STATUSES, STATUS_OK


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BMT01ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up BMT01 sensors."""
    client = entry.runtime_data
    entities: list[SensorEntity] = []
    for index in range(PROBE_COUNT):
        entities.append(BMT01ProbeSensor(client, index))
        entities.append(BMT01ProbeStatusSensor(client, index))
    entities.append(BMT01BatterySensor(client))
    async_add_entities(entities)


class BMT01ProbeSensor(BMT01Entity, SensorEntity):
    """Probe temperature in °C; unavailable unless the probe reports a value."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, client: BMT01Client, index: int) -> None:
        """Initialise."""
        super().__init__(client, f"probe_{index + 1}", f"Probe {index + 1}")
        self._index = index

    @property
    def available(self) -> bool:
        """Only while connected and the probe reports a temperature."""
        return self._client.connected and isinstance(
            self._client.probes[self._index], float
        )

    @property
    def native_value(self) -> float | None:
        """Current temperature."""
        value = self._client.probes[self._index]
        return value if isinstance(value, float) else None


class BMT01ProbeStatusSensor(BMT01Entity, SensorEntity):
    """Probe status as the device reports it."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = PROBE_STATUSES

    def __init__(self, client: BMT01Client, index: int) -> None:
        """Initialise."""
        super().__init__(
            client, f"probe_{index + 1}_status", f"Probe {index + 1} status"
        )
        self._index = index

    @property
    def available(self) -> bool:
        """While connected and a reading has arrived."""
        return self._client.connected and self._client.probes[self._index] is not None

    @property
    def native_value(self) -> str | None:
        """ok, not_connected, too_high, too_low or invalid."""
        value = self._client.probes[self._index]
        if isinstance(value, float):
            return STATUS_OK
        return value


class BMT01BatterySensor(BMT01Entity, SensorEntity):
    """Battery level; keeps the last known value while disconnected."""

    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, client: BMT01Client) -> None:
        """Initialise."""
        super().__init__(client, "battery", "Battery")

    @property
    def available(self) -> bool:
        """Once a reading has been received."""
        return self._client.battery is not None

    @property
    def native_value(self) -> int | None:
        """Battery percentage."""
        return self._client.battery
