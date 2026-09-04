"""Switch entities for EF-PowerOcean-TcpModbus."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, HEARTBEAT_SWITCH, POWER_SAVING_SWITCH
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
    """Manual override for the keepalive that grants Modbus control authority.

    The control mode arms and releases this on its own, so it is off the entity list
    unless someone enables it to debug or to hold control open deliberately.
    """

    _attr_entity_registry_enabled_default = False

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
            "in_control": self.coordinator.in_control,
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_heartbeat_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_heartbeat_enabled(False)


class EcoFlowPowerSavingSwitch(EcoFlowSwitch):
    """Power-saving mode, bit 3 of the write-only control command.

    The coordinator composes the control word from this bit and the selected control
    mode, so toggling here never disturbs the mode. It arms the heartbeat by itself.
    """

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data
        if data is None or data.get("battery_saver_mode_ena") is None:
            return self.coordinator.power_saving_commanded
        return data.get("battery_saver_mode_ena")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        return {
            "commanded": self.coordinator.power_saving_commanded,
            "commanded_word": f"0x{self.coordinator.control_command:08X}",
            "system_modes_raw": data.get("system_modes"),
            "system_state_2_raw": data.get("system_state_2"),
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_power_saving(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_power_saving(False)
