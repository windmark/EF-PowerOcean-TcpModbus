"""Switch entities for EF-PowerOcean-TcpModbus."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONTROL_COMMAND_POWER_SAVING_BIT,
    DOMAIN,
    HEARTBEAT_SWITCH,
    POWER_SAVING_SWITCH,
)
from .coordinator import EcoflowCoordinator
from .entity import EcoFlowBaseEntity
from .models import SwitchDef

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EcoFlow switches from a config entry."""
    coordinator: EcoflowCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        [
            EcoFlowHeartbeatSwitch(coordinator, entry, HEARTBEAT_SWITCH),
            EcoFlowPowerSavingSwitch(coordinator, entry, POWER_SAVING_SWITCH),
        ]
    )


class EcoFlowSwitch(EcoFlowBaseEntity, SwitchEntity):
    """Shared setup for the integration's switches."""

    def __init__(
        self,
        coordinator: EcoflowCoordinator,
        entry: ConfigEntry,
        definition: SwitchDef,
    ) -> None:
        super().__init__(coordinator, entry, definition)
        self._attr_device_class = definition.device_class
        self._attr_entity_category = definition.entity_category
        if definition.icon:
            self._attr_icon = definition.icon


class EcoFlowHeartbeatSwitch(EcoFlowSwitch):
    """Gates the Modbus keepalive that grants the integration control authority."""

    @property
    def available(self) -> bool:
        # Stays operable while disconnected so the keepalive can be armed beforehand.
        return True

    @property
    def is_on(self) -> bool:
        return self.coordinator.heartbeat_enabled

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "accepted_by_device": self.coordinator.heartbeat_supported,
            "last_sent": self.coordinator.last_heartbeat_time,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_heartbeat_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_heartbeat_enabled(False)


class EcoFlowPowerSavingSwitch(EcoFlowSwitch):
    """Power-saving mode, written as a bit of the write-only control command."""

    @property
    def is_on(self) -> bool | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("battery_saver_mode_ena")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        return {
            "commanded_word": f"0x{self.coordinator.control_command:08X}",
            "system_modes_raw": data.get("system_modes"),
            "system_state_2_raw": data.get("system_state_2"),
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_write(enabled=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_write(enabled=False)

    async def _async_write(self, *, enabled: bool) -> None:
        await self.coordinator.async_write_control_command(
            1 << CONTROL_COMMAND_POWER_SAVING_BIT if enabled else 0
        )
