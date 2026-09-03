"""Select entities for EF-PowerOcean-TcpModbus."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONTROL_METHOD_SELECT, DOMAIN
from .coordinator import EcoflowCoordinator
from .entity import EcoFlowBaseEntity
from .models import ControlMode, SelectDef

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EcoFlow selects from a config entry."""
    coordinator: EcoflowCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        [EcoFlowControlMethodSelect(coordinator, entry, CONTROL_METHOD_SELECT)]
    )


class EcoFlowControlMethodSelect(EcoFlowBaseEntity, SelectEntity):
    """Selects which control method the inverter follows.

    Written as bits 4-7 of the write-only System Control Command (0x0215). The
    state shown is the commanded method; the method the device actually reports in
    System State 2 is exposed as an attribute, so a device that is not accepting
    commands is visible as a mismatch.
    """

    def __init__(
        self,
        coordinator: EcoflowCoordinator,
        entry: ConfigEntry,
        definition: SelectDef,
    ) -> None:
        super().__init__(coordinator, entry, definition)
        self._attr_options = list(definition.options)
        self._attr_entity_category = definition.entity_category
        if definition.icon:
            self._attr_icon = definition.icon

    @property
    def current_option(self) -> str | None:
        return str(self.coordinator.control_method)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        reported = self.coordinator.reported_control_method()
        return {
            "device_reported": str(reported) if reported is not None else None,
            "in_sync": reported is self.coordinator.control_method
            if reported is not None
            else None,
            "commanded_word": f"0x{self.coordinator.control_command:08X}",
            "last_written": self.coordinator.last_control_write_time,
            "heartbeat_enabled": self.coordinator.heartbeat_enabled,
        }

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_control_method(ControlMode(option))
