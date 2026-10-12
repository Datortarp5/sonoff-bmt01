"""Config flow for the Sonoff BMT01."""

from __future__ import annotations

import re
from typing import Any

import voluptuous as vol

from homeassistant.components.bluetooth import async_discovered_service_info
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import CONF_KEY, DOMAIN, LOCAL_NAME_PREFIX

MAC_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")


class BMT01ConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add a BMT01: pick the Bluetooth address and enter the eWeLink key."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the user step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            address = user_input[CONF_ADDRESS].strip().upper().replace("-", ":")
            key = user_input[CONF_KEY].strip()
            if not MAC_RE.match(address):
                errors[CONF_ADDRESS] = "invalid_mac"
            elif not key:
                errors[CONF_KEY] = "invalid_key"
            else:
                await self.async_set_unique_id(address)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"BMT01 {address[-5:]}",
                    data={CONF_ADDRESS: address, CONF_KEY: key},
                )

        # Offer nearby thermometers (advertised as ck_<device id>);
        # an address can also be typed in.
        configured = self._async_current_ids(include_ignore=False)
        options = [
            SelectOptionDict(value=info.address, label=f"{info.name} ({info.address})")
            for info in async_discovered_service_info(self.hass, connectable=True)
            if (info.name or "").startswith(LOCAL_NAME_PREFIX)
            and info.address not in configured
        ]

        schema = vol.Schema(
            {
                vol.Required(CONF_ADDRESS): SelectSelector(
                    SelectSelectorConfig(
                        options=options,
                        custom_value=True,
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(CONF_KEY): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)
