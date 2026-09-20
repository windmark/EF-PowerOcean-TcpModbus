"""Select entities for EF-PowerOcean-TcpModbus."""

from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import BATTERY_MODE_SELECT, DOMAIN
from .coordinator import EcoflowCoordinator
from .entity import EcoFlowBaseEntity
from .models import ControlEntityDef, ControlFeature


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up EcoFlow selects from a config entry."""
    coordinator: EcoflowCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        [EcoFlowBatteryModeSelect(coordinator, entry, BATTERY_MODE_SELECT)]
    )


class EcoFlowBatteryModeSelect(EcoFlowBaseEntity, SelectEntity):
    """What the inverter should be doing.

    The protocol follows one control method at a time, so this is a single choice
    rather than several toggles. Each mode's power and the two state-of-charge
    limits are separate entities that stay editable whatever is selected here.
    """

    def __init__(
        self,
        coordinator: EcoflowCoordinator,
        entry: ConfigEntry,
        definition: ControlEntityDef,
    ) -> None:
        super().__init__(coordinator, entry, definition)
        self._attr_options = [str(feature) for feature in ControlFeature]
        self._attr_entity_category = definition.entity_category
        if definition.icon:
            self._attr_icon = definition.icon

    @property
    def current_option(self) -> str:
        return str(self.coordinator.control.selected_feature)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Everything an automation needs to decide, in one place.

        The readings below are duplicated from their own sensors on purpose.
        An automation that wants them otherwise has to find those sensors by
        entity id, and entity ids are derived from display names, which are
        exactly what a user is most likely to rename. Publishing them here
        makes this entity the contract instead: pick the mode, read the state.
        """
        control = self.coordinator.control
        data = self.coordinator.data or {}
        attributes: dict[str, Any] = {
            "status": str(control.status),
            "commanded_power": control.power,
            "charge_limit_soc": control.charge_limit_soc,
            "battery_reserve_soc": control.battery_reserve_soc,
            "battery_soc": data.get("battery_soc"),
            "battery_capacity": data.get("battery_capacity"),
            "grid_power": data.get("grid_power"),
            "feed_in_power_max": data.get("feed_in_power_max"),
        }
        return attributes

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.control.async_select_feature(ControlFeature(option))
