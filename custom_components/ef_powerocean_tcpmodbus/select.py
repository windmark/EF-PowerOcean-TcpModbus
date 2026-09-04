"""Select entities for EF-PowerOcean-TcpModbus."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONTROL_INTENT_SELECT, CONTROL_INTENTS, DOMAIN
from .coordinator import EcoflowCoordinator
from .entity import EcoFlowBaseEntity
from .models import ControlIntent, SelectDef

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EcoFlow selects from a config entry."""
    coordinator: EcoflowCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        [EcoFlowControlIntentSelect(coordinator, entry, CONTROL_INTENT_SELECT)]
    )


class EcoFlowControlIntentSelect(EcoFlowBaseEntity, SelectEntity):
    """Chooses what the inverter should do, in the user's terms.

    Each option pins a control method (bits 4-7 of the write-only System Control
    Command) and the sign of that method's setpoint, so an invalid pairing of the
    two cannot be expressed. Switching mode seeds the setpoint from what the system
    is doing right now, so the change itself never moves any power.
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
        return str(self.coordinator.control_intent)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        definition = CONTROL_INTENTS[self.coordinator.control_intent]
        return {
            "control_method": str(definition.method),
            "setpoint_register": definition.setpoint_key,
            "commanded_word": f"0x{self.coordinator.control_command:08X}",
            "last_written": self.coordinator.last_control_write_time,
            "in_control": self.coordinator.in_control,
        }

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_control_intent(ControlIntent(option))
